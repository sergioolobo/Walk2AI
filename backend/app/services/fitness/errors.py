"""Fitness ingestion errors."""

from app.core.errors import Conflict, Forbidden, NotFound


class BatchIdReused(Conflict):
    code = "batch_id_reused"
    title = "This batch id was already used for different data"


class TestDataDisabled(Forbidden):
    code = "test_data_disabled"
    title = "Test fitness data is not enabled on this instance"


class WorkoutNotFound(NotFound):
    code = "workout_not_found"
    title = "Workout not found"
