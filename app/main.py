import hmac
import logging
import os
from fastapi import FastAPI, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional
from starlette.middleware.sessions import SessionMiddleware

from app.database.init_db import init_db, get_db, SessionLocal
from app.database.models import Configuration, AlertLog
from app.config import get_config
from app.market.data_fetcher import DataFetcher
from app.scheduler.jobs import start_scheduler, stop_scheduler
from app.scheduler.trading_hours import is_market_open_now, get_current_time_et
from app.admin.auth import (
    get_password_hash, verify_admin_password, is_first_time_setup, set_admin_password
)
from app.admin.security import (
    SESSION_COOKIE_NAME,
    get_session_secret,
    get_session_max_age_seconds,
    cookie_secure_enabled,
    is_admin_session,
    login_locked_seconds,
    register_login_failure,
    register_login_success,
    attempt_keys,
    LOGIN_GLOBAL_MAX_FAILURES,
)
from app.alerts.alert_log import count_alerts_today
from app.api.leaps_api import router as leaps_api_router
from app.services import leaps_service

logger = logging.getLogger(__name__)

app = FastAPI(title="QQQ LEAPS Alert System")

# 签名会话 Cookie (替代历史静态 admin_logged_in=true, 后者可被任意伪造)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_session_secret(),
    session_cookie=SESSION_COOKIE_NAME,
    max_age=get_session_max_age_seconds(),
    same_site="lax",
    https_only=cookie_secure_enabled(),
)

app.include_router(leaps_api_router)


def _configured_setup_token() -> str:
    return os.getenv("SETUP_TOKEN", "").strip()


def _is_loopback(request: Request) -> bool:
    host = getattr(getattr(request, "client", None), "host", "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


def _setup_allowed(request: Request, token: Optional[str]) -> bool:
    """
    初始化(/setup)准入判定 (P0 修复):

    - 已设置管理员密码 => 一律拒绝 (410 语义, 由调用方处理)
    - 配置了 SETUP_TOKEN => 必须匹配 (常量时间比较)
    - 未配置 SETUP_TOKEN => 仅允许本机回环访问 (反向代理环境下等于禁用公网初始化)
    """
    expected = _configured_setup_token()
    if expected:
        return hmac.compare_digest(str(token or ""), expected)
    return _is_loopback(request)

templates = Jinja2Templates(directory="app/admin/templates")

data_fetcher: Optional[DataFetcher] = None
config: Optional[get_config] = None


def _build_config_dict(config_db: Configuration) -> dict:
    """DB Configuration 行 -> Config 字典 (LEAPS 全部参数)。"""
    return {
        "wechat_webhook_url": config_db.wechat_webhook_url,
        "alert_log_retention_days": config_db.alert_log_retention_days,
        "daily_report_mode": getattr(config_db, 'daily_report_mode', None),
        "leaps_tp_rsi": getattr(config_db, 'leaps_tp_rsi', None),
        "leaps_time_stop_trading_days": getattr(config_db, 'leaps_time_stop_trading_days', None),
        "leaps_dte_force_days": getattr(config_db, 'leaps_dte_force_days', None),
        "leaps_add_levels": getattr(config_db, 'leaps_add_levels', None),
        "leaps_max_quantity": getattr(config_db, 'leaps_max_quantity', None),
        "leaps_target_delta": getattr(config_db, 'leaps_target_delta', None),
        "leaps_target_tenor_days": getattr(config_db, 'leaps_target_tenor_days', None),
        "leaps_half_tp_pnl": getattr(config_db, 'leaps_half_tp_pnl', None),
    }


@app.on_event("startup")
async def startup_event():
    global data_fetcher, config

    init_db()

    db = SessionLocal()

    try:
        config_db = db.query(Configuration).first()
        if not config_db:
            config_db = Configuration(
                admin_password_hash="",
                wechat_webhook_url=""
            )
            db.add(config_db)
            db.commit()

        db.refresh(config_db)

        config = get_config(_build_config_dict(config_db))

        data_fetcher = DataFetcher()

        start_scheduler(data_fetcher, db, config)

    finally:
        db.close()


@app.on_event("shutdown")
async def shutdown_event():
    stop_scheduler()


@app.get("/")
async def root():
    return {"message": "QQQ LEAPS Alert System", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "healthy", "market_open": is_market_open_now()}


@app.get("/health/detailed")
async def health_detailed(db: Session = Depends(get_db)):
    """
    Detailed health check - checks all critical components
    """
    results = {
        "status": "healthy",
        "components": {}
    }

    # Check database
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        results["components"]["database"] = {"status": "ok"}
    except Exception as e:
        results["components"]["database"] = {"status": "error", "message": str(e)}
        results["status"] = "degraded"

    # Check scheduler
    from app.scheduler.jobs import scheduler
    results["components"]["scheduler"] = {
        "status": "running" if scheduler.running else "stopped"
    }

    # Check QQQ market data source
    if data_fetcher:
        try:
            qqq_data = data_fetcher.get_qqq_data()
            results["components"]["qqq_data"] = {
                "status": "ok" if qqq_data.get("last_price") else "no_data"
            }
        except Exception as e:
            results["components"]["qqq_data"] = {"status": "error", "message": str(e)}
            results["status"] = "degraded"

    # Check option positions
    try:
        from app.database.models import OptionPosition
        count = db.query(OptionPosition).count()
        results["components"]["option_positions"] = {"status": "ok", "count": count}
    except Exception as e:
        results["components"]["option_positions"] = {"status": "error", "message": str(e)}

    return results


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, db: Session = Depends(get_db)):
    if not is_first_time_setup(db):
        # 初始化只能做一次; 已设置密码后该入口彻底关闭
        return RedirectResponse(url="/admin/login", status_code=302)

    setup_token = request.query_params.get("token")
    if not _setup_allowed(request, setup_token):
        return templates.TemplateResponse(request=request, name="setup.html", context={
            "request": request,
            "error": "初始化入口未授权：请在 URL 上携带正确的 SETUP_TOKEN (?token=...)",
            "setup_token_required": bool(_configured_setup_token()),
        }, status_code=403)

    return templates.TemplateResponse(request=request, name="setup.html", context={
        "request": request,
        "setup_token": setup_token or "",
    })


@app.post("/setup")
async def setup(request: Request, password: str = Form(...), setup_token: str = Form(""),
                db: Session = Depends(get_db)):
    try:
        if not is_first_time_setup(db):
            return RedirectResponse(url="/admin/login", status_code=302)

        # 口令来源: 表单隐藏字段 > URL 上的 ?token= (两者任一匹配即可)
        token = setup_token or request.query_params.get("token") or ""
        if not _setup_allowed(request, token):
            return templates.TemplateResponse(request=request, name="setup.html", context={
                "request": request,
                "error": "初始化入口未授权：SETUP_TOKEN 缺失或不匹配（请从带 ?token= 的初始化链接打开本页）",
                "setup_token_required": bool(_configured_setup_token()),
                "setup_token": token,
            }, status_code=403)

        if len(password) < 6:
            return templates.TemplateResponse(request=request, name="setup.html", context={
                "request": request,
                "error": "密码长度至少为 6 位",
                "setup_token": token,
            })

        set_admin_password(db, password)
        logger.info("[Setup] Admin password initialized (scrypt hash stored).")
        return RedirectResponse(url="/admin/login", status_code=302)
    except Exception as e:
        logger.error(f"[Setup] Failed: {e}")
        return templates.TemplateResponse(request=request, name="setup.html", context={
            "request": request,
            "error": f"设置失败: {str(e)}"
        })


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(get_db)):
    if is_first_time_setup(db):
        return RedirectResponse(url="/setup", status_code=302)

    return templates.TemplateResponse(request=request, name="login.html", context={"request": request})


@app.post("/admin/login")
async def login(
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    key, global_key = attempt_keys(request)

    # 登录限速: 同来源 5 次失败 / 全局 30 次失败 -> 429 冷却
    locked_for = max(login_locked_seconds(key), login_locked_seconds(global_key))
    if locked_for > 0:
        return templates.TemplateResponse(request=request, name="login.html", context={
            "request": request,
            "error": f"尝试次数过多，请 {locked_for} 秒后再试",
        }, status_code=429)

    if verify_admin_password(password, db):
        register_login_success(key)
        register_login_success(global_key)
        request.session["admin"] = True
        response = RedirectResponse(url="/admin", status_code=303)
        return response

    register_login_failure(key)
    register_login_failure(global_key, threshold=LOGIN_GLOBAL_MAX_FAILURES)
    return templates.TemplateResponse(request=request, name="login.html", context={
        "request": request,
        "error": "密码错误"
    }, status_code=401)


@app.get("/admin/logout")
async def logout(request: Request):
    request.session.clear()
    response = RedirectResponse(url="/admin/login", status_code=302)
    # 双保险: 同时显式删除 Cookie (兼容旧客户端/人工注入的 token)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


def verify_admin_cookie(request: Request):
    """
    管理员会话校验 (保留历史函数名, 供页面与 leaps_api 复用)。

    两条合法路径 (均为服务端签名, 客户端无法伪造):
      1. starlette SessionMiddleware 会话 (`request.session["admin"] is True`),
         由 /admin/login 登录成功后写入 —— 浏览器主路径;
      2. 直接携带 itsdangerous 签名 token 的调用方 (API/脚本/测试),
         用与登录同一密钥校验。
    """
    try:
        if request.session.get("admin") is True:
            return True
    except Exception:
        # 未安装 SessionMiddleware 等场景: 退回 token 校验
        pass
    return is_admin_session(request.cookies.get(SESSION_COOKIE_NAME))


@app.get("/admin", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    today_logs = count_alerts_today(db)

    market_open = is_market_open_now()

    # QQQ + 仓位聚合 (与 /api/leaps/status 同源)
    qqq_data = data_fetcher.get_qqq_data() if data_fetcher else None
    if isinstance(qqq_data, dict):
        from app.alerts.leaps_monitor import _is_data_fresh
        try:
            qqq_data["is_data_fresh"] = _is_data_fresh(qqq_data)
        except Exception:
            qqq_data["is_data_fresh"] = False
    leaps_dash = leaps_service.get_leaps_dashboard(db, qqq_data)
    qqq = leaps_dash["qqq"] or {}
    waiting = leaps_dash["waiting_position"]
    holding = leaps_dash["holding_position"]

    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "request": request,
        "today_logs": today_logs,
        "market_open": market_open,
        "leaps_dash": leaps_dash,
        "qqq": qqq,
        "waiting": waiting,
        "holding": holding,
    })


@app.get("/admin/positions", response_class=HTMLResponse)
async def positions_page(request: Request, db: Session = Depends(get_db)):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    qqq_data = data_fetcher.get_qqq_data() if data_fetcher else None
    if isinstance(qqq_data, dict):
        from app.alerts.leaps_monitor import _is_data_fresh
        try:
            qqq_data["is_data_fresh"] = _is_data_fresh(qqq_data)
        except Exception:
            qqq_data["is_data_fresh"] = False
    leaps_dash = leaps_service.get_leaps_dashboard(db, qqq_data)
    rows = leaps_service.get_position_history(db, 20)["positions"]
    history = {"positions": [
        {
            "id": r.id,
            "status": r.status,
            "signal_base_price": r.signal_base_price,
            "strike": r.strike,
            "expiration_date": r.expiration_date.isoformat() if r.expiration_date else None,
            "quantity": r.quantity,
            "entry_price": r.entry_price,
            "total_cost": r.total_cost,
            "close_premium": r.close_premium,
            "close_reason": r.close_reason,
            "created_at": str(r.created_at)[:19] if r.created_at else None,
            "closed_at": str(r.closed_at)[:19] if r.closed_at else None,
        } for r in rows
    ]}

    return templates.TemplateResponse(request=request, name="positions.html", context={
        "request": request,
        "leaps_dash": leaps_dash,
        "history": history,
    })


def refresh_global_config(db: Session):
    global config
    config_db = db.query(Configuration).first()
    if not config_db:
        return config if config is not None else get_config()
    config = get_config(_build_config_dict(config_db))
    return config


@app.get("/admin/rules", response_class=HTMLResponse)
async def rules(request: Request, db: Session = Depends(get_db)):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    # 刷新并获取运行时配置 (确保包含 DB 最新保存值)
    runtime_config = refresh_global_config(db)

    return templates.TemplateResponse(request=request, name="rules.html", context={
        "request": request,
        # LEAPS 策略参数: 以后端配置为唯一来源 (env/DB), 前端展示 + 可编辑
        "entry_rsi": runtime_config.get_entry_rsi_threshold(),
        "tp_rsi": runtime_config.get_tp_rsi(),
        "time_stop_days": runtime_config.get_time_stop_trading_days(),
        "dte_force_days": runtime_config.get_dte_force_days(),
        "add_levels": ",".join(f"{v:.2f}".rstrip("0").rstrip(".") for v in runtime_config.get_add_levels()),
        "max_quantity": runtime_config.get_max_quantity(),
        "target_delta": runtime_config.get_target_delta(),
        "target_tenor_days": runtime_config.get_target_tenor_days(),
        "half_tp_pnl": runtime_config.get_half_tp_pnl(),
        "waiting_ttl_trading_days": runtime_config.get_waiting_ttl_trading_days(),
        # 每日日报模式 (运行时配置, 默认 leaps)
        "daily_report_mode": runtime_config.get_daily_report_mode(),
        "report_saved": request.query_params.get("saved") == "1",
    })


def _parse_float(value: Optional[str], default: float) -> float:
    try:
        v = float(str(value).strip())
        return v
    except (TypeError, ValueError):
        return default


def _parse_int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@app.post("/admin/rules/strategy")
async def update_strategy(
    request: Request,
    tp_rsi: str = Form(...),
    time_stop_days: str = Form(...),
    dte_force_days: str = Form(...),
    add_levels: str = Form("0.15,0.25"),
    max_quantity: str = Form("3"),
    target_delta: str = Form("0.65"),
    target_tenor_days: str = Form("365"),
    half_tp_pnl: str = Form("0.5"),
    db: Session = Depends(get_db),
):
    """保存 LEAPS 策略参数 (后台可调; 数值非法时后端静默回退当前值)。"""
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    config_db = db.query(Configuration).first()
    if config_db:
        current = get_config(_build_config_dict(config_db))
        config_db.leaps_tp_rsi = _parse_float(tp_rsi, current.get_tp_rsi())
        config_db.leaps_time_stop_trading_days = _parse_int(time_stop_days, current.get_time_stop_trading_days())
        config_db.leaps_dte_force_days = _parse_int(dte_force_days, current.get_dte_force_days())
        config_db.leaps_add_levels = str(add_levels).strip() or "0.15,0.25"
        config_db.leaps_max_quantity = max(1, _parse_int(max_quantity, current.get_max_quantity()))
        config_db.leaps_target_delta = _parse_float(target_delta, current.get_target_delta())
        config_db.leaps_target_tenor_days = _parse_int(target_tenor_days, current.get_target_tenor_days())
        half_val = _parse_float(half_tp_pnl, current.get_half_tp_pnl())
        config_db.leaps_half_tp_pnl = max(0.0, half_val) if half_val is not None else 0.0
        db.commit()
        refresh_global_config(db)
        logger.info("[INFO] LEAPS strategy params updated")

    return RedirectResponse(url="/admin/rules?saved=1", status_code=303)


@app.post("/admin/rules/daily-report-mode")
async def update_daily_report_mode(
    request: Request,
    daily_report_mode: str = Form(...),
    db: Session = Depends(get_db)
):
    """保存每日 16:30 日报模式 (off / leaps)"""
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    if daily_report_mode in ("legacy", "ndx_grid"):
        # 历史配置兼容: 网格日报已下线, 归一化为 leaps
        daily_report_mode = "leaps"
    if daily_report_mode not in ("off", "leaps"):
        daily_report_mode = "leaps"  # 非法值回退默认

    config_db = db.query(Configuration).first()
    if config_db:
        config_db.daily_report_mode = daily_report_mode
        db.commit()
        refresh_global_config(db)
        logger.info(f"[INFO] Daily report mode updated to: {daily_report_mode}")

    return RedirectResponse(url="/admin/rules?saved=1", status_code=303)


@app.get("/admin/logs", response_class=HTMLResponse)
async def logs(request: Request, db: Session = Depends(get_db)):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    logs = db.query(AlertLog).order_by(AlertLog.triggered_at.desc()).limit(100).all()

    return templates.TemplateResponse(request=request, name="logs.html", context={
        "request": request,
        "logs": logs
    })
