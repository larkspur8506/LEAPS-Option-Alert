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
from app.api.grid_api import router as grid_api_router
from app.services import grid_service

logger = logging.getLogger(__name__)

app = FastAPI(title="NDX Grid Alert System")

# 签名会话 Cookie (替代历史静态 admin_logged_in=true, 后者可被任意伪造)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_session_secret(),
    session_cookie=SESSION_COOKIE_NAME,
    max_age=get_session_max_age_seconds(),
    same_site="lax",
    https_only=cookie_secure_enabled(),
)

app.include_router(grid_api_router)


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

        config_dict = {
            "wechat_webhook_url": config_db.wechat_webhook_url,
            "alert_log_retention_days": config_db.alert_log_retention_days,
            "daily_report_mode": getattr(config_db, 'daily_report_mode', None),
        }

        config = get_config(config_dict)

        data_fetcher = DataFetcher()

        start_scheduler(data_fetcher, db, config)

    finally:
        db.close()


@app.on_event("shutdown")
async def shutdown_event():
    stop_scheduler()


@app.get("/")
async def root():
    return {"message": "NDX Grid Alert System", "status": "running"}


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

    # Check NDX market data source
    if data_fetcher:
        try:
            ndx_data = data_fetcher.get_ndx_data()
            results["components"]["ndx_data"] = {
                "status": "ok" if ndx_data.get("last_price") else "no_data"
            }
        except Exception as e:
            results["components"]["ndx_data"] = {"status": "error", "message": str(e)}
            results["status"] = "degraded"

    # Check grid cycles
    try:
        from app.database.models import GridCycle
        count = db.query(GridCycle).count()
        results["components"]["grid_cycles"] = {"status": "ok", "count": count}
    except Exception as e:
        results["components"]["grid_cycles"] = {"status": "error", "message": str(e)}

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

        if not _setup_allowed(request, setup_token):
            return templates.TemplateResponse(request=request, name="setup.html", context={
                "request": request,
                "error": "初始化入口未授权：SETUP_TOKEN 缺失或不匹配",
                "setup_token_required": bool(_configured_setup_token()),
            }, status_code=403)

        if len(password) < 6:
            return templates.TemplateResponse(request=request, name="setup.html", context={
                "request": request,
                "error": "密码长度至少为 6 位",
                "setup_token": setup_token,
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
    管理员会话校验 (保留历史函数名, 供页面与 grid_api 复用)。

    两条合法路径 (均为服务端签名, 客户端无法伪造):
      1. starlette SessionMiddleware 会话 (`request.session["admin"] is True`),
         由 /admin/login 登录成功后写入 —— 浏览器主路径;
      2. 直接携带 itsdangerous 签名 token 的调用方 (API/脚本/测试),
         用与登录同一密钥校验。

    历史实现只比较明文 `admin_logged_in == "true"`, 任意人手工造 Cookie 即可通过。
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

    # NDX Grid 数据: 复用 grid_service dashboard 聚合 (Phase 1-3 函数)
    grid_dash = grid_service.get_grid_dashboard(db, data_fetcher.get_ndx_data() if data_fetcher else None)
    ndx = grid_dash["ndx"]
    latest_cycles = grid_service.get_cycle_history(db, 1)["cycles"]
    latest_cycle = latest_cycles[0] if latest_cycles else None

    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "request": request,
        "today_logs": today_logs,
        "market_open": market_open,
        "grid_dash": grid_dash,
        "ndx": ndx,
        "latest_cycle": latest_cycle,
    })


@app.get("/admin/grid", response_class=HTMLResponse)
async def grid_page(request: Request, db: Session = Depends(get_db)):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    grid_dash = grid_service.get_grid_dashboard(db, data_fetcher.get_ndx_data() if data_fetcher else None)
    history = grid_service.get_cycle_history(db, 20)

    return templates.TemplateResponse(request=request, name="grid.html", context={
        "request": request,
        "grid_dash": grid_dash,
        "history": history
    })


def refresh_global_config(db: Session):
    global config
    config_db = db.query(Configuration).first()
    if not config_db:
        return config if config is not None else get_config()
    config_dict = {
        "wechat_webhook_url": config_db.wechat_webhook_url,
        "alert_log_retention_days": config_db.alert_log_retention_days,
        "daily_report_mode": getattr(config_db, 'daily_report_mode', None),
    }
    config = get_config(config_dict)
    return config


@app.get("/admin/rules", response_class=HTMLResponse)
async def rules(request: Request, db: Session = Depends(get_db)):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    # 刷新并获取运行时配置 (确保包含 DB 最新保存值)
    runtime_config = refresh_global_config(db)

    # 风险/成本速算 (只读展示; NDX 数据不可用时按方向参数估算)
    risk_snapshot = None
    try:
        ndx = data_fetcher.get_ndx_data() if data_fetcher else None
    except Exception:
        ndx = None
    try:
        risk_snapshot = grid_service.get_risk_snapshot(runtime_config, ndx_data=ndx)
    except Exception:
        risk_snapshot = None

    return templates.TemplateResponse(request=request, name="rules.html", context={
        "request": request,
        # NDX Grid 策略参数: 以后端配置为唯一来源 (env/DB), 前端只展示
        "grid_rsi_threshold": runtime_config.get_rsi_threshold(),
        "grid_upper_pct": runtime_config.get_default_grid_upper_pct(),
        "grid_lower_pct": runtime_config.get_default_grid_lower_pct(),
        "grid_count": runtime_config.get_default_grid_count(),
        "grid_leverage": runtime_config.get_default_grid_leverage(),
        # 每日日报模式 (运行时配置, 默认 ndx_grid)
        "daily_report_mode": runtime_config.get_daily_report_mode(),
        # WAITING 存活期 (交易日) 与风险速算
        "waiting_ttl_trading_days": runtime_config.get_waiting_ttl_trading_days(),
        "risk_snapshot": risk_snapshot,
        "report_saved": request.query_params.get("saved") == "1",
    })


@app.post("/admin/rules/daily-report-mode")
async def update_daily_report_mode(
    request: Request,
    daily_report_mode: str = Form(...),
    db: Session = Depends(get_db)
):
    """保存每日 16:30 日报模式 (off / ndx_grid)"""
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    if daily_report_mode == "legacy":
        # 历史配置兼容: legacy 日报已删除, 归一化为 ndx_grid
        daily_report_mode = "ndx_grid"
    if daily_report_mode not in ("off", "ndx_grid"):
        daily_report_mode = "ndx_grid"  # 非法值回退默认

    config_db = db.query(Configuration).first()
    if config_db:
        config_db.daily_report_mode = daily_report_mode
        db.commit()
        refresh_global_config(db)
        print(f"[INFO] Daily report mode updated to: {daily_report_mode}")

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
