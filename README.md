# QQQ Option Alert System (LEAPS Alpha)

一个基于 FastAPI + APScheduler + Pandas 的 QQQ LEAPS 期权全生命周期监控系统。系统集成了多源行情灾备、高精度指标计算和多维度的阶梯止盈风控体系，旨在为 QQQ LEAPS 策略提供专业的实时追踪与预警。

## 🌟 核心功能

### 1. QQQ 分级入场提醒 (Entry Signals)
系统通过 Pandas 实时计算多维技术指标，捕捉不同强度的入场机会：
- **Level 1 (日常回调)**: QQQ 跌幅 ≥ 1.2% 且股价触碰/接近 MA20（距离 < 0.5%）。
- **Level 2 (黄金坑机会)**: 3日累计跌幅 ≥ 3.5% 且 RSI < 32。
- **Level 3 (极端超卖)**: 价格跌破布林下轨（Bollinger Lower Band）。
- **VIX 配合判断 (NEW)**: 当触发任一入场信号时，系统自动同步获取 **VIX 波动率指数**，并根据 `VIX < 28` 的阈值提供风险偏好建议（安全区 vs 高风险区）。
- **趋势增强**: 若价格低于年线（MA200），自动激活 `⚠️ [熊市趋势]` 标记，提醒降低仓位预期。

### 2. 期权多维止盈体系 (Exit Signals)
针对 LEAPS 期权的长期持仓特性，实现了智能化的动态止盈逻辑：
- **🎯 阶梯止盈 (Tiered TP)**: 随持仓时间推移自动调整利润预期，兼顾爆发力与确定性。
  - 持仓 < 4个月: 50% 利润翻倍点。
  - 持仓 5-6个月: 30% 利润落袋。
  - 持仓 7个月+: 10% 利润保底。
- **🚀 极速爆发 (Fast TP)**: 持仓 ≤ 7天且盈利达到 15%，捕捉短线暴力拉升。
- **📉 移动止盈 (Trailing Stop)**: 历史最高收益 > 30% 后，若利润回撤超过 10% 自动触发提醒。
- **⚠️ 技术见顶 (Technical TP)**: QQQ RSI > 75（极端超买）或突破布林上轨。

### 3. 多级数据灾备与实时补丁 (Data Resilience)
采用三层行情保障机制，解决免费 API 的延迟与稳定性问题：
- **Primary (Yahoo Finance)**: 获取完整 1 年历史数据进行高精度技术指标计算。
- **Fallback (Polygon.io)**: 在 yfinance 失效时，自动获取近 300 天聚合数据重构 DataFrame。
- **Realtime Patch**: 独创“实时补丁”机制，在使用 Polygon EOD 数据的基础上，自动打入 yfinance 今日实时价补丁，确保盘中指标零延迟。

### 4. 严格风险管理 (Risk Control)
- **⏳ 移仓窗口**: DTE < 120 天，提醒寻找更远期的月份滚动。
- **⛔ 强制清仓**: DTE < 90 天，强制退出以规避 Theta 剧烈衰减。
- **🛑 趋势崩坏**: QQQ 有效跌破 MA200（跌幅超过年线 1%），标记长线趋势反转信号。

### 5. 专业 Web 管理后台
- **位置中心**: 支持期权持仓的增删改查，自动追踪 `Max Profit`。
- **配置中心 (NEW)**: 图形化界面开关各项入场/出场规则，实时调整系统参数。
- **透明日志**: 详尽记录每次信号触发的指标细节与发送状态。

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
   - 申请 [Polygon.io](https://polygon.io/) 免费版 API Key。
   - 配置企业微信机器人 Webhook。

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
- `ADMIN_PASSWORD`: 管理员密码
- `POLYGON_API_KEY`: 从 Polygon.io 获取的 API Key
- `WECHAT_WEBHOOK_URL`: 企业微信机器人 Webhook 地址
- 其他可选配置项可根据需要启用/禁用

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
- 更新应用：修改代码后重新运行 `docker-compose up -d --build`

#### 4. 初始化系统
首次启动后，访问 `http://localhost:8000/setup` 进行系统初始化设置。

#### 5. 数据持久化
系统数据（包括 SQLite 数据库）将持久化存储在 Docker 卷 `leaps` 中，即使容器重启也不会丢失数据。

### 传统部署方式

1. **创建虚拟环境**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   # 或
   venv\Scripts\activate  # Windows
   ```

2. **安装依赖**:
   ```bash
   pip install -r requirements.txt
   ```

3. **配置环境变量**:
   复制 `.env.example` -> `.env` 并填写 API Key。

4. **启动应用**:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```


### 数据库更新说明
若从 1.0 版本升级，请确保执行以下 SQL 以支持移动止盈功能：
```sql
ALTER TABLE option_positions ADD COLUMN max_profit FLOAT DEFAULT 0.0;
```

## 📊 规则对照表

### 入场逻辑 (QQQ Entry)

| 级别 | 标签 | 触发条件 | 策略意图 |
| :--- | :--- | :--- | :--- |
| **L1** | `🟢 [日常回调]` | 跌幅 > 1.2% + 触碰 MA20 | 寻找上升趋势中的回踩点 |
| **L2** | `🚨 [黄金坑]` | 3日跌幅 > 3.5% + RSI < 32 | 捕捉中等规模的非理性超跌 |
| **L3** | `📉 [极端超卖]` | 价格 < 布林下轨 | 极端恐慌状态下的左侧左侧机 |

### 出场逻辑 (Option Exit)

| 类别 | 标签 | 触发逻辑 | 备注 |
| :--- | :--- | :--- | :--- |
| **止盈** | `🎯 [目标达成]` | 持仓时长决定 (10% - 50%) | 阶梯式盈利收割 |
| **爆发** | `🚀 [极速爆发]` | 持仓 ≤ 7天 且 利润 ≥ 15% | 加速阶段落袋为安 |
| **回撤** | `📉 [利润回撤]` | Max Profit > 30% 且 回撤 > 10% | 锁定利润，防范利润吐回 |
| **风险** | `⏳ [移仓窗口]` | DTE < 120 天 | 尽早滚动到远期合约 |
| **强制** | `⛔ [强制清仓]` | DTE < 90 天 | 致命 Theta 衰减防御 |
| **止损** | `🛑 [趋势崩坏]` | QQQ < 0.99 * MA200 | 长期趋势彻底走坏 |

## ⚖️ 许可证

MIT License
