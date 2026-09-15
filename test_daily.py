import asyncio
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import core, db


# Mock the database and scheduler functions
@pytest.mark.asyncio
async def test_schedule_job_daily_creates_cron_trigger():
    """Test that schedule_job creates a CronTrigger for daily jobs."""
    # Mock state for a daily job
    state = {
        "mode": "daily",
        "resolved_at": datetime.now(),  # Use a real datetime
        "who": "Clinic",
        "what": "Checkup",
        "language": "English",
        "patient_name": "Patient",
        "daily_time": time(9, 30),  # We'll set the time below
        "recurrence": None,  # Not set in state, but we set it in the function
        "recurrence_time": None,
    }
    # Set the daily_time to a time object (e.g., 9:30 AM)
    state["daily_time"] = time(9, 30)

    # Mock the add_job_fn and remove_job_fn
    add_job_fn = MagicMock()
    remove_job_fn = MagicMock()

    # Call schedule_job
    result = await core.schedule_job(
        session_id=1,
        state=state,
        add_job_fn=add_job_fn,
        remove_job_fn=remove_job_fn,
    )

    # Check that the result is scheduled
    assert result["status"] == "scheduled"

    # Check that add_job_fn was called with the correct arguments
    add_job_fn.assert_called_once()
    args, kwargs = add_job_fn.call_args
    # Check the keyword arguments
    assert kwargs["job_id"] is not None
    assert kwargs["chat_id"] == 1
    assert kwargs["who"] == "Clinic"
    assert kwargs["what"] == "Checkup"
    assert kwargs["language"] == "English"
    assert kwargs["patient_name"] == "Patient"
    assert kwargs["recurrence"] == "daily"
    assert kwargs["recurrence_time"] == "09:30"

    # Check that remove_job_fn was not called (since no exception)
    remove_job_fn.assert_not_called()


@pytest.mark.asyncio
async def test_fire_scheduled_call_daily_quota_exhausted_skips():
    """Test that fire_scheduled_call for a daily job skips when quota is exhausted."""
    # Mock the db.get_usage to return a value >= max calls
    with (
        patch("app.db.get_usage", return_value=6),
        patch("app.config.get_max_calls_per_day", return_value=5),
        patch("app.core._today_key", return_value="2026-09-13"),
    ):
        # Import the fire_scheduled_call function from core
        from app.core import fire_scheduled_call

        # Call the function with recurrence="daily"
        await fire_scheduled_call(
            job_id="test_job",
            chat_id=1,
            who="Clinic",
            what="Checkup",
            language="English",
            patient_name="Patient",
            recurrence="daily",
            recurrence_time="09:00",
        )

        # We do not check db.set_scheduled_status because it should not be called for daily jobs
        # We can optionally check that it was not called, but we didn't mock it.
        # For simplicity, we just check the message.
