from fastapi import FastAPI, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional

from app.database.init_db import init_db, get_db, SessionLocal
from app.database.models import Configuration, AlertLog
from app.config import get_config
from app.market.data_fetcher import DataFetcher
from app.scheduler.jobs import start_scheduler, stop_scheduler
from app.scheduler.trading_hours import is_market_open_now, get_current_time_et
from app.admin.auth import (
    get_password_hash, verify_admin_password, is_first_time_setup
)
from app.api.grid_api import router as grid_api_router
from app.services import grid_service

app = FastAPI(title="NDX Grid Alert System")

app.include_router(grid_api_router)

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
