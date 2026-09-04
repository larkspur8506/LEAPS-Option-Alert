"""
GridCycle Management Service Layer (Phase 4A).

Provides the backend logic for the GridCycle admin API:
- Read current WAITING / RUNNING cycle
- WAITING -> RUNNING with strict actual-parameter validation (no auto-correction)
- RUNNING -> CLOSED (manual close only)
- Cycle history query (latest first, clamped limit)
- Dashboard data aggregation (reuses Phase 1-3 functions, no second computation logic)

State transitions are delegated to the Phase 2 state machine
(app/alerts/grid_cycle.py). This layer only maps outcomes to
domain errors; it never duplicates the state machine.
"""
import logging
import math
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.alerts.grid_cycle import (
    get_running_grid_cycle,
    get_waiting_grid_cycle,
    start_grid_cycle,
    close_grid_cycle,
)
from app.alerts.grid_math import calculate_theoretical_grid_position
from app.alerts.ndx_rules import check_ndx_entry_conditions
from app.database.models import GridCycle

logger = logging.getLogger(__name__)

MANUAL_CLOSE_REASON = "MANUAL_CLOSE"
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200


class GridCycleNotFoundError(Exception):
    """指定的 GridCycle 不存在"""


class GridCycleStateError(Exception):
    """非法状态转换 / 唯一性冲突 (映射为 HTTP 409)"""


class GridParameterError(Exception):
    """实际参数校验失败 (映射为 HTTP 400)"""


def _serialize_dt(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def serialize_cycle(cycle: GridCycle) -> Dict[str, Any]:
    """将 GridCycle ORM 对象序列化为完整 JSON 结构 (suggested_* 与 actual_* 完全分离)"""
    return {
        "id": cycle.id,
        "status": cycle.status,
        "suggested_base_price": cycle.suggested_base_price,
        "suggested_upper_price": cycle.suggested_upper_price,
        "suggested_lower_price": cycle.suggested_lower_price,
        "suggested_grid_count": cycle.suggested_grid_count,
        "suggested_leverage": cycle.suggested_leverage,
        "actual_base_price": cycle.actual_base_price,
        "actual_upper_price": cycle.actual_upper_price,
        "actual_lower_price": cycle.actual_lower_price,
        "actual_grid_count": cycle.actual_grid_count,
        "actual_leverage": cycle.actual_leverage,
        "actual_margin": cycle.actual_margin,
        "created_at": _serialize_dt(cycle.created_at),
        "started_at": _serialize_dt(cycle.started_at),
        "closed_at": _serialize_dt(cycle.closed_at),
        "close_reason": cycle.close_reason,
        "notes": cycle.notes,
    }


def get_waiting_cycle(db: Session) -> Optional[Dict[str, Any]]:
    """获取当前 WAITING 周期; 不存在返回 None (不自动生成新 WAITING)"""
    cycle = get_waiting_grid_cycle(db)
    return serialize_cycle(cycle) if cycle else None


def get_running_cycle(db: Session) -> Optional[Dict[str, Any]]:
    """获取当前 RUNNING 周期; 不存在返回 None"""
    cycle = get_running_grid_cycle(db)
    return serialize_cycle(cycle) if cycle else None


def get_cycle_history(db: Session, limit: Any = DEFAULT_HISTORY_LIMIT) -> Dict[str, Any]:
    """
    查询 GridCycle 历史 (最新优先)。

    limit 钳制在 [1, MAX_HISTORY_LIMIT]，防止 limit=100000000 之类的滥用。
    """
    try:
        limit_val = int(limit)
    except (TypeError, ValueError):
        limit_val = DEFAULT_HISTORY_LIMIT
    limit_val = max(1, min(limit_val, MAX_HISTORY_LIMIT))

    cycles = (
        db.query(GridCycle)
        .order_by(GridCycle.id.desc())
        .limit(limit_val)
        .all()
    )
    return {
        "cycles": [serialize_cycle(c) for c in cycles],
        "count": len(cycles),
        "limit": limit_val,
    }


def validate_actual_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    校验用户录入的实际参数 (Phase 4A 规则)。

    规则:
        actual_base_price   > 0
        actual_upper_price  > actual_base_price
        actual_lower_price  < actual_base_price 且 > 0
        actual_grid_count   >= 1 (整数)
        actual_leverage     > 0 (不强制等于建议值 5.0)
        actual_margin       > 0

    所有数值必须是有限数 (拒绝 NaN / Inf)。
    校验通过后原样返回 (不做任何自动修正/取整)，等待写入状态机。
    """
    def _finite_number(key: str) -> float:
        value = params.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GridParameterError(f"Invalid actual param '{key}': must be a number.")
        value = float(value)
        if not math.isfinite(value):
            raise GridParameterError(f"Invalid actual param '{key}': must be a finite number.")
        return value

    base = _finite_number("actual_base_price")
    upper = _finite_number("actual_upper_price")
    lower = _finite_number("actual_lower_price")
    leverage = _finite_number("actual_leverage")
    margin = _finite_number("actual_margin")

    grid_count = params.get("actual_grid_count")
    if isinstance(grid_count, bool) or not isinstance(grid_count, int):
        raise GridParameterError("Invalid actual param 'actual_grid_count': must be an integer.")

    if base <= 0:
        raise GridParameterError("actual_base_price must be > 0.")
    if upper <= base:
        raise GridParameterError("actual_upper_price must be > actual_base_price.")
    if lower >= base:
        raise GridParameterError("actual_lower_price must be < actual_base_price.")
    if lower <= 0:
        raise GridParameterError("actual_lower_price must be > 0.")
    if grid_count < 1:
        raise GridParameterError("actual_grid_count must be >= 1.")
    if leverage <= 0:
        raise GridParameterError("actual_leverage must be > 0.")
    if margin <= 0:
        raise GridParameterError("actual_margin must be > 0.")

    return {
        "actual_base_price": base,
        "actual_upper_price": upper,
        "actual_lower_price": lower,
        "actual_grid_count": int(grid_count),
        "actual_leverage": leverage,
        "actual_margin": margin,
    }


def start_cycle(db: Session, cycle_id: int, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    WAITING -> RUNNING (用户确认已在交易所实际开网格)。

    防护:
        1. GridCycle 必须存在
        2. 必须处于 WAITING 状态 (RUNNING/CLOSED/STOPPED 再 start 均拒绝)
        3. 系统中不能已存在另一个 RUNNING 周期
        4. 实际参数必须通过 validate_actual_params 校验
        5. 最终仍委托 Phase 2 start_grid_cycle 执行状态转换 (双重防线)
    """
    cycle = db.query(GridCycle).filter(GridCycle.id == cycle_id).first()
    if not cycle:
        raise GridCycleNotFoundError(f"GridCycle {cycle_id} not found.")

    if cycle.status != "WAITING":
        raise GridCycleStateError(
            f"Cannot start GridCycle {cycle_id}: status is '{cycle.status}', must be 'WAITING'."
        )

    running = get_running_grid_cycle(db)
    if running and running.id != cycle.id:
        raise GridCycleStateError(
            f"Cannot start GridCycle {cycle_id}: GridCycle {running.id} is already RUNNING."
        )

    validated = validate_actual_params(params)

    try:
        started = start_grid_cycle(db, cycle_id, **validated)
    except ValueError as e:
        # 并发竞争下的兜底: 状态机在写入前最终校验失败 (如另一线程已先启动)
        raise GridCycleStateError(str(e))
    logger.info(
        f"[Grid Service] Cycle {cycle_id} STARTED: "
        f"base={started.actual_base_price}, upper={started.actual_upper_price}, "
        f"lower={started.actual_lower_price}, count={started.actual_grid_count}, "
        f"leverage={started.actual_leverage}, margin={started.actual_margin}"
    )
    return serialize_cycle(started)


def close_running_cycle(db: Session, cycle_id: int) -> Dict[str, Any]:
    """
    RUNNING -> CLOSED / MANUAL_CLOSE (记录用户已手动结束网格)。

    不操作交易所，仅记录状态。close_reason 固定为 MANUAL_CLOSE。
    只有 RUNNING 状态允许关闭 (WAITING/CLOSED/STOPPED 均拒绝)。
    """
    cycle = db.query(GridCycle).filter(GridCycle.id == cycle_id).first()
    if not cycle:
        raise GridCycleNotFoundError(f"GridCycle {cycle_id} not found.")

    if cycle.status != "RUNNING":
        raise GridCycleStateError(
            f"Cannot close GridCycle {cycle_id}: status is '{cycle.status}', must be 'RUNNING'."
        )

    try:
        closed = close_grid_cycle(db, cycle_id, reason=MANUAL_CLOSE_REASON)
    except ValueError as e:
        # 并发竞争下的兜底 (如另一请求已先关闭)
        raise GridCycleStateError(str(e))
    logger.info(
        f"[Grid Service] Cycle {cycle_id} MANUALLY CLOSED at {closed.closed_at}"
    )
    return serialize_cycle(closed)


def get_grid_dashboard(
    db: Session,
    ndx_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    汇总 dashboard JSON 数据。

    复用 Phase 1-3 已有函数:
        - get_waiting_grid_cycle / get_running_grid_cycle (Phase 2)
        - check_ndx_entry_conditions (Phase 1, 只读判定, 不创建 WAITING)
        - calculate_theoretical_grid_position (Phase 1, 基于 actual 参数)

    无 RUNNING 时 theoretical_grid_position / actual 相关字段为 null。
    全部理论值均为 THEORETICAL, 不代表真实持仓/成交/PnL。
    """
    running = get_running_grid_cycle(db)
    waiting = get_waiting_grid_cycle(db)

    ndx: Dict[str, Any] = {
        "last_price": None,
        "rsi": None,
        "ma200": None,
        "entry_signal": None,
        "is_data_valid": False,
        "data_timestamp": None,
    }

    if ndx_data and ndx_data.get("last_price"):
        price = ndx_data["last_price"]
        is_valid = bool(ndx_data.get("is_data_valid", False))
        ndx.update({
            "last_price": price,
            "rsi": ndx_data.get("rsi"),
            "ma200": ndx_data.get("ma200"),
            "is_data_valid": is_valid,
            "data_timestamp": ndx_data.get("data_timestamp"),
        })
        if is_valid:
            try:
                ndx["entry_signal"] = check_ndx_entry_conditions(price, ndx_data)
            except Exception:
                ndx["entry_signal"] = None

    theoretical: Optional[Dict[str, Any]] = None
    if (
        running
        and ndx["last_price"] is not None
        and running.actual_upper_price is not None
        and running.actual_lower_price is not None
        and running.actual_grid_count is not None
    ):
        try:
            pos = calculate_theoretical_grid_position(
                ndx["last_price"],
                running.actual_lower_price,
                running.actual_upper_price,
                running.actual_grid_count,
            )
            theoretical = dict(pos)
            theoretical["distance_to_upper"] = round(
                float(running.actual_upper_price) - float(ndx["last_price"]), 2
            )
            theoretical["distance_to_lower"] = round(
                float(ndx["last_price"]) - float(running.actual_lower_price), 2
            )
            theoretical["basis"] = "ACTUAL_PARAMS"
            theoretical["note"] = "THEORETICAL ONLY - not real position/fill/PnL"
        except (ValueError, TypeError):
            theoretical = None

    return {
        "ndx": ndx,
        "running_cycle": serialize_cycle(running) if running else None,
        "waiting_cycle": serialize_cycle(waiting) if waiting else None,
        "theoretical_grid_position": theoretical,
    }
