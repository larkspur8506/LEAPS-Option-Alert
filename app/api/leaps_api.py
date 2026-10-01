"""
OptionPosition Admin API (LEAPS 模式).

REST endpoints for managing OptionPosition lifecycle:
    GET  /api/leaps/status        - dashboard data (QQQ + positions)
    GET  /api/leaps/waiting       - current WAITING suggestion (or null)
    GET  /api/leaps/holding       - current HOLDING position (or null)
    GET  /api/leaps/history       - position history (latest first)
    POST /api/leaps/{id}/confirm  - WAITING -> HOLDING (录入实际合约参数)
    POST /api/leaps/{id}/add-lot  - HOLDING 加仓 (累加张数/成本)
    POST /api/leaps/{id}/close    - HOLDING -> CLOSED (平仓记录)
    POST /api/leaps/{id}/partial-close - HOLDING 部分平仓 (卖部分张数, 记 realized_premium)
    POST /api/leaps/{id}/dismiss  - WAITING -> DISMISSED

Authentication reuses the existing admin cookie mechanism
(app.main.verify_admin_cookie). No new auth system is introduced.
State transitions delegate to app/services/leaps_service.py.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.database.init_db import get_db
from app.services import leaps_service
from app.services.leaps_service import (
    OptionPositionNotFoundError,
    OptionPositionStateError,
    OptionParameterError,
)
from app.notification.wechat import (
    WeChatNotifier,
    format_leaps_exit,
    get_wechat_notifier,
)
from app.alerts.alert_log import log_alert

import logging

logger = logging.getLogger(__name__)

HTTP_CONFLICT = 409


def require_admin(request: Request) -> None:
    """复用现有 admin cookie 认证 (app.main.verify_admin_cookie)，未认证返回 401"""
    from app.main import verify_admin_cookie

    if not verify_admin_cookie(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )


router = APIRouter(prefix="/api/leaps", tags=["leaps"], dependencies=[Depends(require_admin)])


def _get_wechat_notifier() -> Optional[WeChatNotifier]:
    """惰性获取运行时配置中的 webhook (避免与 app.main 循环导入); 未配置返回 None"""
    try:
        from app.config import load_config_from_db
        from app.database.init_db import SessionLocal

        db = SessionLocal()
        try:
            config = load_config_from_db(db)
            webhook = config.get_wechat_webhook_url()
            return get_wechat_notifier(webhook) if webhook else None
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[LEAPS API] notifier init failed: {e}")
        return None


def _notify_manual_close(position) -> None:
    """手动平仓记录性通知 (不改变已提交的状态)。"""
    notifier = _get_wechat_notifier()
    if notifier is None:
        return
    try:
        breach = {"type": "MANUAL_CLOSE", "reason": "后台手动平仓"}
        message = format_leaps_exit(position, breach, {})
        notifier.send_message(message)
    except Exception as e:
        logger.warning(f"[LEAPS API] manual close notify failed: {e}")


# ------------------------------------------------------------------
# 请求模型
# ------------------------------------------------------------------

class ConfirmEntryRequest(BaseModel):
    strike: float
    expiration_date: str          # YYYY-MM-DD
    entry_price: float            # 每张权利金
    quantity: int = 1
    notes: Optional[str] = None

    @field_validator("expiration_date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError as e:
            raise ValueError("expiration_date 必须为 YYYY-MM-DD") from e
        return v


class AddLotRequest(BaseModel):
    add_price: float              # 本次加仓每张权利金
    quantity: int = 1


class ClosePositionRequest(BaseModel):
    close_premium: Optional[float] = None   # 平仓每张权利金 (可选记录)
    notes: Optional[str] = None


class PartialCloseRequest(BaseModel):
    sell_qty: int                    # 本次卖出张数 (必须 < 当前张数)
    sell_price: Optional[float] = None  # 每张卖出权利金 (可选记录)
    notes: Optional[str] = None


# ------------------------------------------------------------------
# 错误映射 (与 grid_api 的语义一致)
# ------------------------------------------------------------------

def _raise_http(exc: Exception):
    if isinstance(exc, OptionPositionNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, OptionPositionStateError):
        raise HTTPException(status_code=HTTP_CONFLICT, detail=str(exc))
    if isinstance(exc, OptionParameterError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise HTTPException(status_code=400, detail=str(exc))


# ------------------------------------------------------------------
# 查询端点
# ------------------------------------------------------------------

@router.get("/status")
def get_status(request: Request, db: Session = Depends(get_db)):
    from app.market.data_fetcher import DataFetcher
    try:
        qqq_data = DataFetcher().get_qqq_data()
    except Exception:
        qqq_data = None
    if isinstance(qqq_data, dict):
        from app.alerts.leaps_monitor import _is_data_fresh
        try:
            qqq_data["is_data_fresh"] = _is_data_fresh(qqq_data)
        except Exception:
            qqq_data["is_data_fresh"] = False
    return leaps_service.get_leaps_dashboard(db, qqq_data)


@router.get("/waiting")
def get_waiting(request: Request, db: Session = Depends(get_db)):
    rec = leaps_service.get_waiting_position(db)
    return {"position": leaps_service.get_leaps_dashboard(db, None)["waiting_position"] if rec else None}


@router.get("/holding")
def get_holding(request: Request, db: Session = Depends(get_db)):
    rec = leaps_service.get_holding_position(db)
    return {"position": leaps_service.get_leaps_dashboard(db, None)["holding_position"] if rec else None}


@router.get("/history")
def get_history(request: Request, limit: int = 20, db: Session = Depends(get_db)):
    rows = leaps_service.get_position_history(db, limit)["positions"]
    return {"positions": [
        {
            "id": r.id,
            "status": r.status,
            "signal_base_price": r.signal_base_price,
            "strike": r.strike,
            "expiration_date": r.expiration_date.isoformat() if r.expiration_date else None,
            "entry_price": r.entry_price,
            "quantity": r.quantity,
            "total_cost": r.total_cost,
            "add_count": r.add_count,
            "close_reason": r.close_reason,
            "close_premium": r.close_premium,
            "created_at": str(r.created_at)[:19] if r.created_at else None,
            "closed_at": str(r.closed_at)[:19] if r.closed_at else None,
        } for r in rows
    ]}


# ------------------------------------------------------------------
# 状态迁移端点
# ------------------------------------------------------------------

@router.post("/{position_id}/confirm")
def confirm_entry(position_id: int, payload: ConfirmEntryRequest,
                  request: Request, db: Session = Depends(get_db)):
    try:
        rec = leaps_service.confirm_entry(
            db, position_id,
            strike=payload.strike,
            expiration=date.fromisoformat(payload.expiration_date),
            entry_price=payload.entry_price,
            quantity=payload.quantity,
            notes=payload.notes,
        )
    except Exception as e:
        _raise_http(e)
        raise
    logger.info(f"[LEAPS API] Position #{rec.id} WAITING -> HOLDING "
                f"(strike={rec.strike}, exp={rec.expiration_date}, qty={rec.quantity})")
    return {"status": "OK", "position_id": rec.id, "position_status": rec.status}


@router.post("/{position_id}/add-lot")
def add_lot(position_id: int, payload: AddLotRequest,
            request: Request, db: Session = Depends(get_db)):
    try:
        rec = leaps_service.add_lot(db, position_id, payload.add_price, payload.quantity)
    except Exception as e:
        _raise_http(e)
        raise
    logger.info(f"[LEAPS API] Position #{rec.id} add-lot -> qty={rec.quantity}, add_count={rec.add_count}")
    return {"status": "OK", "position_id": rec.id, "quantity": rec.quantity, "add_count": rec.add_count}


@router.post("/{position_id}/close")
def close_position(position_id: int, payload: ClosePositionRequest,
                   request: Request, db: Session = Depends(get_db)):
    try:
        rec = leaps_service.close_position(
            db, position_id, reason="MANUAL_CLOSE",
            close_premium=payload.close_premium, notes=payload.notes,
        )
    except Exception as e:
        _raise_http(e)
        raise
    logger.info(f"[LEAPS API] Position #{rec.id} HOLDING -> CLOSED (MANUAL_CLOSE)")
    _notify_manual_close(rec)
    return {"status": "OK", "position_id": rec.id, "position_status": rec.status}


@router.post("/{position_id}/partial-close")
def partial_close_position(position_id: int, payload: PartialCloseRequest,
                           request: Request, db: Session = Depends(get_db)):
    try:
        rec = leaps_service.partial_close(
            db, position_id, payload.sell_qty, payload.sell_price, notes=payload.notes,
        )
    except Exception as e:
        _raise_http(e)
        raise
    logger.info(f"[LEAPS API] Position #{rec.id} partial-close -> qty={rec.quantity}, "
                f"realized={rec.realized_premium}")
    return {"status": "OK", "position_id": rec.id, "quantity": rec.quantity,
            "total_cost": rec.total_cost, "realized_premium": rec.realized_premium}


@router.post("/{position_id}/dismiss")
def dismiss_waiting(position_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        rec = leaps_service.dismiss_waiting(db, position_id)
    except Exception as e:
        _raise_http(e)
        raise
    logger.info(f"[LEAPS API] Position #{rec.id} WAITING -> DISMISSED")
    return {"status": "OK", "position_id": rec.id, "position_status": rec.status}
