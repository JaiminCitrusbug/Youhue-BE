"""System health endpoint. Serves GET /health (the sandbox adapter contract + liveness)."""
from fastapi import APIRouter

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
