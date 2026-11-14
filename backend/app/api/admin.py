"""Administration endpoints. Every route requires the admin role; every write is audited."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select

from app.api.deps import (
    AdminDep,
    ClockDep,
    IngestionServiceDep,
    RequestMetaDep,
    RewardRuleServiceDep,
    SessionDep,
    WalletServiceDep,
    require_admin,
)
from app.api.wallet import transaction_response
from app.core.errors import NotFound
from app.models.audit import AuditLog
from app.models.enums import ActorType, TransactionType
from app.models.fitness import WorkoutTypeRule
from app.models.rewards import RewardRule
from app.models.user import User
from app.schemas.fitness import RewardResult
from app.schemas.rewards import (
    RecalculateRequest,
    RewardRuleCreate,
    RewardRuleResponse,
    RewardRuleUpdate,
    WorkoutTypeResponse,
    WorkoutTypeUpdate,
)
from app.schemas.wallet import (
    AdjustmentRequest,
    AuditEntryResponse,
    AuditPage,
    DiscrepancyResponse,
    ReconciliationResponse,
    TransactionResponse,
)
from app.services.audit import record_audit
from app.services.fitness.ingestion import reward_payload
from app.services.fitness.model import VALID_WORKOUT_TYPE, normalise_workout_type

# The dependency is on the router itself: a route added here cannot forget it.
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post(
    "/wallet/adjustments",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def adjust_wallet(
    body: AdjustmentRequest,
    admin: AdminDep,
    session: SessionDep,
    wallets: WalletServiceDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> TransactionResponse:
    """Credit or debit a wallet by hand. Requires a reason; cannot overdraw."""
    if await session.get(User, body.user_id) is None:
        raise NotFound("User not found.")
    transaction = await wallets.adjust(
        body.user_id,
        body.amount_wc,
        kind=TransactionType.ADMIN_ADJUSTMENT,
        reason=body.reason,
        actor_id=admin.user.id,
        idempotency_key=body.idempotency_key,
    )
    record_audit(
        session,
        clock,
        action="wallet.admin_adjustment",
        actor_type=ActorType.ADMIN,
        actor_user_id=admin.user.id,
        entity_type="wallet_transaction",
        entity_id=str(transaction.id),
        details={
            "user_id": str(body.user_id),
            "amount_wc": body.amount_wc,
            "reason": body.reason,
            "balance_after_wc": transaction.balance_after,
        },
        meta=meta,
    )
    await session.commit()
    return transaction_response(transaction)


@router.get("/wallet/reconcile", response_model=ReconciliationResponse)
async def reconcile_wallets(wallets: WalletServiceDep) -> ReconciliationResponse:
    """Check every wallet's balances against the ledger. Read-only."""
    report = await wallets.reconcile()
    return ReconciliationResponse(
        ok=report.ok,
        wallets_checked=report.wallets_checked,
        discrepancies=[DiscrepancyResponse.model_validate(d) for d in report.discrepancies],
    )


@router.get("/audit-log", response_model=AuditPage)
async def audit_log(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[int | None, Query(ge=1)] = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
) -> AuditPage:
    query = select(AuditLog)
    if cursor is not None:
        query = query.where(AuditLog.id < cursor)
    if action is not None:
        query = query.where(AuditLog.action == action)
    rows = (await session.scalars(query.order_by(AuditLog.id.desc()).limit(limit + 1))).all()
    next_cursor = rows[limit - 1].id if len(rows) > limit else None
    return AuditPage(
        items=[AuditEntryResponse.model_validate(r) for r in rows[:limit]],
        next_cursor=next_cursor,
    )


# ------------------------------------------------------------------ reward rules


def _rule_response(rule: RewardRule, earliest_start: date) -> RewardRuleResponse:
    """A rule and where it stands relative to the earliest date that may still change."""
    if rule.effective_from >= earliest_start:
        state = "scheduled"
    elif rule.effective_until is not None and rule.effective_until < earliest_start:
        state = "ended"
    else:
        state = "in_force"
    return RewardRuleResponse(
        id=rule.id,
        activity_type=rule.activity_type,
        conversion_rate=rule.conversion_rate,
        daily_limit=rule.daily_limit,
        effective_from=rule.effective_from,
        effective_until=rule.effective_until,
        enabled=rule.enabled,
        state=state,
        created_at=rule.created_at,
    )


def _rule_details(rule: RewardRule) -> dict[str, str | int | None]:
    return {
        "activity_type": rule.activity_type,
        "conversion_rate": (
            format(rule.conversion_rate, "f") if rule.conversion_rate is not None else None
        ),
        "daily_limit": rule.daily_limit,
        "effective_from": rule.effective_from.isoformat(),
        "effective_until": rule.effective_until.isoformat() if rule.effective_until else None,
    }


@router.get("/reward-rules", response_model=list[RewardRuleResponse])
async def list_reward_rules(rules: RewardRuleServiceDep) -> list[RewardRuleResponse]:
    """Every rule version: past, in force and scheduled."""
    earliest = rules.earliest_start()
    return [_rule_response(rule, earliest) for rule in await rules.list_rules()]


@router.post(
    "/reward-rules", response_model=RewardRuleResponse, status_code=status.HTTP_201_CREATED
)
async def create_reward_rule(
    body: RewardRuleCreate,
    admin: AdminDep,
    session: SessionDep,
    rules: RewardRuleServiceDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> RewardRuleResponse:
    """Schedule a new rule version. It can only start on a day that has not begun yet,
    so no reward that already exists is ever changed. The rule currently open for that
    activity ends on the day the new one starts."""
    rule = await rules.create_rule(
        activity_type=body.activity_type,
        conversion_rate=body.conversion_rate,
        daily_limit=body.daily_limit,
        effective_from=body.effective_from,
        created_by=admin.user.id,
    )
    record_audit(
        session,
        clock,
        action="reward_rule.create",
        actor_type=ActorType.ADMIN,
        actor_user_id=admin.user.id,
        entity_type="reward_rule",
        entity_id=rule.id,
        details=_rule_details(rule),
        meta=meta,
    )
    await session.commit()
    return _rule_response(rule, rules.earliest_start())


@router.put("/reward-rules/{rule_id}", response_model=RewardRuleResponse)
async def update_reward_rule(
    rule_id: uuid.UUID,
    body: RewardRuleUpdate,
    admin: AdminDep,
    session: SessionDep,
    rules: RewardRuleServiceDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> RewardRuleResponse:
    """Edit a rule. One that has not started can be changed freely; one in force can
    only be given an end date."""
    rule = await rules.update_rule(
        rule_id,
        conversion_rate=body.conversion_rate,
        daily_limit=body.daily_limit,
        effective_from=body.effective_from,
        effective_until=body.effective_until,
        fields=frozenset(body.model_fields_set),
    )
    record_audit(
        session,
        clock,
        action="reward_rule.update",
        actor_type=ActorType.ADMIN,
        actor_user_id=admin.user.id,
        entity_type="reward_rule",
        entity_id=rule.id,
        details={"changed": sorted(body.model_fields_set), **_rule_details(rule)},
        meta=meta,
    )
    await session.commit()
    return _rule_response(rule, rules.earliest_start())


@router.delete("/reward-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_reward_rule(
    rule_id: uuid.UUID,
    admin: AdminDep,
    session: SessionDep,
    rules: RewardRuleServiceDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> Response:
    """Withdraw a scheduled rule before it starts. Rules in force cannot be deleted."""
    rule = await rules.cancel_rule(rule_id)
    record_audit(
        session,
        clock,
        action="reward_rule.cancel",
        actor_type=ActorType.ADMIN,
        actor_user_id=admin.user.id,
        entity_type="reward_rule",
        entity_id=rule_id,
        details=_rule_details(rule),
        meta=meta,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------- workout types


@router.get("/workout-types", response_model=list[WorkoutTypeResponse])
async def list_workout_types(session: SessionDep) -> list[WorkoutTypeRule]:
    """How each workout type is treated: step_based, non_step or excluded."""
    result = await session.scalars(select(WorkoutTypeRule).order_by(WorkoutTypeRule.workout_type))
    return list(result.all())


@router.put("/workout-types/{workout_type}", response_model=WorkoutTypeResponse)
async def classify_workout_type(
    workout_type: str,
    body: WorkoutTypeUpdate,
    admin: AdminDep,
    session: SessionDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> WorkoutTypeRule:
    """Classify a workout type. Unclassified types are stored but earn nothing, so a
    type the watch reports under an unexpected name can only ever under-reward.

    Takes effect the next time a day is evaluated (its next sync, or a recalculation).
    """
    name = normalise_workout_type(workout_type)
    if not VALID_WORKOUT_TYPE.match(name):
        raise NotFound("That is not a valid workout type name.")
    now = clock.now()
    rule = await session.get(WorkoutTypeRule, name)
    previous = rule.category if rule else None
    if rule is None:
        rule = WorkoutTypeRule(workout_type=name, category=body.category.value, updated_at=now)
        session.add(rule)
    else:
        rule.category = body.category.value
        rule.updated_at = now
    record_audit(
        session,
        clock,
        action="workout_type.classify",
        actor_type=ActorType.ADMIN,
        actor_user_id=admin.user.id,
        entity_type="workout_type",
        entity_id=name,
        details={"from": previous, "to": body.category.value},
        meta=meta,
    )
    await session.commit()
    return rule


@router.post("/rewards/recalculate", response_model=list[RewardResult])
async def recalculate_rewards(
    body: RecalculateRequest,
    admin: AdminDep,
    session: SessionDep,
    ingestion: IngestionServiceDep,
    clock: ClockDep,
    meta: RequestMetaDep,
) -> list[RewardResult]:
    """Re-evaluate a user's days, for example after classifying a workout type.

    Follows exactly the rules a sync follows: only days inside the reward window can
    be paid, each under its own date's rules and cap, and nothing is ever paid twice.
    """
    user = await session.get(User, body.user_id)
    if user is None:
        raise NotFound("User not found.")
    rewards = await ingestion.recalculate(user, body.date_from, body.date_to)
    record_audit(
        session,
        clock,
        action="rewards.recalculate",
        actor_type=ActorType.ADMIN,
        actor_user_id=admin.user.id,
        entity_type="user",
        entity_id=user.id,
        details={
            "date_from": body.date_from.isoformat(),
            "date_to": body.date_to.isoformat(),
            "awarded_wc": sum(r.paid_now for r in rewards),
        },
        meta=meta,
    )
    await session.commit()
    return [RewardResult.model_validate(reward_payload(r)) for r in rewards]
