from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import os

from .models import Base

DATABASE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "qqq_alert.db")
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

# 默认连接池: 每个 session 独立连接, 事务互不交错。
# 不能使用 StaticPool (全局共享单连接): 一个 session 的 commit/rollback
# 会连带影响其他 session 的未提交事务, 破坏状态机原子性。
# check_same_thread=False 仍然需要: session 可能跨线程创建与使用
# (scheduler 线程 / FastAPI worker 线程), SQLite 交叉线程复用连接由
# 文件锁 + busy timeout 串行化, 写入冲突概率极低 (低流量管理系统)。
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _apply_lightweight_migrations()


def _apply_lightweight_migrations():
    """
    轻量列迁移: create_all 不会给已有表加新列。
    使用 ADD COLUMN IF NOT EXISTS 语义, 幂等可重复执行。

    历史 grid 相关列 (alert_logs.cycle_id 等) 保留不删, 只是代码不再写入;
    新增列: configuration 的 LEAPS 策略参数 (NULL = 用 env/默认值)。
    """
    migrations = [
        "ALTER TABLE configuration ADD COLUMN leaps_tp_rsi FLOAT",
        "ALTER TABLE configuration ADD COLUMN leaps_time_stop_trading_days INTEGER",
        "ALTER TABLE configuration ADD COLUMN leaps_dte_force_days INTEGER",
        "ALTER TABLE configuration ADD COLUMN leaps_add_levels VARCHAR(50)",
        "ALTER TABLE configuration ADD COLUMN leaps_max_quantity INTEGER",
        "ALTER TABLE configuration ADD COLUMN leaps_target_delta FLOAT",
        "ALTER TABLE configuration ADD COLUMN leaps_target_tenor_days INTEGER",
        "ALTER TABLE configuration ADD COLUMN leaps_half_tp_pnl FLOAT",
        "ALTER TABLE option_positions ADD COLUMN realized_premium FLOAT",
        "ALTER TABLE option_positions ADD COLUMN half_tp_alerted BOOLEAN DEFAULT 0",
        "ALTER TABLE alert_logs ADD COLUMN position_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_alert_logs_position_id ON alert_logs (position_id)",
        "CREATE INDEX IF NOT EXISTS ix_alert_logs_type_time ON alert_logs (alert_type, triggered_at)",
        # option_positions 历史遗留表 (早期 LEAPS 应用创建过): 补齐新 schema 列
        "ALTER TABLE option_positions ADD COLUMN status VARCHAR(20)",
        "ALTER TABLE option_positions ADD COLUMN strike FLOAT",
        "ALTER TABLE option_positions ADD COLUMN signal_base_price FLOAT",
        "ALTER TABLE option_positions ADD COLUMN signal_rsi FLOAT",
        "ALTER TABLE option_positions ADD COLUMN signal_bar_date VARCHAR(30)",
        "ALTER TABLE option_positions ADD COLUMN suggested_delta FLOAT",
        "ALTER TABLE option_positions ADD COLUMN suggested_tenor_days INTEGER",
        "ALTER TABLE option_positions ADD COLUMN total_cost FLOAT",
        "ALTER TABLE option_positions ADD COLUMN add_count INTEGER",
        "ALTER TABLE option_positions ADD COLUMN current_premium FLOAT",
        "ALTER TABLE option_positions ADD COLUMN premium_updated_at TIMESTAMP",
        "ALTER TABLE option_positions ADD COLUMN max_pnl_pct FLOAT",
        "ALTER TABLE option_positions ADD COLUMN closed_at TIMESTAMP",
        "ALTER TABLE option_positions ADD COLUMN close_reason VARCHAR(100)",
        "ALTER TABLE option_positions ADD COLUMN close_premium FLOAT",
        "ALTER TABLE option_positions ADD COLUMN notes TEXT",
        "ALTER TABLE option_positions ADD COLUMN created_at TIMESTAMP",
        "ALTER TABLE option_positions ADD COLUMN updated_at TIMESTAMP",
        "UPDATE option_positions SET status = 'CLOSED', closed_at = CURRENT_TIMESTAMP WHERE status IS NULL",
    ]
    with engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                # 列已存在 (sqlite OperationalError duplicate column) -> 幂等跳过
                pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
