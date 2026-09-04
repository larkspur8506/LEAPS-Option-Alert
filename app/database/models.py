from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Date, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
from datetime import date, datetime

Base = declarative_base()


class Configuration(Base):
    __tablename__ = "configuration"

    id = Column(Integer, primary_key=True, default=1)

    admin_password_hash = Column(String, nullable=False)

    polygon_api_key = Column(String, nullable=True)
    wechat_webhook_url = Column(String, nullable=True)

    # 新版 QQQ 入场规则 (Level 1/2/3)
    entry_level1_enabled = Column(Boolean, default=True)  # 日常回调
    entry_level2_enabled = Column(Boolean, default=True)  # 黄金坑
    entry_level3_enabled = Column(Boolean, default=True)  # 极端超卖

    # 新版期权出场规则
    exit_hard_tp_enabled = Column(Boolean, default=True)      # 硬性止盈 50%
    exit_fast_tp_enabled = Column(Boolean, default=True)      # 极速止盈
    exit_trailing_tp_enabled = Column(Boolean, default=True)  # 移动止盈
    exit_tech_tp_enabled = Column(Boolean, default=True)      # 技术止盈 (RSI/BB)
    exit_dte_warning_enabled = Column(Boolean, default=True)  # DTE 移仓窗口
    exit_dte_force_enabled = Column(Boolean, default=True)    # DTE 强制清仓
    exit_trend_stop_enabled = Column(Boolean, default=True)   # 趋势崩坏止损

    alert_log_retention_days = Column(Integer, default=90)
    daily_qqq_data_retention_days = Column(Integer, default=30)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = {'sqlite_autoincrement': True}


class OptionPosition(Base):
    __tablename__ = "option_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)

    underlying = Column(String, default="QQQ", nullable=False)
    option_type = Column(String, nullable=False)

    strike_price = Column(Float, nullable=False)
    expiration_date = Column(Date, nullable=False)

    entry_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=True)
    entry_date = Column(Date, nullable=False)

    current_price = Column(Float, nullable=True)
    last_price_update = Column(DateTime, nullable=True)
    max_profit = Column(Float, default=0.0)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AlertLog(Base):
    __tablename__ = "alert_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    alert_type = Column(String, nullable=False)
    rule_name = Column(String, nullable=False)

    triggered_at = Column(DateTime, server_default=func.now())
    message = Column(Text, nullable=False)

    sent_successfully = Column(Boolean, default=True)
    error_message = Column(Text, nullable=True)

    position_id = Column(Integer, nullable=True)


class DailyQQQData(Base):
    __tablename__ = "daily_qqq_data"

    id = Column(Integer, primary_key=True, autoincrement=True)

    date = Column(Date, unique=True, nullable=False)

    open_price = Column(Float, nullable=True)
    high_price = Column(Float, nullable=True)
    low_price = Column(Float, nullable=True)
    close_price = Column(Float, nullable=True)

    fetched_at = Column(DateTime, server_default=func.now())


class GridCycle(Base):
    __tablename__ = "grid_cycles"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 生命周期状态: 'WAITING', 'RUNNING', 'CLOSED', 'STOPPED'
    status = Column(String(20), default="WAITING", nullable=False, index=True)

    # --- 系统建议参数 (开仓信号触发时生成并保存) ---
    suggested_base_price = Column(Float, nullable=False)
    suggested_upper_price = Column(Float, nullable=False)
    suggested_lower_price = Column(Float, nullable=False)
    suggested_grid_count = Column(Integer, default=200, nullable=False)
    suggested_leverage = Column(Float, default=5.0, nullable=False)

    # --- 用户实际参数 (用户在交易所开仓后录入，RUNNING 启动时永久冻结) ---
    actual_base_price = Column(Float, nullable=True)
    actual_upper_price = Column(Float, nullable=True)
    actual_lower_price = Column(Float, nullable=True)
    actual_grid_count = Column(Integer, nullable=True)
    actual_leverage = Column(Float, nullable=True)
    actual_margin = Column(Float, nullable=True)

    # --- 时间戳轨迹 ---
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    started_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)

    # --- 结束原因与备注 ---
    close_reason = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
