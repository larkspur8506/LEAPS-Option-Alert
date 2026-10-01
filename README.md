# QQQ LEAPS 长期看涨期权信号提醒系统

一个基于 FastAPI + APScheduler + yfinance 的 **QQQ（纳指100 ETF）LEAPS 买入信号提醒与持仓跟踪系统**。

> ⚠️ 本系统只做信号提醒与持仓记录，**不连接券商、不自动下单**。所有买卖操作由用户在券商手动完成。

## 策略说明（回测验证口径）

信号与规则基于 ^NDX/QQQ 1999→2026 历史数据回测（同款入场逻辑共 49 个 QQQ 历史信号）：

- **网格对照**：原 MEXC 永续网格方案（±15%/300格/3x 等）在 0 手续费下每周期期望仍为 **-5.4% ~ 0%**（主因是下轨尾部：45 次摸上轨平均只赚 +0.57% 保证金，15 次打穿下轨平均 -24%），已整体下线。
- **LEAPS 买入**（当前实现）：同批信号期望 **+63% ~ +74%**（权利金口径，含分批止盈）；2026-10 复测定稿：1 年期合约 + DTE 90 组合下 49 笔仅 1 笔亏损（-6.8%）、其余全部 RSI>65 止盈，2010 年起顺序复利 CAGR ≈ +103%（回测引擎口径，非收益承诺）。

| 规则 | 内容 | 默认值（后台可调） |
|---|---|---|
| 入场 | RSI14 < 阈值 + 连续 3 日收盘 > SMA200 + 收盘 > 一年前收盘（收盘确认制） | RSI < 35 |
| 建议合约 | QQQ Call，轻微实值 LEAPS | Delta ≈ 0.65 / 约 1 年（365 天） |
| 加仓 | 较信号基准回撤达档位且 RSI 仍在入场区，逐档提醒 | -15% / -25%，最多 3 张 |
| 分批止盈 | 总盈利 ≥ 阈值 → 提醒卖出一半（仅提醒，持仓页确认部分平仓） | +50%（0 = 关闭） |
| 止盈 | RSI14 > 阈值 → 提醒全部清仓 | RSI > 65 |
| 时间止损 | 持仓超 N 个交易日且未回本 → 提醒平仓 | 关闭（0；复测表明该规则制造了历史唯二亏损） |
| 到期风控 | 距到期不足 N 天 → 强制清仓提醒 | 90 天（1 年期合约配套值） |
| 建议有效期 | WAITING 建议 TTL（交易日）自动过期，防止信号闸门被永久占用 | 3 交易日 |

退出规则**先到先出**，任一命中即推送提醒（分批止盈除外——仅提醒、不改仓位状态），并把仓位标记 CLOSED（用户确认实际平仓后状态由后台记录）。

## 数据与链路

- **数据源**：yfinance 获取 QQQ 日线（2 年窗口）+ 实时价格；S&P 500 / VIX 仅作日报辅助展示。
- **收盘确认制**：盘中最后一根日 K 是未收盘的实时 bar，开仓判定固定用「最后一根已收盘 bar」的等价指标（`closed_*`），盘中 RSI 瞬时破位不会制造假信号。
- **期权报价跟踪**：yfinance 期权链尽力刷新持仓权利金（60 分钟节流），仅用于展示与告警富化；**退出判定不依赖报价**（数据缺失时时间止损按「未回本」保守处理）。
- **监控频率**：APScheduler 每 5 分钟轮询；每日 16:30（美东）LEAPS 日报。
- **通知**：企业微信 webhook；AlertLog 与实际推送内容逐字一致（审计口径）。

## 后台页面

- **`/admin`**：Dashboard。QQQ 行情指标、入场信号状态、当前仓位（WAITING/HOLDING）与快捷入口。
- **`/admin/positions`**：持仓管理。确认建仓（录入实际合约参数）、加仓录入、平仓归档、忽略建议，以及仓位历史。
- **`/admin/rules`**：策略参数（止盈 RSI / 时间止损 / DTE / 加仓档位 / 最大张数 / 建议 Delta 与期限）与日报模式，保存后下一轮监控生效。
- **`/admin/logs`**：提醒日志（与推送内容逐字一致）。

## REST API (`/api/leaps/*`)

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/leaps/status` | QQQ 数据 + 当前仓位聚合 |
| GET | `/api/leaps/waiting` / `/holding` / `/history` | 分状态查询 |
| POST | `/api/leaps/{id}/confirm` | WAITING → HOLDING（录入实际合约参数） |
| POST | `/api/leaps/{id}/add-lot` | HOLDING 加仓（累加张数/成本） |
| POST | `/api/leaps/{id}/close` | HOLDING → CLOSED（平仓记录） |
| POST | `/api/leaps/{id}/partial-close` | HOLDING 部分平仓（卖部分张数，累计已落袋权利金） |
| POST | `/api/leaps/{id}/dismiss` | WAITING → DISMISSED（释放信号闸门） |

全部接口受签名会话 Cookie 保护（未认证 401）；非法状态迁移 409；参数错误 400。

## 部署

```bash
cp .env.example .env   # 填入 WECHAT_WEBHOOK_URL / SETUP_TOKEN
docker compose up -d
```

首次访问 `/setup?token=<SETUP_TOKEN>` 设置管理员口令（设置后该入口永久关闭）。

### 环境变量（节选，全部可在后台规则页覆盖）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LEAPS_TP_RSI` | `65.0` | 止盈 RSI 阈值 |
| `LEAPS_TIME_STOP_TRADING_DAYS` | `0` | 时间止损（交易日，0 = 关闭） |
| `LEAPS_DTE_FORCE_DAYS` | `90` | DTE 强制平仓（自然日，1 年期合约配套值） |
| `LEAPS_ADD_LEVELS` | `0.15,0.25` | 加仓回撤档位 |
| `LEAPS_MAX_QUANTITY` | `3` | 单信号最大张数 |
| `LEAPS_TARGET_DELTA` / `LEAPS_TARGET_TENOR_DAYS` | `0.65` / `365` | 建议合约参数 |
| `LEAPS_HALF_TP_PNL` | `0.5` | 分批止盈阈值（总盈利比例，0 = 关闭） |
| `WAITING_TTL_TRADING_DAYS` | `3` | WAITING 建议存活期（0 = 不过期） |
| `DAILY_REPORT_MODE` | `leaps` | 日报模式（`off` / `leaps`） |

## 项目结构

```
app/
├── admin/            # 认证与会话安全 (scrypt 口令 + 签名 Cookie + 登录限速)
├── alerts/
│   ├── qqq_rules.py      # 入场/退出/加仓规则引擎 (纯函数)
│   ├── leaps_monitor.py  # 监控状态机驱动 (fail-closed)
│   ├── alert_log.py      # 统一 AlertLog 写入层
│   └── dedup.py          # 提醒去重 (按日/按周/落库级)
├── api/leaps_api.py  # REST API (签名会话保护)
├── database/         # SQLAlchemy 模型 + 轻量迁移
├── market/           # yfinance 数据获取与指标 (RSI14/SMA200/52周)
├── notification/     # 企业微信格式化与发送
├── scheduler/        # APScheduler 任务 + NYSE 交易日历
└── services/         # OptionPosition 状态机 (WAITING→HOLDING→CLOSED)
```

## 历史沿革

本项目早期即 QQQ/LEAPS 信号应用（`7a9105a`），中途切换为 NDX 永续网格（`4a7a02c` 移除 LEAPS），2026-09 基于网格期望为负的回测结论（62 个 ^NDX 历史信号 + QQQ 复验）整体切回 LEAPS 并加装回测验证过的加仓/退出规则。
