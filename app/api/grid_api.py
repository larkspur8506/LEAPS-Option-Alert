"""
GridCycle Admin API (Phase 4A).

REST endpoints for managing GridCycle lifecycle:
    GET  /api/grid/status    - dashboard data (NDX + cycles + theoretical position)
    GET  /api/grid/waiting   - current WAITING cycle (or null)
    GET  /api/grid/running   - current RUNNING cycle (or null)
    GET  /api/grid/history   - cycle history (latest first, clamped limit)
    POST /api/grid/{id}/start - WAITING -> RUNNING with actual params
    POST /api/grid/{id}/close - RUNNING -> CLOSED (MANUAL_CLOSE)

Authentication reuses the existing admin cookie mechanism
(app.main.verify_admin_cookie). No new auth system is introduced.
State transitions delegate to the Phase 2 state machine via the
service layer (app/services/grid_service.py).
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database.init_db import get_db
from app.services import grid_service
from app.services.grid_service import (
    GridCycleNotFoundError,
    GridCycleStateError,
    GridParameterError,
)
from app.notification.wechat import (
    SEPARATOR,
    WeChatNotifier,
    format_ndx_grid_manual_close,
    get_wechat_notifier,
)
from app.alerts.alert_log import log_alert
from app.alerts import dedup

import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/grid", tags=["grid"])

HTTP_CONFLICT = 409

def require_admin(request: Request) -> None:
    """复用现有 admin cookie 认证 (app.main.verify_admin_cookie)，未认证返回 401"""
    from app.main import verify_admin_cookie

    if not verify_admin_cookie(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )


def _get_wechat_notifier() -> Optional[WeChatNotifier]:
    """惰性获取全局运行时配置中的 webhook (避免与 app.main 循环导入); 未配置返回 None"""
    try:
        from app.main import config

        if config is not None and hasattr(config, "get_wechat_webhook_url"):
            webhook_url = config.get_wechat_webhook_url()
            if webhook_url:
                return get_wechat_notifier(webhook_url)
    except Exception:
        pass
    return None


def _get_ndx_data() -> Dict[str, Any]:
    """惰性获取全局 data_fetcher 的 NDX 数据 (避免与 app.main 循环导入)"""
    try:
        from app.main import data_fetcher

        if data_fetcher is None:
            return None
        return data_fetcher.get_ndx_data()
    except Exception:
        return None


class GridStartRequest(BaseModel):
    """WAITING -> RUNNING 时用户录入的交易所实际参数 (原样保存, 系统不自动修正)"""
    actual_base_price: float
    actual_upper_price: float
    actual_lower_price: float
    actual_grid_count: int
    actual_leverage: float
    actual_margin: float


@router.get("/status")
def grid_status(db: Session = Depends(get_db), _: None = Depends(require_admin)):
    """Dashboard 数据: NDX 指标 + 当前 WAITING/RUNNING + 理论网格位置"""
    return grid_service.get_grid_dashboard(db, _get_ndx_data())


@router.get("/waiting")
def get_waiting(db: Session = Depends(get_db), _: None = Depends(require_admin)):
    """当前 WAITING 周期; 不存在返回 null。附 TTL 状态 (剩余多少个交易日过期)。"""
    cycle = grid_service.get_waiting_cycle(db)
    if cycle is None:
        return None
    return {
        **cycle,
        "ttl": grid_service.get_waiting_ttl_state(db, cycle_id=cycle["id"]),
    }


@router.get("/running")
def get_running(db: Session = Depends(get_db), _: None = Depends(require_admin)):
    """当前 RUNNING 周期; 不存在返回 null"""
    return grid_service.get_running_cycle(db)


@router.get("/history")
def get_history(
    limit: int = grid_service.DEFAULT_HISTORY_LIMIT,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin)
):
    """GridCycle 历史 (最新优先; limit 自动钳制到 [1, 200])"""
    return grid_service.get_cycle_history(db, limit)


@router.post("/{cycle_id}/start")
def start_cycle(
    cycle_id: int,
    req: GridStartRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin)
):
    """WAITING -> RUNNING: 用户录入实际参数并确认启动"""
    params = {
        "actual_base_price": req.actual_base_price,
        "actual_upper_price": req.actual_upper_price,
        "actual_lower_price": req.actual_lower_price,
        "actual_grid_count": req.actual_grid_count,
        "actual_leverage": req.actual_leverage,
        "actual_margin": req.actual_margin,
    }
    try:
        return grid_service.start_cycle(db, cycle_id, params)
    except GridCycleNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except GridCycleStateError as e:
        raise HTTPException(status_code=HTTP_CONFLICT, detail=str(e))
    except GridParameterError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{cycle_id}/dismiss")
def dismiss_cycle(
    cycle_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin)
):
    """
    WAITING -> DISMISSED: 人工忽略这条开仓建议 (终态, 释放信号闸门)。

    历史问题: WAITING 一旦生成会永久阻塞后续信号, 用户不打算开这张网格时
    没有任何出口。本接口提供显式取消。
    """
    try:
        result = grid_service.dismiss_waiting_cycle(db, cycle_id)
    except GridCycleNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except GridCycleStateError as e:
        raise HTTPException(status_code=HTTP_CONFLICT, detail=str(e))

    # 状态变化通知 (🚫 已忽略): 通知/日志失败不影响已提交的状态变更
    try:
        notifier = _get_wechat_notifier()
        if notifier is not None:
            message = "\n".join([
                "🚫 NDX Grid 入场建议已忽略",
                SEPARATOR,
                f"记录 ID：{result.get('id')}",
                f"Base：{result.get('suggested_base_price')}",
                f"Upper：{result.get('suggested_upper_price')}",
                f"Lower：{result.get('suggested_lower_price')}",
                "",
                "该建议已标记 DISMISSED (终态, 未在交易所执行)，",
                "信号闸门已重新打开，后续满足条件会重新发出入场建议。",
            ])
            try:
                success = bool(notifier.send_message(message))
            except Exception as e:
                logger.error(f"Failed to send dismiss notification: {e}")
                success = False
            log_alert(
                db, "NDX_GRID_WAITING_DISMISSED", "NDX Grid Waiting Dismissed",
                message, success, cycle_id=result.get("id"),
            )
    except Exception as e:
        logger.warning(f"dismiss notification skipped: {e}")

    # cycle 结束: 允许下一个 cycle 重新触发 proximity 提醒
    dedup.clear_proximity()

    return result


@router.post("/{cycle_id}/close")
def close_cycle(
    cycle_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin)
):
    """RUNNING -> CLOSED / MANUAL_CLOSE: 记录用户已手动结束网格 (不操作交易所)"""
    try:
        result = grid_service.close_running_cycle(db, cycle_id)
    except GridCycleNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except GridCycleStateError as e:
        raise HTTPException(status_code=HTTP_CONFLICT, detail=str(e))

    # 状态变化通知 (🔄 手动关闭): 通知/日志失败不影响已提交的状态变更
    try:
        notifier = _get_wechat_notifier()
        if notifier is not None:
            message = format_ndx_grid_manual_close(result)
            try:
                success = bool(notifier.send_message(message))
            except Exception as e:
                logger.error(f"Failed to send manual close notification: {e}")
                success = False
            log_alert(db, "NDX_GRID_MANUAL_CLOSE", "NDX Grid Manual Close", message, success)
    except Exception as e:
        logger.warning(f"manual close notification skipped: {e}")

    # cycle 结束: 允许下一个 cycle 重新触发 proximity 提醒
    dedup.clear_proximity()

    return result
