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

    qqq_rule_a_enabled = Column(Boolean, default=True)
    qqq_rule_b_enabled = Column(Boolean, default=True)
    qqq_rule_c_enabled = Column(Boolean, default=True)
    qqq_rule_d_enabled = Column(Boolean, default=True)

    max_holding_days = Column(Integer, default=270)

    take_profit_phase1_threshold = Column(Float, default=0.50)
    take_profit_phase1_days = Column(Integer, default=120)
    take_profit_phase2_threshold = Column(Float, default=0.30)
    take_profit_phase2_days = Column(Integer, default=180)
    take_profit_phase3_threshold = Column(Float, default=0.10)

    stop_loss_threshold = Column(Float, default=0.30)
    dte_warning_days = Column(Integer, default=45)

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
