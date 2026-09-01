"""The scripted demo. Three modes, run as separate processes.

    $env:PYTHONIOENCODING="utf-8"
    .venv/Scripts/python.exe scripts/demo.py phase1
    .venv/Scripts/python.exe scripts/demo.py phase2
    .venv/Scripts/python.exe scripts/demo.py no-memory

phase1      Fresh store. Publishes the policy, sets the threshold, and runs
            three identical refund requests that stall on the same rule. A
            scripted human agrees each time. Exits.

phase2      A SEPARATE PROCESS against the same store. One more identical
            request. It auto-handles from memory, with no human involved.
            Nothing is held in memory between the two: the only thing
            crossing the process boundary is Sibyl.

no-memory   Phase two against a tenant that was never written to. Same code,
            same request, no patterns, so it escalates. The deletion test on
            screen.

Human input is scripted, so the run is reproducible take after take.

Output is spaced for a camera rather than for a log file. Everything printed
here is derived from the store or from the agent's own control state.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent import (  # noqa: E402
    AUTO_REFUND_LIMIT,
    HumanDecision,
    RefundRequest,
    check_policy_consistency,
    evaluate_policy,
    handle_request,
    publish_policy,
)
from memory import (  # noqa: E402
    PATTERN_CATEGORY,
    open_memory,
    set_threshold,
)
from signature import escalation_signature  # noqa: E402

DEMO_TENANT = "escalation-memory-demo"
EMPTY_TENANT = "escalation-memory-demo-empty"
THRESHOLD = 0.8

# Overridable so a smoke test can run the three modes without clobbering the
# store a recorded take is using.
DEMO_DB = Path(
    os.environ.get(
        "ESCALATION_DEMO_DB",
        Path(tempfile.gettempdir()) / "escalation-memory-demo" / "demo.db",
    )
)

WIDTH = 74


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------


def blank(n: int = 1) -> None:
    print("\n" * n, end="")


def banner(title: str) -> None:
    blank()
    print("=" * WIDTH)
    print(f"  {title}")
    print("=" * WIDTH)
    blank()


def rule_line() -> None:
    print("  " + "-" * (WIDTH - 4))


def field(label: str, value: object) -> None:
    print(f"    {label:<22}{value}")


def confidence_bar(value: float | None, threshold: float) -> None:
    """Confidence, large and legible, with the threshold marked."""
    blank()
    if value is None:
        print("    CONFIDENCE            none recalled")
        print(f"    THRESHOLD             {threshold:.4f}")
        blank()
        return

    slots = 40
    filled = int(round(value * slots))
    mark = int(round(threshold * slots))
    cells = []
    for i in range(slots):
        if i == mark:
            cells.append("|")
        elif i < filled:
            cells.append("#")
        else:
            cells.append(".")
    verdict = "AT OR ABOVE THRESHOLD" if value >= threshold else "below threshold"
    print(f"    CONFIDENCE            {value:.4f}      {verdict}")
    print(f"                          [{''.join(cells)}]")
    print(f"    THRESHOLD             {threshold:.4f}      (marked | above)")
    blank()


def phase_header(title: str, tenant: str) -> None:
    """Tenant, store, and clock, so the video shows one store across two
    real processes."""
    banner(title)
    field("time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    field("process id", f"os pid {os.getpid()}")
    field("tenant", tenant)
    field("store", DEMO_DB)
    blank()


def describe_request(n: int, request: RefundRequest) -> None:
    rule_line()
    print(f"  REQUEST {n}    {request.request_id}")
    rule_line()
    blank()
    field("amount", f"{request.amount:.2f}")
    field("days since delivery", request.days_since_delivery)
    field("customer says", f'"{request.customer_message}"')
    blank()


def describe_recall(decision) -> None:
    """Say what the recall actually did, in words that survive on camera.

    A bare verdict code is misleading here. The fallback search is cross tier,
    so on a cold start it happily matches the policy document in REFERENCE and
    reports OK while returning no patterns at all. Printing "no pattern
    (verdict: ok)" looks like a contradiction; it is not.
    """
    recall = decision.recall
    field("signature", decision.signature)

    if recall.source == "exact":
        field("recall", "exact key hit, no search needed")
        return

    field("recall", "NO PATTERN for this signature")
    code = recall.verdict_code.value if recall.verdict_code else "none"
    if code == "empty_store":
        field("fallback search", "store is empty, nothing written yet")
    elif code == "ok":
        field("fallback search", "matched other tiers only, no pattern rows")
    elif code == "abstained_on":
        field("fallback search", "these terms appear in no stored pattern")
    else:
        field("fallback search", f"verdict {code}")
    if recall.blocking_tokens:
        field("blocked on", ", ".join(recall.blocking_tokens))


# --------------------------------------------------------------------------
# The scripted human
# --------------------------------------------------------------------------


class ScriptedHuman:
    """Stands in for the human on the other end of the escalation.

    A parameter rather than stdin, so the take is reproducible.
    """

    def __init__(self, resolution: str = "approve_refund"):
        self.resolution = resolution
        self.count = 0

    def __call__(self, request, stall, recall) -> HumanDecision:
        self.count += 1
        print("    >>> ESCALATED TO A HUMAN")
        blank()
        field("they are asked", stall.policy_rule)
        field("agent was about to", stall.pending_action)
        field("human decides", self.resolution)
        blank()
        return HumanDecision(resolution=self.resolution)


def refund_request(request_id: str) -> RefundRequest:
    """The one situation this demo repeats.

    Identical control state every time, so every one of these derives the
    same signature. The wording differs to make the point that the key does
    not come from the customer's words.
    """
    return RefundRequest(
        request_id=request_id,
        amount=AUTO_REFUND_LIMIT + 70.0,
        days_since_delivery=5,
        customer_message=MESSAGES[request_id],
    )


MESSAGES = {
    "req-1": "The jacket arrived torn. I would like my money back please.",
    "req-2": "Item turned up damaged, requesting a full refund.",
    "req-3": "This coat is ripped and I want a refund, this is unacceptable.",
    "req-4": "Package was damaged in transit. Please refund me.",
}


def stored_confidence(client, signature: str) -> float | None:
    from sibyl_memory_client.exceptions import NotFoundError

    try:
        return client.get_entity(PATTERN_CATEGORY, signature)["body"]["confidence"]
    except NotFoundError:
        return None


# --------------------------------------------------------------------------
# Phase one
# --------------------------------------------------------------------------


def phase_one() -> None:
    # A fresh store, so the take starts from a true cold start every time.
    if DEMO_DB.parent.exists():
        shutil.rmtree(DEMO_DB.parent)
    DEMO_DB.parent.mkdir(parents=True, exist_ok=True)

    phase_header("PHASE 1   COLD START. NOTHING IS KNOWN YET.", DEMO_TENANT)

    client = open_memory(DEMO_TENANT, path=str(DEMO_DB))
    publish_policy(client)
    rule_ids = check_policy_consistency(client)
    set_threshold(client, THRESHOLD)

    field("policy published", f"{len(rule_ids)} rules, ids checked against the vocabulary")
    field("threshold set", f"{THRESHOLD:.4f}")
    blank()

    human = ScriptedHuman("approve_refund")

    for n, request_id in enumerate(["req-1", "req-2", "req-3"], start=1):
        request = refund_request(request_id)
        describe_request(n, request)

        stall = evaluate_policy(request)
        field("policy stalls on", stall.policy_rule)
        field("trigger", stall.trigger)
        blank()

        before = stored_confidence(client, escalation_signature(stall.signature_fields()))
        decision = handle_request(client, request, human)
        describe_recall(decision)
        blank()

        after = stored_confidence(client, decision.signature)
        print(f"    confidence before     "
              f"{'none' if before is None else format(before, '.4f')}")
        confidence_bar(after, THRESHOLD)

        field("decision", decision.resolution)
        field("escalated", "yes" if decision.escalated else "NO, auto handled")
        blank(2)

    banner("PHASE 1 COMPLETE")
    field("humans asked", human.count)
    field("patterns stored", len(client.list_entities(PATTERN_CATEGORY)))
    field("journal events", len(client.read_events()))
    blank()
    print("  The process now exits. Nothing is kept in memory.")
    print("  Everything the next process knows, it will read from Sibyl.")
    blank()


# --------------------------------------------------------------------------
# Phase two
# --------------------------------------------------------------------------


def phase_two(tenant: str, title: str) -> None:
    phase_header(title, tenant)

    if not DEMO_DB.exists():
        print("  No demo store found. Run phase1 first.")
        blank()
        raise SystemExit(1)

    client = open_memory(tenant, path=str(DEMO_DB))
    if tenant == EMPTY_TENANT:
        # Same code path, same policy, same threshold. The only thing this
        # tenant does not have is a history.
        publish_policy(client)
        set_threshold(client, THRESHOLD)
        field("patterns in this tenant", len(client.list_entities(PATTERN_CATEGORY)))
        blank()

    human = ScriptedHuman("approve_refund")
    request = refund_request("req-4")
    describe_request(4, request)

    stall = evaluate_policy(request)
    field("policy stalls on", stall.policy_rule)
    field("trigger", stall.trigger)
    blank()
    print("    Same stall as the three requests in phase 1,")
    print("    although the customer worded it differently.")
    blank()

    decision = handle_request(client, request, human)
    describe_recall(decision)
    blank()

    recall = decision.recall
    if recall.found:
        body = recall.body
        field("times a human decided", body["times_seen"])
        field("times they agreed", body["times_human_agreed"])
        field("stored resolution", body["resolution"])
    confidence_bar(recall.confidence, THRESHOLD)

    rule_line()
    if decision.escalated:
        print("  RESULT    ESCALATED. No usable pattern, so a human was needed.")
    else:
        print("  RESULT    AUTO HANDLED FROM MEMORY. No human was involved.")
    rule_line()
    blank()
    field("resolution", decision.resolution)
    field("humans asked", human.count)
    blank()


# --------------------------------------------------------------------------

MODES = {
    "phase1": lambda: phase_one(),
    "phase2": lambda: phase_two(
        DEMO_TENANT, "PHASE 2   NEW PROCESS. THE ONLY THING CARRIED OVER IS SIBYL."
    ),
    "no-memory": lambda: phase_two(
        EMPTY_TENANT, "NO MEMORY   SAME CODE, SAME REQUEST, NO STORED PATTERNS."
    ),
}


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in MODES:
        print(__doc__)
        print(f"  modes: {', '.join(MODES)}")
        raise SystemExit(2)
    MODES[mode]()


if __name__ == "__main__":
    main()
