"""The Sibyl layer. Every read and write of escalation memory goes through here.

This is the first module that touches Sibyl, and it is deliberately the only
one. `signature.py` stays pure so the key derivation can be tested without a
store; everything that needs the store is here.

MEMORY IS LOAD-BEARING
======================
There is no path through this module that works without Sibyl. No try/except
degrades to a local dict, no cache answers a recall the store could not, and
no function returns a usable default when the store is missing. If Sibyl is
removed, every entry point raises, the agent has no calibration, and it
escalates every time. That is the intended failure mode, not a bug: an
escalation memory that keeps working without its memory is not doing anything.

The one exception that is caught anywhere in this module is NotFoundError, on
the exact-lookup path, because that is the API's way of saying "no such
pattern" rather than a failure. It is caught narrowly, on one call, and never
around a write.

CapExceededError is never caught. A cap failure means the audit log stopped
recording, which is the loudest condition this system has: the agent would
carry on making decisions with no record of them. It propagates to the caller
untouched.

TENANCY
=======
Dev, test, and spike data never mix. Tests use TEST_TENANT and clean up after
themselves. See CLAUDE.md: the SDK default tenant is a UUID, the CLI derives
its own from the account, and this project passes its tenant explicitly every
time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sibyl_memory_client import MemoryClient, VerdictCode
from sibyl_memory_client.exceptions import NotFoundError

# multi_record_search is the policy engine behind the SDK's search surface. It
# is reachable as a submodule but is NOT exported at package top level and the
# module declares no __all__, so this import is the single place upstream can
# break us. Everything else in this module calls _search_patterns instead, and
# a break upstream is a one-function fix here. See _search_patterns.
from sibyl_memory_client.multi_record import multi_record_search

from signature import (
    FIELD_SPEC,
    VOCABULARIES,
    escalation_signature,
    validate_confidence_threshold,
)

__all__ = [
    "DEV_TENANT",
    "TEST_TENANT",
    "PATTERN_CATEGORY",
    "THRESHOLD_STATE_KEY",
    "PENDING_STATE_KEY",
    "DEFAULT_SEARCH_LIMIT",
    "CONFIDENCE_PRIOR_WEIGHT",
    "VOCABULARY_TOKENS",
    "MemoryNotInitializedError",
    "RecallSource",
    "PatternRecall",
    "open_memory",
    "derive_confidence",
    "recall_pattern",
    "record_outcome",
    "journal_escalation",
    "set_threshold",
    "get_threshold",
]

# Tenants. Plain strings are accepted; there is no UUID validation.
DEV_TENANT = "escalation-memory-dev"
TEST_TENANT = "escalation-memory-test"

PATTERN_CATEGORY = "escalation_pattern"

THRESHOLD_STATE_KEY = "confidence_threshold"
PENDING_STATE_KEY = "pending_escalation"

# multi_record_search defaults limit to 10, where client.search defaults to
# 20. Different from the rest of the SDK surface, so it is passed explicitly
# on every call rather than left to the upstream default, which could move.
DEFAULT_SEARCH_LIMIT = 20

# Additive smoothing weight for derived confidence. See derive_confidence.
CONFIDENCE_PRIOR_WEIGHT = 2.0
CONFIDENCE_PRIOR = 0.5


class MemoryNotInitializedError(RuntimeError):
    """Raised when the store holds no threshold for this tenant.

    Not a convenience error. The threshold is the calibration, and calibration
    lives in Sibyl. Returning a hardcoded default here would be exactly the
    fallback path the hard rules forbid: the agent would keep deciding with a
    number this module invented, and removing Sibyl would stop breaking
    anything. Callers must call set_threshold once, against a real store.
    """


class RecallSource:
    """How a recall was answered. Plain string constants, journal-friendly."""

    EXACT = "exact"
    FUZZY = "fuzzy"
    NONE = "none"


@dataclass
class PatternRecall:
    """The outcome of a recall, carrying enough to make the escalation call.

    `found` alone is never enough. A zero has five different causes, and
    whether a given one means "could not tell" or "not present" is what
    `match_failed` records. For ABSTAINED_ON that depends on the blocking
    token, so the two are not interchangeable. See the verdict table in
    CLAUDE.md.
    """

    signature: str
    found: bool
    source: str
    verdict_code: VerdictCode | None
    body: dict[str, Any] | None = None
    #: True when the search could not answer: a scoring gate, a negation
    #: policy, or an abstention on a token we do not own. The store may well
    #: hold this pattern. Treating that as "new" would mint a duplicate and
    #: split a counter.
    #:
    #: False for an abstention on a token our own vocabularies produced,
    #: which is a confident miss rather than a failure. See
    #: _blocking_token_is_registered.
    match_failed: bool = False
    #: The token the engine reported as blocking, when it reported one. Kept
    #: for the journal so a reclassification is auditable after the fact.
    #: Comes from the query we built, never from a stored record.
    blocking_tokens: list[str] = field(default_factory=list)
    #: Rows the fuzzy pass returned that were not the exact key, for the
    #: journal. Signatures only, never bodies.
    near_misses: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float | None:
        """Stored confidence, or None when nothing was recalled."""
        if not self.body:
            return None
        return self.body.get("confidence")

    @property
    def times_seen(self) -> int:
        return int(self.body.get("times_seen", 0)) if self.body else 0

    def should_auto_handle(self, threshold: float) -> bool:
        """Whether the agent may act without a human.

        Deliberately conservative on every uncertain path: a failed match, a
        cold start, and a genuine miss all fall through to False, so the agent
        escalates. Only a real recall with confidence at or above the
        threshold clears it.
        """
        if self.match_failed or not self.found:
            return False
        confidence = self.confidence
        if confidence is None:
            return False
        return confidence >= validate_confidence_threshold(
            threshold, field_name="threshold"
        )


def open_memory(tenant_id: str = DEV_TENANT, *, path: str | None = None) -> MemoryClient:
    """Open the one client this system uses.

    The tenant is always passed explicitly. Defaulting to the SDK's own
    DEFAULT_TENANT would silently share a namespace with anything else on the
    machine using the default.
    """
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    if path is None:
        return MemoryClient.local(tenant_id=tenant_id)
    return MemoryClient.local(path, tenant_id=tenant_id)


# --------------------------------------------------------------------------
# The single upstream seam
# --------------------------------------------------------------------------


def _search_patterns(
    client: MemoryClient, query: str, *, limit: int = DEFAULT_SEARCH_LIMIT
):
    """The only call to multi_record_search in this project.

    Wrapped for two reasons. First, it is a semi-public engine: reachable as a
    submodule, named without a leading underscore, and documented as the
    canonical search path, but absent from the package's top-level exports and
    from any __all__. If it moves, this function is the only thing that has to
    change.

    Second, it is chosen over client.search on purpose. client.search does not
    enforce AND across query tokens: a query carrying one token with zero
    corpus support still returns rows, so its NO_MATCH is weak evidence and
    cannot anchor a confidence counter. multi_record_search suppresses the
    query and names the blocking token instead. It also reports EMPTY_STORE
    natively, without the extra count probe refine_zero would cost.

    `limit` is passed explicitly because this function's upstream default is
    10 while the rest of the SDK search surface defaults to 20.

    Returns SearchResults, a list subclass carrying .verdict. Never test the
    result for truthiness alone.
    """
    return multi_record_search(client, query, limit=limit)


def _fallback_query(fields: dict[str, Any]) -> str:
    """Build the fuzzy-pass query from registered vocabulary values only.

    Customer text never reaches this function. The values here are the same
    closed-vocabulary identifiers the signature is derived from, so the query
    is agent-internal by construction and an injection-shaped escalation
    cannot steer the search.

    Raises if a value is not registered, rather than passing it through.
    """
    terms: list[str] = []
    for name in sorted(FIELD_SPEC):
        value = fields.get(name)
        if value is None:
            continue
        if value not in VOCABULARIES[name]:
            raise ValueError(
                f"refusing to build a query from unregistered value {value!r} "
                f"for field {name!r}. Only vocabulary values may reach the "
                "search; free text belongs in the entity body."
            )
        terms.append(str(value))
    return " ".join(terms)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


# --------------------------------------------------------------------------
# Recall
# --------------------------------------------------------------------------

#: Causes that always mean "could not tell" rather than "never seen".
#: ABSTAINED_ON is deliberately absent: its meaning depends on what the
#: blocking token was. See _blocking_token_is_registered.
_MATCH_FAILED_CODES = frozenset({
    VerdictCode.GATED,
    VerdictCode.NEGATION_ABSTAIN,
})


# Mirrors the SDK sanitizer's token rule: alphanumerics and underscores are
# token characters, everything else separates.
_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-zA-Z_]+")


def _vocabulary_tokens() -> frozenset[str]:
    """Every FTS token a registered vocabulary value can produce.

    Not the vocabulary values themselves. The FTS5 tokenizer is
    'porter unicode61' and the SDK sanitizer keeps alphanumerics and
    underscores, so a dotted id splits:

        refund.over_limit           -> refund, over_limit
        account.identity_unverified -> account, identity_unverified
        issue_refund                -> issue_refund

    which means the token the engine reports as blocking is often a fragment
    of a registered value rather than the value itself. Comparing against the
    raw vocabulary would miss every dotted policy rule.
    """
    tokens: set[str] = set()
    for vocabulary in VOCABULARIES.values():
        for value in vocabulary:
            for piece in _TOKEN_SPLIT_RE.split(value):
                if piece:
                    tokens.add(piece.lower())
    return frozenset(tokens)


VOCABULARY_TOKENS = _vocabulary_tokens()


def _blocking_token_is_registered(verdict) -> bool:
    """Whether an ABSTAINED_ON was blocked by a term we own.

    ABSTAINED_ON means one content token has zero corpus support anywhere in
    the store. What that IMPLIES depends entirely on where the token came
    from, which is why this reads the token rather than trusting a flag the
    caller passes.

    If the blocking token is one our vocabularies can produce, the engine has
    told us that this exact registered term appears in no stored pattern.
    That is a MORE confident miss than NO_MATCH, not a weaker one: NO_MATCH
    says the scorer found nothing good enough, while this says the term is
    absent from the corpus outright.

    If the blocking token is anything else, some free-text surface put a word
    in the query and the conservative reading stands: the store may well hold
    the pattern and one unsupported word blocked the search.

    Gating on the token rather than on a provenance flag means a future
    free-text caller inherits the conservative reading automatically, with no
    new argument to remember to pass and no way to assert a provenance it does
    not have.
    """
    tokens = getattr(verdict, "tokens", None) or []
    if not tokens:
        # ABSTAINED_ON should always name its token. If it ever does not, we
        # cannot establish provenance, so we stay conservative.
        return False
    return all(token.lower() in VOCABULARY_TOKENS for token in tokens)


def _classify_match_failure(verdict) -> bool:
    """True when a zero means 'could not tell' rather than 'not present'."""
    if verdict.code in _MATCH_FAILED_CODES:
        return True
    if verdict.code is VerdictCode.ABSTAINED_ON:
        return not _blocking_token_is_registered(verdict)
    return False


def recall_pattern(client: MemoryClient, fields: dict[str, Any]) -> PatternRecall:
    """Recall the pattern for one escalation.

    Lookup order per CLAUDE.md: exact key first, then the fuzzy second pass,
    then branch on the verdict.

    The exact hit is the common case and costs one indexed read. The fuzzy
    pass exists because a near-miss pattern is still worth knowing about, and
    because the verdict on a zero distinguishes a cold start from a genuine
    miss from a search that could not answer.

    ABSTAINED_ON is the common outcome here, not NO_MATCH. Our fallback query
    carries only vocabulary terms, so a genuinely new situation is a query
    whose terms have no corpus support, and the engine abstains rather than
    scoring. That reads as a CONFIDENT miss: the engine is reporting that
    these exact registered terms appear in no stored pattern. See
    _blocking_token_is_registered for why the reclassification is gated on
    the blocking token rather than on the caller's word.
    """
    signature = escalation_signature(fields)

    # Exact key. NotFoundError is the miss signal, not a failure, and it
    # raises rather than returning None. This is the only caught exception in
    # the module, and it is caught around this one call so a CapExceededError
    # or StorageError from anywhere else cannot be absorbed by it.
    try:
        row = client.get_entity(PATTERN_CATEGORY, signature)
    except NotFoundError:
        row = None

    if row is not None:
        return PatternRecall(
            signature=signature,
            found=True,
            source=RecallSource.EXACT,
            verdict_code=None,  # no search ran
            body=row["body"],
        )

    # Fuzzy second pass. FTS5 over the entity body, which is where the raw
    # context lives.
    results = _search_patterns(client, _fallback_query(fields))
    verdict_code = results.verdict.code

    # multi_record_search is cross-tier and takes no category or tier filter,
    # so journal rows can and do surface. On those rows `key` is an event
    # UUID and `category` is None, and feeding one to get_entity would raise
    # NotFoundError. Filter before treating any key as a signature.
    matches = [
        row
        for row in results
        if row.get("tier") == "entity" and row.get("category") == PATTERN_CATEGORY
    ]

    for row in matches:
        if row.get("key") == signature:
            # The exact key surfaced through search but not through
            # get_entity. Should not happen; treat as a fuzzy hit rather than
            # asserting, since the recall is still usable.
            return PatternRecall(
                signature=signature,
                found=True,
                source=RecallSource.FUZZY,
                verdict_code=verdict_code,
                body=row.get("body"),
                near_misses=[r["key"] for r in matches if r.get("key") != signature],
            )

    return PatternRecall(
        signature=signature,
        found=False,
        source=RecallSource.NONE,
        verdict_code=verdict_code,
        body=None,
        match_failed=_classify_match_failure(results.verdict),
        blocking_tokens=list(getattr(results.verdict, "tokens", None) or []),
        near_misses=[row["key"] for row in matches if row.get("key")],
    )


# --------------------------------------------------------------------------
# Pattern write
# --------------------------------------------------------------------------


def derive_confidence(times_seen: int, times_human_agreed: int) -> float:
    """Confidence that the stored resolution is the right one.

    Hand-written. The paid-tier `learn` and `lint` methods are off limits on
    free tier, and every confidence number in this project is computed here.

    Additive smoothing rather than a raw ratio. A raw ratio makes the first
    agreement read as 1.0, which would let the agent auto-handle a pattern it
    has seen exactly once, and the demo would show it doing so. Smoothing
    toward a 0.5 prior costs evidence for confidence:

        1 of 1   -> 0.667      5 of 5   -> 0.857
        2 of 2   -> 0.750      10 of 10 -> 0.917
        9 of 10  -> 0.833      0 of 3   -> 0.200

    so a threshold of 0.8 needs roughly four consecutive agreements before the
    agent stops asking, which is the behaviour the escalation story wants.
    """
    if times_seen < 0 or times_human_agreed < 0:
        raise ValueError("counters cannot be negative")
    if times_human_agreed > times_seen:
        raise ValueError(
            f"times_human_agreed ({times_human_agreed}) cannot exceed "
            f"times_seen ({times_seen})"
        )
    if times_seen == 0:
        return CONFIDENCE_PRIOR
    numerator = times_human_agreed + CONFIDENCE_PRIOR_WEIGHT * CONFIDENCE_PRIOR
    denominator = times_seen + CONFIDENCE_PRIOR_WEIGHT
    return round(numerator / denominator, 4)


def record_outcome(
    client: MemoryClient,
    fields: dict[str, Any],
    *,
    resolution: str,
    human_agreed: bool,
    raw_context: str | None = None,
) -> dict[str, Any]:
    """Create or update the pattern entity after a human has decided.

    `raw_context` is the free customer text. It goes in the body, never in the
    key, and it is what FTS5 serves on the fuzzy pass. Passing it is optional
    but recommended: without it the second pass has only vocabulary terms to
    match on.

    CapExceededError from set_entity propagates. A pattern that cannot be
    written is a pattern that will be re-learned from scratch, and silently
    swallowing that would leave the agent looking calibrated when it is not.
    """
    if not isinstance(resolution, str) or not resolution.strip():
        raise ValueError("resolution must be a non-empty string")

    signature = escalation_signature(fields)

    try:
        existing = client.get_entity(PATTERN_CATEGORY, signature)["body"]
    except NotFoundError:
        existing = None

    times_seen = int(existing["times_seen"]) + 1 if existing else 1
    times_agreed = int(existing["times_human_agreed"]) if existing else 0
    times_overridden = int(existing["times_overridden"]) if existing else 0
    if human_agreed:
        times_agreed += 1
    else:
        times_overridden += 1

    body = {
        "resolution": resolution,
        "times_seen": times_seen,
        "times_human_agreed": times_agreed,
        "times_overridden": times_overridden,
        "confidence": derive_confidence(times_seen, times_agreed),
        "last_outcome": "agreed" if human_agreed else "overridden",
        "last_seen": _utc_now(),
        "fields": dict(fields),
    }
    if raw_context is not None:
        body["raw_context"] = str(raw_context)
    elif existing and "raw_context" in existing:
        body["raw_context"] = existing["raw_context"]

    client.set_entity(PATTERN_CATEGORY, signature, body)
    return body


# --------------------------------------------------------------------------
# Journal write
# --------------------------------------------------------------------------


def journal_escalation(
    client: MemoryClient,
    recall: PatternRecall,
    *,
    threshold: float,
    escalated: bool,
    resolution: str | None = None,
    forwarded: dict[str, Any] | None = None,
) -> str:
    """Append one event to the decision audit log.

    One event per escalation instance, using the semantic channels:
    `evaluated` for what the agent considered, `acted` for what it did,
    `forward` for what it passed to the human.

    The threshold in force is recorded here rather than in the key. It is
    tunable config, so keying on it would fork every pattern on the next tune,
    but it is exactly what makes the decision auditable after the fact.

    CapExceededError propagates. If the journal cannot accept an event the
    audit log has stopped, and the agent must not carry on deciding as though
    it were still recording.
    """
    validate_confidence_threshold(threshold, field_name="threshold")

    evaluated = {
        "signature": recall.signature,
        "recall_source": recall.source,
        "found": recall.found,
        "verdict": recall.verdict_code.value if recall.verdict_code else None,
        "match_failed": recall.match_failed,
        "blocking_tokens": recall.blocking_tokens,
        "confidence": recall.confidence,
        "times_seen": recall.times_seen,
        "threshold": threshold,
        "near_misses": recall.near_misses,
    }
    acted = {
        "escalated": escalated,
        "auto_handled": not escalated,
        "resolution": resolution,
        "ts": _utc_now(),
    }
    return client.write_event(
        evaluated=evaluated,
        acted=acted,
        forward=forwarded if escalated else None,
    )


# --------------------------------------------------------------------------
# HOT state
# --------------------------------------------------------------------------


def set_threshold(client: MemoryClient, threshold: float) -> float:
    """Store the confidence threshold. Validated before it lands."""
    validated = validate_confidence_threshold(threshold, field_name="threshold")
    client.set_state(THRESHOLD_STATE_KEY, {"value": validated, "updated": _utc_now()})
    return validated


def get_threshold(client: MemoryClient) -> float:
    """Read the confidence threshold.

    Raises MemoryNotInitializedError when unset. There is deliberately no
    default: a hardcoded fallback would let the agent calibrate itself without
    the store, which is the failure mode the deletion test exists to catch.
    """
    state = client.get_state(THRESHOLD_STATE_KEY)
    if state is None:
        raise MemoryNotInitializedError(
            f"no {THRESHOLD_STATE_KEY} in state for tenant "
            f"{client.get_tenant()!r}. Call set_threshold once against a real "
            "store before recalling. There is no default by design."
        )
    # get_state returns {"body": ..., "updated_at": ...}, not the bare body.
    return validate_confidence_threshold(
        state["body"]["value"], field_name="threshold"
    )
