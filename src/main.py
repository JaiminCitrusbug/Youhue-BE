"""Youhue backend application entrypoint (FastAPI).

Layered structure (agents/backend.md): interfaces -> application -> domain / infrastructure.
This is the scaffold; feature routers are mounted per ticket in execution order.
"""
from fastapi import FastAPI

from src.config import settings
from src.interfaces.health import router as health_router

app = FastAPI(title=settings.app_name, version="0.1.0")
app.include_router(health_router)
