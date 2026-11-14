"""Reward rules: which ones apply to a day, and how an administrator changes them.

Rules are versioned by activity date. The administrator can never change what a day
that has already begun is worth:

* a new rule may only start on a date that has not begun anywhere on Earth;
* a rule in force can only be given an end date (it is closed and succeeded, never
  rewritten);
* only a rule that has not started yet can be edited freely.

"Changing reward rates must not retroactively alter historical rewards" therefore holds
by construction, and the database's EXCLUDE constraint guarantees that exactly one rule
of each kind applies to any date.
"""

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.models.enums import RewardActivityType
from app.models.rewards import RewardRule
from app.services.rewards.calculator import RuleSet

# The furthest-ahead timezone is UTC+14, so a local date is at most one day ahead of
# the UTC date. Two days from the UTC date has not started anywhere.
_NOT_STARTED_ANYWHERE = timedelta(days=2)


class RuleInForce(Conflict):
    code = "rule_in_force"
    title = "A rule that is already in force cannot be changed"


class RuleConflict(Conflict):
    code = "rule_conflict"
    title = "The rule would overlap another rule"


async def rules_for_date(session: AsyncSession, day: date) -> RuleSet:
    """The enabled rules whose date range contains ``day``."""
    rows = (
        await session.scalars(
            select(RewardRule).where(
                RewardRule.enabled.is_(True),
                RewardRule.effective_from <= day,
                or_(RewardRule.effective_until.is_(None), RewardRule.effective_until > day),
            )
        )
    ).all()
    by_type = {row.activity_type: row for row in rows}
    steps = by_type.get(RewardActivityType.STEPS.value)
    minutes = by_type.get(RewardActivityType.WORKOUT_MINUTES.value)
    cap = by_type.get(RewardActivityType.DAILY_TOTAL.value)
    return RuleSet(
        steps_rate=steps.conversion_rate if steps else None,
        steps_limit=steps.daily_limit if steps else None,
        minutes_rate=minutes.conversion_rate if minutes else None,
        minutes_limit=minutes.daily_limit if minutes else None,
        daily_cap=cap.daily_limit if cap else None,
        steps_rule_id=steps.id if steps else None,
        minutes_rule_id=minutes.id if minutes else None,
        cap_rule_id=cap.id if cap else None,
    )


class RewardRuleService:
    """Administrative changes to reward rules. The caller commits."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    def earliest_start(self) -> date:
        """The first date a new or edited rule may take effect."""
        return self._clock.now().date() + _NOT_STARTED_ANYWHERE

    async def list_rules(self) -> Sequence[RewardRule]:
        result = await self._session.scalars(
            select(RewardRule).order_by(RewardRule.activity_type, RewardRule.effective_from.desc())
        )
        return result.all()

    async def create_rule(
        self,
        *,
        activity_type: RewardActivityType,
        conversion_rate: Decimal | None,
        daily_limit: int | None,
        effective_from: date,
        created_by: uuid.UUID,
    ) -> RewardRule:
        """Add a rule version. The rule currently open for that type is closed on the
        day the new one starts."""
        self._validate_shape(activity_type, conversion_rate, daily_limit)
        self._require_future(effective_from, "effective_from")

        existing = (
            await self._session.scalars(
                select(RewardRule)
                .where(
                    RewardRule.activity_type == activity_type.value, RewardRule.enabled.is_(True)
                )
                .with_for_update()
            )
        ).all()
        for rule in existing:
            if rule.effective_from >= effective_from:
                raise RuleConflict(
                    f"A {activity_type.value} rule starting {rule.effective_from} already "
                    "exists on or after that date. Edit or end that rule instead."
                )
            if rule.effective_until is None or rule.effective_until > effective_from:
                rule.effective_until = effective_from
        await self._session.flush()

        rule = RewardRule(
            activity_type=activity_type.value,
            conversion_rate=conversion_rate,
            daily_limit=daily_limit,
            effective_from=effective_from,
            created_by=created_by,
            created_at=self._clock.now(),
        )
        self._session.add(rule)
        await self._session.flush()
        await self._session.refresh(rule)  # values as the database stores them
        return rule

    async def update_rule(
        self,
        rule_id: uuid.UUID,
        *,
        conversion_rate: Decimal | None,
        daily_limit: int | None,
        effective_from: date | None,
        effective_until: date | None,
        fields: frozenset[str],
    ) -> RewardRule:
        """Edit a rule. ``fields`` names what the caller actually wants to change."""
        rule = await self._session.scalar(
            select(RewardRule).where(RewardRule.id == rule_id).with_for_update()
        )
        if rule is None:
            raise NotFound("Reward rule not found.")

        started = rule.effective_from < self.earliest_start()
        if started and fields - {"effective_until"}:
            raise RuleInForce(
                "This rule is already in force. It can only be given an end date; "
                "create a new rule to change the rate or limit from a future date."
            )

        if "effective_until" in fields:
            if effective_until is not None:
                self._require_future(effective_until, "effective_until")
            rule.effective_until = effective_until
        if not started:
            if "effective_from" in fields and effective_from is not None:
                self._require_future(effective_from, "effective_from")
                # Keep the timeline continuous: the rule this one succeeds ends on the
                # day this one starts, so move that end date along with it.
                predecessor = await self._predecessor(rule)
                if predecessor is not None and effective_from <= predecessor.effective_from:
                    raise RuleConflict("The rule cannot start before the rule it succeeds.")
                if rule.effective_until is not None and rule.effective_until <= effective_from:
                    raise ValidationFailed("effective_until must be after effective_from.")
                # The two ranges may never overlap, not even between two statements, so
                # the order depends on the direction of the move.
                if effective_from > rule.effective_from:
                    rule.effective_from = effective_from  # vacate the days first...
                    await self._session.flush()
                    if predecessor is not None:
                        predecessor.effective_until = effective_from  # ...then extend into them
                else:
                    if predecessor is not None:
                        predecessor.effective_until = effective_from  # make room first
                        await self._session.flush()
                    rule.effective_from = effective_from
            if "conversion_rate" in fields:
                rule.conversion_rate = conversion_rate
            if "daily_limit" in fields:
                rule.daily_limit = daily_limit
            self._validate_shape(
                RewardActivityType(rule.activity_type), rule.conversion_rate, rule.daily_limit
            )
        if rule.effective_until is not None and rule.effective_until <= rule.effective_from:
            raise ValidationFailed("effective_until must be after effective_from.")
        await self._session.flush()
        await self._session.refresh(rule)
        return rule

    async def cancel_rule(self, rule_id: uuid.UUID) -> RewardRule:
        """Withdraw a rule that has not started. The rule it would have succeeded
        carries on as if the change had never been scheduled."""
        rule = await self._session.scalar(
            select(RewardRule).where(RewardRule.id == rule_id).with_for_update()
        )
        if rule is None:
            raise NotFound("Reward rule not found.")
        if rule.effective_from < self.earliest_start():
            raise RuleInForce(
                "This rule is already in force and cannot be withdrawn. Give it an end date."
            )
        predecessor = await self._predecessor(rule)
        await self._session.delete(rule)
        await self._session.flush()
        if predecessor is not None:
            predecessor.effective_until = rule.effective_until
            await self._session.flush()
        return rule

    async def _predecessor(self, rule: RewardRule) -> RewardRule | None:
        """The rule of the same kind that ends on the day ``rule`` starts."""
        return await self._session.scalar(
            select(RewardRule)
            .where(
                RewardRule.activity_type == rule.activity_type,
                RewardRule.enabled.is_(True),
                RewardRule.id != rule.id,
                RewardRule.effective_until == rule.effective_from,
            )
            .with_for_update()
        )

    def _require_future(self, value: date, name: str) -> None:
        earliest = self.earliest_start()
        if value < earliest:
            raise ValidationFailed(
                f"{name} must be {earliest.isoformat()} or later: a rule can only change days "
                "that have not started yet, so that no existing reward is altered."
            )

    @staticmethod
    def _validate_shape(
        activity_type: RewardActivityType, conversion_rate: Decimal | None, daily_limit: int | None
    ) -> None:
        if activity_type is RewardActivityType.DAILY_TOTAL:
            if conversion_rate is not None:
                raise ValidationFailed("The daily total cap has a limit, not a conversion rate.")
            if daily_limit is None:
                raise ValidationFailed("The daily total cap requires daily_limit.")
        elif conversion_rate is None:
            raise ValidationFailed(f"A {activity_type.value} rule requires conversion_rate.")
