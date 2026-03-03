# DTRS — Dynamic Trading & Risk System

> 基于币安合约的全自动量化交易系统，支持多周期扫描、三条件入场、五层风控、分批止盈与跟单功能。

---

## 项目简介

DTRS（Dynamic Trading & Risk System）是一套运行在 Linux 服务器上的合约量化交易引擎，通过调用币安 Futures API 实现全自动开仓、止盈、止损和持仓管理。系统采用前后端分离架构，后端引擎负责信号扫描与交易执行，前端管理界面提供实时监控与参数配置。

---

## 核心功能

| 功能模块 | 说明 |
|----------|------|
| 多周期扫描 | 支持 1h / 4h / 1d 三个时间周期，整点自动触发 |
| 三条件入场 | MA20 穿越 + MACD 金叉/死叉 + 量能放大，三者同时满足才开仓 |
| 信号时效验证 | 只在信号K线的下一根K线开仓，拒绝过期信号 |
| 五层风控 | 保证金分配、最大持仓数、保证金率熔断、大周期方向优先、紧急止损 |
| 分批止盈 | TP1（40%仓位）+ TP2（剩余仓位），TP1触达后止损上移锁利 |
| 移动止盈 | EMA20 追踪止损 + MA20 穿线止盈 |
| WebSocket 实时监控 | 订阅标记价格推送，毫秒级止盈止损检测，不轮询不限流 |
| 跟单功能 | 支持多账户跟单，按比例同步开平仓 |
| 前端管理界面 | 实时持仓、信号监控、历史记录、参数配置 |

---

## 文档导航

- [部署操作说明](DEPLOY.md) — 从零开始部署系统的完整步骤
- [代码详细解释](CODE_GUIDE.md) — 每个模块的功能与实现逻辑
- [交易策略说明](STRATEGY.md) — 入场、出场、风控的完整策略文档

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 语言 | Python 3.11 |
| Web 框架 | FastAPI + Uvicorn |
| 数据库 | SQLite（本地持久化） |
| 行情接口 | 币安 Futures REST API + WebSocket |
| 指标计算 | pandas-ta / ta-lib |
| 部署 | systemd 服务 / Docker（可选） |

---

## 目录结构

```
dtrs-engine/
├── main.py                 # 应用入口，启动 FastAPI + 调度器
├── position_monitor.py     # 持仓监控主进程（WebSocket 实时价格）
├── requirements.txt        # Python 依赖
├── Dockerfile              # Docker 镜像构建文件
├── docker-compose.yml      # Docker Compose 编排文件
├── api/
│   ├── __init__.py
│   └── routes.py           # REST API 路由（前端接口）
├── config/
│   ├── __init__.py
│   └── settings.py         # 配置加载与数据结构定义
├── core/
│   ├── binance_client.py   # 币安 API 封装
│   ├── copy_trader.py      # 跟单执行器
│   ├── database.py         # 数据库操作（持仓、信号、日志）
│   ├── executor.py         # 交易执行器（下单、止盈止损）
│   ├── indicators.py       # 技术指标计算（MA/MACD/RSI/ATR）
│   ├── monitor.py          # 持仓监控逻辑（止盈止损判断）
│   ├── scanner.py          # 信号扫描器（入场条件判断）
│   └── scheduler.py        # 定时调度器（多周期扫描触发）
├── data/
│   └── config.json         # 运行时参数配置（可热修改）
└── utils/
    └── __init__.py
```

---

## 快速开始

详见 [DEPLOY.md](DEPLOY.md)。

---

## 免责声明

本系统仅供学习和研究使用。加密货币合约交易具有极高风险，可能导致本金全部损失。使用本系统产生的任何盈亏，由使用者自行承担。
