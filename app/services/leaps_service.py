"""LEAPS 服务层: OptionPosition 状态机 + 信号闸门 + 聚合视图。

生命周期: WAITING -> HOLDING -> CLOSED
          WAITING -> DISMISSED / EXPIRED (未执行终态)

闸门规则 (与旧网格状态机一致的业务约束):
  - 同一时刻最多一个 WAITING 或一个 HOLDING 仓位
  - WAITING 超过 TTL (交易日) 自动 EXPIRED, 释放闸门
"""
import logging
from datetime import datetime, date
from typing import Any, Dict, Optional
from pytz import timezone
from sqlalchemy.orm import Session

from app.database.models import OptionPosition

et_tz = timezone("America/New_York")
logger = logging.getLogger(__name__)


class OptionPositionNotFoundError(ValueError):
    """指定的 OptionPosition 不存在 (API 映射 404)。"""


class OptionPositionStateError(ValueError):
    """非法状态迁移 (API 映射 409)。"""


class OptionParameterError(ValueError):
    """参数校验失败 (API 映射 400)。"""


def get_current_time() -> datetime:
    return datetime.now(et_tz)


# ---------------------------------------------------------------------------
# 查询 helpers
# ---------------------------------------------------------------------------

def get_waiting_position(db: Session) -> Optional[OptionPosition]:
    return db.query(OptionPosition).filter(OptionPosition.status == "WAITING").first()


def get_holding_position(db: Session) -> Optional[OptionPosition]:
    return db.query(OptionPosition).filter(OptionPosition.status == "HOLDING").first()


def get_position_history(db: Session, limit: int = 20) -> Dict[str, Any]:
    rows = (
        db.query(OptionPosition)
        .order_by(OptionPosition.id.desc())
        .limit(max(1, min(int(limit or 20), 100)))
        .all()
    )
    return {"positions": rows}


def has_active_position(db: Session) -> bool:
    return get_waiting_position(db) is not None or get_holding_position(db) is not None


# ---------------------------------------------------------------------------
# 状态迁移
# ---------------------------------------------------------------------------

def create_waiting_position(db: Session, signal: Dict[str, Any],
                            config: Any) -> OptionPosition:
    """
    入场信号触发后创建 WAITING 建议记录 (含建议合约参数)。

    signal: qqq_rules.check_entry_signal 的返回值。
    """
    if has_active_position(db):
        raise OptionPositionStateError("Cannot create WAITING position: an active position (WAITING/HOLDING) already exists.")

    rec = OptionPosition(
        status="WAITING",
        signal_base_price=float(signal["signal_base_price"]),
        signal_rsi=float(signal["signal_rsi"]),
        signal_bar_date=str(signal.get("signal_bar_date") or ""),
        suggested_delta=float(config.get_target_delta()),
        suggested_tenor_days=int(config.get_target_tenor_days()),
        quantity=None,
        add_count=0,
        created_at=get_current_time(),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def confirm_entry(db: Session, position_id: int, strike: float, expiration: date,
                  entry_price: float, quantity: int = 1, notes: Optional[str] = None) -> OptionPosition:
    """WAITING -> HOLDING: 用户确认已买入建议合约 (录入实际参数)。"""
    rec = db.query(OptionPosition).filter(OptionPosition.id == position_id).first()
    if not rec:
        raise OptionPositionNotFoundError(f"OptionPosition {position_id} not found.")
    if rec.status != "WAITING":
        raise OptionPositionStateError(f"Illegal transition: {rec.status} -> HOLDING (must be WAITING).")

    if strike is None or strike <= 0:
        raise OptionParameterError("strike must be positive.")
    if expiration is None:
        raise OptionParameterError("expiration_date is required.")
    if entry_price is None or entry_price <= 0:
        raise OptionParameterError("entry_price must be positive.")
    if quantity is None or quantity < 1:
        raise OptionParameterError("quantity must be >= 1.")

    rec.status = "HOLDING"
    rec.strike = float(strike)
    rec.expiration_date = expiration
    rec.entry_price = float(entry_price)
    rec.quantity = int(quantity)
    rec.total_cost = float(entry_price) * int(quantity)
    rec.entry_date = date.today()
    if notes:
        rec.notes = notes
    db.commit()
    db.refresh(rec)
    return rec


def dismiss_waiting(db: Session, position_id: int, reason: str = "MANUAL_DISMISS") -> OptionPosition:
    """WAITING -> DISMISSED (用户忽略建议, 释放闸门)。"""
    rec = db.query(OptionPosition).filter(OptionPosition.id == position_id).first()
    if not rec:
        raise OptionPositionNotFoundError(f"OptionPosition {position_id} not found.")
    if rec.status != "WAITING":
        raise OptionPositionStateError(f"Illegal transition: {rec.status} -> DISMISSED (must be WAITING).")
    rec.status = "DISMISSED"
    rec.closed_at = get_current_time()
    rec.close_reason = reason
    db.commit()
    db.refresh(rec)
    return rec


def expire_waiting(db: Session, position_id: int) -> OptionPosition:
    """WAITING -> EXPIRED (TTL 超时, 释放闸门)。"""
    rec = db.query(OptionPosition).filter(OptionPosition.id == position_id).first()
    if not rec:
        raise OptionPositionNotFoundError(f"OptionPosition {position_id} not found.")
    if rec.status != "WAITING":
        raise OptionPositionStateError(f"Illegal transition: {rec.status} -> EXPIRED (must be WAITING).")
    rec.status = "EXPIRED"
    rec.closed_at = get_current_time()
    rec.close_reason = "WAITING_TTL_EXPIRED"
    db.commit()
    db.refresh(rec)
    return rec


def add_lot(db: Session, position_id: int, add_price: float, quantity: int = 1) -> OptionPosition:
    """HOLDING 加仓: 累加张数与总成本, add_count+1 (监控层已校验档位/RSI 条件)。"""
    rec = db.query(OptionPosition).filter(OptionPosition.id == position_id).first()
    if not rec:
        raise OptionPositionNotFoundError(f"OptionPosition {position_id} not found.")
    if rec.status != "HOLDING":
        raise OptionPositionStateError(f"Illegal transition: add_lot on {rec.status} (must be HOLDING).")
    if add_price is None or add_price <= 0:
        raise OptionParameterError("add_price must be positive.")

    rec.quantity = int(rec.quantity or 0) + int(quantity)
    rec.total_cost = float(rec.total_cost or 0) + float(add_price) * int(quantity)
    rec.add_count = int(rec.add_count or 0) + 1
    db.commit()
    db.refresh(rec)
    return rec


def close_position(db: Session, position_id: int, reason: str,
                   close_premium: Optional[float] = None, notes: Optional[str] = None) -> OptionPosition:
    """HOLDING -> CLOSED (RSI_TP / TIME_STOP / DTE_FORCE / MANUAL_CLOSE)。"""
    rec = db.query(OptionPosition).filter(OptionPosition.id == position_id).first()
    if not rec:
        raise OptionPositionNotFoundError(f"OptionPosition {position_id} not found.")
    if rec.status != "HOLDING":
        raise OptionPositionStateError(f"Illegal transition: close {rec.status} (must be HOLDING).")

    rec.status = "CLOSED"
    rec.closed_at = get_current_time()
    rec.close_reason = reason
    if close_premium is not None and close_premium > 0:
        rec.close_premium = float(close_premium)
    if notes:
        rec.notes = f"{rec.notes}\n{notes}" if rec.notes else notes
    db.commit()
    db.refresh(rec)
    return rec


# ---------------------------------------------------------------------------
# TTL / 聚合视图
# ---------------------------------------------------------------------------

def cfg_waiting_ttl(db: Session) -> int:
    """从运行时配置读 WAITING TTL (失败回退默认 3)。"""
    try:
        from app.config import load_config_from_db
        return int(load_config_from_db(db).get_waiting_ttl_trading_days())
    except Exception:
        return 3

def _count_trading_days_between(start_et: datetime, end_et: datetime) -> Optional[int]:
    try:
        from app.scheduler.trading_hours import count_trading_days
        return count_trading_days(start_et, end_et)
    except Exception:
        return None


def waiting_ttl_state(rec: OptionPosition, ttl_trading_days: int,
                      now_et: Optional[datetime] = None) -> Dict[str, Any]:
    """WAITING 建议的 TTL 状态 (交易日口径, 周末/节假日不消耗确认时间)。"""
    now = now_et or get_current_time()
    ttl = int(ttl_trading_days or 0)
    state: Dict[str, Any] = {
        "ttl_enabled": ttl > 0,
        "ttl_trading_days": ttl,
        "elapsed_trading_days": None,
        "remaining_trading_days": None,
        "expired": False,
    }
    if ttl <= 0 or rec is None or rec.created_at is None:
        return state

    created = rec.created_at
    if created.tzinfo is None:
        # 历史数据可能是 naive UTC (server_default=func.now()), 统一按 UTC 解释
        created = created.replace(tzinfo=timezone("UTC"))
    start_et = created.astimezone(et_tz)

    elapsed = _count_trading_days_between(start_et, now)
    if elapsed is None:
        return state

    state["elapsed_trading_days"] = elapsed
    state["remaining_trading_days"] = max(0, ttl - elapsed)
    state["expired"] = elapsed >= ttl
    return state


def get_leaps_dashboard(db: Session, qqq_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Dashboard / 日报共用的聚合视图 (纯读取, 不改状态)。"""
    waiting = get_waiting_position(db)
    holding = get_holding_position(db)
    latest = (
        db.query(OptionPosition)
        .order_by(OptionPosition.id.desc())
        .first()
    )

    def _ser(rec: Optional[OptionPosition], ttl_cfg: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        if rec is None:
            return None
        out = {
            "id": rec.id,
            "status": rec.status,
            "signal_base_price": rec.signal_base_price,
            "signal_rsi": rec.signal_rsi,
            "suggested_delta": rec.suggested_delta,
            "suggested_tenor_days": rec.suggested_tenor_days,
            "strike": rec.strike,
            "expiration_date": rec.expiration_date.isoformat() if rec.expiration_date else None,
            "entry_price": rec.entry_price,
            "quantity": rec.quantity,
            "total_cost": rec.total_cost,
            "entry_date": rec.entry_date.isoformat() if rec.entry_date else None,
            "add_count": rec.add_count,
            "current_premium": rec.current_premium,
            "close_reason": rec.close_reason,
            "created_at": str(rec.created_at)[:19] if rec.created_at else None,
        }
        if ttl_cfg is not None:
            out["ttl"] = waiting_ttl_state(rec, ttl_cfg)
        return out

    # qqq 数据缺失/部分缺失时补齐空键, 保证模板 format(qqq.xxx) 永不崩溃 (渲染 N/A/-)
    _QQQ_KEYS = (
        "ticker", "is_data_valid", "is_data_fresh", "last_price", "prev_close",
        "rsi", "ma200", "is_above_sma200_3d", "price_1y_ago", "entry_signal",
        "data_timestamp",
    )
    qqq = dict(qqq_data) if isinstance(qqq_data, dict) else {}
    for k in _QQQ_KEYS:
        qqq.setdefault(k, None)

    dashboard = {
        "qqq": qqq,
        "waiting_position": _ser(waiting, ttl_cfg=cfg_waiting_ttl(db)) if waiting else None,
        "holding_position": _ser(holding) if holding else None,
        "latest_position": _ser(latest) if latest else None,
    }

    # HOLDING 附加持仓评估摘要 (只读; 失败不阻塞 dashboard)
    if holding is not None:
        try:
            from app.alerts.qqq_rules import evaluate_position, _trading_days_held
            from app.config import load_config_from_db

            cfg = load_config_from_db(db)
            summary: Dict[str, Any] = {}
            today = date.today()
            summary["holding_days"] = _trading_days_held(holding.entry_date, today)
            if holding.expiration_date:
                summary["dte"] = (holding.expiration_date - today).days
            if qqq.get("closed_price") and cfg:
                levels = cfg.get_add_levels()
                add_count = int(holding.add_count or 0)
                if holding.quantity and holding.quantity < cfg.get_max_quantity() and add_count < len(levels):
                    base = holding.signal_base_price
                    cur = qqq["closed_price"]
                    if base:
                        next_level = levels[add_count]
                        trigger_at = base * (1 - next_level)
                        summary["next_add_level_pct"] = next_level
                        summary["next_add_trigger_price"] = trigger_at
                        summary["next_add_distance_pct"] = (cur / trigger_at - 1) * 100.0
            dashboard["holding_summary"] = summary
        except Exception as e:
            logger.debug(f"[LEAPS Service] holding summary skipped: {e}")

    return dashboard
