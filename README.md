# QQQ Option Alert System (LEAPS 长期复利引擎)

一个基于 FastAPI + APScheduler + Pandas 的 QQQ LEAPS 期权全生命周期监控系统。系统集成了多源行情灾备、高精度指标计算，旨在为 **QQQ LEAPS 长期复利引擎** 策略提供严格的机械化执行、实时追踪与预警。

## 🌟 核心策略与功能

### 1. 底层资产与交易工具 (Asset & Instrument)
- **底层资产**: QQQ (Nasdaq 100 ETF)
- **交易工具**: LEAPS Call (长期看涨期权)
- **期权筛选规则**:
  - **到期日 (Expiration)**: 选择距离现在约 2 年（730天左右）到期的合约。
  - **Delta 值**: 选择深度实值，Delta 接近 0.7 的期权。

### 2. 入场逻辑 (Entry Signals)
系统通过 Pandas 实时计算日线级别技术指标，精准捕捉市场恐慌情绪：
- **技术指标**: 日线级别 **RSI (14)**。
- **触发条件**: 当 RSI 跌破 **35** 时（代表市场进入超卖区/恐慌期），触发入场警报。
- **执行频率限制 (Frequency Gate)**: 为平摊成本，系统级管控每周**最多只允许触发一次**买入指令。

### 3. 出场逻辑 / 阶梯止盈 (Exit & Profit Taking)
该策略严格执行基于**持仓时间**的阶梯止盈（需在系统 Dashboard 录入期权仓位，系统将自动追踪盈亏并基于录入成本计算）：
- **持有期 < 12 个月**: 利润达到 **100%** 时，立即止盈平仓。
- **持有期 12-15 个月**: 利润达到 **50%** 时，立即止盈平仓。
- **持有期 16-18 个月**: 利润达到 **30%** 时，立即止盈平仓。

### 4. 强制止损 / 时间风控 (Hard Stop)
- **时间止损**: 当期权合约距离到期日仅剩 **6 个月 (约 180 天)** 时，无论当前盈亏状态如何，必须强制平仓，以严格规避末期加速的时间价值衰减（Theta Decay）。

### 5. 专业 Web 管理后台
- **市场感知 (Dashboard)**: 首页集成实时行情挂件，展示 QQQ 现价与实时 RSI(14) 指标。跌破 35 时自动标记为红色超卖区。
- **仓位管理 (Positions)**: 支持期权持仓的增删改查。系统将基于录入的买入成本和时间，每天自动计算期权现价盈亏，并适配动态阶梯止盈规则。
- **规则与日志 (Rules & Logs)**: 静态展示策略规则及查阅历史所有推送记录。

## 🛠️ 技术栈

- **后端**: FastAPI + APScheduler
- **分析**: **Pandas** (指标计算核心)
- **数据库**: SQLite + SQLAlchemy
- **行情**: yfinance + Polygon.io (双路互备 + 实时补丁)
- **推送**: 企业微信 Webhook (WeChat Alert)
- **部署**: Docker + Docker Compose

## 🚀 部署指南

### 环境准备

1. **获取 API Key**:
   - 申请 [Polygon.io](https://polygon.io/) 免费版 API Key（用于期权报价和精准股票兜底数据）。
   - 配置企业微信群机器人，获取 Webhook URL。

2. **克隆项目**:
   ```bash
   git clone <repository-url>
   cd leaps-option-alert
   ```

### Docker 部署（推荐）

#### 1. 环境配置
复制 `.env.example` 文件为 `.env`，并填入真实配置值：
```bash
cp .env.example .env
```

编辑 `.env` 文件，填入以下必要配置：
- `POLYGON_API_KEY`: 从 Polygon.io 获取的 API Key
- `WECHAT_WEBHOOK_URL`: 企业微信机器人 Webhook 地址

#### 2. 构建并启动服务
使用 Docker Compose 构建并启动服务：
```bash
docker-compose up -d --build
```

#### 3. 服务管理命令
- 查看服务状态：`docker-compose ps`
- 查看实时日志：`docker-compose logs -f`
- 重启服务：`docker-compose restart`
- 停止服务：`docker-compose down`

#### 4. 初始化系统
首次启动后，访问 `http://localhost:8000/setup` 进行系统初始化设置（设置 Web 管理端密码）。

#### 5. 数据持久化
系统数据（包括 SQLite 数据库）将持久化存储在 Docker 卷 `leaps` 中，即使容器重启也不会丢失数据。


## 📊 规则对照表

### 入场规则 (QQQ Entry)

| 指标 | 触发条件 | 频率限制 | 策略意图 |
| :--- | :--- | :--- | :--- |
| **RSI(14)** | `< 35` | 每周至多 1 次 | 捕捉大盘极度超卖/恐慌时的左侧机会，严格控制建仓节奏平摊成本 |

### 出场规则 (Option Exit)

| 类别 | 触发逻辑 (持仓时间 / DTE) | 盈亏条件 | 备注 |
| :--- | :--- | :--- | :--- |
| **阶梯止盈** | 持仓 `< 12 个月` | 利润 `≥ 100%` | 早期爆发，享受翻倍利润 |
| **阶梯止盈** | 持仓 `12 - 15 个月` | 利润 `≥ 50%` | 中期兑现，降低收益预期 |
| **阶梯止盈** | 持仓 `16 - 18 个月` | 利润 `≥ 30%` | 后期收割，落袋为安 |
| **强制平仓** | 距离到期日 `DTE ≤ 180 天` | **无视盈亏** | 规避 Theta 加速衰减的绝对红线 |

## ⚖️ 许可证

MIT License
