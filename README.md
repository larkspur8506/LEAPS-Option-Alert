# QQQ Option Alert System

一个基于 FastAPI + APScheduler + yfinance 的 QQQ LEAPS 期权监控系统，运行在 Docker 环境中。引入了基于 Pandas 的详尽行情指标计算，实现了“分级入场”、“多维止盈”和“严格风险控制”的策略体系。

## 功能特性

### QQQ 分级入场提醒
系统实时计算多维技术指标（MA20, MA200, RSI, Bollinger Bands），提供分级提醒：
- **Level 1 (日常回调)**: 跌幅 >= 1.2% 且触碰 MA20（或布林中轨）。
- **Level 2 (黄金坑机会)**: 3日累计跌幅 >= 3.5% 且 RSI < 32。
- **Level 3 (极端超卖)**: 价格跌破布林下轨。
- **趋势过滤**: 若价格低于年线（MA200），自动标记 `⚠️ [熊市趋势]`。

### 多级数据灾备机制 (Market Data Resilience)
针对大盘行情，系统实现了高可靠的多级失效切换：
- **Level 1 (Yahoo Finance)**: 获取完整 1 年历史数据进行指标计算（首选）。
- **Level 2 (Polygon.io Reconstruction)**: 若 yfinance 历史请求失败，自动从 Polygon 拉取约 300 天聚合数据重构 DataFrame。
- **Realtime Patch**: 在 Polygon 历史数据基础上，自动打入 `yfinance` 今日实时最新价补丁，解决免费版 EOD 数据延迟问题，确保指标包含最新波动。

### 期权仓位监控与风控
- **实时价格追踪**: 自动获取期权当前价格（yfinance / Polygon）。
- **多维止盈体系**:
  - **🎯 硬性止盈**: 盈利达到 50%。
  - **🚀 极速爆发**: 持仓 <= 7天且盈利达到 15%。
  - **📉 移动止盈**: 历史最高收益 > 30% 后回撤超过 10%。
  - **⚠️ 技术见顶**: QQQ RSI > 75 或突破布林上轨。
- **严格风险控制**:
  - **⏳ 移仓窗口**: DTE < 120 天。
  - **⛔ 强制清仓**: DTE < 90 天。
  - **🛑 趋势崩坏**: QQQ 有效跌破 MA200（< 99%）。

### 高精度技术指标
- **原生 Pandas 实现**: 弃用占位符，指标计算与主流行情软件对齐。
- **Wilder's Smoothing**: RSI 使用 `ewm(com=13)` 实现。
- **历史回溯**: 真实获取昨日及3日前收盘价，确保逻辑严密。

### Web 后台管理
- **仓位管理**: 增删改期权仓位，支持 `Max Profit` 追踪。
- **配置中心**: 实时开关规则、调整全局参数。
- **健康检查**: 包含数据库、任务调度及行情源状态。

## 技术栈

- **后端**: FastAPI + APScheduler
- **分析库**: **Pandas** (技术指标核心)
- **数据库**: SQLite (支持历史最高收益追踪)
- **行情源**: yfinance + Polygon.io (双路互备 + 实时补丁)
- **部署**: Docker / Docker Compose

## 部署指南

### 快速部署

1. **环境准备**:
   ```bash
   git clone <repository-url>
   cd qqq-option-alert
   ```

2. **环境变量**:
   复制并修改 `.env`:
   ```env
   POLYGON_API_KEY=your_key
   WECHAT_WEBHOOK_URL=your_webhook
   ```

3. **启动**:
   ```bash
   docker-compose up -d --build
   ```

4. **初始化**:
   访问 `http://localhost:8000/setup` 设置密码。

### 数据库更新提醒 (重要)
由于新增了 `max_profit` 追踪字段，若通过旧版本升级，请手动执行以下 SQL 或删除 `app.db` 重建：
```sql
ALTER TABLE option_positions ADD COLUMN max_profit FLOAT DEFAULT 0.0;
```

## 规则说明

### QQQ 入场信号等级

| 等级 | 标签 | 核心条件 | 目的 |
| :--- | :--- | :--- | :--- |
| **L1** | `🟢 [日常回调]` | 跌幅 > 1.2% + MA20 支撑 | 寻找强势股回踩买点 |
| **L2** | `🚨 [黄金坑]` | 3日跌幅 > 3.5% + RSI < 32 | 捕捉超跌反弹机会 |
| **L3** | `📉 [极端超卖]` | 股价跌破布林下轨 | 极端恐慌位博弈 |

### 期权退出信号

| 类别 | 标签 | 触发逻辑 |
| :--- | :--- | :--- |
| **止盈** | `🎯 [目标达成]` | 利润达 50% |
| **止盈** | `🚀 [极速爆发]` | 短期(7天内)快速获利 15% |
| **回撤** | `📉 [利润回撤]` | 浮盈曾 >30%，现回撤超过 10% |
| **时间** | `⛔ [强制清仓]` | DTE < 90 天，规避 Theta 剧烈衰减 |
| **趋势** | `🛑 [趋势崩坏]` | QQQ 跌破 MA200 (跌幅 1%) |

## 许可证

MIT
