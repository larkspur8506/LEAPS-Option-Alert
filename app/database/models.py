from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Date, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class Configuration(Base):
    __tablename__ = "configuration"

    id = Column(Integer, primary_key=True, default=1)

    admin_password_hash = Column(String, nullable=False)

    wechat_webhook_url = Column(String, nullable=True)

    alert_log_retention_days = Column(Integer, default=90)

    # 每日 16:30 日报模式 ('off' / 'leaps')
    # 历史 'ndx_grid' / 'legacy' 值在 Config 读取层归一化为 'leaps'
    daily_report_mode = Column(String(20), nullable=True, default="leaps")

    # ---- LEAPS 策略参数 (后台可调, DB 优先于 env) ----
    leaps_tp_rsi = Column(Float, nullable=True)              # 止盈 RSI (默认 65)
    leaps_time_stop_trading_days = Column(Integer, nullable=True)  # 时间止损交易日 (0/NULL=关闭; 复测默认禁用)
    leaps_dte_force_days = Column(Integer, nullable=True)    # DTE 强制平仓 (默认 90 天, 1y 合约配套)
    leaps_add_levels = Column(String(50), nullable=True)     # 加仓回撤档 "0.15,0.25"
    leaps_max_quantity = Column(Integer, nullable=True)      # 单信号最大张数 (默认 3)
    leaps_target_delta = Column(Float, nullable=True)        # 建议 Delta (默认 0.65)
    leaps_target_tenor_days = Column(Integer, nullable=True) # 建议期限 (默认 365 天 ≈ 1 年)
    leaps_half_tp_pnl = Column(Float, nullable=True)         # 分批止盈: PnL≥该比例提醒卖半 (0=关闭, 默认 0.5)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = {'sqlite_autoincrement': True}


class AlertLog(Base):
    __tablename__ = "alert_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    alert_type = Column(String, nullable=False)
    rule_name = Column(String, nullable=False)

    triggered_at = Column(DateTime, server_default=func.now(), index=True)
    message = Column(Text, nullable=False)

    sent_successfully = Column(Boolean, default=True)
    error_message = Column(Text, nullable=True)

    # 关联的 OptionPosition id (可空): 用于"同一仓位只提醒一次"的落库级去重
    position_id = Column(Integer, nullable=True, index=True)

    # 历史 grid 周期遗留列: LEAPS 模式始终写 NULL, 保留以免触碰历史 schema
    cycle_id = Column(Integer, nullable=True)


class OptionPosition(Base):
    """LEAPS 期权仓位 (信号建议 → 用户确认买入 → 持仓监控 → 平仓)。

    生命周期: WAITING -> HOLDING -> CLOSED
              WAITING -> DISMISSED / EXPIRED (终态, 未执行)
    """
    __tablename__ = "option_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)

    status = Column(String(20), default="WAITING", nullable=False, index=True)

    # --- 信号基准 (WAITING 时生成, 不可变) ---
    signal_base_price = Column(Float, nullable=False)   # 触发日 QQQ 收盘价 (加仓判断基准)
    signal_rsi = Column(Float, nullable=True)           # 触发时 RSI
    signal_bar_date = Column(String(30), nullable=True) # 判定基准 bar (收盘确认制)
    suggested_delta = Column(Float, nullable=True)      # 建议 Delta (0.65)
    suggested_tenor_days = Column(Integer, nullable=True)  # 建议期限 (交易日)

    # --- 用户录入 (确认买入时写入, 此后仅 quantity/total_cost 随加仓变化) ---
    strike = Column(Float, nullable=True)
    expiration_date = Column(Date, nullable=True)
    entry_price = Column(Float, nullable=True)          # 首张每张权利金
    quantity = Column(Integer, nullable=True)           # 当前总张数
    total_cost = Column(Float, nullable=True)           # 累计已投入权利金 (加仓累加)
    entry_date = Column(Date, nullable=True)
    add_count = Column(Integer, default=0, nullable=False)  # 已加仓次数 (0-2)

    # --- 跟踪与归档 ---
    current_premium = Column(Float, nullable=True)      # 最新每张权利金 (yfinance 期权链, 尽力而为)
    premium_updated_at = Column(DateTime, nullable=True)
    max_pnl_pct = Column(Float, default=0.0)            # 持仓期间最高 PnL% (展示)
    realized_premium = Column(Float, nullable=True)     # 部分平仓累计已卖出权利金
    half_tp_alerted = Column(Boolean, default=False, nullable=False)  # 分批止盈提醒已发
    closed_at = Column(DateTime, nullable=True)
    close_reason = Column(String(100), nullable=True)   # RSI_TP / TIME_STOP / DTE_FORCE / MANUAL_CLOSE
    close_premium = Column(Float, nullable=True)        # 平仓总权利金 (用户录入, 仅记录)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
