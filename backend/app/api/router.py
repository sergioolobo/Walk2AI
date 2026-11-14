"""Assembles the versioned API."""

from fastapi import APIRouter, Depends

from app.api import admin, auth, devices, fitness, health, wallet
from app.api.deps import enforce_same_origin
from app.core.ratelimit import rate_limit

API_PREFIX = "/api/v1"

api_router = APIRouter(
    dependencies=[
        Depends(enforce_same_origin),
        # A generous ceiling for the whole API; sensitive routes add tighter limits.
        Depends(rate_limit("api", limit=600, window_seconds=60)),
    ]
)
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(devices.router)
api_router.include_router(wallet.router)
api_router.include_router(fitness.router)
api_router.include_router(admin.router)
