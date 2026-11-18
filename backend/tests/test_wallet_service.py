"""WalletService: every way credits can move, and every way they must not."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.enums import ReservationStatus, TransactionType
from app.models.wallet import WalletTransaction
from app.services.wallet import (
    DuplicateRequest,
    IdempotencyConflict,
    InsufficientCredits,
    InvalidAmount,
    ReservationNotActive,
    ReservationNotFound,
    WalletNotFound,
    WalletService,
)
from app.services.wallet.maintenance import expire_stale_reservations
from tests.support import (
    MutableClock,
    assert_ledger_consistent,
    create_user_with_wallet,
    wallet_state,
)

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]
TTL = timedelta(minutes=5)


async def ledger(session_factory: Sessions, user_id: uuid.UUID) -> list[WalletTransaction]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(WalletTransaction)
            .where(WalletTransaction.user_id == user_id)
            .order_by(WalletTransaction.id)
        )
        return list(rows.all())


# --------------------------------------------------------------------- the spec


async def test_specification_wallet_example(session_factory: Sessions, clock: MutableClock) -> None:
    """Previous 2,000 + 800 + 150 + 1,000 - 650 AI usage = 3,300 WC available."""
    user = await create_user_with_wallet(session_factory, clock, balance=2000)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        wallets = WalletService(session, clock)
        await wallets.reward(user, 800, idempotency_key="mon-steps", description="Monday steps")
        await wallets.reward(user, 150, idempotency_key="mon-workout", description="Monday workout")
        await wallets.reward(user, 1000, idempotency_key="tue-steps", description="Tuesday steps")
        await wallets.reserve(user, 900, request_id=request_id, ttl=TTL)
        settlement = await wallets.settle(request_id, 650)

    assert (settlement.charged, settlement.released, settlement.shortfall) == (650, 250, 0)
    wallet = await wallet_state(session_factory, user)
    assert wallet.available_balance == 3300
    assert wallet.reserved_balance == 0
    assert wallet.lifetime_earned == 1950
    assert wallet.lifetime_spent == 650
    await assert_ledger_consistent(session_factory, clock)


# ---------------------------------------------------------------------- rewards


async def test_reward_credits_wallet_and_writes_one_ledger_row(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock)
    async with session_factory() as session, session.begin():
        tx = await WalletService(session, clock).reward(
            user, 950, idempotency_key="reward:2026-11-12", reference_type="daily_reward"
        )

    assert tx.transaction_type == TransactionType.ACTIVITY_REWARD.value
    assert (tx.amount, tx.reserved_delta, tx.balance_after, tx.reserved_after) == (950, 0, 950, 0)
    assert tx.created_at == clock.now()
    rows = await ledger(session_factory, user)
    assert [r.amount for r in rows] == [950]
    await assert_ledger_consistent(session_factory, clock)


async def test_reward_replay_with_same_key_pays_nothing(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """Repeating a synchronisation must never pay the same activity twice."""
    user = await create_user_with_wallet(session_factory, clock)
    for _ in range(5):
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).reward(user, 800, idempotency_key="day-1")

    wallet = await wallet_state(session_factory, user)
    assert wallet.available_balance == 800
    assert wallet.lifetime_earned == 800
    assert len(await ledger(session_factory, user)) == 1


async def test_reward_key_reused_for_a_different_amount_is_rejected(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock)
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reward(user, 800, idempotency_key="day-1")
    with pytest.raises(IdempotencyConflict):
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).reward(user, 900, idempotency_key="day-1")
    assert (await wallet_state(session_factory, user)).available_balance == 800


@pytest.mark.parametrize("amount", [0, -1, -500])
async def test_reward_must_be_positive(
    session_factory: Sessions, clock: MutableClock, amount: int
) -> None:
    user = await create_user_with_wallet(session_factory, clock)
    with pytest.raises(InvalidAmount):
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).reward(user, amount, idempotency_key="k")
    assert await ledger(session_factory, user) == []


@pytest.mark.parametrize("amount", [1.5, 100.0, "100", True, None])
async def test_non_integer_amounts_are_rejected(
    session_factory: Sessions, clock: MutableClock, amount: object
) -> None:
    """Walk Credits are integers. A float can never reach the ledger."""
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    async with session_factory() as session:
        wallets = WalletService(session, clock)
        with pytest.raises(InvalidAmount):
            await wallets.reward(user, amount, idempotency_key="k")  # type: ignore[arg-type]
        with pytest.raises(InvalidAmount):
            await wallets.reserve(user, amount, request_id=uuid.uuid4(), ttl=TTL)  # type: ignore[arg-type]
        with pytest.raises(InvalidAmount):
            await wallets.adjust(
                user,
                amount,  # type: ignore[arg-type]
                kind=TransactionType.ADMIN_ADJUSTMENT,
                reason="test",
                actor_id=None,
            )
    assert (await wallet_state(session_factory, user)).available_balance == 1000


async def test_operations_on_a_missing_wallet_fail(
    session_factory: Sessions, clock: MutableClock
) -> None:
    async with session_factory() as session:
        wallets = WalletService(session, clock)
        with pytest.raises(WalletNotFound):
            await wallets.reward(uuid.uuid4(), 10, idempotency_key="k")
        with pytest.raises(WalletNotFound):
            await wallets.get_wallet(uuid.uuid4())


# ------------------------------------------------------------------ reservations


async def test_reserve_moves_credits_from_available_to_reserved(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        reservation = await WalletService(session, clock).reserve(
            user, 400, request_id=request_id, ttl=TTL, description="chat request"
        )

    assert reservation.status == ReservationStatus.ACTIVE.value
    assert reservation.expires_at == clock.now() + TTL
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (600, 400)
    last = (await ledger(session_factory, user))[-1]
    assert last.transaction_type == TransactionType.CREDIT_RESERVATION.value
    assert (last.amount, last.reserved_delta) == (-400, 400)
    assert last.reference_id == reservation.id
    await assert_ledger_consistent(session_factory, clock)


async def test_reserve_exactly_the_whole_balance_is_allowed(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 1000, request_id=uuid.uuid4(), ttl=TTL)
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (0, 1000)


async def test_reserve_more_than_available_is_refused_and_changes_nothing(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    with pytest.raises(InsufficientCredits) as excinfo:
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).reserve(
                user, 1001, request_id=uuid.uuid4(), ttl=TTL
            )

    assert excinfo.value.status_code == 402
    assert excinfo.value.extra == {"required_wc": 1001, "available_wc": 1000}
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (1000, 0)
    assert len(await ledger(session_factory, user)) == 1  # only the funding row


async def test_reserved_credits_cannot_be_reserved_again(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 700, request_id=uuid.uuid4(), ttl=TTL)
    with pytest.raises(InsufficientCredits):
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).reserve(user, 301, request_id=uuid.uuid4(), ttl=TTL)
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 300, request_id=uuid.uuid4(), ttl=TTL)
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (0, 1000)


async def test_request_id_cannot_be_reserved_twice(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 100, request_id=request_id, ttl=TTL)
    with pytest.raises(DuplicateRequest):
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).reserve(user, 100, request_id=request_id, ttl=TTL)
    assert (await wallet_state(session_factory, user)).reserved_balance == 100


# -------------------------------------------------------------------- settlement


@pytest.mark.parametrize(
    ("actual", "charged", "released", "available_after"),
    [
        (650, 650, 250, 350),  # typical: actual below the worst-case hold
        (900, 900, 0, 100),  # actual equals the hold exactly
        (1, 1, 899, 999),  # minimum charge
        (0, 0, 900, 1000),  # nothing billable: everything comes back
    ],
)
async def test_settle_charges_actual_cost_and_releases_the_rest(
    session_factory: Sessions,
    clock: MutableClock,
    actual: int,
    charged: int,
    released: int,
    available_after: int,
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 900, request_id=request_id, ttl=TTL)
    async with session_factory() as session, session.begin():
        settlement = await WalletService(session, clock).settle(request_id, actual)

    assert (settlement.charged, settlement.released, settlement.shortfall) == (charged, released, 0)
    assert settlement.reservation.status == ReservationStatus.SETTLED.value
    assert settlement.reservation.settled_amount == charged
    assert settlement.reservation.released_amount == released
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (available_after, 0)
    assert wallet.lifetime_spent == charged
    await assert_ledger_consistent(session_factory, clock)


async def test_settle_above_the_hold_takes_the_excess_from_available(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 300, request_id=request_id, ttl=TTL)
        settlement = await WalletService(session, clock).settle(request_id, 450)

    assert (settlement.charged, settlement.released, settlement.shortfall) == (450, 0, 0)
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (550, 0)
    await assert_ledger_consistent(session_factory, clock)


async def test_settle_never_drives_the_balance_negative(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """If the cost exceeds hold + balance, the wallet stops at zero and reports the gap."""
    user = await create_user_with_wallet(session_factory, clock, balance=500)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 400, request_id=request_id, ttl=TTL)
        settlement = await WalletService(session, clock).settle(request_id, 700)

    assert (settlement.charged, settlement.released, settlement.shortfall) == (500, 0, 200)
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (0, 0)
    assert wallet.lifetime_spent == 500
    usage = (await ledger(session_factory, user))[-1]
    assert usage.metadata_ == {"shortfall": 200}
    await assert_ledger_consistent(session_factory, clock)


async def test_reservation_cannot_be_settled_twice(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 500, request_id=request_id, ttl=TTL)
        await WalletService(session, clock).settle(request_id, 200)
    with pytest.raises(ReservationNotActive):
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).settle(request_id, 200)

    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.lifetime_spent) == (800, 200)
    await assert_ledger_consistent(session_factory, clock)


async def test_settle_rejects_negative_amounts(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 500, request_id=request_id, ttl=TTL)
    with pytest.raises(InvalidAmount):
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).settle(request_id, -1)
    assert (await wallet_state(session_factory, user)).reserved_balance == 500


async def test_settling_an_unknown_request_fails(
    session_factory: Sessions, clock: MutableClock
) -> None:
    async with session_factory() as session:
        with pytest.raises(ReservationNotFound):
            await WalletService(session, clock).settle(uuid.uuid4(), 10)


# ----------------------------------------------------------------------- release


async def test_release_returns_the_whole_hold(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """A failed AI request costs nothing: the reservation comes back in full."""
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 600, request_id=request_id, ttl=TTL)
    async with session_factory() as session, session.begin():
        reservation = await WalletService(session, clock).release(
            request_id, reason="provider error"
        )

    assert reservation.status == ReservationStatus.RELEASED.value
    assert (reservation.settled_amount, reservation.released_amount) == (0, 600)
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance, wallet.lifetime_spent) == (
        1000,
        0,
        0,
    )
    await assert_ledger_consistent(session_factory, clock)


async def test_release_is_idempotent(session_factory: Sessions, clock: MutableClock) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 600, request_id=request_id, ttl=TTL)
    for _ in range(3):
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).release(request_id)

    assert (await wallet_state(session_factory, user)).available_balance == 1000
    releases = [
        r
        for r in await ledger(session_factory, user)
        if r.transaction_type == TransactionType.CREDIT_RELEASE.value
    ]
    assert len(releases) == 1


async def test_release_after_settlement_does_not_refund_the_charge(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 600, request_id=request_id, ttl=TTL)
        await WalletService(session, clock).settle(request_id, 400)
    async with session_factory() as session, session.begin():
        reservation = await WalletService(session, clock).release(request_id)

    assert reservation.status == ReservationStatus.SETTLED.value
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.lifetime_spent) == (600, 400)


async def test_a_failed_unit_of_work_leaves_no_trace(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """Reserve + crash before commit: the transaction rolls back, nothing is held."""
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    with pytest.raises(RuntimeError, match="simulated crash"):
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).reserve(user, 600, request_id=uuid.uuid4(), ttl=TTL)
            raise RuntimeError("simulated crash")

    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (1000, 0)
    assert len(await ledger(session_factory, user)) == 1
    await assert_ledger_consistent(session_factory, clock)


# ------------------------------------------------------------------------ expiry


async def test_expired_reservations_are_released_by_the_sweep(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    stale, fresh = uuid.uuid4(), uuid.uuid4()
    async with session_factory() as session, session.begin():
        wallets = WalletService(session, clock)
        await wallets.reserve(user, 300, request_id=stale, ttl=timedelta(minutes=2))
        await wallets.reserve(user, 200, request_id=fresh, ttl=timedelta(minutes=30))

    clock.advance(minutes=1)
    assert await expire_stale_reservations(session_factory, clock) == 0

    clock.advance(minutes=5)
    assert await expire_stale_reservations(session_factory, clock) == 1
    assert await expire_stale_reservations(session_factory, clock) == 0  # nothing left to do

    async with session_factory() as session:
        wallets = WalletService(session, clock)
        assert (await wallets.get_reservation(stale)).status == ReservationStatus.EXPIRED.value
        assert (await wallets.get_reservation(fresh)).status == ReservationStatus.ACTIVE.value
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.reserved_balance) == (800, 200)
    await assert_ledger_consistent(session_factory, clock)


async def test_an_expired_reservation_can_no_longer_be_settled(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(
            user, 300, request_id=request_id, ttl=timedelta(minutes=1)
        )
    clock.advance(minutes=2)
    await expire_stale_reservations(session_factory, clock)

    with pytest.raises(ReservationNotActive):
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).settle(request_id, 100)
    assert (await wallet_state(session_factory, user)).available_balance == 1000


# ------------------------------------------------------------------- adjustments


async def test_adjustment_credits_and_debits(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock)
    async with session_factory() as session, session.begin():
        wallets = WalletService(session, clock)
        await wallets.adjust(
            user, 500, kind=TransactionType.ADMIN_ADJUSTMENT, reason="goodwill", actor_id=None
        )
        tx = await wallets.adjust(
            user, -200, kind=TransactionType.ADMIN_ADJUSTMENT, reason="correction", actor_id=None
        )

    assert tx.balance_after == 300
    wallet = await wallet_state(session_factory, user)
    # Adjustments are neither "earned" nor "spent".
    assert (wallet.available_balance, wallet.lifetime_earned, wallet.lifetime_spent) == (300, 0, 0)
    await assert_ledger_consistent(session_factory, clock)


async def test_negative_adjustment_cannot_overdraw(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=100)
    with pytest.raises(InsufficientCredits):
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).adjust(
                user, -101, kind=TransactionType.ADMIN_ADJUSTMENT, reason="too much", actor_id=None
            )
    assert (await wallet_state(session_factory, user)).available_balance == 100


async def test_adjustment_requires_a_reason_and_a_nonzero_amount(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock)
    async with session_factory() as session:
        wallets = WalletService(session, clock)
        with pytest.raises(InvalidAmount):
            await wallets.adjust(
                user, 0, kind=TransactionType.ADMIN_ADJUSTMENT, reason="zero", actor_id=None
            )
        with pytest.raises(InvalidAmount):
            await wallets.adjust(
                user, 10, kind=TransactionType.ADMIN_ADJUSTMENT, reason="   ", actor_id=None
            )
        with pytest.raises(ValueError, match="not an adjustment type"):
            await wallets.adjust(
                user, 10, kind=TransactionType.ACTIVITY_REWARD, reason="sneaky", actor_id=None
            )


async def test_adjustment_with_idempotency_key_applies_once(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock)
    for _ in range(3):
        async with session_factory() as session, session.begin():
            await WalletService(session, clock).adjust(
                user,
                250,
                kind=TransactionType.ADMIN_ADJUSTMENT,
                reason="one-off",
                actor_id=None,
                idempotency_key="adjust-2026-11",
            )
    assert (await wallet_state(session_factory, user)).available_balance == 250


# --------------------------------------------------------------------- reversals


async def test_reward_reversal_takes_credits_back(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock)
    async with session_factory() as session, session.begin():
        wallets = WalletService(session, clock)
        await wallets.reward(user, 950, idempotency_key="day-1")
        tx = await wallets.reverse_reward(user, 150, idempotency_key="day-1:exclude-workout")

    assert tx is not None
    assert tx.amount == -150
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.lifetime_earned) == (800, 800)
    await assert_ledger_consistent(session_factory, clock)


async def test_reward_reversal_is_capped_at_the_available_balance(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """Credits already spent cannot be clawed back; the wallet stops at zero."""
    user = await create_user_with_wallet(session_factory, clock)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        wallets = WalletService(session, clock)
        await wallets.reward(user, 500, idempotency_key="day-1")
        await wallets.reserve(user, 400, request_id=request_id, ttl=TTL)
        await wallets.settle(request_id, 400)
        tx = await wallets.reverse_reward(user, 300, idempotency_key="day-1:reverse")

    assert tx is not None
    assert tx.amount == -100
    assert tx.metadata_ == {"requested": 300, "uncollected": 200}
    assert (await wallet_state(session_factory, user)).available_balance == 0
    await assert_ledger_consistent(session_factory, clock)


async def test_reward_reversal_with_nothing_to_collect_is_a_no_op(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock)
    async with session_factory() as session, session.begin():
        result = await WalletService(session, clock).reverse_reward(
            user, 100, idempotency_key="nothing"
        )
    assert result is None
    assert await ledger(session_factory, user) == []


async def test_reward_reversal_cannot_claw_back_adjustments(
    session_factory: Sessions, clock: MutableClock
) -> None:
    user = await create_user_with_wallet(session_factory, clock, balance=1000)  # an adjustment
    async with session_factory() as session, session.begin():
        wallets = WalletService(session, clock)
        await wallets.reward(user, 100, idempotency_key="day-1")
        tx = await wallets.reverse_reward(user, 600, idempotency_key="day-1:reverse")

    assert tx is not None
    assert tx.amount == -100  # only what was earned
    wallet = await wallet_state(session_factory, user)
    assert (wallet.available_balance, wallet.lifetime_earned) == (1000, 0)
    await assert_ledger_consistent(session_factory, clock)


# ---------------------------------------------------------------- reconciliation


async def test_reconcile_reports_a_clean_ledger(
    session_factory: Sessions, clock: MutableClock
) -> None:
    for balance in (0, 500, 1500):
        await create_user_with_wallet(session_factory, clock, balance=balance)
    async with session_factory() as session:
        report = await WalletService(session, clock).reconcile()
    assert report.ok
    assert report.wallets_checked == 3


async def test_reconcile_detects_a_tampered_balance(
    session_factory: Sessions, clock: MutableClock
) -> None:
    """A balance edited behind the ledger's back is caught and described."""
    from sqlalchemy import text

    honest = await create_user_with_wallet(session_factory, clock, balance=500)
    victim = await create_user_with_wallet(session_factory, clock, balance=500)
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE wallet_accounts SET available_balance = 999999 WHERE user_id = :u"),
            {"u": victim},
        )

    async with session_factory() as session:
        wallets = WalletService(session, clock)
        report = await wallets.reconcile()
        assert (await wallets.reconcile(honest)).ok

    assert not report.ok
    assert report.wallets_checked == 2
    assert [(d.user_id, d.check, d.expected, d.actual) for d in report.discrepancies] == [
        (victim, "available_balance = sum(amount)", 500, 999999)
    ]


async def test_reconcile_detects_a_hold_without_a_reservation(
    session_factory: Sessions, clock: MutableClock
) -> None:
    from sqlalchemy import text

    user = await create_user_with_wallet(session_factory, clock, balance=500)
    request_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await WalletService(session, clock).reserve(user, 200, request_id=request_id, ttl=TTL)
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE wallet_reservations SET status = 'released', closed_at = now() "
                "WHERE request_id = :r"
            ),
            {"r": request_id},
        )

    async with session_factory() as session:
        report = await WalletService(session, clock).reconcile()
    assert [d.check for d in report.discrepancies] == ["reserved_balance = active holds"]
