"""Authentication, account and device schemas."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import EmailStr, Field, StringConstraints

from app.schemas.common import RequestModel, ResponseModel

# Length only, no composition rules (NIST SP 800-63B). The upper bound keeps a single
# request from forcing an arbitrarily expensive hash.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

# Passwords are never whitespace-trimmed: a leading or trailing space is part of them.
NewPassword = Annotated[
    str,
    StringConstraints(
        min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH, strip_whitespace=False
    ),
]
ExistingPassword = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_PASSWORD_LENGTH, strip_whitespace=False)
]
DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=80)]


class RegisterRequest(RequestModel):
    email: EmailStr
    password: NewPassword
    display_name: DisplayName | None = None


class LoginRequest(RequestModel):
    email: EmailStr
    password: ExistingPassword


class UpdateProfileRequest(RequestModel):
    display_name: DisplayName | None = None
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None


class ChangePasswordRequest(RequestModel):
    current_password: ExistingPassword
    new_password: NewPassword


class UserResponse(ResponseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    timezone: str
    created_at: datetime


class PairingCodeResponse(ResponseModel):
    code: str
    expires_at: datetime


class PairDeviceRequest(RequestModel):
    code: Annotated[str, StringConstraints(min_length=8, max_length=16)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    platform: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,20}$")] = "ios"


class DeviceResponse(ResponseModel):
    id: uuid.UUID
    name: str
    platform: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class PairDeviceResponse(ResponseModel):
    device: DeviceResponse
    token: str = Field(description="Shown once. Store it in the device's secure storage.")


class CurrentDeviceResponse(ResponseModel):
    device: DeviceResponse
    user_id: uuid.UUID
    timezone: str
