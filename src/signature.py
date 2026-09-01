"""Deterministic escalation signature derivation.

The signature is the pattern key for the escalation memory layer. It is
derived only from the agent's own structured internal state, never from
user-supplied text, so two escalations phrased differently by two customers
collapse onto one key when the agent stalled for the same reason.

This module is pure: no Sibyl imports, no IO, no clock, no randomness. It
produces a key, it does not store or recall one. It is not a fallback for the
memory layer and must never become one.

Determinism is load-bearing. A regenerated signature creates a fresh entity
and silently resets the confidence counter, so every input is normalized to a
canonical form before hashing, and anything the schema does not recognize
raises instead of being dropped. A partial signature is worse than no
signature: it looks like a valid key and accumulates confidence on the wrong
pattern.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, NamedTuple

__all__ = [
    "SIGNATURE_VERSION",
    "FIELD_SPEC",
    "REQUIRED_FIELDS",
    "SignatureError",
    "MissingFieldError",
    "UnknownFieldError",
    "InvalidFieldError",
    "escalation_signature",
    "canonical_form",
    "validate_confidence_threshold",
]

# Bump when the field set or the normalization rules change. The version is
# part of the hashed payload and of the emitted key, so a schema change shows
# up in the store as a new pattern rather than as a silent counter reset on an
# existing one.
#
# v1: trigger, policy_rule, tool, pending_action, confidence_threshold.
# v2: confidence_threshold removed. It is tunable agent config held in HOT
#     state, not a property of the situation, so keying on it forked every
#     stored pattern the moment the threshold was retuned and orphaned the
#     counters. Patterns written under v1 are not readable under v2 by design.
SIGNATURE_VERSION = "v2"

# Length of the truncated digest carried in the key. 16 hex characters is 64
# bits, far beyond the collision headroom needed for the number of distinct
# escalation shapes one agent can produce.
_DIGEST_LENGTH = 16

# Upper bound on any single normalized field value. Sibyl validates entity
# names as non-empty, control-character-free, length <= 1024, and the emitted
# key stays well inside that regardless of this bound.
_MAX_VALUE_LENGTH = 256

# Longest slug segment carried in the emitted key. The slug is readability
# only. The digest is what identifies the pattern.
_MAX_SLUG_LENGTH = 48

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


class _Field(NamedTuple):
    nullable: bool
    doc: str


# The schema. Every key here must be supplied on every call. A field that does
# not apply to a given escalation is passed explicitly as None, which encodes
# distinctly from any value: the caller states the absence rather than omitting
# the key, so a dropped field can never masquerade as a not-applicable one.
#
# The test for membership is whether the field describes the situation the
# agent stalled in, not how the agent was configured when it stalled. Tunable
# config belongs in HOT state. Anything retunable that is keyed on here forks
# every stored pattern on the next tune and orphans its counters, which is why
# the confidence threshold is deliberately absent. Do not add it back. See
# validate_confidence_threshold for its validation, which lives on where the
# threshold is actually stored.
FIELD_SPEC: Mapping[str, _Field] = {
    "trigger": _Field(
        nullable=False,
        doc="Why the agent stalled, as an agent-internal trigger identifier.",
    ),
    "policy_rule": _Field(
        nullable=True,
        doc="Identifier of the policy rule that could not be resolved.",
    ),
    "tool": _Field(
        nullable=True,
        doc="Name of the tool that returned an ambiguous or unusable result.",
    ),
    "pending_action": _Field(
        nullable=True,
        doc="The action the agent was about to take when it stalled.",
    ),
}

REQUIRED_FIELDS = frozenset(FIELD_SPEC)


class SignatureError(ValueError):
    """Base class for every refusal to produce a signature."""


class MissingFieldError(SignatureError):
    """A field in the schema was not supplied."""


class UnknownFieldError(SignatureError):
    """A key outside the schema was supplied."""


class InvalidFieldError(SignatureError):
    """A field was supplied with a value that cannot be normalized.

    Also raised by validate_confidence_threshold, which is not a signature
    input but shares the error type so callers have one thing to catch.
    """


def _check_keys(fields: Mapping[str, Any]) -> None:
    supplied = set(fields)
    missing = REQUIRED_FIELDS - supplied
    if missing:
        raise MissingFieldError(
            "escalation signature is missing required field(s): "
            + ", ".join(sorted(missing))
            + ". Pass None explicitly for a field that does not apply."
        )
    unknown = supplied - REQUIRED_FIELDS
    if unknown:
        raise UnknownFieldError(
            "escalation signature received unknown field(s): "
            + ", ".join(sorted(unknown))
            + ". Known fields: "
            + ", ".join(sorted(REQUIRED_FIELDS))
            + "."
        )


def _normalize_text(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidFieldError(
            f"field {name!r} must be a string, got {type(value).__name__}."
        )
    if _CONTROL_RE.search(value):
        raise InvalidFieldError(
            f"field {name!r} contains a control character. Signature inputs "
            "come from agent-internal state and must be plain identifiers."
        )
    # NFC first so visually identical strings from different sources hash the
    # same, then collapse whitespace runs and lowercase. These inputs are
    # agent-internal identifiers, where case and spacing carry no meaning.
    normalized = " ".join(unicodedata.normalize("NFC", value).split()).lower()
    if not normalized:
        raise InvalidFieldError(
            f"field {name!r} is empty or whitespace only. Pass None to mark a "
            "field that does not apply."
        )
    if len(normalized) > _MAX_VALUE_LENGTH:
        raise InvalidFieldError(
            f"field {name!r} is {len(normalized)} characters after "
            f"normalization, over the {_MAX_VALUE_LENGTH} character limit."
        )
    return normalized


def validate_confidence_threshold(
    value: Any, *, field_name: str = "confidence_threshold"
) -> float:
    """Validate a confidence threshold and return it as a float.

    The threshold is tunable agent config, so it is not a signature input: it
    describes how the agent was configured, not the situation it stalled in.
    This validator lives here because the rule it enforces was written here,
    and belongs on the HOT state write path where the threshold is actually
    stored. Raises InvalidFieldError on anything outside 0.0 to 1.0.
    """
    # bool is a subclass of int. True would read as a threshold of 1.0, which
    # disables escalation entirely, so reject it outright.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidFieldError(
            f"field {field_name!r} must be a real number, got "
            f"{type(value).__name__}."
        )
    numeric = float(value)
    # Also rejects NaN, which fails every comparison.
    if not 0.0 <= numeric <= 1.0:
        raise InvalidFieldError(
            f"field {field_name!r} must be in the range 0.0 to 1.0, got "
            f"{value!r}. Confidence is a fraction, not a percentage."
        )
    return numeric


def _normalize_field(name: str, value: Any) -> str:
    """Normalize one field to its canonical string, or "" for null."""
    spec = FIELD_SPEC[name]
    if value is None:
        if not spec.nullable:
            raise InvalidFieldError(
                f"field {name!r} is required and cannot be None."
            )
        # A normalized value is never empty, so "" is an unambiguous marker for
        # a field the caller declared inapplicable.
        return ""
    return _normalize_text(name, value)


def canonical_form(fields: Mapping[str, Any]) -> str:
    """Return the canonical serialization the signature is hashed from.

    Exposed so the canonical form can be recorded on the journal event beside
    the key it produced, and so a signature can be explained without rerunning
    the hash. Validates exactly as escalation_signature does.
    """
    if not isinstance(fields, Mapping):
        raise InvalidFieldError(
            "escalation signature expects a mapping of fields, got "
            f"{type(fields).__name__}."
        )
    _check_keys(fields)
    # Sorting by key is what makes the result independent of the order the
    # caller happened to build the mapping in. Every key appears exactly once
    # and no normalized value can contain a newline, so the joined form parses
    # back unambiguously.
    lines = ["escalation-signature", SIGNATURE_VERSION]
    lines.extend(
        f"{name}={_normalize_field(name, fields[name])}"
        for name in sorted(FIELD_SPEC)
    )
    return "\n".join(lines)


def _slug(value: str) -> str:
    slug = _SLUG_STRIP_RE.sub("-", value).strip("-")[:_MAX_SLUG_LENGTH].strip("-")
    # A trigger written entirely in a non-Latin script slugifies to nothing.
    # The digest still identifies the pattern, so a placeholder is enough.
    return slug or "trigger"


def escalation_signature(fields: Mapping[str, Any]) -> str:
    """Derive the stable pattern key for one escalation.

    Takes the agent's structured escalation state as a mapping over exactly
    the keys in FIELD_SPEC and returns a key of the form
    esc.<version>.<trigger-slug>.<digest>, safe to use directly as a Sibyl
    entity name.

    Raises MissingFieldError if a schema field is absent, UnknownFieldError if
    a key outside the schema is present, and InvalidFieldError if a value
    cannot be normalized. It never returns a signature derived from part of
    the schema.
    """
    canonical = canonical_form(fields)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
    trigger = _normalize_text("trigger", fields["trigger"])
    return f"esc.{SIGNATURE_VERSION}.{_slug(trigger)}.{digest}"
