"""Shared auth surface: logout and the current-session probe (/me).

The STUDENT auth surface lives in ``src.routers.student_auth`` (Lane A), the STAFF auth surface
(email/pw, MFA, reset, SSO) in ``src.routers.staff_auth``, and admin (Lane C) in its own module —
the shared INFRA-01 router was decomposed per decision #4 so each surface is module-disjoint. A
student session never resolves a staff route. Thin router — business logic lives in
``src.application.*``.
"""
from fastapi import APIRouter, Response, status

from src.application.auth import sessions
from src.application.me import services as me_svc
from src.infrastructure.middlewares.auth_middleware import AnySessionDep, DbDep, SessionDep
from src.schemas.auth import MeResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(sess: AnySessionDep, db: DbDep) -> Response:
    sessions.revoke_session(db, sess.id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


me_router = APIRouter(tags=["auth"])


@me_router.get("/me", response_model=MeResponse)
def me(sess: SessionDep, db: DbDep) -> MeResponse:
    return me_svc.resolve_me(db, sess)
