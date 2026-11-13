"""Wallet and administration schemas.

Credits are integers (``*_wc``). Money and rates are decimal strings — never floats.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field, PlainSerializer, StringConstraints

from app.schemas.common import RequestModel, ResponseModel

# Serialise Decimal as a plain string in JSON so no client ever parses money as a float.
DecimalString = Annotated[Decimal, PlainSerializer(lambda v: format(v, "f"), return_type=str)]


class WalletResponse(ResponseModel):
    available_wc: int
    reserved_wc: int
    lifetime_earned_wc: int
    lifetime_spent_wc: int
    wc_per_eur: DecimalString | None = Field(
        description="Internal conversion rate in force. Null if none is configured."
    )
    available_eur: DecimalString | None = Field(
        description="EUR equivalent of the available balance. An internal allowance, "
        "not a prepaid balance at any AI provider."
    )
    updated_at: datetime


class TransactionResponse(ResponseModel):
    id: int
    type: str
    amount_wc: int
    reserved_delta_wc: int
    balance_after_wc: int
    reserved_after_wc: int
    description: str
    reference_type: str | None
    reference_id: uuid.UUID | None
    created_at: datetime


class TransactionPage(ResponseModel):
    items: list[TransactionResponse]
    next_cursor: int | None


class PeriodTotals(ResponseModel):
    earned_wc: int = Field(description="Activity rewards, net of reversals.")
    spent_wc: int = Field(description="Credits charged for AI usage.")
    adjustments_wc: int = Field(description="Net manual and administrative adjustments.")


class TodayTotals(PeriodTotals):
    date: date


class MonthTotals(PeriodTotals):
    month: str = Field(description="YYYY-MM")


class WalletSummaryResponse(ResponseModel):
    timezone: str = Field(description="The timezone that defines 'today' and 'this month'.")
    today: TodayTotals
    month: MonthTotals


class ReservationResponse(ResponseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    reserved_wc: int
    settled_wc: int | None
    released_wc: int | None
    status: str
    description: str
    created_at: datetime
    expires_at: datetime
    closed_at: datetime | None


class AdjustmentRequest(RequestModel):
    user_id: uuid.UUID
    amount_wc: int = Field(description="Signed. Positive credits the wallet, negative debits it.")
    reason: Annotated[str, StringConstraints(min_length=3, max_length=500)]
    idempotency_key: Annotated[str, StringConstraints(min_length=8, max_length=100)] | None = None


class DiscrepancyResponse(ResponseModel):
    wallet_id: uuid.UUID
    user_id: uuid.UUID
    check: str
    expected: int
    actual: int


class ReconciliationResponse(ResponseModel):
    ok: bool
    wallets_checked: int
    discrepancies: list[DiscrepancyResponse]


class AuditEntryResponse(ResponseModel):
    id: int
    actor_user_id: uuid.UUID | None
    actor_type: str
    action: str
    entity_type: str | None
    entity_id: str | None
    details: dict[str, Any]
    ip_address: str | None
    created_at: datetime


class AuditPage(ResponseModel):
    items: list[AuditEntryResponse]
    next_cursor: int | None
