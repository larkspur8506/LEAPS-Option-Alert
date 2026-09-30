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
import functools
import logging
import math
import threading
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.alerts.grid_cycle import (
    get_running_grid_cycle,
    get_waiting_grid_cycle,
    start_grid_cycle,
    close_grid_cycle,
    dismiss_waiting_grid_cycle,
    waiting_ttl_state,
)
from app.alerts.grid_math import calculate_theoretical_grid_position
from app.alerts.ndx_rules import check_ndx_entry_conditions
from app.alerts.grid_monitor import _is_data_fresh
from app.config import load_config_from_db, get_config
from app.database.models import GridCycle

logger = logging.getLogger(__name__)

# 状态迁移串行化锁。
# 场景: 双击 / HTTP 重试 / 两个线程同时对同一 cycle 发起 start, 会让
# "检查是否已有 RUNNING" 与 "写入 RUNNING" 之间出现竞争窗口, 轻则状态不一致,
# 重则出现两个 RUNNING 周期 (SQLite 单连接下还会抛
# "cannot start a transaction within a transaction")。
# 本应用按 --workers 1 单进程多线程部署, 进程内锁即可覆盖; 多进程/多副本场景
# 需要把互斥下沉到数据库 (唯一索引或事务级锁)。
_STATE_LOCK = threading.RLock()


def _serialized(func):
    """将状态迁移函数串行化执行 (进程内 RLock)。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _STATE_LOCK:
            return func(*args, **kwargs)

    return wrapper


MANUAL_CLOSE_REASON = "MANUAL_CLOSE"
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200


class GridCycleNotFoundError(Exception):
    """指定的 GridCycle 不存在"""


class GridCycleStateError(Exception):
    """非法状态转换 / 唯一性冲突 (映射为 HTTP 409)"""


class GridParameterError(Exception):
    """实际参数校验失败 (映射为 HTTP 400)"""


class GridCycleLifecycleError(Exception):
    """WAITING 周期的生命周期操作失败 (忽略/过期, 映射为 HTTP 409)"""


def _serialize_dt(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _grid_step(upper: Optional[float], lower: Optional[float], count: Optional[int]) -> Optional[float]:
    """由 Phase 1 网格定义派生 grid step = (upper - lower) / count (仅展示用)"""
    if upper is None or lower is None or not count or count <= 0:
        return None
    try:
        return (float(upper) - float(lower)) / float(count)
    except (TypeError, ValueError):
        return None


def serialize_cycle(cycle: GridCycle) -> Dict[str, Any]:
    """将 GridCycle ORM 对象序列化为完整 JSON 结构 (suggested_* 与 actual_* 完全分离)

    额外派生只读展示字段 suggested_grid_step / actual_grid_step,
    用于 UI 显示; 不写库, 不参与状态机。
    """
    return {
        "id": cycle.id,
        "status": cycle.status,
        "suggested_base_price": cycle.suggested_base_price,
        "suggested_upper_price": cycle.suggested_upper_price,
        "suggested_lower_price": cycle.suggested_lower_price,
        "suggested_grid_count": cycle.suggested_grid_count,
        "suggested_leverage": cycle.suggested_leverage,
        "suggested_grid_step": _grid_step(
            cycle.suggested_upper_price, cycle.suggested_lower_price, cycle.suggested_grid_count
        ),
        "actual_base_price": cycle.actual_base_price,
        "actual_upper_price": cycle.actual_upper_price,
        "actual_lower_price": cycle.actual_lower_price,
        "actual_grid_count": cycle.actual_grid_count,
        "actual_leverage": cycle.actual_leverage,
        "actual_margin": cycle.actual_margin,
        "actual_grid_step": _grid_step(
            cycle.actual_upper_price, cycle.actual_lower_price, cycle.actual_grid_count
        ),
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


@_serialized
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


@_serialized
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


def get_waiting_ttl_state(db: Session, cycle_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    查询 WAITING 周期的 TTL 状态 (只读)。cycle_id 为空时取当前 WAITING 周期。

    返回 None 表示当前没有 WAITING 周期。
    """
    cycle = None
    if cycle_id is not None:
        cycle = db.query(GridCycle).filter(GridCycle.id == cycle_id).first()
    else:
        cycle = get_waiting_grid_cycle(db)
    if cycle is None:
        return None
    try:
        ttl_days = load_config_from_db(db).get_waiting_ttl_trading_days()
    except Exception:
        ttl_days = 0
    return waiting_ttl_state(cycle, ttl_days)


@_serialized
def dismiss_waiting_cycle(db: Session, cycle_id: int, reason: str = "MANUAL_DISMISS") -> Dict[str, Any]:
    """
    WAITING -> DISMISSED: 人工忽略这条开仓建议 (终态, 释放信号闸门)。

    只有 WAITING 状态允许忽略; RUNNING/CLOSED/STOPPED/EXPIRED 均拒绝 (409)。
    """
    cycle = db.query(GridCycle).filter(GridCycle.id == cycle_id).first()
    if not cycle:
        raise GridCycleNotFoundError(f"GridCycle {cycle_id} not found.")

    if cycle.status != "WAITING":
        raise GridCycleStateError(
            f"Cannot dismiss GridCycle {cycle_id}: status is '{cycle.status}', must be 'WAITING'."
        )

    try:
        dismissed = dismiss_waiting_grid_cycle(db, cycle_id, reason=reason)
    except ValueError as e:
        raise GridCycleStateError(str(e))

    logger.info(f"[Grid Service] Cycle {cycle_id} DISMISSED (reason={reason})")
    return serialize_cycle(dismissed)


def get_risk_snapshot(config: Any = None, cycle: Optional[GridCycle] = None,
                      ndx_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    风险/成本速算 (只读展示, 不参与任何状态机判定)。

    基于当前策略默认参数 (或传入的 RUNNING 周期实际参数) 计算:
      - grid_step_pct: 每格价格幅度 (%)
      - round_trip_fee_pct: 一次买入+卖出的手续费 (%)
      - net_edge_pct_per_grid: 每格毛利扣除手续费后的净幅度 (%)
      - fee_ratio_of_step: 手续费占每格毛利的比例 (100% = 白干)
      - avg_entry_at_full_load / loss_at_lower_pct_of_notional: 满仓均价与打到下轨的浮亏
      - margin_loss_at_lower_pct: 上述浮亏折算到保证金 (乘杠杆)
      - est_liquidation_price / distance_to_liquidation_pct: 估算强平价与距离
      - funding_cost_pct_per_day / funding_cost_pct_30d: 资金费拖累

    所有数字均为**估算**(未计滑点、资金费随行情浮动、交易所强平规则差异)。
    """
    cfg = config if config is not None else get_config()

    def _num(getter_name: str, default: float) -> float:
        try:
            value = getattr(cfg, getter_name)()
            return float(value)
        except Exception:
            return default

    if cycle is not None:
        base = cycle.actual_base_price
        upper = cycle.actual_upper_price
        lower = cycle.actual_lower_price
        count = cycle.actual_grid_count
        leverage = cycle.actual_leverage
        basis = "ACTUAL_PARAMS"
    else:
        base = (ndx_data or {}).get("last_price")
        upper_pct = _num("get_default_grid_upper_pct", 0.20)
        lower_pct = _num("get_default_grid_lower_pct", 0.20)
        count = _num("get_default_grid_count", 200)
        leverage = _num("get_default_grid_leverage", 5.0)
        upper = float(base) * (1 + upper_pct) if base else None
        lower = float(base) * (1 - lower_pct) if base else None
        basis = "SUGGESTED_PARAMS"

    maker_fee = _num("get_grid_maker_fee_pct", 0.0)
    taker_fee = _num("get_grid_taker_fee_pct", 0.0)
    funding_8h = _num("get_funding_rate_pct_8h", 0.0)
    fee_pct = max(maker_fee, taker_fee)

    snapshot: Dict[str, Any] = {
        "basis": basis,
        "base_price": base,
        "upper_price": upper,
        "lower_price": lower,
        "grid_count": count,
        "leverage": leverage,
        "fee_pct_per_side": fee_pct,
        "funding_rate_pct_8h": funding_8h,
        "note": "ESTIMATE ONLY - 未计滑点/资金费浮动/交易所强平规则差异",
    }

    try:
        step = (float(upper) - float(lower)) / float(count)
        base_price = float(base) if base else float((float(upper) + float(lower)) / 2)
        step_pct = step / base_price * 100.0
        round_trip = fee_pct * 2.0
        snapshot.update({
            "grid_step": round(step, 4),
            "grid_step_pct": round(step_pct, 4),
            "round_trip_fee_pct": round(round_trip, 4),
            "net_edge_pct_per_grid": round(step_pct - round_trip, 4),
            "fee_ratio_of_step_pct": round(round_trip / step_pct * 100.0, 1) if step_pct > 0 else None,
        })

        avg_entry = (float(lower) + base_price) / 2.0
        loss_notional = (float(lower) - avg_entry) / avg_entry * 100.0
        snapshot.update({
            "avg_entry_at_full_load": round(avg_entry, 2),
            "loss_at_lower_pct_of_notional": round(loss_notional, 2),
            "margin_loss_at_lower_pct": round(loss_notional * float(leverage), 2),
        })

        lev = float(leverage) if leverage else 0.0
        if lev > 0:
            liq = avg_entry * (1.0 - 1.0 / lev)
            snapshot.update({
                "est_liquidation_price": round(liq, 2),
                "distance_to_liquidation_pct": round((liq - avg_entry) / avg_entry * 100.0, 2),
                "lower_vs_liquidation_pct": round((float(lower) - liq) / liq * 100.0, 2),
            })

        if funding_8h:
            snapshot.update({
                "funding_cost_pct_per_day": round(funding_8h * 3.0, 4),
                "funding_cost_pct_30d": round(funding_8h * 3.0 * 30.0, 3),
            })
    except (TypeError, ValueError, ZeroDivisionError):
        snapshot["error"] = "INSUFFICIENT_PARAMS"

    return snapshot


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
        "price_1y_ago": None,
        "is_data_fresh": False,
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
            "price_1y_ago": ndx_data.get("price_1y_ago"),
            # 收盘确认制展示字段 (开仓判定实际使用的基准)
            "closed_bar_date": ndx_data.get("closed_bar_date"),
            "closed_rsi": ndx_data.get("closed_rsi"),
            "closed_price": ndx_data.get("closed_price"),
            "closed_indicators_available": bool(ndx_data.get("closed_indicators_available")),
            "live_bar_is_provisional": bool(ndx_data.get("live_bar_is_provisional")),
        })
        if is_valid:
            try:
                ndx["entry_signal"] = check_ndx_entry_conditions(price, ndx_data)
            except Exception:
                ndx["entry_signal"] = None
        # 数据新鲜度判定复用 Phase 3 freshness (显示用途)
        if "is_data_fresh" in ndx_data:
            ndx["is_data_fresh"] = bool(ndx_data["is_data_fresh"])
        else:
            try:
                ndx["is_data_fresh"] = bool(_is_data_fresh(ndx_data))
            except Exception:
                ndx["is_data_fresh"] = False

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

    no_active_info: Optional[Dict[str, str]] = None
    if not running and not waiting:
        if not ndx.get("is_data_valid") or ndx.get("last_price") is None:
            no_active_info = {
                "reason": "DATA_UNAVAILABLE",
                "detail": "当前 NDX 数据不可用，暂时无法评估 Entry Signal。系统将在获得有效数据后自动重新检查。",
            }
        elif not ndx.get("is_data_fresh"):
            no_active_info = {
                "reason": "DATA_STALE",
                "detail": "NDX 当前数据不是最新交易日数据，暂不评估 Entry Signal。请等待下一个美股交易日获得最新数据。",
            }
        else:
            no_active_info = {
                "reason": "SIGNAL_NOT_MET",
                "detail": "当前 NDX Entry Signal 未满足，因此系统尚未创建 WAITING 周期。",
            }

    # WAITING 的 TTL 状态 (只读展示): 剩余多少个交易日会被自动过期
    waiting_ttl: Optional[Dict[str, Any]] = None
    if waiting is not None:
        try:
            waiting_ttl = waiting_ttl_state(
                waiting, load_config_from_db(db).get_waiting_ttl_trading_days()
            )
        except Exception:
            waiting_ttl = None

    # 风险/成本速算 (只读展示, 不参与状态机)
    risk_snapshot: Optional[Dict[str, Any]] = None
    try:
        risk_snapshot = get_risk_snapshot(load_config_from_db(db), cycle=running, ndx_data=ndx_data)
    except Exception:
        risk_snapshot = None

    return {
        "ndx": ndx,
        "running_cycle": serialize_cycle(running) if running else None,
        "waiting_cycle": serialize_cycle(waiting) if waiting else None,
        "waiting_ttl": waiting_ttl,
        "theoretical_grid_position": theoretical,
        "risk_snapshot": risk_snapshot,
        "no_active_info": no_active_info,
    }
