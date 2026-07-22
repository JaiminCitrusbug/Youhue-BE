"""Youhue backend application entrypoint (FastAPI).

Layered structure (interfaces -> application -> domain / infrastructure). Feature routers are
mounted per ticket in execution order.
"""
from fastapi import FastAPI

from config.env_config import settings
from src.routers.auth import me_router
from src.routers.auth import router as auth_router
from src.routers.health import router as health_router
from src.routers.notifications import router as notifications_router
from src.routers.students import router as students_router

app = FastAPI(title=settings.app_name, version="0.1.0")
app.include_router(health_router)
app.include_router(auth_router, prefix=settings.api_prefix)
app.include_router(me_router, prefix=settings.api_prefix)
app.include_router(students_router, prefix=settings.api_prefix)
app.include_router(notifications_router, prefix=settings.api_prefix)
