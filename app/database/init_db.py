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
    轻量列迁移 (Phase 6): create_all 不会给已有表加新列。
    使用 ADD COLUMN IF NOT EXISTS 语义, 幂等可重复执行。
    """
    migrations = [
        "ALTER TABLE configuration ADD COLUMN daily_report_mode VARCHAR(20)",
        "ALTER TABLE alert_logs ADD COLUMN cycle_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_alert_logs_cycle_id ON alert_logs (cycle_id)",
        "CREATE INDEX IF NOT EXISTS ix_alert_logs_type_time ON alert_logs (alert_type, triggered_at)",
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
