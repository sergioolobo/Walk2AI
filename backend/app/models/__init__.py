"""SQLAlchemy models. Importing this package registers every table on Base.metadata."""

from app.models.ai import AIModel, AIUsage, ChatMessage, ChatSession, ExchangeRate, ModelPricing
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.fitness import FitnessDaily, FitnessSyncBatch, WorkoutEvent, WorkoutTypeRule
from app.models.rewards import DailyReward, RewardRule
from app.models.user import AuthSession, DevicePairingCode, DeviceToken, User
from app.models.wallet import SpendingLimits, WalletAccount, WalletReservation, WalletTransaction

__all__ = [
    "AIModel",
    "AIUsage",
    "AuditLog",
    "AuthSession",
    "Base",
    "ChatMessage",
    "ChatSession",
    "DailyReward",
    "DevicePairingCode",
    "DeviceToken",
    "ExchangeRate",
    "FitnessDaily",
    "FitnessSyncBatch",
    "ModelPricing",
    "RewardRule",
    "SpendingLimits",
    "User",
    "WalletAccount",
    "WalletReservation",
    "WalletTransaction",
    "WorkoutEvent",
    "WorkoutTypeRule",
]
