# DTRS 部署操作说明

本文档介绍如何在全新的 Linux 服务器上从零部署 DTRS 交易引擎和前端管理界面。

---

## 一、服务器要求

| 项目 | 最低要求 | 推荐配置 |
|------|----------|----------|
| 操作系统 | Ubuntu 20.04 LTS | Ubuntu 22.04 LTS |
| CPU | 1 核 | 2 核 |
| 内存 | 1 GB | 2 GB |
| 硬盘 | 20 GB | 40 GB |
| 网络 | 能访问 api.binance.com | 低延迟亚洲节点（推荐新加坡/香港） |
| Python | 3.10+ | 3.11 |

> **重要：** 服务器 IP 必须能正常访问币安 API（`api.binance.com`、`fapi.binance.com`）。中国大陆 IP 通常无法直连，建议使用境外服务器。

---

## 二、环境准备

### 2.1 更新系统并安装基础依赖

```bash
apt update && apt upgrade -y
apt install -y python3.11 python3.11-pip python3.11-venv git curl
```

### 2.2 验证 Python 版本

```bash
python3.11 --version
# 输出: Python 3.11.x
```

---

## 三、部署交易引擎（dtrs-engine）

### 3.1 创建项目目录并上传代码

```bash
mkdir -p /opt/dtrs-engine
cd /opt/dtrs-engine
# 将本仓库代码上传到此目录（scp 或 git clone）
```

如果使用 git clone：

```bash
git clone https://github.com/WrbMax/- /opt/dtrs-engine
```

### 3.2 安装 Python 依赖

```bash
cd /opt/dtrs-engine
pip3.11 install -r requirements.txt
```

主要依赖包括：

| 包名 | 用途 |
|------|------|
| fastapi | Web API 框架 |
| uvicorn | ASGI 服务器 |
| python-binance | 币安 API 客户端 |
| pandas / numpy | 数据处理 |
| ta | 技术指标计算（MA/MACD/RSI/ATR） |
| websocket-client | WebSocket 实时行情 |
| sqlite3 | 数据库（Python 内置） |

### 3.3 配置环境变量

创建环境变量文件：

```bash
cat > /opt/dtrs-engine/runtime.env << 'EOF'
BINANCE_API_KEY=你的币安API_KEY
BINANCE_API_SECRET=你的币安API_SECRET
EOF
chmod 600 /opt/dtrs-engine/runtime.env
```

> **获取币安 API Key：** 登录币安 → 账户中心 → API 管理 → 创建 API。权限只需勾选「合约交易」，**不要勾选提现权限**。建议绑定服务器 IP 白名单。

### 3.4 修改运行时配置

编辑 `data/config.json`，根据实际情况调整参数：

```json
{
  "scan": {
    "scan_scope": 500,          // 扫描标的数量（按24h成交量排名前N）
    "exclude_list": ["LUNAUSDT", "USTCUSDT"],  // 黑名单
    "auto_blacklist_enabled": false
  },
  "entry": {
    "volume_threshold": 1.5,    // 量能放大倍数（当前成交量 > 均量 × 此值）
    "rsi_enabled": false
  },
  "risk": {
    "max_positions": 20,        // 最大同时持仓数
    "margin_per_trade": 50,     // 每笔交易保证金（USDT）
    "leverage": 10,             // 杠杆倍数
    "emergency_stop_pct": 0.30, // 紧急止损阈值（保证金亏损30%触发）
    "margin_rate_limit": 0.80   // 保证金率熔断阈值
  },
  "exit": {
    "tp1_ratio": 0.618,         // TP1 止盈比例（相对ATR）
    "tp2_ratio": 1.618,         // TP2 止盈比例
    "tp1_close_pct": 0.40,      // TP1 触达时平仓比例（40%）
    "ema_check_interval_minutes": 15  // EMA追踪止损检查间隔
  }
}
```

### 3.5 创建 systemd 服务（引擎）

```bash
cat > /etc/systemd/system/dtrs-engine.service << 'EOF'
[Unit]
Description=DTRS Trading Engine
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/dtrs-engine
EnvironmentFile=/opt/dtrs-engine/runtime.env
ExecStart=/usr/bin/python3.11 main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable dtrs-engine
systemctl start dtrs-engine
```

### 3.6 创建 systemd 服务（持仓监控）

```bash
cat > /etc/systemd/system/dtrs-monitor.service << 'EOF'
[Unit]
Description=DTRS Position Monitor (WebSocket)
After=network.target dtrs-engine.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/dtrs-engine
EnvironmentFile=/opt/dtrs-engine/runtime.env
ExecStart=/usr/bin/python3.11 position_monitor.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable dtrs-monitor
systemctl start dtrs-monitor
```

### 3.7 验证服务状态

```bash
systemctl status dtrs-engine
systemctl status dtrs-monitor

# 查看实时日志
journalctl -u dtrs-engine -f
journalctl -u dtrs-monitor -f
```

正常启动日志应包含：
```
DTRS Scheduler starting...
Scan pool refreshed: 500 symbols
All task loops started
Uvicorn running on http://0.0.0.0:8888
```

---

## 四、部署前端管理界面（dtrs-trading）

前端是一个独立的 React Web 应用，可部署在同一台服务器或其他服务器上。

### 4.1 安装 Node.js

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs
node --version  # v20.x.x
```

### 4.2 安装依赖并构建

```bash
cd /opt/dtrs-trading  # 前端代码目录
npm install
npm run build
```

### 4.3 配置 Nginx 反向代理

```bash
apt install -y nginx

cat > /etc/nginx/sites-available/dtrs << 'EOF'
server {
    listen 80;
    server_name 你的服务器IP或域名;

    # 前端静态文件
    root /opt/dtrs-trading/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    # 反向代理到后端 API
    location /api/ {
        proxy_pass http://127.0.0.1:8888;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF

ln -s /etc/nginx/sites-available/dtrs /etc/nginx/sites-enabled/
nginx -t
systemctl restart nginx
```

---

## 五、Docker 部署（可选）

如果服务器已安装 Docker，可使用 Docker Compose 一键部署：

```bash
cd /opt/dtrs-engine

# 先配置环境变量
cp runtime.env .env

# 启动所有服务
docker-compose up -d

# 查看日志
docker-compose logs -f
```

---

## 六、常用运维命令

### 服务管理

```bash
# 重启引擎
systemctl restart dtrs-engine

# 重启监控
systemctl restart dtrs-monitor

# 查看引擎日志（最近100行）
journalctl -u dtrs-engine -n 100 --no-pager

# 查看监控日志
journalctl -u dtrs-monitor -n 100 --no-pager
```

### 数据库查询

```bash
# 查看当前持仓
sqlite3 /opt/dtrs-engine/data/trading.db "SELECT symbol, direction, entry_price, tp1, sl, status FROM positions WHERE status='OPEN';"

# 查看最近信号
sqlite3 /opt/dtrs-engine/data/trading.db "SELECT symbol, period, direction, status, created_at FROM signals ORDER BY created_at DESC LIMIT 20;"

# 查看历史盈亏
sqlite3 /opt/dtrs-engine/data/trading.db "SELECT symbol, realized_pnl, closed_at FROM positions WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT 20;"
```

### 紧急操作

```bash
# 紧急平仓所有持仓（谨慎使用）
cd /opt/dtrs-engine && python3.11 close_all.py

# 手动触发一次扫描（通过 API）
curl -X POST http://localhost:8888/api/scan/manual

# 更新黑名单后重启生效
systemctl restart dtrs-engine
```

---

## 七、安全建议

1. **API Key 权限最小化：** 只开启合约交易权限，绝对不要开启提现权限。
2. **IP 白名单：** 在币安 API 管理页面绑定服务器 IP，防止 Key 泄露后被滥用。
3. **防火墙配置：** 只开放必要端口（80/443 用于前端，8888 仅本机访问）。
4. **定期备份数据库：** `cp /opt/dtrs-engine/data/trading.db /backup/trading_$(date +%Y%m%d).db`
5. **不要将 runtime.env 提交到 Git：** 该文件已在 `.gitignore` 中排除。

---

## 八、故障排查

| 现象 | 可能原因 | 解决方法 |
|------|----------|----------|
| 服务启动失败 | Python 依赖未安装 | `pip3.11 install -r requirements.txt` |
| API 连接失败 | IP 被币安封禁（418） | 等待封禁解除，检查请求频率 |
| 信号不触发 | 入场条件未满足 | 查看扫描日志，确认三条件是否同时满足 |
| 止盈未触发 | WebSocket 断线 | `systemctl restart dtrs-monitor` |
| 前端无法访问 | Nginx 配置错误 | `nginx -t` 检查配置，`systemctl status nginx` |
