"""
GridCycle Lifecycle State Machine and Manager.
Handles WAITING -> RUNNING -> CLOSED / STOPPED transitions with strict validation,
parameter freezing, and uniqueness guarantees.
"""
from typing import Optional, Dict, Any
from datetime import datetime
from pytz import timezone
from sqlalchemy.orm import Session
from app.database.models import GridCycle

et_tz = timezone("America/New_York")


def get_current_time():
    """获取当前纽约东部时间"""
    return datetime.now(et_tz)


def get_waiting_grid_cycle(db: Session) -> Optional[GridCycle]:
    """获取当前处于 WAITING 状态的网格周期 (如果有)"""
    return db.query(GridCycle).filter(GridCycle.status == "WAITING").first()


def has_waiting_grid_cycle(db: Session) -> bool:
    """系统当前是否存在 WAITING 网格周期"""
    return get_waiting_grid_cycle(db) is not None


def get_running_grid_cycle(db: Session) -> Optional[GridCycle]:
    """获取当前处于 RUNNING 状态的网格周期 (如果有)"""
    return db.query(GridCycle).filter(GridCycle.status == "RUNNING").first()


def has_running_grid_cycle(db: Session) -> bool:
    """系统当前是否存在 RUNNING 网格周期"""
    return get_running_grid_cycle(db) is not None


def get_latest_stopped_grid_cycle(db: Session) -> Optional[GridCycle]:
    """
    获取最近一个 STOPPED 状态的网格周期 (如果有)。
    仅用于 STOP_LOSS 风险观察 (跌破 Lower 后继续下跌的止损提醒), 只读, 不改状态。
    """
    return (
        db.query(GridCycle)
        .filter(GridCycle.status == "STOPPED")
        .order_by(GridCycle.id.desc())
        .first()
    )


def create_waiting_grid_cycle(
    db: Session,
    suggested_params: Dict[str, Any]
) -> GridCycle:
    """
    创建处于 WAITING 状态的网格周期。
    
    业务规则:
        1. 系统内同一时刻最多只能存在一个 WAITING 网格周期。
        2. 系统内若已有正在 RUNNING 的网格，也不应重复生成 WAITING。
        3. suggested 参数必须完整且合法。
        4. actual 参数在此阶段初始化为 None。
    """
    if has_running_grid_cycle(db):
        raise ValueError("Cannot create WAITING grid cycle: A grid cycle is already RUNNING.")

    if has_waiting_grid_cycle(db):
        raise ValueError("Cannot create WAITING grid cycle: A WAITING grid cycle already exists.")

    required_keys = ["base_price", "upper_price", "lower_price", "grid_count", "leverage"]
    for k in required_keys:
        if k not in suggested_params or suggested_params[k] is None:
            raise ValueError(f"Missing required suggested parameter: {k}")

    cycle = GridCycle(
        status="WAITING",
        suggested_base_price=float(suggested_params["base_price"]),
        suggested_upper_price=float(suggested_params["upper_price"]),
        suggested_lower_price=float(suggested_params["lower_price"]),
        suggested_grid_count=int(suggested_params["grid_count"]),
        suggested_leverage=float(suggested_params["leverage"]),
        # actual 参数显式初始化为 None
        actual_base_price=None,
        actual_upper_price=None,
        actual_lower_price=None,
        actual_grid_count=None,
        actual_leverage=None,
        actual_margin=None,
        created_at=get_current_time(),
        started_at=None,
        closed_at=None,
        close_reason=None,
        notes=None
    )

    db.add(cycle)
    db.commit()
    db.refresh(cycle)
    return cycle


def start_grid_cycle(
    db: Session,
    cycle_id: int,
    actual_base_price: float,
    actual_upper_price: float,
    actual_lower_price: float,
    actual_grid_count: int,
    actual_leverage: float,
    actual_margin: Optional[float] = None,
    notes: Optional[str] = None
) -> GridCycle:
    """
    将 WAITING 网格周期激活为 RUNNING 状态，并永久冻结实际参数。
    
    业务规则:
        1. 必须且只能从 WAITING -> RUNNING。
        2. 若系统中已经有另一个正在 RUNNING 的网格，禁止激活 (RUNNING 唯一性)。
        3. 实际参数输入完整校验 (base > 0, upper > lower, grid_count > 0, leverage > 0)。
        4. 实际参数写入后自此固化，记录 started_at。
    """
    cycle = db.query(GridCycle).filter(GridCycle.id == cycle_id).first()
    if not cycle:
        raise ValueError(f"GridCycle with id {cycle_id} not found.")

    if cycle.status != "WAITING":
        raise ValueError(f"Illegal state transition: Cannot start GridCycle in status '{cycle.status}'. Must be 'WAITING'.")

    # 检查全局 RUNNING 唯一性
    running_cycle = get_running_grid_cycle(db)
    if running_cycle and running_cycle.id != cycle.id:
        raise ValueError(f"Cannot start GridCycle: GridCycle {running_cycle.id} is already RUNNING.")

    # 实际参数校验
    if actual_base_price is None or actual_base_price <= 0:
        raise ValueError("actual_base_price must be a positive number.")
    if actual_upper_price is None or actual_lower_price is None or actual_upper_price <= actual_lower_price:
        raise ValueError(f"actual_upper_price ({actual_upper_price}) must be > actual_lower_price ({actual_lower_price}).")
    if actual_grid_count is None or not isinstance(actual_grid_count, int) or actual_grid_count <= 0:
        raise ValueError("actual_grid_count must be a positive integer.")
    if actual_leverage is None or actual_leverage <= 0:
        raise ValueError("actual_leverage must be a positive number.")
    if actual_margin is not None and actual_margin < 0:
        raise ValueError("actual_margin cannot be negative.")

    # 状态跃迁与参数冻结
    cycle.status = "RUNNING"
    cycle.actual_base_price = float(actual_base_price)
    cycle.actual_upper_price = float(actual_upper_price)
    cycle.actual_lower_price = float(actual_lower_price)
    cycle.actual_grid_count = int(actual_grid_count)
    cycle.actual_leverage = float(actual_leverage)
    cycle.actual_margin = float(actual_margin) if actual_margin is not None else None
    cycle.started_at = get_current_time()
    if notes:
        cycle.notes = notes

    db.commit()
    db.refresh(cycle)
    return cycle


def close_grid_cycle(
    db: Session,
    cycle_id: int,
    reason: str = "MANUAL_CLOSE",
    notes: Optional[str] = None
) -> GridCycle:
    """
    正常关闭网格周期 (RUNNING -> CLOSED)。
    例如到达上限 (UPPER_REACHED) 或用户手动关闭 (MANUAL_CLOSE)。
    """
    cycle = db.query(GridCycle).filter(GridCycle.id == cycle_id).first()
    if not cycle:
        raise ValueError(f"GridCycle with id {cycle_id} not found.")

    if cycle.status != "RUNNING":
        raise ValueError(f"Illegal state transition: Cannot close GridCycle in status '{cycle.status}'. Must be 'RUNNING'.")

    cycle.status = "CLOSED"
    cycle.closed_at = get_current_time()
    cycle.close_reason = reason
    if notes:
        cycle.notes = f"{cycle.notes}\n{notes}" if cycle.notes else notes

    db.commit()
    db.refresh(cycle)
    return cycle


def stop_grid_cycle(
    db: Session,
    cycle_id: int,
    reason: str = "LOWER_BREACHED",
    notes: Optional[str] = None
) -> GridCycle:
    """
    破位下限止损网格周期 (RUNNING -> STOPPED)。
    例如跌破下限 (LOWER_BREACHED)。
    """
    cycle = db.query(GridCycle).filter(GridCycle.id == cycle_id).first()
    if not cycle:
        raise ValueError(f"GridCycle with id {cycle_id} not found.")

    if cycle.status != "RUNNING":
        raise ValueError(f"Illegal state transition: Cannot stop GridCycle in status '{cycle.status}'. Must be 'RUNNING'.")

    cycle.status = "STOPPED"
    cycle.closed_at = get_current_time()
    cycle.close_reason = reason
    if notes:
        cycle.notes = f"{cycle.notes}\n{notes}" if cycle.notes else notes

    db.commit()
    db.refresh(cycle)
    return cycle


def dismiss_waiting_grid_cycle(
    db: Session,
    cycle_id: int,
    reason: str = "MANUAL_DISMISS",
    notes: Optional[str] = None
) -> GridCycle:
    """
    人工忽略一个 WAITING 周期 (WAITING -> DISMISSED, 终态)。

    背景: 历史上 WAITING 是"一旦生成就永久阻塞新信号"的状态, 若用户不打算
    在交易所开这张网格, 系统会静默哑火且无法取消。本函数提供显式出口。
    """
    cycle = db.query(GridCycle).filter(GridCycle.id == cycle_id).first()
    if not cycle:
        raise ValueError(f"GridCycle with id {cycle_id} not found.")

    if cycle.status != "WAITING":
        raise ValueError(
            f"Illegal state transition: Cannot dismiss GridCycle in status '{cycle.status}'. Must be 'WAITING'."
        )

    cycle.status = "DISMISSED"
    cycle.closed_at = get_current_time()
    cycle.close_reason = reason
    if notes:
        cycle.notes = f"{cycle.notes}\n{notes}" if cycle.notes else notes

    db.commit()
    db.refresh(cycle)
    return cycle


def expire_waiting_grid_cycle(
    db: Session,
    cycle_id: int,
    notes: Optional[str] = None
) -> GridCycle:
    """
    WAITING 周期超时自动过期 (WAITING -> EXPIRED, 终态)。

    由监控任务按 TTL (交易天数) 判定, 目的同上: 避免一条被忽略的开仓建议
    永久阻塞后续信号。
    """
    cycle = db.query(GridCycle).filter(GridCycle.id == cycle_id).first()
    if not cycle:
        raise ValueError(f"GridCycle with id {cycle_id} not found.")

    if cycle.status != "WAITING":
        raise ValueError(
            f"Illegal state transition: Cannot expire GridCycle in status '{cycle.status}'. Must be 'WAITING'."
        )

    cycle.status = "EXPIRED"
    cycle.closed_at = get_current_time()
    cycle.close_reason = "WAITING_TTL_EXPIRED"
    if notes:
        cycle.notes = f"{cycle.notes}\n{notes}" if cycle.notes else notes

    db.commit()
    db.refresh(cycle)
    return cycle


def count_trading_days_between(start_et: datetime, end_et: datetime) -> Optional[int]:
    """
    统计 (start_et.date(), end_et.date()] 之间的 NYSE 交易日数量 (不含起始日)。

    WAITING TTL 用"交易日"而非自然日: 周末/节假日不应消耗确认时间。
    日历查询失败时返回 None (调用方按"未过期"保守处理)。
    """
    try:
        from app.scheduler.trading_hours import count_trading_days
        return count_trading_days(start_et, end_et)
    except Exception:
        return None


def waiting_ttl_state(cycle: GridCycle, ttl_trading_days: int, now_et: Optional[datetime] = None) -> Dict[str, Any]:
    """
    计算 WAITING 周期的 TTL 状态 (只读, 供监控/日报/界面展示)。

    返回:
        {
          "ttl_enabled": bool,
          "ttl_trading_days": int,
          "elapsed_trading_days": int|None,
          "remaining_trading_days": int|None,
          "expired": bool,
        }
    """
    now = now_et or get_current_time()
    ttl = int(ttl_trading_days or 0)
    state: Dict[str, Any] = {
        "ttl_enabled": ttl > 0,
        "ttl_trading_days": ttl,
        "elapsed_trading_days": None,
        "remaining_trading_days": None,
        "expired": False,
    }
    if ttl <= 0 or cycle is None or cycle.created_at is None:
        return state

    created_at = cycle.created_at
    if created_at.tzinfo is None:
        # 历史数据可能是 naive UTC (server_default=func.now()), 统一按 UTC 解释
        created_at = created_at.replace(tzinfo=timezone("UTC"))
    start_et = created_at.astimezone(et_tz)

    elapsed = count_trading_days_between(start_et, now)
    if elapsed is None:
        return state

    state["elapsed_trading_days"] = elapsed
    state["remaining_trading_days"] = max(0, ttl - elapsed)
    state["expired"] = elapsed >= ttl
    return state
