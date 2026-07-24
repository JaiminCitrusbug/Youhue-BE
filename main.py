"""Youhue backend application entrypoint (FastAPI).

Layered structure (routers -> application -> domain / infrastructure). Feature routers are
mounted per ticket in execution order.
"""
from fastapi import FastAPI

from config.env_config import settings
from src.routers.admin import router as admin_router
from src.routers.auth import me_router
from src.routers.auth import router as auth_router
from src.routers.calendar import router as calendar_router
from src.routers.health import router as health_router
from src.routers.leadership import router as leadership_router
from src.routers.notifications import router as notifications_router
from src.routers.risk import router as risk_router
from src.routers.schools import router as schools_router
from src.routers.staff_auth import router as staff_auth_router
from src.routers.student_auth import router as student_auth_router
from src.routers.students import router as students_router

app = FastAPI(title=settings.app_name, version="0.1.0")
app.include_router(health_router)
app.include_router(auth_router, prefix=settings.api_prefix)
app.include_router(student_auth_router, prefix=settings.api_prefix)
app.include_router(staff_auth_router, prefix=settings.api_prefix)
app.include_router(schools_router, prefix=settings.api_prefix)
app.include_router(leadership_router, prefix=settings.api_prefix)
app.include_router(me_router, prefix=settings.api_prefix)
app.include_router(admin_router, prefix=settings.api_prefix)
app.include_router(students_router, prefix=settings.api_prefix)
app.include_router(notifications_router, prefix=settings.api_prefix)
app.include_router(risk_router, prefix=settings.api_prefix)
app.include_router(calendar_router, prefix=settings.api_prefix)
