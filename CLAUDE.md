# escalation-memory

An escalation memory layer for production agents. When an agent hands off to a
human, the system persists the trigger, the human's decision, and the outcome.
On later runs it recalls that history to decide whether to escalate,
auto-handle, or route differently.

Built for the Sibyl Labs Hackathon. Build window Sep 1 to 10, 2026.

## Hard rules

- **Memory is load-bearing.** Never write a fallback path that works without
  Sibyl. No try/except that degrades gracefully to a local dict, no in-memory
  cache that keeps the system functional when the store is gone. If Sibyl is
  removed the system must stop working. This is a pass/fail judging gate.
- **Never guess the Sibyl API.** The verified surface is below and in
  `docs/api-actual.txt` (dumped from the installed package). If something is
  not listed there, inspect the installed source before using it. Do not rely
  on the PyPI README or docs.sibyllabs.org: both are stale relative to 0.8.0.
- No em dashes or en dashes anywhere in code, comments, docs, or commit
  messages.
- Small, frequent commits. Judges look at commit history.

## Environment

- Windows, PowerShell, venv at `.venv`
- Python 3.14
- `sibyl-memory-client==0.8.0`, `sibyl-memory-cli==0.4.0`,
  `sibyl-memory-mcp==0.2.0`, `sibyl-memory-hermes==0.4.0`
- Set `PYTHONIOENCODING=utf-8` before any script that prints library source or
  non-ASCII. The Windows cp1252 console codec will raise UnicodeEncodeError
  otherwise. Write files with `encoding="utf-8"` explicitly.

## Tenancy

The SDK and the CLI use different tenants against the same database file.

- `DEFAULT_TENANT` (SDK default) is `00000000-0000-0000-0000-000000000001`
- `sibyl health` reports the account-derived tenant
- This project uses `escalation-memory-dev`, passed explicitly
- Tests use a separate tenant so sizing and demo data never mix

Plain strings are accepted as tenant ids. No UUID validation.

    from sibyl_memory_client import MemoryClient
    memory = MemoryClient.local(tenant_id="escalation-memory-dev")

## Storage cap

Free tier is capped at `FREE_TIER_CAP_BYTES` = 5,242,880 bytes, confirmed
server-side via `sibyl status`. Baseline schema is about 282 KB. Writes past
the cap raise `CapExceededError`.

The journal is the unbounded growth risk (one event per escalation, forever).
Pattern entities are bounded by the number of distinct signatures. Measure
per-event cost before assuming there is room.

## Verified API surface

Signatures below are from the installed 0.8.0 package, not from docs.

    MemoryClient.local(path='~/.sibyl-memory/memory.db', *, tenant_id=...,
                       tier='free', account_id=None, session_token=None,
                       credentials_claim=None, credentials_signature=None)

    set_entity(category, name, body, *, status=None) -> dict
    get_entity(category, name)
    list_entities(...)
    archive_entity(category, name)   # recoverable
    delete_entity(category, name)    # permanent

    set_state(key, body) / get_state(key)
    set_reference(key, body) / get_reference(key)

    write_event(*, evaluated=None, acted=None, forward=None, extra=None,
                ts=None) -> str
    read_events(...)

    search(query, *, limit=20, prefix=False, tiers=None) -> SearchResults
    search_entities(query, *, limit=20, prefix=False, category=None)
        -> SearchResults

    get_tenant() / set_tenant(tenant_id)
    get_tier() / set_tier(tier)
    schema_version()
    free_tier_status()

Note: the parameter is `category`, NOT `kind`. The PyPI README says `kind` and
is wrong.

`search_entities` returns warm-tier entity rows only. Each row is a dict with
keys: `id`, `tenant_id`, `category`, `name`, `status`, `body`, `created_at`,
`updated_at`. Body is JSON-deserialized. Pass `category="..."` to anchor the
search and avoid topical bleed across categories.

`search` is cross-tier (entities, state, reference, journal).

### Paid tier only, do not use

`learn`, `learner`, `lint`, `list_skill_proposals`, `accept_skill_proposal`,
`reject_skill_proposal`. These exist on the client but are gated on free tier.
All confidence and pattern logic in this project is hand-written.

## Verdicts

Both search methods return a `SearchResults` list subclass carrying
`.verdict`. Never test a search result for truthiness alone. Branch on the
verdict code.

`VerdictCode` values: `OK`, `NO_MATCH`, `EMPTY_STORE`, `GATED`,
`NEGATION_ABSTAIN`, `ABSTAINED_ON`.

`refine_zero(client, results)` upgrades a bare `NO_MATCH` to `EMPTY_STORE` by
paying one count probe. It is intended for user-facing surfaces, called once
per zero-row response, never on a hot per-token path. A non-empty result, or a
zero already carrying a more specific cause, is returned untouched.

`explain()` renders a verdict as one actionable sentence built only from enum
fields and caller query tokens, never from stored text, so it is safe to print
or return to a user.

### Why this matters here

The five zero causes are five different decisions:

| Cause | Meaning | Agent behaviour |
| --- | --- | --- |
| `OK` | Prior patterns found | Compare confidence, maybe auto-handle |
| `EMPTY_STORE` | Cold start | Escalate and record. Normal, not a failure |
| `NO_MATCH` | Store populated, nothing similar | Escalate, record as a new pattern |
| `GATED` | A scoring gate dropped the query | Escalate, flag match as failed |
| `NEGATION_ABSTAIN` | Negation policy declined | Escalate, flag match as failed |
| `ABSTAINED_ON` | Engine abstained on a term | Escalate, flag match as failed |

The bottom three are not "never seen before", they are "could not tell".
Collapsing them into `NO_MATCH` would create a phantom new pattern for a
situation already handled, and accumulate confidence on a duplicate.

Escalation context contains user-supplied text. The gates are what make an
injection-shaped query return nothing, so treat gated results as a signal, not
an error to suppress.

## Signature design

The pattern key is deterministic and derived from the agent's own structured
internal state, never from user-supplied text.

Inputs are things like: which policy rule could not be resolved, which tool
returned an ambiguous result, which confidence threshold was missed, what
action was pending. Two escalations phrased differently by two customers
produce the same signature when the agent stalled for the same reason.

Rationale:
- Determinism is required for confidence accumulation. A regenerated signature
  creates a fresh entity and silently resets the counter.
- No per-escalation model call. Free tier, zero budget.
- The demo requires one unbroken take with reproducible recall.

The raw context goes in the entity body so FTS5 can serve as a fuzzy second
pass when the exact key misses. Lookup order: exact key, then
`search_entities` anchored to the pattern category, then branch on verdict.

## Memory layout

- WARM entity, category `escalation_pattern`, name = signature: the chosen
  resolution, times seen, times the human agreed, times overridden, derived
  confidence, last outcome, last seen.
- COLD journal, one event per escalation instance. Use the semantic channels:
  `evaluated` for what the agent considered, `acted` for what it did,
  `forward` for what it passed to the human, `extra` for the rest. This is the
  decision audit log.
- HOT state: current confidence threshold, pending escalation.
- REFERENCE: the escalation policy document.

## Deletion test

Removing the Sibyl layer must break the core function. With no patterns there
is no calibration, so the system escalates every time. Keep this true. Any
convenience cache that preserves behaviour without Sibyl fails the gate.
