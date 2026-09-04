from fastapi import FastAPI, Depends, HTTPException, status, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session
from typing import Optional
from datetime import date

from app.database.init_db import init_db, get_db, engine, SessionLocal
from app.database.models import Configuration, OptionPosition, AlertLog
from app.config import get_config
from app.market.polygon_client import CachedPolygonClient
from app.market.data_fetcher import DataFetcher
from app.scheduler.jobs import start_scheduler, stop_scheduler
from app.scheduler.trading_hours import is_market_open_now, get_current_time_et
from app.admin.auth import (
    get_password_hash, verify_admin_password, is_first_time_setup,
    authenticate_admin
)
from app.api.grid_api import router as grid_api_router
from app.services import grid_service

app = FastAPI(title="QQQ Option Alert System")

app.include_router(grid_api_router)

templates = Jinja2Templates(directory="app/admin/templates")
security = HTTPBasic()

polygon_client: Optional[CachedPolygonClient] = None
data_fetcher: Optional[DataFetcher] = None
config: Optional[get_config] = None


@app.on_event("startup")
async def startup_event():
    global polygon_client, data_fetcher, config

    init_db()

    db = SessionLocal()

    try:
        config_db = db.query(Configuration).first()
        if not config_db:
            config_db = Configuration(
                admin_password_hash="",
                polygon_api_key="",
                wechat_webhook_url=""
            )
            db.add(config_db)
            db.commit()

        db.refresh(config_db)

        config_dict = {
            "polygon_api_key": config_db.polygon_api_key,
            "wechat_webhook_url": config_db.wechat_webhook_url,
            # New entry rules
            "entry_level1_enabled": getattr(config_db, 'entry_level1_enabled', None),
            "entry_level2_enabled": getattr(config_db, 'entry_level2_enabled', None),
            "entry_level3_enabled": getattr(config_db, 'entry_level3_enabled', None),
            # New exit rules
            "exit_hard_tp_enabled": getattr(config_db, 'exit_hard_tp_enabled', None),
            "exit_fast_tp_enabled": getattr(config_db, 'exit_fast_tp_enabled', None),
            "exit_trailing_tp_enabled": getattr(config_db, 'exit_trailing_tp_enabled', None),
            "exit_tech_tp_enabled": getattr(config_db, 'exit_tech_tp_enabled', None),
            "exit_dte_warning_enabled": getattr(config_db, 'exit_dte_warning_enabled', None),
            "exit_dte_force_enabled": getattr(config_db, 'exit_dte_force_enabled', None),
            "exit_trend_stop_enabled": getattr(config_db, 'exit_trend_stop_enabled', None),
            # Parameters
            "alert_log_retention_days": config_db.alert_log_retention_days,
            "daily_qqq_data_retention_days": config_db.daily_qqq_data_retention_days,
        }

        config = get_config(config_dict)

        polygon_client = CachedPolygonClient(config.get_polygon_api_key())
        data_fetcher = DataFetcher(polygon_client, db)

        start_scheduler(data_fetcher, db, config)

    finally:
        db.close()


@app.on_event("shutdown")
async def shutdown_event():
    stop_scheduler()


@app.get("/")
async def root():
    return {"message": "QQQ Option Alert System", "status": "running"}


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

    # Check market data sources
    if data_fetcher:
        try:
            qqq_data = data_fetcher.get_qqq_data()
            results["components"]["qqq_data"] = {
                "status": "ok" if qqq_data.get("last_price") else "no_data"
            }
        except Exception as e:
            results["components"]["qqq_data"] = {"status": "error", "message": str(e)}
            results["status"] = "degraded"

    # Count positions
    try:
        from app.database.models import OptionPosition
        count = db.query(OptionPosition).count()
        results["components"]["positions"] = {"status": "ok", "count": count}
    except Exception as e:
        results["components"]["positions"] = {"status": "error", "message": str(e)}

    return results


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, db: Session = Depends(get_db)):
    if not is_first_time_setup(db):
        return RedirectResponse(url="/admin/login", status_code=302)

    return templates.TemplateResponse(request=request, name="setup.html", context={"request": request})


@app.post("/setup")
async def setup(request: Request, password: str = Form(...), db: Session = Depends(get_db)):
    try:
        if not is_first_time_setup(db):
            return RedirectResponse(url="/admin/login", status_code=302)

        if len(password) < 6:
            return templates.TemplateResponse(request=request, name="setup.html", context={
                "request": request,
                "error": "密码长度至少为 6 位"
            })

        config_db = db.query(Configuration).first()
        if config_db:
            config_db.admin_password_hash = get_password_hash(password)
            db.commit()

        return RedirectResponse(url="/admin/login", status_code=302)
    except Exception as e:
        print(f"Setup error: {e}")
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
    if verify_admin_password(password, db):
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie(key="admin_logged_in", value="true")
        return response

    return templates.TemplateResponse(request=request, name="login.html", context={
        "request": request,
        "error": "密码错误"
    })


@app.get("/admin/logout")
async def logout():
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie(key="admin_logged_in")
    return response


def verify_admin_cookie(request: Request):
    return request.cookies.get("admin_logged_in") == "true"


@app.get("/admin", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    positions_count = db.query(OptionPosition).count()
    today_logs = db.query(AlertLog).filter(
        AlertLog.triggered_at >= get_current_time_et().replace(hour=0, minute=0, second=0, microsecond=0)
    ).count()

    market_open = is_market_open_now()

    # NDX Grid 数据: 复用 grid_service dashboard 聚合 (Phase 1-3 函数)
    grid_dash = grid_service.get_grid_dashboard(db, data_fetcher.get_ndx_data() if data_fetcher else None)
    ndx = grid_dash["ndx"]
    latest_cycles = grid_service.get_cycle_history(db, 1)["cycles"]
    latest_cycle = latest_cycles[0] if latest_cycles else None

    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "request": request,
        "positions_count": positions_count,
        "today_logs": today_logs,
        "market_open": market_open,
        "grid_dash": grid_dash,
        "ndx": ndx,
        "latest_cycle": latest_cycle,
        # 兼容旧模板变量 (市场感知卡片)
        "qqq_price": None,
        "rsi": ndx.get("rsi"),
        "is_above_sma200": None
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


@app.get("/admin/positions", response_class=HTMLResponse)
async def positions_redirect(request: Request):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    # Phase 4B: 旧期权仓位页面退役, 兼容性重定向到 Grid 管理页
    return RedirectResponse(url="/admin/grid", status_code=302)


# ---------------------------------------------------------------------
# Legacy option position POST endpoints retired (Phase 5 audit).
#
# 调用方追踪结论: 唯一调用方是 positions.html (该页面已因 GET redirect
# 不可达); 无 scheduler / API / 其他 Python 模块引用。期权仓位功能已由
# NDX Grid 体系替代, 旧的建仓/删除/刷新入口一并退役。
#
# 保留项:
#   - OptionPosition 表与历史数据完整保留 (只读, 不删除)
#   - jobs.check_qqq_and_options 对 OptionPosition 的读取逻辑保留
#   - /admin/positions GET 保留 redirect 兼容旧链接
# ---------------------------------------------------------------------


@app.get("/admin/rules", response_class=HTMLResponse)
async def rules(request: Request, db: Session = Depends(get_db)):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    config_db = db.query(Configuration).first()

    # 优先使用运行时配置 (含 DB 覆盖值); 未启动时退回 env 默认
    runtime_config = config if config is not None else get_config()

    return templates.TemplateResponse(request=request, name="rules.html", context={
        "request": request,
        "config": config_db,
        # NDX Grid 策略参数: 以后端配置为唯一来源 (env/DB), 前端只展示
        "grid_rsi_threshold": runtime_config.get_rsi_threshold(),
        "grid_upper_pct": runtime_config.get_default_grid_upper_pct(),
        "grid_lower_pct": runtime_config.get_default_grid_lower_pct(),
        "grid_count": runtime_config.get_default_grid_count(),
        "grid_leverage": runtime_config.get_default_grid_leverage(),
    })


@app.get("/admin/logs", response_class=HTMLResponse)
async def logs(request: Request, db: Session = Depends(get_db)):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    logs = db.query(AlertLog).order_by(AlertLog.triggered_at.desc()).limit(100).all()

    return templates.TemplateResponse(request=request, name="logs.html", context={
        "request": request,
        "logs": logs
    })
