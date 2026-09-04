from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class Configuration(Base):
    __tablename__ = "configuration"

    id = Column(Integer, primary_key=True, default=1)

    admin_password_hash = Column(String, nullable=False)

    wechat_webhook_url = Column(String, nullable=True)

    alert_log_retention_days = Column(Integer, default=90)

    # 每日 16:30 日报模式 ('off' / 'ndx_grid')
    # 历史 'legacy' 值与 NULL / 非法值在 Config 读取层归一化为 'ndx_grid'
    daily_report_mode = Column(String(20), nullable=True, default="ndx_grid")

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = {'sqlite_autoincrement': True}


class AlertLog(Base):
    __tablename__ = "alert_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    alert_type = Column(String, nullable=False)
    rule_name = Column(String, nullable=False)

    triggered_at = Column(DateTime, server_default=func.now())
    message = Column(Text, nullable=False)

    sent_successfully = Column(Boolean, default=True)
    error_message = Column(Text, nullable=True)

    # legacy 期权仓位遗留列: NDX 始终写 NULL, 保留以免触碰历史 schema
    position_id = Column(Integer, nullable=True)


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
