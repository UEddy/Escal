"""Tests for the deterministic escalation signature.

Three things have to hold for the memory layer to accumulate confidence on
the right pattern:

1. Determinism. The same escalation state produces the same key on every
   call, in every process, forever. A drifting key silently resets a counter.
2. Order independence. The key depends on the field values, not on the order
   the caller happened to build the mapping in.
3. No partial signatures. A missing, unknown, or unusable field raises. A key
   derived from part of the schema would look valid and poison a pattern.
"""

import os
import random
import re
import subprocess
import sys
import textwrap
import unicodedata
from pathlib import Path

import pytest

from signature import (
    FIELD_SPEC,
    PENDING_ACTIONS,
    POLICY_RULES,
    REQUIRED_FIELDS,
    SIGNATURE_VERSION,
    TOOLS,
    TRIGGERS,
    VOCABULARIES,
    InvalidFieldError,
    MissingFieldError,
    SignatureError,
    UnknownFieldError,
    UnregisteredValueError,
    canonical_form,
    escalation_signature,
    validate_confidence_threshold,
)

# A fully populated escalation: every field carries a value.
FULL = {
    "trigger": "policy_rule_unresolved",
    "policy_rule": "refund.over_limit",
    "tool": "billing_lookup",
    "pending_action": "issue_refund",
}

# A realistic sparse escalation: no tool was involved.
SPARSE = {
    "trigger": "confidence_below_threshold",
    "policy_rule": None,
    "tool": None,
    "pending_action": "close_ticket",
}


def variant(**overrides):
    return {**FULL, **overrides}


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_repeated_calls_agree():
    first = escalation_signature(FULL)
    assert all(escalation_signature(FULL) == first for _ in range(100))


def test_equal_but_distinct_mappings_agree():
    assert escalation_signature(FULL) == escalation_signature(dict(FULL))


def test_signature_is_stable_across_processes_and_hash_seeds():
    """Guards the property a golden value alone cannot: that nothing in the
    derivation reaches PYTHONHASHSEED-dependent iteration order."""
    script = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, sys.argv[1])
        from signature import escalation_signature
        print(escalation_signature({
            "trigger": "policy_rule_unresolved",
            "policy_rule": "refund.over_limit",
            "tool": "billing_lookup",
            "pending_action": "issue_refund",
        }))
        """
    )
    src = str(Path(__file__).resolve().parent.parent / "src")
    seen = set()
    for seed in ("0", "1", "random"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [sys.executable, "-c", script, src],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        seen.add(result.stdout.strip())
    assert seen == {escalation_signature(FULL)}


def test_golden_value():
    """Pins the derivation. This value is a persisted pattern key: changing it
    orphans every stored pattern, so a break here means SIGNATURE_VERSION has
    to be bumped deliberately, not that the constant should be edited."""
    assert (
        escalation_signature(FULL)
        == "esc.v2.policy-rule-unresolved.9ae9cdb8e397597c"
    )


@pytest.mark.parametrize(
    "written",
    ["POLICY_RULE_UNRESOLVED", "  policy_rule_unresolved  ", "Policy_Rule_Unresolved"],
)
def test_case_and_surrounding_whitespace_are_normalized(written):
    assert escalation_signature(variant(trigger=written)) == (
        escalation_signature(FULL)
    )


def test_normalization_runs_before_the_vocabulary_lookup():
    """A caller that shouts a registered id still lands on the pattern. This
    is the whole reach of normalization now: it equates spellings of one id,
    never phrasings of one stall."""
    assert escalation_signature(variant(pending_action="  ISSUE_REFUND ")) == (
        escalation_signature(variant(pending_action="issue_refund"))
    )


def test_every_registered_value_is_canonical():
    """Lookup happens after normalization, so a vocabulary entry that is not
    already canonical is unreachable: nothing a caller passes could ever
    match it. This pins the invariant the vocabularies are written under."""
    for field, vocabulary in VOCABULARIES.items():
        for value in vocabulary:
            assert value == unicodedata.normalize("NFC", value), (field, value)
            assert value == value.lower(), (field, value)
            assert value == " ".join(value.split()), (field, value)
            assert value, field


# ---------------------------------------------------------------------------
# Order independence
# ---------------------------------------------------------------------------


def test_insertion_order_does_not_change_the_key():
    expected = escalation_signature(FULL)
    keys = list(FULL)
    rng = random.Random(20260901)
    for _ in range(25):
        rng.shuffle(keys)
        shuffled = {key: FULL[key] for key in keys}
        assert escalation_signature(shuffled) == expected


def test_reversed_order_does_not_change_the_key():
    reversed_fields = {key: FULL[key] for key in reversed(list(FULL))}
    assert list(reversed_fields) == list(reversed(list(FULL)))
    assert escalation_signature(reversed_fields) == escalation_signature(FULL)


def test_canonical_form_is_sorted_regardless_of_input_order():
    reversed_fields = {key: FULL[key] for key in reversed(list(FULL))}
    body = canonical_form(reversed_fields).splitlines()[2:]
    names = [line.split("=", 1)[0] for line in body]
    assert names == sorted(FIELD_SPEC)


# ---------------------------------------------------------------------------
# Distinctness: different state must not collapse onto one key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"trigger": "tool_result_ambiguous"},
        {"policy_rule": "refund.under_limit"},
        {"tool": "crm_lookup"},
        {"pending_action": "escalate_to_billing"},
    ],
)
def test_changing_any_field_changes_the_key(overrides):
    assert escalation_signature(variant(**overrides)) != escalation_signature(FULL)


def test_null_field_is_distinct_from_a_populated_one():
    assert escalation_signature(variant(tool=None)) != escalation_signature(FULL)


def test_the_string_none_is_not_a_null_sentinel():
    """None is the only way to say a field does not apply. A caller that
    stringifies it cannot quietly land on a different pattern."""
    with pytest.raises(UnregisteredValueError):
        escalation_signature(variant(tool="none"))
    with pytest.raises(UnregisteredValueError):
        escalation_signature(variant(tool="null"))


def test_values_do_not_bleed_between_fields():
    """Each value has to sit under its own key in the canonical form, or two
    fields sharing a value would hash the same. The vocabularies happen to be
    disjoint today, which makes the swap unconstructible, so this checks the
    boundary structurally instead."""
    lines = canonical_form(FULL).splitlines()
    assert "policy_rule=refund.over_limit" in lines
    assert "tool=billing_lookup" in lines
    assert sum(line.startswith("tool=") for line in lines) == 1


def test_version_is_carried_in_the_key_and_the_canonical_form():
    assert escalation_signature(FULL).startswith(f"esc.{SIGNATURE_VERSION}.")
    assert canonical_form(FULL).splitlines()[1] == SIGNATURE_VERSION


# ---------------------------------------------------------------------------
# Raise cases: missing fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dropped", sorted(REQUIRED_FIELDS))
def test_any_dropped_field_raises(dropped):
    fields = {key: value for key, value in FULL.items() if key != dropped}
    with pytest.raises(MissingFieldError, match=dropped):
        escalation_signature(fields)


def test_empty_mapping_raises_and_names_every_field():
    with pytest.raises(MissingFieldError) as excinfo:
        escalation_signature({})
    assert all(name in str(excinfo.value) for name in REQUIRED_FIELDS)


def test_missing_field_is_reported_before_unknown_field():
    """Both are wrong, but the absent field is the one that would have made a
    partial key, so it leads the message."""
    fields = {key: value for key, value in FULL.items() if key != "tool"}
    fields["toool"] = "billing_lookup"
    with pytest.raises(MissingFieldError):
        escalation_signature(fields)


# ---------------------------------------------------------------------------
# Raise cases: unknown fields
# ---------------------------------------------------------------------------


def test_unknown_field_raises_rather_than_being_ignored():
    with pytest.raises(UnknownFieldError, match="customer_message"):
        escalation_signature(variant(customer_message="I want a refund now"))


def test_confidence_threshold_is_not_a_signature_field():
    """Regression guard for the v2 schema change. The threshold is tunable
    agent config held in HOT state, not a property of the situation. Keying on
    it forks every stored pattern on the next tune and orphans its counters,
    so passing it must fail loudly rather than quietly changing keys."""
    assert "confidence_threshold" not in FIELD_SPEC
    with pytest.raises(UnknownFieldError, match="confidence_threshold"):
        escalation_signature(variant(confidence_threshold=0.85))


def test_schema_is_pinned():
    """Every field here has to describe the situation the agent stalled in,
    not how the agent was configured when it stalled. Adding a field orphans
    every stored pattern, so this breaking is the point: it should break in a
    commit that also bumps SIGNATURE_VERSION."""
    assert REQUIRED_FIELDS == {
        "trigger",
        "policy_rule",
        "tool",
        "pending_action",
    }
    assert set(FULL) == REQUIRED_FIELDS
    assert set(SPARSE) == REQUIRED_FIELDS


def test_near_miss_field_name_raises():
    """The failure mode this guards: a typo silently drops a real field and
    the caller gets a valid-looking key for the wrong pattern."""
    fields = dict(FULL)
    fields["policy_rules"] = fields.pop("policy_rule")
    with pytest.raises(SignatureError):
        escalation_signature(fields)


# ---------------------------------------------------------------------------
# Raise cases: unusable values
# ---------------------------------------------------------------------------


def test_required_field_cannot_be_null():
    with pytest.raises(InvalidFieldError, match="trigger"):
        escalation_signature(variant(trigger=None))


@pytest.mark.parametrize("empty", ["", "   ", "\t ", "\u00a0"])
def test_empty_or_whitespace_only_text_raises(empty):
    with pytest.raises(InvalidFieldError):
        escalation_signature(variant(policy_rule=empty))


@pytest.mark.parametrize("bad", [123, 1.5, True, [], {}, object()])
def test_non_string_text_field_raises(bad):
    with pytest.raises(InvalidFieldError, match="tool"):
        escalation_signature(variant(tool=bad))


@pytest.mark.parametrize("bad", ["refund\nover_limit", "refund\x00limit", "a\x1bb"])
def test_control_characters_raise(bad):
    with pytest.raises(InvalidFieldError, match="control character"):
        escalation_signature(variant(policy_rule=bad))


def test_overlong_text_raises():
    with pytest.raises(InvalidFieldError, match="limit"):
        escalation_signature(variant(policy_rule="r" * 257))


@pytest.mark.parametrize("bad", [None, [], "trigger", 7, ("trigger", "x")])
def test_non_mapping_input_raises(bad):
    with pytest.raises(InvalidFieldError, match="mapping"):
        escalation_signature(bad)


def test_every_error_is_a_signature_error():
    for bad in ({}, variant(extra=1), variant(trigger=None)):
        with pytest.raises(SignatureError):
            escalation_signature(bad)


# ---------------------------------------------------------------------------
# Raise cases: values outside the closed vocabularies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", sorted(REQUIRED_FIELDS))
def test_unregistered_value_raises_on_every_field(field):
    with pytest.raises(UnregisteredValueError, match=field):
        escalation_signature(variant(**{field: "not_a_registered_value"}))


@pytest.mark.parametrize(
    "phrasing",
    [
        "could not resolve the policy rule",
        "policy rule unresolved",
        "Policy rule could not be resolved for this refund",
        "the agent was unable to resolve which refund policy applies",
        "unresolved policy rule",
    ],
)
def test_free_text_phrasings_of_a_real_trigger_all_raise(phrasing):
    """The failure the vocabularies exist for. Every one of these describes
    the same stall as the registered policy_rule_unresolved. Before the
    vocabularies each produced its own pattern with its own counter, and none
    of them ever reached a threshold. They must not produce a key at all."""
    with pytest.raises(UnregisteredValueError):
        escalation_signature(variant(trigger=phrasing))


def test_a_value_registered_for_another_field_still_raises():
    """Vocabularies are per field. A real tool name is not a real action."""
    with pytest.raises(UnregisteredValueError, match="pending_action"):
        escalation_signature(variant(pending_action="billing_lookup"))
    with pytest.raises(UnregisteredValueError, match="tool"):
        escalation_signature(variant(tool="refund.over_limit"))


def test_near_miss_value_raises():
    """A plural, a typo, or a renamed id fails loudly rather than opening a
    second pattern beside the one it meant."""
    for near_miss in ("issue_refunds", "issue-refund", "issuerefund"):
        with pytest.raises(UnregisteredValueError):
            escalation_signature(variant(pending_action=near_miss))


def test_unregistered_error_names_the_registered_values():
    """The message has to be actionable at the call site."""
    with pytest.raises(UnregisteredValueError) as excinfo:
        escalation_signature(variant(tool="stripe_lookup"))
    message = str(excinfo.value)
    assert "stripe_lookup" in message
    assert all(name in message for name in TOOLS)


def test_unregistered_error_truncates_a_long_leaked_value():
    """A leaked paragraph should be recognizable in the error, not pasted
    into the log in full."""
    leaked = "customer says " + "the refund is late " * 5
    assert len(leaked) < 256  # still passes the length check, fails membership
    with pytest.raises(UnregisteredValueError) as excinfo:
        escalation_signature(variant(policy_rule=leaked))
    assert "..." in str(excinfo.value)
    assert leaked not in str(excinfo.value)


def test_unregistered_value_is_a_signature_error():
    with pytest.raises(SignatureError):
        escalation_signature(variant(trigger="something_new"))


def test_vocabularies_cover_exactly_the_schema():
    assert set(VOCABULARIES) == REQUIRED_FIELDS
    assert all(VOCABULARIES[name] for name in REQUIRED_FIELDS)
    assert VOCABULARIES["trigger"] is TRIGGERS
    assert VOCABULARIES["policy_rule"] is POLICY_RULES
    assert VOCABULARIES["tool"] is TOOLS
    assert VOCABULARIES["pending_action"] is PENDING_ACTIONS


def test_vocabularies_are_immutable():
    """The memory layer is handed these to validate at its own boundary. It
    must not be able to widen the key space by mutating them."""
    for vocabulary in VOCABULARIES.values():
        assert isinstance(vocabulary, frozenset)
        with pytest.raises(AttributeError):
            vocabulary.add("anything")


def test_fixtures_are_drawn_from_the_vocabularies():
    """Keeps the test fixtures honest: they must be values the agent could
    really produce, not invented strings that only exist here."""
    for fields in (FULL, SPARSE):
        for name, value in fields.items():
            if value is not None:
                assert value in VOCABULARIES[name], (name, value)


# ---------------------------------------------------------------------------
# The confidence threshold validator, which is not a signature input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0.0, 0.5, 0.85, 1.0, 0, 1])
def test_validator_accepts_the_unit_interval_and_returns_a_float(value):
    result = validate_confidence_threshold(value)
    assert result == float(value)
    assert isinstance(result, float)


@pytest.mark.parametrize("bad", [85, 100, -0.1, 1.0001, float("inf"), float("-inf")])
def test_validator_rejects_values_outside_the_unit_interval(bad):
    with pytest.raises(InvalidFieldError, match="0.0 to 1.0"):
        validate_confidence_threshold(bad)


def test_validator_rejects_nan():
    """NaN fails every comparison, so it would slip through a naive range
    check and then compare false against any confidence forever."""
    with pytest.raises(InvalidFieldError, match="0.0 to 1.0"):
        validate_confidence_threshold(float("nan"))


@pytest.mark.parametrize("bad", ["0.85", None, [], {}, object()])
def test_validator_rejects_non_numbers(bad):
    with pytest.raises(InvalidFieldError, match="real number"):
        validate_confidence_threshold(bad)


@pytest.mark.parametrize("bad", [True, False])
def test_validator_rejects_bools(bad):
    """bool is a subclass of int. True would read as a threshold of 1.0,
    which disables escalation entirely."""
    with pytest.raises(InvalidFieldError, match="real number"):
        validate_confidence_threshold(bad)


def test_validator_reports_the_caller_supplied_field_name():
    with pytest.raises(InvalidFieldError, match="auto_handle_floor"):
        validate_confidence_threshold(2.0, field_name="auto_handle_floor")


def test_validator_error_is_a_signature_error_and_a_value_error():
    with pytest.raises(SignatureError):
        validate_confidence_threshold(2.0)
    with pytest.raises(ValueError):
        validate_confidence_threshold(2.0)


# ---------------------------------------------------------------------------
# The key has to survive Sibyl's entity name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fields", [FULL, SPARSE])
def test_key_shape_is_a_valid_sibyl_entity_name(fields):
    """validate_identifier: non-empty, no control characters, length <= 1024."""
    key = escalation_signature(fields)
    assert key
    assert len(key) <= 1024
    assert not re.search(r"[\x00-\x1f\x7f-\x9f]", key)
    assert key == key.strip()


@pytest.mark.parametrize("trigger", sorted(TRIGGERS))
def test_every_registered_trigger_produces_a_valid_key(trigger):
    """The vocabulary is the whole key space for this field, so covering it
    exhaustively covers every key the agent can ever emit."""
    key = escalation_signature(variant(trigger=trigger))
    pattern = rf"esc\.{re.escape(SIGNATURE_VERSION)}\.[a-z0-9-]+\.[0-9a-f]{{16}}"
    assert re.fullmatch(pattern, key), key


def test_all_registered_values_produce_distinct_keys():
    """Nothing in the reachable key space collides. Cheap to check outright
    now that the space is closed."""
    keys = set()
    for trigger in TRIGGERS:
        for rule in POLICY_RULES | {None}:
            for tool in TOOLS | {None}:
                for action in PENDING_ACTIONS | {None}:
                    keys.add(escalation_signature({
                        "trigger": trigger,
                        "policy_rule": rule,
                        "tool": tool,
                        "pending_action": action,
                    }))
    expected = (
        len(TRIGGERS)
        * (len(POLICY_RULES) + 1)
        * (len(TOOLS) + 1)
        * (len(PENDING_ACTIONS) + 1)
    )
    assert len(keys) == expected


def test_sparse_escalation_produces_a_usable_key():
    assert re.fullmatch(
        rf"esc\.{re.escape(SIGNATURE_VERSION)}\.confidence-below-threshold\.[0-9a-f]{{16}}",
        escalation_signature(SPARSE),
    )
