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
