"""Device pairing endpoints (the iOS companion's way in)."""

import uuid
from collections.abc import Sequence

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import (
    DeviceServiceDep,
    FitnessDeviceDep,
    PrincipalDep,
    RequestMetaDep,
    SessionDep,
)
from app.core.ratelimit import rate_limit
from app.models.user import DeviceToken
from app.schemas.auth import (
    CurrentDeviceResponse,
    DeviceResponse,
    PairDeviceRequest,
    PairDeviceResponse,
    PairingCodeResponse,
)

router = APIRouter(prefix="/auth/devices", tags=["devices"])


@router.post(
    "/pairing-code",
    response_model=PairingCodeResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("pairing-code", limit=10, window_seconds=60))],
)
async def create_pairing_code(
    principal: PrincipalDep, session: SessionDep, devices: DeviceServiceDep, meta: RequestMetaDep
) -> PairingCodeResponse:
    """Issue a one-time code to type into the companion app. Replaces any unused one."""
    pairing = await devices.create_pairing_code(principal.user, meta)
    await session.commit()
    return PairingCodeResponse(code=pairing.code, expires_at=pairing.expires_at)


@router.post(
    "/pair",
    response_model=PairDeviceResponse,
    status_code=status.HTTP_201_CREATED,
    # The code is short; this limit is what makes guessing it hopeless.
    dependencies=[Depends(rate_limit("pair", limit=10, window_seconds=60))],
)
async def pair_device(
    body: PairDeviceRequest, session: SessionDep, devices: DeviceServiceDep, meta: RequestMetaDep
) -> PairDeviceResponse:
    """Exchange a pairing code for a device token. The token is returned exactly once."""
    paired = await devices.pair(code=body.code, name=body.name, platform=body.platform, meta=meta)
    await session.commit()
    return PairDeviceResponse(
        device=DeviceResponse.model_validate(paired.device), token=paired.token
    )


@router.get("", response_model=list[DeviceResponse])
async def list_devices(principal: PrincipalDep, devices: DeviceServiceDep) -> Sequence[DeviceToken]:
    return await devices.list_devices(principal.user)


@router.get("/current", response_model=CurrentDeviceResponse)
async def current_device(principal: FitnessDeviceDep, session: SessionDep) -> CurrentDeviceResponse:
    """Lets a paired device confirm its token still works (device token required)."""
    await session.commit()  # persists last_used_at
    return CurrentDeviceResponse(
        device=DeviceResponse.model_validate(principal.device),
        user_id=principal.user.id,
        timezone=principal.user.timezone,
    )


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device(
    device_id: uuid.UUID,
    principal: PrincipalDep,
    session: SessionDep,
    devices: DeviceServiceDep,
    meta: RequestMetaDep,
) -> Response:
    """Disconnect a device. Its token stops working immediately."""
    await devices.revoke(principal.user, device_id, meta)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
