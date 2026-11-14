"""Liveness and readiness probes."""

import logging

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import SessionDep

logger = logging.getLogger("walk2ai.health")

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    database: bool
    schema_current: bool


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """The process is up. Touches nothing else."""
    return HealthResponse(status="ok")


@router.get("/health/ready", response_model=ReadinessResponse)
async def ready(request: Request, response: Response, session: SessionDep) -> ReadinessResponse:
    """The database is reachable and migrated to the revision this build expects."""
    database = False
    schema_current = False
    try:
        await session.execute(text("SELECT 1"))
        database = True
        applied = await session.scalar(text("SELECT version_num FROM alembic_version"))
        schema_current = applied == request.app.state.schema_head
    except SQLAlchemyError:
        logger.warning("readiness check failed", exc_info=True)

    ok = database and schema_current
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ok" if ok else "unavailable", database=database, schema_current=schema_current
    )
