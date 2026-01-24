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

app = FastAPI(title="QQQ Option Alert System")

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
            "qqq_rule_a_enabled": config_db.qqq_rule_a_enabled,
            "qqq_rule_b_enabled": config_db.qqq_rule_b_enabled,
            "qqq_rule_c_enabled": config_db.qqq_rule_c_enabled,
            "qqq_rule_d_enabled": config_db.qqq_rule_d_enabled,
            "max_holding_days": config_db.max_holding_days,
            "take_profit_phase1_threshold": config_db.take_profit_phase1_threshold,
            "take_profit_phase1_days": config_db.take_profit_phase1_days,
            "take_profit_phase2_threshold": config_db.take_profit_phase2_threshold,
            "take_profit_phase2_days": config_db.take_profit_phase2_days,
            "take_profit_phase3_threshold": config_db.take_profit_phase3_threshold,
            "stop_loss_threshold": config_db.stop_loss_threshold,
            "dte_warning_days": config_db.dte_warning_days,
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


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, db: Session = Depends(get_db)):
    if not is_first_time_setup(db):
        return RedirectResponse(url="/admin/login", status_code=302)

    return templates.TemplateResponse("setup.html", {"request": request})


@app.post("/setup")
async def setup(request: Request, password: str = Form(...), db: Session = Depends(get_db)):
    try:
        if not is_first_time_setup(db):
            return RedirectResponse(url="/admin/login", status_code=302)

        if len(password) < 6:
            return templates.TemplateResponse("setup.html", {
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
        return templates.TemplateResponse("setup.html", {
            "request": request,
            "error": f"设置失败: {str(e)}"
        })


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(get_db)):
    if is_first_time_setup(db):
        return RedirectResponse(url="/setup", status_code=302)

    return templates.TemplateResponse("login.html", {"request": request})


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

    return templates.TemplateResponse("login.html", {
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

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "positions_count": positions_count,
        "today_logs": today_logs,
        "market_open": market_open
    })


@app.get("/admin/positions", response_class=HTMLResponse)
async def positions(request: Request, db: Session = Depends(get_db)):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    positions = db.query(OptionPosition).order_by(OptionPosition.created_at.desc()).all()

    return templates.TemplateResponse("positions.html", {
        "request": request,
        "positions": positions
    })


@app.post("/admin/positions")
async def add_position(
    request: Request,
    underlying: str = Form("QQQ"),
    option_type: str = Form(...),
    strike_price: float = Form(...),
    expiration_date: str = Form(...),
    entry_price: float = Form(...),
    quantity: Optional[int] = Form(None),
    entry_date: str = Form(...),
    db: Session = Depends(get_db)
):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    position = OptionPosition(
        underlying=underlying,
        option_type=option_type,
        strike_price=strike_price,
        expiration_date=date.fromisoformat(expiration_date),
        entry_price=entry_price,
        quantity=quantity,
        entry_date=date.fromisoformat(entry_date)
    )

    db.add(position)
    db.commit()

    return RedirectResponse(url="/admin/positions", status_code=303)


@app.post("/admin/positions/{position_id}/delete")
async def delete_position(
    position_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    position = db.query(OptionPosition).filter(OptionPosition.id == position_id).first()
    if position:
        db.delete(position)
        db.commit()

    return RedirectResponse(url="/admin/positions", status_code=303)


@app.get("/admin/rules", response_class=HTMLResponse)
async def rules(request: Request, db: Session = Depends(get_db)):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    config_db = db.query(Configuration).first()

    return templates.TemplateResponse("rules.html", {
        "request": request,
        "config": config_db
    })


@app.post("/admin/rules")
async def update_rules(
    request: Request,
    qqq_rule_a_enabled: bool = Form(False),
    qqq_rule_b_enabled: bool = Form(False),
    qqq_rule_c_enabled: bool = Form(False),
    qqq_rule_d_enabled: bool = Form(False),
    max_holding_days: int = Form(270),
    take_profit_phase1_threshold: float = Form(0.50),
    take_profit_phase1_days: int = Form(120),
    take_profit_phase2_threshold: float = Form(0.30),
    take_profit_phase2_days: int = Form(180),
    take_profit_phase3_threshold: float = Form(0.10),
    stop_loss_threshold: float = Form(0.30),
    dte_warning_days: int = Form(45),
    db: Session = Depends(get_db)
):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    config_db = db.query(Configuration).first()

    config_db.qqq_rule_a_enabled = qqq_rule_a_enabled
    config_db.qqq_rule_b_enabled = qqq_rule_b_enabled
    config_db.qqq_rule_c_enabled = qqq_rule_c_enabled
    config_db.qqq_rule_d_enabled = qqq_rule_d_enabled
    config_db.max_holding_days = max_holding_days
    config_db.take_profit_phase1_threshold = take_profit_phase1_threshold
    config_db.take_profit_phase1_days = take_profit_phase1_days
    config_db.take_profit_phase2_threshold = take_profit_phase2_threshold
    config_db.take_profit_phase2_days = take_profit_phase2_days
    config_db.take_profit_phase3_threshold = take_profit_phase3_threshold
    config_db.stop_loss_threshold = stop_loss_threshold
    config_db.dte_warning_days = dte_warning_days

    db.commit()

    return RedirectResponse(url="/admin/rules", status_code=303)


@app.get("/admin/logs", response_class=HTMLResponse)
async def logs(request: Request, db: Session = Depends(get_db)):
    if not verify_admin_cookie(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    logs = db.query(AlertLog).order_by(AlertLog.triggered_at.desc()).limit(100).all()

    return templates.TemplateResponse("logs.html", {
        "request": request,
        "logs": logs
    })
