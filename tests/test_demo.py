"""Smoke test for the demo script.

The demo is the deliverable the video is recorded against, so it is worth
knowing it still runs before the camera is on rather than after. These tests
drive it exactly the way the video does: three separate process invocations,
not three function calls, because "the only thing crossing the process
boundary is Sibyl" is the claim being made and an in-process test could not
demonstrate it.

Each run gets its own store via ESCALATION_DEMO_DB under tmp_path, so a
recorded take in progress is never clobbered.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parent.parent / "scripts" / "demo.py"


def run(mode: str, db: Path, expect_ok: bool = True) -> str:
    env = {
        **os.environ,
        "ESCALATION_DEMO_DB": str(db),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        [sys.executable, str(DEMO), mode],
        capture_output=True,
        text=True,
        env=env,
    )
    if expect_ok:
        assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.fixture
def db(tmp_path):
    return tmp_path / "demo" / "demo.db"


def test_phase_one_escalates_three_times(db):
    out = run("phase1", db)
    assert out.count(">>> ESCALATED TO A HUMAN") == 3
    assert "humans asked          3" in out
    assert "patterns stored       1" in out
    assert "journal events        3" in out


def test_phase_one_shows_confidence_climbing(db):
    out = run("phase1", db)
    for value in ("0.6667", "0.7500", "0.8000"):
        assert value in out


def test_phase_two_auto_handles_with_no_human(db):
    run("phase1", db)
    out = run("phase2", db)
    assert ">>> ESCALATED TO A HUMAN" not in out
    assert "AUTO HANDLED FROM MEMORY" in out
    assert "humans asked          0" in out


def test_the_signature_is_identical_across_the_process_boundary(db):
    """The same stall, worded differently by four customers, in two
    processes, is one key."""
    first = run("phase1", db)
    second = run("phase2", db)

    def signatures(text):
        return set(re.findall(r"esc\.v\d+\.[a-z0-9-]+\.[0-9a-f]{16}", text))

    assert len(signatures(first)) == 1, signatures(first)
    assert signatures(first) == signatures(second)
    # Three different customer wordings in phase 1, one key.
    assert first.count(">>> ESCALATED TO A HUMAN") == 3


def test_phase_two_reads_what_phase_one_wrote(db):
    run("phase1", db)
    out = run("phase2", db)
    assert "times a human decided 3" in out
    assert "times they agreed     3" in out


def test_no_memory_mode_escalates_the_same_request(db):
    """The deletion test on screen: same code, same request, no patterns."""
    run("phase1", db)
    out = run("no-memory", db)
    assert ">>> ESCALATED TO A HUMAN" in out
    assert "RESULT    ESCALATED" in out
    assert "humans asked          1" in out
    assert "AUTO HANDLED" not in out


def test_no_memory_and_phase_two_differ_only_in_stored_patterns(db):
    """Both run the identical request through the identical code. The only
    difference is whether the tenant has a history."""
    run("phase1", db)
    with_memory = run("phase2", db)
    without = run("no-memory", db)
    assert "AUTO HANDLED FROM MEMORY" in with_memory
    assert "RESULT    ESCALATED" in without
    # Same situation reached in both.
    assert "refund.over_limit" in with_memory
    assert "refund.over_limit" in without


def test_phase_one_is_repeatable(db):
    """Every take starts from a true cold start."""
    first = run("phase1", db)
    second = run("phase1", db)
    assert first.count(">>> ESCALATED TO A HUMAN") == 3
    assert second.count(">>> ESCALATED TO A HUMAN") == 3


def test_phase_two_without_a_store_fails_loudly(db):
    out = run("phase2", db, expect_ok=False)
    assert "Run phase1 first" in out


def test_an_unknown_mode_prints_usage(db):
    out = run("not-a-mode", db, expect_ok=False)
    assert "modes:" in out


def test_each_phase_prints_tenant_store_and_a_distinct_pid(db):
    """The header is what proves on camera that this is one store across two
    real processes."""
    first = run("phase1", db)
    second = run("phase2", db)
    for out in (first, second):
        assert "escalation-memory-demo" in out
        assert str(db) in out
        assert "os pid" in out

    def pid(text):
        line = next(x for x in text.splitlines() if "os pid" in x)
        return line.split()[-1]

    assert pid(first) != pid(second)


def test_demo_does_not_touch_the_dev_or_test_tenants(db):
    out = run("phase1", db) + run("phase2", db) + run("no-memory", db)
    assert "escalation-memory-dev" not in out
    assert "escalation-memory-test" not in out
