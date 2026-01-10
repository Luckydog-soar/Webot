from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler
from contextlib import asynccontextmanager
import os
from sqlmodel import Session, select, desc
from .database import create_db_and_tables, engine
from .models import SystemLog, SystemStatus
from .scanner import scanner

templates = Jinja2Templates(directory="app/templates")
scheduler = BackgroundScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        create_db_and_tables()
    except Exception as e:
        print(f"DB Init Failed: {e}")

    try:
        interval = int(os.getenv("SCAN_INTERVAL_SECONDS", 60))
        scheduler.add_job(scanner.run_scan, 'interval', seconds=interval)
        scheduler.start()
    except Exception as e:
        print(f"Scheduler Failed: {e}")

    yield
    try:
        scheduler.shutdown()
    except: pass

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/data")
def get_data():
    """V1.1 API: 返回聚合后的仪表盘数据"""
    try:
        # 1. 获取 scanner 内存里的聚合数据 (排行榜 + 热度)
        dashboard_data = scanner.get_dashboard_data()
        
        # 2. 补充运行状态 (Heartbeat)
        is_running = False
        with Session(engine) as session:
            status = session.get(SystemStatus, 1)
            if status:
                dashboard_data["round"] = status.scan_round
                is_running = True
            
            # 3. 获取日志 (只取最近 50 条)
            logs = session.exec(select(SystemLog).order_by(desc(SystemLog.created_at)).limit(50)).all()
            dashboard_data["logs"] = logs
            dashboard_data["is_running"] = is_running
            
        return dashboard_data
        
    except Exception as e:
        return {
            "market_heat": {"score": 0, "level": "Error", "icon": "❌", "color_class": "text-gray-500", "delta": 0},
            "hot_list": [],
            "logs": [{"level": "ERROR", "message": str(e), "created_at": "now"}],
            "is_running": False
        }