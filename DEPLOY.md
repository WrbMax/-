# DTRS 交易引擎 - 阿里云部署指南

## 一、系统架构

DTRS 系统由两部分组成：

| 组件 | 技术栈 | 部署位置 | 说明 |
|------|--------|----------|------|
| **前端管理界面** | React + TypeScript + TailwindCSS | Manus 托管 | 仪表盘、持仓管理、信号监控、策略配置 |
| **后端交易引擎** | Python + FastAPI | 阿里云 ECS | 策略执行、币安 API 交互、定时扫描 |

前端通过 REST API 与后端引擎通信，后端引擎独立运行在阿里云服务器上，负责所有实际的交易操作。

---

## 二、阿里云服务器准备

### 2.1 最低配置要求

| 项目 | 推荐配置 |
|------|----------|
| CPU | 2 核 |
| 内存 | 4 GB |
| 系统盘 | 40 GB SSD |
| 操作系统 | Ubuntu 22.04 LTS |
| 带宽 | 5 Mbps |

### 2.2 安全组配置

在阿里云控制台的安全组中，需要开放以下端口：

| 端口 | 协议 | 用途 |
|------|------|------|
| 22 | TCP | SSH 远程连接 |
| 8888 | TCP | DTRS 引擎 API |

> **安全建议**：8888 端口建议仅对你的 IP 地址开放，不要对 0.0.0.0/0 开放。

### 2.3 SSH 连接服务器

```bash
ssh root@你的服务器IP
```

---

## 三、环境安装

### 3.1 安装 Docker 和 Docker Compose

```bash
# 更新系统
apt update && apt upgrade -y

# 安装 Docker
curl -fsSL https://get.docker.com | sh

# 启动 Docker
systemctl enable docker
systemctl start docker

# 验证安装
docker --version
docker compose version
```

### 3.2 安装 Git（如果需要）

```bash
apt install -y git
```

---

## 四、部署引擎

### 4.1 上传代码到服务器

**方法一：使用 scp 上传**

在你的本地电脑上执行：

```bash
# 将整个 dtrs-engine 目录上传到服务器
scp -r ./dtrs-engine root@你的服务器IP:/opt/
```

**方法二：使用 Git**

如果你把代码推送到了 Git 仓库：

```bash
cd /opt
git clone 你的仓库地址 dtrs-engine
```

### 4.2 配置环境变量

```bash
cd /opt/dtrs-engine

# 复制环境变量模板
cp .env.example .env

# 编辑配置文件
nano .env
```

填入你的币安 API 密钥：

```
BINANCE_API_KEY=你的API_KEY
BINANCE_API_SECRET=你的API_SECRET
BINANCE_TESTNET=true
```

> **重要提醒**：
> 1. 首次部署请务必使用 `BINANCE_TESTNET=true`（测试网），确认一切正常后再切换到生产环境
> 2. API 密钥仅开启「合约交易」权限，**绝对不要开启提现权限**
> 3. 建议设置 IP 白名单，仅允许你的服务器 IP 访问

### 4.3 启动引擎

```bash
cd /opt/dtrs-engine

# 构建并启动（后台运行）
docker compose up -d --build

# 查看运行状态
docker compose ps

# 查看实时日志
docker compose logs -f
```

### 4.4 验证部署

```bash
# 检查引擎状态
curl http://localhost:8888/api/status

# 应该返回类似：
# {"engine_status":"running","wallet_balance":0,...}
```

---

## 五、前端连接后端

前端管理界面需要配置后端引擎的 API 地址。在前端的「策略配置」页面中，将 API 地址设置为：

```
http://你的服务器IP:8888
```

> 如果你配置了域名和 HTTPS，则使用：`https://你的域名`

---

## 六、从测试网切换到生产环境

当你在测试网上确认策略运行正常后：

```bash
cd /opt/dtrs-engine

# 编辑环境变量
nano .env

# 将 BINANCE_TESTNET 改为 false
# BINANCE_TESTNET=false

# 重启引擎
docker compose down
docker compose up -d --build
```

---

## 七、日常运维

### 7.1 常用命令

```bash
# 查看引擎状态
docker compose ps

# 查看实时日志
docker compose logs -f

# 重启引擎
docker compose restart

# 停止引擎
docker compose down

# 启动引擎
docker compose up -d

# 查看引擎日志文件
tail -f /opt/dtrs-engine/logs/dtrs.log
```

### 7.2 数据备份

```bash
# 备份数据库
cp /opt/dtrs-engine/data/dtrs.db /opt/dtrs-engine/data/dtrs.db.backup.$(date +%Y%m%d)

# 建议设置定时备份
crontab -e
# 添加以下行（每天凌晨3点备份）：
# 0 3 * * * cp /opt/dtrs-engine/data/dtrs.db /opt/dtrs-engine/data/dtrs.db.backup.$(date +\%Y\%m\%d)
```

### 7.3 更新引擎

```bash
cd /opt/dtrs-engine

# 停止当前引擎
docker compose down

# 更新代码（如果使用 Git）
git pull

# 或者重新上传代码（如果使用 scp）

# 重新构建并启动
docker compose up -d --build
```

---

## 八、安全加固建议

1. **防火墙**：使用 `ufw` 或阿里云安全组，仅开放必要端口
2. **HTTPS**：建议使用 Nginx 反向代理 + Let's Encrypt 证书
3. **API 密钥**：定期轮换币安 API 密钥
4. **监控告警**：配置阿里云云监控，设置 CPU/内存告警
5. **日志审计**：定期检查 `logs/dtrs.log` 中的异常记录

### 8.1 Nginx 反向代理配置（可选）

```bash
apt install -y nginx certbot python3-certbot-nginx

# 创建 Nginx 配置
cat > /etc/nginx/sites-available/dtrs << 'EOF'
server {
    listen 80;
    server_name 你的域名;

    location / {
        proxy_pass http://127.0.0.1:8888;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

ln -s /etc/nginx/sites-available/dtrs /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# 申请 SSL 证书
certbot --nginx -d 你的域名
```

---

## 九、故障排查

| 问题 | 排查方法 |
|------|----------|
| 引擎无法启动 | `docker compose logs` 查看错误日志 |
| API 连接失败 | 检查安全组是否开放 8888 端口 |
| 币安 API 报错 | 检查 API Key 权限和 IP 白名单 |
| 交易未执行 | 检查 `logs/dtrs.log` 中的 SCANNER 和 EXECUTOR 日志 |
| 内存不足 | `docker stats` 查看资源使用情况 |

---

## 十、联系方式

如有问题，请检查以下资源：
- 引擎日志：`/opt/dtrs-engine/logs/dtrs.log`
- 数据库：`/opt/dtrs-engine/data/dtrs.db`
- API 文档：`http://你的服务器IP:8888/docs`（FastAPI 自动生成）
