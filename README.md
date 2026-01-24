# QQQ Option Alert System

一个长期稳定运行的 QQQ LEAPS 期权行情提醒系统，支持 QQQ 跌幅分级提醒和期权仓位实时监控，通过企业微信机器人推送提醒。

## 功能特性

### QQQ 跌幅提醒
- **Rule A**: 昨日收盘 → 当前价 (-2%)
- **Rule B**: 当日最高 → 当前价 (-2%)
- **Rule C**: 2日前Close → 当前价 (累计≥-2%)
- **Rule D**: 3日滚动高点 → 当前价 (-2%)

每条规则可独立开关，同一天只提醒一次。

### 期权仓位监控
- 手动录入期权仓位（标的、类型、行权价、到期日、入场价、数量、入场日期）
- **实时价格追踪**：自动获取期权当前价格
- **盈亏显示**：实时显示盈亏金额和比例（1张=100股）
- **一键刷新**：手动刷新单个或全部期权价格
- 分阶段止盈规则（根据持仓时间自动调整阈值）
- 止损规则（可配置百分比）
- 时间风险提醒（剩余到期天数预警）
- 最大持仓周期提醒（270天）

### 混合行情数据源
- **Yahoo Finance**：获取 QQQ 和期权当日实时价格（免费，无限制）
- **Polygon.io**：获取历史数据作为备用和补充
- 智能切换，自动重试

### 交易时段控制
- 仅在美股交易日运行
- 仅在美股交易时段（盘中）拉取行情和判断规则
- 非交易时间不消耗 API 配额

### Web 后台管理
- 首次启动设置管理员密码
- 仓位管理（增删改、实时价格刷新）
- 规则配置（实时调整阈值）
- 提醒日志查看
- 健康检查面板

### 企业微信推送
- 支持 Markdown 格式消息
- 包含触发规则、价格、跌幅、盈亏金额、时间等详细信息

## 技术栈

- **后端**: FastAPI + APScheduler
- **数据库**: SQLite
- **行情数据**: Yahoo Finance（实时） + Polygon.io（历史）
- **交易日历**: pandas_market_calendars
- **容器化**: Docker + docker-compose

## 部署指南

### 前置要求

- Docker 和 docker-compose
- **可选**: Polygon.io API Key（免费版可作为备用）
- 企业微信机器人 Webhook URL

### 快速部署

1. 克隆项目

```bash
git clone <repository-url>
cd qqq-option-alert
```

2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env` 文件，填入以下信息：

```env
POLYGON_API_KEY=your_polygon_api_key
WECHAT_WEBHOOK_URL=your_wechat_webhook_url
```

3. 启动服务

```bash
docker-compose up -d
```

4. 访问 Web 后台

首次访问会跳转到设置页面：`http://your-server:8000/setup`

设置管理员密码后即可登录：`http://your-server:8000/admin/login`

### Oracle 2C1G VPS 部署

本项目已针对 Oracle 免费版 2C1G 配置进行优化：

- 使用 Alpine 镜像（更小）
- 资源限制：最大 512MB 内存，1 CPU
- 数据库连接池优化
- 智能缓存减少 API 调用

启动命令：

```bash
docker-compose up -d
```

查看日志：

```bash
docker-compose logs -f
```

## 使用说明

### 1. 首次设置

访问 `http://your-server:8000/setup`，设置管理员密码。

### 2. 添加期权仓位

登录后进入「仓位管理」，填写期权信息：
- 标的（默认 QQQ）
- 类型（Call/Put）
- 行权价
- 到期日
- 入场价
- 数量（可选）
- 入场日期

### 3. 配置规则

进入「规则配置」，可以：
- 启用/禁用 QQQ 跌幅规则
- 调整期权止盈止损阈值
- 修改 DTE 警告天数

### 4. 查看日志

进入「提醒日志」，查看所有历史提醒记录。

## 规则说明

### QQQ 跌幅规则

| 规则 | 条件 | 频率 |
|------|------|------|
| A | `当前价 <= 昨日收盘 * 0.98` | 每天最多1次 |
| B | `当前价 <= 当日最高 * 0.98` | 每天最多1次 |
| C | `当前价 <= 2日前收盘 * 0.98` | 每天最多1次 |
| D | `当前价 <= 3日滚动最高 * 0.98` | 每天最多1次 |

### 期权规则

#### 分阶段止盈
- **阶段1**（前 120 天）：盈利 +50% 提醒
- **阶段2**（120-180 天）：盈利 +30% 提醒
- **阶段3**（180 天后）：盈利 +10% 提醒

#### 止损
- 亏损达到配置的百分比（默认 -30%）提醒

#### 时间风险
- 剩余到期天数 ≤ 配置天数（默认 45 天）提醒

#### 最大持仓周期
- 持仓达到配置天数（默认 270 天）提醒

## 常见问题

### Q: 行情数据来源是什么？

A: 系统使用混合数据源策略：
- **Yahoo Finance**：获取 QQQ 和期权当日实时价格，完全免费，无需 API Key
- **Polygon.io**：作为备用数据源获取历史数据，需要注册获取免费 API Key

### Q: 如何获取 Polygon.io API Key？

A: 访问 https://polygon.io/ 注册账号，免费版每分钟可调用 5 次 API。

### Q: 如何创建企业微信机器人？

A:
1. 在企业微信群中添加群机器人
2. 复制 Webhook URL
3. 将 URL 填入 `.env` 文件

### Q: 期权盈亏金额如何计算？

A: 系统按照标准期权合约计算：
- **1 张期权 = 100 股**
- 盈亏金额 = (当前价 - 入场价) × 数量 × 100
- 盈亏比例 = (当前价 - 入场价) / 入场价 × 100%

例如：入场价 $6.50，当前价 $7.50，1 张合约 = 盈利 $100

### Q: 系统消耗多少资源？

A:
- 内存：150-300MB
- CPU：< 5%（大部分时间空闲）
- 磁盘：< 50MB（随日志增长）

### Q: 如何更新系统？

A:
```bash
docker-compose down
git pull
docker-compose up -d --build
```

### Q: 数据存储在哪里？

A:
- 数据库文件：`./data/qqq_alert.db`
- 数据通过 Docker volume 持久化，更新容器不会丢失

## 文件结构

```
qqq-option-alert/
├── app/
│   ├── admin/          # Web 后台
│   ├── alerts/         # 提醒规则
│   ├── config.py       # 配置管理
│   ├── database/       # 数据库
│   ├── main.py         # FastAPI 应用
│   ├── market/         # 行情数据
│   ├── notification/   # 企业微信推送
│   └── scheduler/      # 定时任务
├── data/               # 数据目录
├── tests/              # 测试
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## 许可证

MIT

## 贡献

欢迎提交 Issue 和 Pull Request。
