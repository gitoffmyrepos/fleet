import os
import time

import httpx
import pytest


@pytest.fixture(scope="session")
def fleet_url() -> str:
    url = os.environ.get("FLEET_URL", "http://localhost:18000")
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=2)
            if r.status_code == 200:
                return url
        except httpx.HTTPError:
            pass
        time.sleep(2)
    pytest.fail(f"fleet not ready at {url}")


@pytest.fixture
def headers() -> dict[str, str]:
    return {"authorization": "Bearer test", "content-type": "application/json"}
