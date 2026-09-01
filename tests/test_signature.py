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
from pathlib import Path

import pytest

from signature import (
    FIELD_SPEC,
    REQUIRED_FIELDS,
    SIGNATURE_VERSION,
    InvalidFieldError,
    MissingFieldError,
    SignatureError,
    UnknownFieldError,
    canonical_form,
    escalation_signature,
)

# A fully populated escalation: every field carries a value.
FULL = {
    "trigger": "policy_rule_unresolved",
    "policy_rule": "refund.over_limit",
    "tool": "billing_lookup",
    "pending_action": "issue_refund",
    "confidence_threshold": 0.85,
}

# A realistic sparse escalation: no tool was involved.
SPARSE = {
    "trigger": "confidence_below_threshold",
    "policy_rule": None,
    "tool": None,
    "pending_action": "close_ticket",
    "confidence_threshold": 0.6,
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
            "confidence_threshold": 0.85,
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
        == "esc.v1.policy-rule-unresolved.844c0131039b0a41"
    )


def test_float_representation_noise_does_not_change_the_key():
    noisy = 0.1 + 0.2 + 0.55  # 0.8500000000000001, not 0.85
    assert noisy != 0.85
    assert escalation_signature(variant(confidence_threshold=noisy)) == (
        escalation_signature(variant(confidence_threshold=0.85))
    )


def test_int_and_float_thresholds_agree():
    assert escalation_signature(variant(confidence_threshold=1)) == (
        escalation_signature(variant(confidence_threshold=1.0))
    )


@pytest.mark.parametrize(
    "written",
    ["POLICY_RULE_UNRESOLVED", "  policy_rule_unresolved  ", "Policy_Rule_Unresolved"],
)
def test_case_and_surrounding_whitespace_are_normalized(written):
    assert escalation_signature(variant(trigger=written)) == (
        escalation_signature(FULL)
    )


def test_internal_whitespace_runs_are_collapsed():
    assert escalation_signature(variant(pending_action="issue  refund")) == (
        escalation_signature(variant(pending_action="issue refund"))
    )


def test_unicode_composition_is_normalized():
    composed = "r\u00e9fund_review"           # single code point e-acute
    decomposed = "re\u0301fund_review"        # e + combining acute
    assert composed != decomposed
    assert escalation_signature(variant(policy_rule=composed)) == (
        escalation_signature(variant(policy_rule=decomposed))
    )


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
        {"confidence_threshold": 0.86},
    ],
)
def test_changing_any_field_changes_the_key(overrides):
    assert escalation_signature(variant(**overrides)) != escalation_signature(FULL)


def test_null_field_is_distinct_from_a_populated_one():
    assert escalation_signature(variant(tool=None)) != escalation_signature(FULL)


def test_null_field_is_distinct_from_the_string_none():
    assert escalation_signature(variant(tool=None)) != (
        escalation_signature(variant(tool="none"))
    )


def test_values_do_not_bleed_between_fields():
    """A shared value in two swapped fields must not hash the same, or the
    canonical form is losing the field boundary."""
    swapped = variant(policy_rule="billing_lookup", tool="refund.over_limit")
    assert escalation_signature(swapped) != escalation_signature(FULL)


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


@pytest.mark.parametrize("bad", ["0.85", [], object()])
def test_non_numeric_threshold_raises(bad):
    with pytest.raises(InvalidFieldError, match="confidence_threshold"):
        escalation_signature(variant(confidence_threshold=bad))


def test_bool_threshold_raises():
    """True would quantize to 1.0000 and read as a threshold of 1.0."""
    with pytest.raises(InvalidFieldError, match="confidence_threshold"):
        escalation_signature(variant(confidence_threshold=True))


@pytest.mark.parametrize("bad", [85, -0.1, 1.0001, float("nan"), float("inf")])
def test_threshold_outside_the_unit_interval_raises(bad):
    with pytest.raises(InvalidFieldError, match="0.0 to 1.0"):
        escalation_signature(variant(confidence_threshold=bad))


@pytest.mark.parametrize("bad", [None, [], "trigger", 7, ("trigger", "x")])
def test_non_mapping_input_raises(bad):
    with pytest.raises(InvalidFieldError, match="mapping"):
        escalation_signature(bad)


def test_every_error_is_a_signature_error():
    for bad in ({}, variant(extra=1), variant(trigger=None)):
        with pytest.raises(SignatureError):
            escalation_signature(bad)


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


@pytest.mark.parametrize(
    "trigger",
    ["policy rule unresolved!!", "TOOL::result/ambiguous", "\u30a8\u30b9\u30ab\u30ec"],
)
def test_key_stays_ascii_and_slug_shaped_whatever_the_trigger(trigger):
    key = escalation_signature(variant(trigger=trigger))
    assert re.fullmatch(r"esc\.v1\.[a-z0-9-]+\.[0-9a-f]{16}", key), key


def test_sparse_escalation_produces_a_usable_key():
    assert re.fullmatch(
        r"esc\.v1\.confidence-below-threshold\.[0-9a-f]{16}",
        escalation_signature(SPARSE),
    )
