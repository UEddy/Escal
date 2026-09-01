"""The support agent. A refund handler that escalates when the policy stalls.

This is the producer the signature was designed for. It is scoped to exactly
the demo scenario: refund requests evaluated against the structured escalation
policy, nothing else. It is not a general agent framework and should not grow
into one.

WHERE THE SIGNATURE FIELDS COME FROM
====================================
The four fields are read out of the agent's own control state at the moment it
stalls. The branch that gave up knows which branch it is, the policy rule that
could not be resolved is a rule id from the policy document, the tool is a
name from the registry, and the pending action is the operation that was about
to be invoked. There is no model call here and no customer text: two customers
phrasing the same problem differently produce one signature, and an
injection-shaped message cannot reach the key.

Customer text goes in the entity body, where FTS5 serves it as the fuzzy
second pass. That is the only place it belongs.

THE GATE
========
Remove Sibyl and this agent still runs, but it can no longer recall anything,
so every stall escalates to a human and no confidence ever accumulates. That
is the whole system reduced to a plain escalation queue, which is the point of
the deletion test. Nothing here caches a pattern, and handle_request cannot
even start without a threshold read from the store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from memory import (
    PatternRecall,
    get_threshold,
    journal_escalation,
    recall_pattern,
    record_outcome,
)
from signature import POLICY_RULES, escalation_signature

__all__ = [
    "POLICY_DOCUMENT",
    "POLICY_REFERENCE_KEY",
    "AUTO_REFUND_LIMIT",
    "RETURNS_WINDOW_DAYS",
    "PolicyConsistencyError",
    "RefundRequest",
    "StallState",
    "HumanDecision",
    "Decision",
    "publish_policy",
    "check_policy_consistency",
    "evaluate_policy",
    "handle_request",
]

POLICY_REFERENCE_KEY = "escalation_policy"

AUTO_REFUND_LIMIT = 50.0
RETURNS_WINDOW_DAYS = 30

# The escalation policy. Structured, with a stable id on every rule, so
# `policy_rule` is a lookup key into this document and never a quotation from
# it. The ids here and the ids in signature.POLICY_RULES are two hands on the
# same rope; check_policy_consistency is what keeps them in step.
#
# `requires_human` marks the rules the agent cannot close on its own. Those
# are the stalls. A rule that does not require a human carries the resolution
# to apply.
POLICY_DOCUMENT: dict[str, Any] = {
    "version": "1",
    "auto_refund_limit": AUTO_REFUND_LIMIT,
    "returns_window_days": RETURNS_WINDOW_DAYS,
    "rules": [
        {
            "id": "refund.under_limit",
            "title": "Small refund inside the returns window",
            "requires_human": False,
            "resolution": "approve_refund",
            "pending_action": "issue_refund",
            "tool": "refund_api",
        },
        {
            "id": "refund.over_limit",
            "title": "Refund above the automatic approval limit",
            "requires_human": True,
            "trigger": "policy_rule_unresolved",
            "pending_action": "issue_refund",
            "tool": "billing_lookup",
        },
        {
            "id": "refund.outside_window",
            "title": "Refund requested after the returns window closed",
            "requires_human": True,
            "trigger": "policy_rule_unresolved",
            "pending_action": "deny_refund",
            "tool": "order_lookup",
        },
        {
            "id": "refund.duplicate_request",
            "title": "A refund for this order was already requested",
            "requires_human": True,
            "trigger": "policy_rule_unresolved",
            "pending_action": "deny_refund",
            "tool": "order_lookup",
        },
        {
            "id": "account.identity_unverified",
            "title": "Identity not established for the requesting account",
            "requires_human": True,
            "trigger": "authorization_required",
            "pending_action": "request_identity_document",
            "tool": "identity_verify",
        },
        {
            "id": "account.closure_requested",
            "title": "Account closure requested alongside the refund",
            "requires_human": True,
            "trigger": "policy_rule_unresolved",
            "pending_action": "close_ticket",
            "tool": "crm_lookup",
        },
        {
            "id": "billing.dispute_open",
            "title": "A billing dispute is already open on this account",
            "requires_human": True,
            "trigger": "conflicting_policy_rules",
            "pending_action": "escalate_to_billing",
            "tool": "billing_lookup",
        },
        {
            "id": "escalation.vip_account",
            "title": "VIP account, refund decisions need a supervisor",
            "requires_human": True,
            "trigger": "authorization_required",
            "pending_action": "escalate_to_supervisor",
            "tool": "crm_lookup",
        },
    ],
}


class PolicyConsistencyError(RuntimeError):
    """The stored policy document and POLICY_RULES have drifted apart.

    Raised at startup rather than at the first escalation. A rule id in the
    document that is not registered would raise UnregisteredValueError deep in
    signature derivation, halfway through handling a real request; a
    registered id missing from the document is a rule the agent can never
    reach. Both are configuration errors and both should stop the process
    before it takes traffic.
    """


@dataclass(frozen=True)
class RefundRequest:
    """One inbound refund request.

    Every field except `customer_message` is structured fact the agent can
    branch on. `customer_message` is the free text: it reaches the entity body
    and nothing else.
    """

    request_id: str
    amount: float
    days_since_delivery: int
    identity_verified: bool = True
    duplicate_request: bool = False
    open_dispute: bool = False
    vip_account: bool = False
    closure_requested: bool = False
    #: Tools the agent could not reach on this run. Drives the
    #: tool_unavailable trigger.
    unavailable_tools: frozenset[str] = frozenset()
    customer_message: str = ""


@dataclass(frozen=True)
class StallState:
    """The agent's control state at the moment it could not proceed.

    This is the whole input to the signature. Built by evaluate_policy from
    the branch that stalled, never from the customer message.
    """

    trigger: str
    policy_rule: str | None
    tool: str | None
    pending_action: str | None
    #: Every rule that matched, for the journal. The signature uses only
    #: `policy_rule`; this records what else was in play.
    matched_rules: tuple[str, ...] = ()

    def signature_fields(self) -> dict[str, Any]:
        """The four fields, in the shape escalation_signature expects."""
        return {
            "trigger": self.trigger,
            "policy_rule": self.policy_rule,
            "tool": self.tool,
            "pending_action": self.pending_action,
        }


@dataclass(frozen=True)
class HumanDecision:
    """What the human decided when the agent handed off."""

    resolution: str
    note: str = ""


@dataclass
class Decision:
    """What the agent did with one request."""

    request_id: str
    resolution: str
    escalated: bool
    #: None when the policy resolved the request without stalling, so no
    #: pattern was ever involved.
    signature: str | None = None
    recall: PatternRecall | None = None
    event_id: str | None = None
    matched_rules: tuple[str, ...] = ()

    @property
    def auto_handled(self) -> bool:
        return not self.escalated


# --------------------------------------------------------------------------
# Policy document, and the check that it matches the vocabulary
# --------------------------------------------------------------------------


def _document_rule_ids() -> set[str]:
    return {rule["id"] for rule in POLICY_DOCUMENT["rules"]}


def publish_policy(client) -> None:
    """Write the policy document to the REFERENCE tier.

    The stored copy is the policy of record. Publishing it here means the
    audit log and the policy an escalation was judged against live in the same
    store.
    """
    client.set_reference(POLICY_REFERENCE_KEY, POLICY_DOCUMENT)


def check_policy_consistency(client) -> set[str]:
    """Compare the STORED policy document against POLICY_RULES.

    This is why the check could not live in signature.py: it reads the policy
    of record out of Sibyl, so it needs a client, and signature.py is pure by
    design.

    Reading the stored copy rather than the module constant is the point. The
    module constant and POLICY_RULES could agree perfectly while the store
    holds a stale document from an older deploy, and the stored one is what a
    human reviewing an escalation would consult.

    Returns the set of rule ids, which are the ids both sides agree on.
    Raises PolicyConsistencyError on any drift in either direction.
    """
    stored = client.get_reference(POLICY_REFERENCE_KEY)
    if stored is None:
        raise PolicyConsistencyError(
            f"no policy document at reference key {POLICY_REFERENCE_KEY!r} for "
            f"tenant {client.get_tenant()!r}. Call publish_policy first. There "
            "is no in-memory fallback by design: the stored document is the "
            "policy of record."
        )
    # Asymmetry worth knowing: set_reference coerces a dict to canonical JSON,
    # but get_reference returns that body as a raw STRING. It does not
    # deserialize the way entity and state bodies do, so the parse is ours.
    body = stored["body"]
    document = json.loads(body) if isinstance(body, str) else body
    stored_ids = {rule["id"] for rule in document["rules"]}

    unregistered = stored_ids - POLICY_RULES
    unreachable = POLICY_RULES - stored_ids
    if unregistered or unreachable:
        problems = []
        if unregistered:
            problems.append(
                "rule id(s) in the stored policy that are not registered in "
                f"POLICY_RULES: {', '.join(sorted(unregistered))}. Deriving a "
                "signature for one of these would raise mid-request."
            )
        if unreachable:
            problems.append(
                "registered POLICY_RULES value(s) absent from the stored "
                f"policy: {', '.join(sorted(unreachable))}. The agent can "
                "never reach these rules."
            )
        raise PolicyConsistencyError(" Also: ".join(problems))
    return stored_ids


# --------------------------------------------------------------------------
# The policy loop
# --------------------------------------------------------------------------


def _rule(rule_id: str) -> dict[str, Any]:
    for rule in POLICY_DOCUMENT["rules"]:
        if rule["id"] == rule_id:
            return rule
    raise KeyError(rule_id)  # pragma: no cover - guarded by the startup check


def _matching_rule_ids(request: RefundRequest) -> list[str]:
    """Which policy rules this request triggers.

    Evaluated in a fixed order so the result is deterministic. Two runs of the
    same request must produce the same rule list, or the signature derived
    from it would not be stable.
    """
    matched: list[str] = []
    if not request.identity_verified:
        matched.append("account.identity_unverified")
    if request.duplicate_request:
        matched.append("refund.duplicate_request")
    if request.days_since_delivery > RETURNS_WINDOW_DAYS:
        matched.append("refund.outside_window")
    if request.amount > AUTO_REFUND_LIMIT:
        matched.append("refund.over_limit")
    if request.open_dispute:
        matched.append("billing.dispute_open")
    if request.vip_account:
        matched.append("escalation.vip_account")
    if request.closure_requested:
        matched.append("account.closure_requested")
    return matched


def evaluate_policy(request: RefundRequest) -> StallState | str:
    """Resolve the request against the policy, or report where it stalled.

    Returns a resolution string when the policy closes the request on its own,
    and a StallState when it cannot. The StallState is read entirely from the
    branch taken here, which is what makes the signature deterministic.
    """
    matched = _matching_rule_ids(request)

    if not matched:
        # Nothing blocking: a small refund inside the window.
        rule = _rule("refund.under_limit")
        needed_tool = rule["tool"]
        if needed_tool in request.unavailable_tools:
            # The agent knows what it wanted to do and which tool it could not
            # reach. That is control state, not a description of a failure.
            return StallState(
                trigger="tool_unavailable",
                policy_rule=rule["id"],
                tool=needed_tool,
                pending_action=rule["pending_action"],
                matched_rules=("refund.under_limit",),
            )
        return rule["resolution"]

    # Sorted so the reported rule does not depend on the order the conditions
    # happen to be written in above. Reordering that function must not fork
    # every stored pattern.
    matched_sorted = tuple(sorted(matched))
    primary = _rule(matched_sorted[0])

    if primary["tool"] in request.unavailable_tools:
        return StallState(
            trigger="tool_unavailable",
            policy_rule=primary["id"],
            tool=primary["tool"],
            pending_action=primary["pending_action"],
            matched_rules=matched_sorted,
        )

    # More than one rule in play is its own kind of stall: the agent cannot
    # tell which one governs.
    trigger = (
        "conflicting_policy_rules" if len(matched_sorted) > 1 else primary["trigger"]
    )
    return StallState(
        trigger=trigger,
        policy_rule=primary["id"],
        tool=primary["tool"],
        pending_action=primary["pending_action"],
        matched_rules=matched_sorted,
    )


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

#: Called when the agent hands off. Takes the request, the control state it
#: stalled in, and whatever memory could recall, and returns the decision.
#: A parameter rather than stdin so the demo is scriptable and reproducible.
AskHuman = Callable[[RefundRequest, StallState, PatternRecall], HumanDecision]


def handle_request(
    client, request: RefundRequest, ask_human: AskHuman
) -> Decision:
    """Handle one refund request end to end.

    The loop: resolve against the policy; on a stall, read control state into
    the four signature fields, derive the key, and recall. A pattern at or
    above the threshold is auto-handled with the stored resolution. Anything
    else escalates, and the human's decision is recorded so the next
    occurrence knows more than this one did.

    The threshold is read from the store first, so a client with no
    initialized state raises before any work is done rather than halfway
    through.
    """
    threshold = get_threshold(client)

    outcome = evaluate_policy(request)
    if isinstance(outcome, str):
        # The policy closed it. No stall, so no pattern and no escalation.
        return Decision(
            request_id=request.request_id,
            resolution=outcome,
            escalated=False,
            matched_rules=("refund.under_limit",),
        )

    stall = outcome
    fields = stall.signature_fields()
    signature = escalation_signature(fields)
    recall = recall_pattern(client, fields)

    forwarded = {
        "request_id": request.request_id,
        "trigger": stall.trigger,
        "policy_rule": stall.policy_rule,
        "pending_action": stall.pending_action,
        "matched_rules": list(stall.matched_rules),
        # The free text reaches the human and the journal, never the key.
        "customer_message": request.customer_message,
    }

    if recall.should_auto_handle(threshold):
        resolution = recall.body["resolution"]
        event_id = journal_escalation(
            client,
            recall,
            threshold=threshold,
            escalated=False,
            resolution=resolution,
        )
        return Decision(
            request_id=request.request_id,
            resolution=resolution,
            escalated=False,
            signature=signature,
            recall=recall,
            event_id=event_id,
            matched_rules=stall.matched_rules,
        )

    decision = ask_human(request, stall, recall)
    if not isinstance(decision, HumanDecision):
        raise TypeError(
            f"ask_human must return a HumanDecision, got "
            f"{type(decision).__name__}"
        )

    # Agreement means the human landed on what memory would have suggested.
    # With nothing recalled there is nothing to disagree with, so the first
    # decision establishes the pattern rather than counting as an override.
    agreed = (not recall.found) or decision.resolution == recall.body["resolution"]

    record_outcome(
        client,
        fields,
        resolution=decision.resolution,
        human_agreed=agreed,
        raw_context=request.customer_message or None,
    )
    event_id = journal_escalation(
        client,
        recall,
        threshold=threshold,
        escalated=True,
        resolution=decision.resolution,
        forwarded=forwarded,
    )
    return Decision(
        request_id=request.request_id,
        resolution=decision.resolution,
        escalated=True,
        signature=signature,
        recall=recall,
        event_id=event_id,
        matched_rules=stall.matched_rules,
    )
