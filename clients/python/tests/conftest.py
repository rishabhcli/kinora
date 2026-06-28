"""Shared test fixtures. All HTTP is mocked via respx — zero live calls."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from kinora import AsyncKinoraClient, KinoraClient, RetryPolicy

BASE_URL = "http://testserver"

# Retries with no real delay so retry tests are instant. The transport's sleep
# uses time.sleep / asyncio.sleep; base_delay_s=0 keeps the jitter window at 0.
FAST_RETRY = RetryPolicy(max_attempts=3, base_delay_s=0.0, max_delay_s=0.0)


@pytest.fixture
def client() -> Iterator[KinoraClient]:
    c = KinoraClient(BASE_URL, retry=FAST_RETRY)
    yield c
    c.close()


@pytest.fixture
async def async_client() -> AsyncKinoraClient:
    return AsyncKinoraClient(BASE_URL, retry=FAST_RETRY)
