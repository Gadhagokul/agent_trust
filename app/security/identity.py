# app/security/identity.py
"""
Identity resolution for dashboard-backed requests.

Laravel (the trusted internal caller) passes the authenticated user via the
``X-User-Id`` header and, for agent requests, the optional ``X-Agent-Id``
header. FastAPI resolves the caller's role itself from the shared Laravel
database:

    users -> model_has_roles -> roles

and, for agents, the agent record via ``agents.user_id``.

The role is never trusted from the wire: it is always read from the database.
"""

import logging
from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from app.infra.db.repository import AgentRepository
from app.infra.settings import get_settings

logger = logging.getLogger(__name__)

DEV_ENVS = ("development", "local", "test")


@dataclass(frozen=True)
class Identity:
    user_id: int
    role: str  # "admin" | "agent"
    agent_id: int | None = None


def _require_user_id(request: Request) -> int:
    raw = request.headers.get("x-user-id")
    if raw is None or not raw.isdigit() or int(raw) <= 0:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-User-Id header",
        )
    return int(raw)


def _dev_role_from_headers(request: Request) -> str:
    role = request.headers.get("x-user-role", "agent").strip().lower()
    return role if role in ("admin", "agent") else "agent"


def _verify_claimed_agent_id(request: Request, agent_id: int) -> None:
    claimed = request.headers.get("x-agent-id")
    if claimed is None:
        return
    if not claimed.isdigit() or int(claimed) != agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="X-Agent-Id does not match the authenticated user",
        )


def resolve_identity(request: Request, db: Session) -> Identity:
    settings = get_settings()
    user_id = _require_user_id(request)
    is_dev = settings.app_env in DEV_ENVS

    repo = AgentRepository()
    roles: list[str] = []
    try:
        roles = repo.get_user_role(db, user_id, settings.identity_model_type)
    except Exception as exc:
        # In dev environments the seed DB may not include the Laravel role
        # tables; fall back to dev-only headers so local smoke tests work.
        # In production a role-resolution failure is a hard error.
        if not is_dev:
            logger.error("Role resolution failed for user %s: %s", user_id, exc)
            raise
        roles = []

    if is_dev and not roles:
        roles = [_dev_role_from_headers(request)]

    if "admin" in roles:
        return Identity(user_id=user_id, role="admin", agent_id=None)

    if "agent" in roles:
        agent = repo.get_agent_for_user(db, user_id)
        if agent is None or not bool(agent.get("is_active")):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No active agent account for this user",
            )
        agent_id = int(agent["id"])
        _verify_claimed_agent_id(request, agent_id)
        return Identity(user_id=user_id, role="agent", agent_id=agent_id)

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="User has no recognized role (admin or agent)",
    )
