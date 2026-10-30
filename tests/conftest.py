from datetime import datetime, timezone

import pytest

OFF_PEAK = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def off_peak():
    return lambda: OFF_PEAK
