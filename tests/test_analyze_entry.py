"""
Minimal integration test for POST /safety/analyze_entry.

Requires:
    pip install pytest httpx

Run:
    pytest tests/test_analyze_entry.py -v

NOTE: pytest is not currently installed in this repo.  This file exists as a
stub so the test can be run once pytest and httpx are added to requirements.txt.
"""

import pytest

# Skip entire module if dependencies are absent
pytest.importorskip("httpx", reason="httpx not installed — pip install pytest httpx")

from fastapi.testclient import TestClient  # noqa: E402

# Import after the importorskip guard so missing httpx gives a clean skip
from ucanrr_sasi_safety_eval_api import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_analyze_entry_returns_sasi_nested_dict(client):
    """HTTP 200, response contains a non-empty 'sasi' dict with is_crisis as bool."""
    resp = client.post(
        "/safety/analyze_entry",
        json={"entry_text": "Today was a hard day.", "user_id": "pytest-001"},
    )
    assert resp.status_code == 200, resp.text

    body = resp.json()

    # Top-level sasi_* keys present
    assert "sasi_risk_level" in body
    assert "sasi_gate_blocked" in body
    assert isinstance(body["sasi_gate_blocked"], bool)

    # Nested sasi dict present and non-empty
    sasi = body.get("sasi")
    assert sasi, "response['sasi'] is absent or empty"
    assert isinstance(sasi, dict)

    # is_crisis equivalent must be a bool
    assert isinstance(sasi["sasi_crisis_detected"], bool), (
        "sasi_crisis_detected should be bool, got %r" % type(sasi["sasi_crisis_detected"])
    )

    # share_allowed must be bool
    assert isinstance(sasi["share_allowed"], bool)

    # sasi_risk_level must be a non-empty string
    assert isinstance(sasi["sasi_risk_level"], str)
    assert sasi["sasi_risk_level"] != ""
