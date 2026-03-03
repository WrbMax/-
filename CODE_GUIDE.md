# DTRS 代码详细解释

本文档对 DTRS 系统每个核心模块的功能、实现逻辑和关键代码进行详细说明。

---

## 一、系统整体架构

```
┌─────────────────────────────────────────────────────┐
│                   前端管理界面                        │
│         （React + Tailwind，实时监控与配置）           │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP REST API
┌──────────────────────▼──────────────────────────────┐
│                  dtrs-engine（主进程）                │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐  │
│  │  Scheduler  │  │   Scanner    │  │  Executor  │  │
│  │  定时调度器  │→ │  信号扫描器   │→ │  交易执行器 │  │
│  └─────────────┘  └──────────────┘  └────────────┘  │
│                         │                            │
│                   ┌─────▼──────┐                     │
│                   │  Database  │                     │
│                   │  SQLite DB │                     │
│                   └─────┬──────┘                     │
└─────────────────────────┼───────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────┐
│              dtrs-monitor（监控进程）                 │
│  WebSocket 订阅标记价格 → 实时检查止盈止损             │
└─────────────────────────────────────────────────────┘
                          │
                   币安 Futures API
```

系统由两个独立进程组成：

- **dtrs-engine**：负责信号扫描、交易执行和 REST API 服务。
- **dtrs-monitor**：通过 WebSocket 实时接收标记价格，毫秒级触发止盈止损。

---

## 二、核心模块详解

### 2.1 `core/scanner.py` — 信号扫描器

扫描器是整个系统的"大脑"，负责从 500 个标的中筛选出满足入场条件的信号。

#### 入场条件（三条件全部满足才开仓）

**条件一：价格位（MA20 穿越）**

```python
# 做多：前一根K线收盘 ≤ MA20，当前K线收盘 > MA20（刚刚穿越上去）
long_cond_ma = (close_prev <= ma20_prev) and (close > ma20)

# 做空：前一根K线收盘 ≥ MA20，当前K线收盘 < MA20（刚刚跌破下来）
short_cond_ma = (close_prev >= ma20_prev) and (close < ma20)
```

关键设计：只在穿越发生的那一根K线触发信号，而不是"在MA20上方就触发"。这确保了信号的时效性，避免追高追跌。

**条件二：动能位（MACD 金叉/死叉）**

```python
# 做多：DIF 在 DEA 上方（金叉状态）+ 柱状图为正 + 当前绿柱 > 前一根绿柱
long_cond_macd = (dif > dea) and (macd_hist > 0) and (macd_hist > macd_hist_prev)

# 做空：DIF 在 DEA 下方（死叉状态）+ 柱状图为负 + 当前红柱绝对值 > 前一根
short_cond_macd = (dif < dea) and (macd_hist < 0) and (macd_hist < macd_hist_prev)
```

**条件三：量能位（成交量放大）**

```python
# 当前成交量 > 过去10根K线平均成交量的1.5倍
volume_ratio = volume / vol_avg
long_cond_vol = volume_ratio >= config.entry.volume_threshold   # 默认1.5
short_cond_vol = volume_ratio >= config.entry.volume_threshold
```

**最终入场判断：**

```python
# 三条件必须全部满足
is_long  = long_cond_ma  and long_cond_macd  and long_cond_vol
is_short = short_cond_ma and short_cond_macd and short_cond_vol
```

#### K线新鲜度验证

为防止扫描到过期信号，系统会检查信号K线的开盘时间：

```python
# 信号K线年龄不能超过 1.5 个周期
# 1h 信号：不超过 90 分钟
# 4h 信号：不超过 6 小时
# 1d 信号：不超过 36 小时
max_age_seconds = period_seconds * 1.5
if age_seconds > max_age_seconds:
    return None  # 拒绝过期信号
```

#### 止盈止损计算

```python
# ATR 止损：入场价 ± ATR × 止损系数
sl = entry_price - atr * config.exit.sl_atr_multiplier  # 做多
sl = entry_price + atr * config.exit.sl_atr_multiplier  # 做空

# TP1：ATR × 0.618 倍（黄金比例）
tp1 = entry_price + atr * config.exit.tp1_ratio  # 做多

# TP2：ATR × 1.618 倍
tp2 = entry_price + atr * config.exit.tp2_ratio  # 做多
```

---

### 2.2 `core/scheduler.py` — 定时调度器

调度器管理所有定时任务，使用 `asyncio` 异步框架实现多任务并发。

#### 扫描时机

系统在每根K线**收盘前10秒**触发扫描，使用已确认收盘的倒数第二根K线（`idx=-2`）作为信号K线：

```python
# 等待到下一个整点前10秒
await self._wait_until_next(3600, offset_seconds=10)  # 1h 扫描
await self._wait_until_next(14400, offset_seconds=10) # 4h 扫描
await self._wait_until_next(86400, offset_seconds=10) # 1d 扫描
```

#### 防并发重复扫描

使用双重锁机制防止同一时刻多个扫描并发执行：

```python
self._scan_lock = asyncio.Lock()       # 防止 asyncio 层面的并发
self._scan_thread_lock = threading.Lock()  # 防止线程池层面的并发

# 任意时刻只允许一个扫描运行
if not self._scan_thread_lock.acquire(blocking=False):
    logger.warning("扫描跳过：另一个扫描正在运行")
    continue
```

---

### 2.3 `core/executor.py` — 交易执行器

执行器负责将扫描器产生的信号转化为实际的币安合约订单。

#### 执行流程

```
信号 → 价格偏离检查 → 设置杠杆 → 下市价单 → 设置止损单 → 更新数据库
```

#### 价格偏离检查

防止在信号产生后价格已大幅偏离时仍然开仓：

```python
# 当前价格与信号价格偏差超过1%，拒绝开仓
price_deviation = abs(current_price - signal_price) / signal_price
if price_deviation > 0.01:
    mark_signal_failed(signal_id, f"价格偏离 {price_deviation:.2%}")
    return
```

#### 下单失败处理

```python
try:
    order = client.place_market_order(symbol, side, quantity)
    update_signal_status(signal_id, "executed")
except Exception as e:
    # 下单失败时将信号标记为 filtered，而非 executed
    mark_signal_failed(signal_id, f"下单失败: {e}")
```

---

### 2.4 `position_monitor.py` — 持仓监控（WebSocket）

监控进程通过 WebSocket 订阅所有持仓标的的标记价格实时推送，替代原来的 REST API 轮询，彻底解决限流问题。

#### WebSocket 订阅

```python
# 为所有持仓标的订阅标记价格流
# 格式：btcusdt@markPrice、ethusdt@markPrice
streams = [f"{sym.lower()}@markPrice" for sym in active_symbols]
ws_url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"
```

#### 防重复触发

每次价格更新后，系统会检查是否已在冷却期内，防止同一止盈点被重复触发：

```python
# 每个持仓的止盈止损操作有3秒冷却时间
last_check = self._last_check_time.get(position_id, 0)
if time.time() - last_check < 3:
    return  # 冷却期内跳过
```

#### 动态更新订阅

当有新仓位开仓或现有仓位平仓时，WebSocket 订阅列表会自动更新：

```python
# 每30秒检查一次持仓变化，动态调整订阅
def _refresh_subscriptions(self):
    current_symbols = get_active_position_symbols()
    if current_symbols != self._subscribed_symbols:
        self._reconnect_websocket(current_symbols)
```

---

### 2.5 `core/monitor.py` — 持仓监控逻辑

`monitor.py` 包含所有止盈止损的判断逻辑，被 `position_monitor.py` 在每次价格更新时调用。

#### 五层出场逻辑（按优先级）

**第一层：紧急止损（保证金亏损超过30%）**

```python
margin_loss_pct = unrealized_pnl / initial_margin
if margin_loss_pct <= -config.risk.emergency_stop_pct:
    close_position(pos, "紧急止损")
```

**第二层：ATR 止损**

```python
if direction == "LONG" and mark_price <= sl:
    close_position(pos, "ATR止损触发")
elif direction == "SHORT" and mark_price >= sl:
    close_position(pos, "ATR止损触发")
```

**第三层：TP1 止盈（40% 仓位）**

```python
if not tp1_hit:
    if (direction == "LONG" and mark_price >= tp1) or \
       (direction == "SHORT" and mark_price <= tp1):
        close_partial(pos, 0.40, "TP1止盈")
        # TP1触达后，止损上移至TP1价格，锁定利润
        update_sl(pos, tp1)
        mark_tp1_hit(pos)
```

**第四层：TP2 止盈（剩余仓位）**

```python
if tp1_hit:
    if (direction == "LONG" and mark_price >= tp2) or \
       (direction == "SHORT" and mark_price <= tp2):
        close_position(pos, "TP2止盈")
```

**第五层：EMA20 追踪止损 / MA20 穿线止盈**（每15分钟检查一次）

```python
# 价格跌破 EMA20 时止盈（趋势结束信号）
if direction == "LONG" and close < ema20:
    close_position(pos, "EMA20追踪止盈")

# 价格穿越 MA20 时止盈
if direction == "LONG" and close < ma20:
    close_position(pos, "MA20穿线止盈")
```

---

### 2.6 `core/indicators.py` — 技术指标计算

负责从原始K线数据计算所有技术指标。

```python
def calculate_all_indicators(klines, cfg):
    # MA20：简单移动平均线（20周期）
    ma20 = ta.trend.SMAIndicator(close, window=20).sma_indicator().values

    # EMA20：指数移动平均线（20周期）
    ema20 = ta.trend.EMAIndicator(close, window=20).ema_indicator().values

    # MACD：DIF(12,26) - DEA(9) = Histogram
    macd = ta.trend.MACD(close, window_fast=12, window_slow=26, window_sign=9)
    dif  = macd.macd().values          # DIF 线
    dea  = macd.macd_signal().values   # DEA 线
    hist = macd.macd_diff().values     # 柱状图

    # RSI：相对强弱指数（14周期）
    rsi = ta.momentum.RSIIndicator(close, window=14).rsi().values

    # ATR：真实波动幅度均值（14周期），用于止损计算
    atr = ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range().values

    # 成交量均值（10周期），用于量能判断
    vol_avg = ta.trend.SMAIndicator(volume, window=10).sma_indicator().values

    return {
        "open_time": klines["open_time"],  # K线开盘时间（用于新鲜度验证）
        "close": klines["close"],
        "ma20": ma20, "ema20": ema20,
        "dif": dif, "dea": dea, "macd_hist": hist,
        "rsi": rsi, "atr": atr, "vol_avg": vol_avg,
    }
```

---

### 2.7 `core/database.py` — 数据库操作

使用 SQLite 存储所有持仓、信号和系统日志数据。

#### 主要数据表

**positions 表（持仓记录）**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | TEXT | 唯一标识（UUID） |
| symbol | TEXT | 交易对（如 BTCUSDT） |
| direction | TEXT | 方向（LONG/SHORT） |
| period | TEXT | 信号周期（1h/4h/1d） |
| entry_price | REAL | 入场价格 |
| quantity | REAL | 持仓数量 |
| margin | REAL | 保证金（USDT） |
| sl | REAL | 当前止损价 |
| tp1 / tp2 | REAL | 止盈目标价 |
| tp1_hit | INTEGER | TP1 是否已触发（0/1） |
| status | TEXT | 状态（OPEN/PARTIAL/CLOSED） |
| realized_pnl | REAL | 已实现盈亏 |
| created_at | TEXT | 开仓时间（UTC） |
| closed_at | TEXT | 平仓时间（UTC） |

**signals 表（信号记录）**

| 字段 | 类型 | 说明 |
|------|------|------|
| symbol | TEXT | 交易对 |
| period | TEXT | 扫描周期 |
| direction | TEXT | 信号方向 |
| status | TEXT | executed/filtered/conflict/circuit_break |
| reason | TEXT | 过滤原因（如"价格偏离1.3%"） |
| created_at | TEXT | 信号产生时间 |

---

### 2.8 `core/binance_client.py` — 币安 API 封装

对 python-binance 库进行二次封装，提供统一的接口：

```python
class BinanceClient:
    def get_klines(self, symbol, interval, limit=100)    # 获取K线数据
    def get_mark_price(self, symbol)                     # 获取标记价格
    def get_ticker_24h(self)                             # 获取24h行情（用于扫描池排序）
    def get_wallet_balance(self)                         # 获取账户余额
    def get_positions(self)                              # 获取当前持仓
    def set_leverage(self, symbol, leverage)             # 设置杠杆
    def place_market_order(self, symbol, side, qty)      # 市价开仓
    def place_stop_order(self, symbol, side, qty, price) # 止损单
    def close_position(self, symbol, side, qty)          # 市价平仓
```

---

### 2.9 `core/copy_trader.py` — 跟单执行器

支持将主账户的交易同步到多个跟单账户。

```python
# 跟单配置示例（在 config.json 中）
{
  "copy_trading": {
    "enabled": true,
    "followers": [
      {
        "name": "账户B",
        "api_key": "...",
        "api_secret": "...",
        "ratio": 0.5    // 跟单比例（主账户开50USDT，跟单账户开25USDT）
      }
    ]
  }
}
```

跟单逻辑：主账户开仓后，遍历所有跟单账户，按比例计算仓位大小，同步执行相同方向的订单。

---

### 2.10 `config/settings.py` — 配置管理

配置采用 Pydantic 数据模型，支持从 `data/config.json` 动态加载：

```python
class ScanConfig(BaseModel):
    scan_scope: int = 500           # 扫描标的数量
    exclude_list: List[str] = []    # 黑名单
    auto_blacklist_enabled: bool = False

class RiskConfig(BaseModel):
    max_positions: int = 20         # 最大持仓数
    margin_per_trade: float = 50    # 每笔保证金（USDT）
    leverage: int = 10              # 杠杆倍数
    emergency_stop_pct: float = 0.30  # 紧急止损阈值

class Config(BaseModel):
    scan: ScanConfig
    entry: EntryConfig
    risk: RiskConfig
    exit: ExitConfig
```

---

### 2.11 `api/routes.py` — REST API 路由

提供前端所需的所有接口：

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 系统状态（服务运行、余额、持仓数） |
| `/api/positions` | GET | 持仓列表（支持 status 过滤） |
| `/api/signals` | GET | 信号记录列表 |
| `/api/performance` | GET | 绩效统计（总盈亏、胜率、最大回撤） |
| `/api/config` | GET/POST | 获取/更新运行时配置 |
| `/api/scan/manual` | POST | 手动触发一次扫描 |
| `/api/positions/{id}/close` | POST | 手动平仓指定持仓 |
| `/api/logs` | GET | 系统日志 |

---

## 三、数据流图

```
币安 API（K线数据）
        │
        ▼
  indicators.py
  （计算 MA20/MACD/RSI/ATR/量能）
        │
        ▼
   scanner.py
  （三条件判断：MA20穿越 + MACD金叉 + 量能放大）
        │
   信号产生
        │
        ▼
  executor.py
  （价格偏离检查 → 下市价单 → 设置止损单）
        │
   持仓创建
        │
        ▼
position_monitor.py
  （WebSocket 实时价格 → 止盈止损检测）
        │
   ┌────┴────┐
   ▼         ▼
TP1止盈    ATR止损
（平40%）  （全平）
   │
止损上移至TP1
   │
   ▼
TP2止盈/EMA20追踪止盈
（平剩余仓位）
```

---

## 四、关键设计决策

### 为什么用 WebSocket 而不是轮询？

原来的 REST API 轮询方案每60秒调用一次价格接口，当持仓数量多时（如20个），每分钟需要调用20次 API，容易触发币安的限流封禁（HTTP 418）。改用 WebSocket 后，一个连接可以同时订阅所有持仓的实时价格推送，API 调用量降低了99%以上，同时止盈止损的响应速度从"最多延迟60秒"提升到"毫秒级"。

### 为什么用 SQLite 而不是 MySQL？

DTRS 是单机部署的交易系统，并发写入量极低（每次扫描最多产生几个信号），SQLite 完全满足需求，且无需额外安装数据库服务，部署更简单，数据文件也更容易备份。

### 为什么信号K线用 `idx=-2` 而不是 `idx=-1`？

`idx=-1` 是当前正在形成中的K线，数据不完整（还没收盘）。`idx=-2` 是最近已确认收盘的K线，数据完整可靠。系统在K线收盘前10秒触发扫描，此时 `idx=-2` 已经完全确认，`idx=-1` 是即将收盘的K线（下一根K线就是开仓时机）。
