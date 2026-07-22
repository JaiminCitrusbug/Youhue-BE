"""Youhue backend application entrypoint (FastAPI).

Layered structure (interfaces -> application -> domain / infrastructure). Feature routers are
mounted per ticket in execution order.
"""
from fastapi import FastAPI

from src.config import settings
from src.interfaces.auth import me_router
from src.interfaces.auth import router as auth_router
from src.interfaces.health import router as health_router
from src.interfaces.students import router as students_router

app = FastAPI(title=settings.app_name, version="0.1.0")
app.include_router(health_router)
app.include_router(auth_router, prefix=settings.api_prefix)
app.include_router(me_router, prefix=settings.api_prefix)
app.include_router(students_router, prefix=settings.api_prefix)
