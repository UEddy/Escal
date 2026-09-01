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
| `NO_MATCH` | Store populated, nothing scored well enough | Escalate, record as a new pattern |
| `GATED` | A scoring gate dropped the query | Escalate, flag match as failed |
| `NEGATION_ABSTAIN` | Negation policy declined | Escalate, flag match as failed |
| `ABSTAINED_ON` | A query term has zero corpus support | Depends on provenance, see below |

`GATED` and `NEGATION_ABSTAIN` are not "never seen before", they are "could
not tell". Collapsing them into `NO_MATCH` would create a phantom new pattern
for a situation already handled, and accumulate confidence on a duplicate.

Escalation context contains user-supplied text. The gates are what make an
injection-shaped query return nothing, so treat gated results as a signal, not
an error to suppress.

#### `ABSTAINED_ON` depends on where the query came from

`ABSTAINED_ON` means one content token has zero corpus support anywhere in the
store. What that IMPLIES depends on who put the token in the query, so the
same code is read two ways:

- **The query was built from registered vocabulary values.** The engine is
  reporting that an exact registered term appears in no stored pattern. That
  is a *more* confident miss than `NO_MATCH`, not a weaker one: `NO_MATCH`
  says the scorer found nothing good enough, this says the term is absent from
  the corpus outright. Treat as a confident miss, `match_failed = False`.
- **The query carried free text.** The conservative reading stands. The store
  may well hold the pattern and one unsupported word blocked the search.
  Treat as a failed match, `match_failed = True`.

This is the ordinary outcome on the vocabulary path, not an edge case: a
genuinely new situation is a query whose terms have no corpus support, so it
abstains rather than scoring. A recall that never reclassified would report
almost every new pattern as "could not tell".

**Read provenance from the blocking token, never from a flag the caller
passes.** `Verdict.tokens` carries the blocking token, and
`memory._blocking_token_is_registered` checks it against `VOCABULARY_TOKENS`.
Gating this way means a future free-text surface inherits the conservative
reading automatically, with no new argument to remember and no way to assert a
provenance it does not have. If any reported token is unregistered, or none is
reported at all, the conservative reading applies.

`VOCABULARY_TOKENS` holds the FTS *tokens* a registered value can produce, not
the values themselves. The tokenizer is `porter unicode61` and the SDK
sanitizer keeps alphanumerics and underscores, so a dotted id splits:
`refund.over_limit` becomes `refund` and `over_limit`, while `issue_refund`
survives whole. Comparing against raw vocabulary values would miss every
dotted policy rule.

### Recall must go through `multi_record_search`

`client.search` and `client.search_entities` are policy-free primitives. Their
own source says so: no abstention, no relevance gate, so **only `OK` and
`NO_MATCH` are reachable from them**. The other four causes are stamped in
`multi_record.py`. Measured, not assumed: see `scripts/spike_store.py`.

`client.search` is disqualified for a second, worse reason. It does not
enforce AND across query tokens. A query carrying one token with zero corpus
support still returns rows (`"flibbertigibbet refund"` returns 20 rows and
`OK`, where the same query returns zero from the other two), so its `NO_MATCH`
is weak evidence and cannot anchor a confidence counter.
`multi_record_search` suppresses the query and names the blocking token.

`multi_record_search` is a semi-public engine: reachable as a submodule, named
without a leading underscore, documented as the canonical search path, but
absent from the package top-level exports and from any `__all__`, and never
called by `client.py`. Treat the import as the one place upstream can break
us. It is wrapped behind `memory._search_patterns`, which is the only call
site in the project, so a break is a one-function fix.

Details that differ from the rest of the SDK surface:

- `limit` defaults to **10**, where `client.search` defaults to 20. Pass it
  explicitly.
- It is cross-tier with **no `category` or `tiers` kwarg**. Journal rows do
  surface, and on those `key` is an event UUID and `category` is `None`.
  Filter on `row["tier"] == "entity"` and `row["category"] ==
  "escalation_pattern"` before treating any `key` as a signature, or
  `get_entity` will raise `NotFoundError`.
- Rows come back in `client.search` shape (`tier`, `key`, `category`, `body`,
  `snippet`, `rank`, `ts`), **not** `search_entities` shape. There is no
  `name` field; for entity-tier rows the signature is in `key`.
- It reports `EMPTY_STORE` natively, so `refine_zero` is not needed on this
  path.
- The `diagnostics` kwarg is deprecated upstream. Use `result.verdict`.

## Signature design

The pattern key is deterministic and derived from the agent's own structured
internal state, never from user-supplied text.

Inputs are things like: why the agent stalled, which policy rule could not be
resolved, which tool returned an ambiguous result, what action was pending.
Two escalations phrased differently by two customers produce the same
signature when the agent stalled for the same reason.

Membership rule: a field belongs in the signature if it describes the
situation, not if it describes the configuration. Tunable config is not a
property of the situation, and anything retunable that is keyed on forks every
stored pattern on the next tune and orphans its accumulated counters. The
confidence threshold is the case in point, and is deliberately absent: it
lives in HOT state, and the value in force at the time is recorded on the
journal event, where it stays auditable without entering the key.

Rationale:
- Determinism is required for confidence accumulation. A regenerated signature
  creates a fresh entity and silently resets the counter.
- No per-escalation model call. Free tier, zero budget.
- The demo requires one unbroken take with reproducible recall.

### Closed vocabularies

Every signature field draws from a closed vocabulary hardcoded in
`src/signature.py`: `TRIGGERS`, `POLICY_RULES`, `TOOLS`, `PENDING_ACTIONS`,
exposed together as `VOCABULARIES`. An unregistered value raises
`UnregisteredValueError`, the same refusal an unknown field gets. Adding a
value is a deliberate one-line edit in that module.

Determinism without a closed vocabulary is decorative. Normalization equates
two spellings of one string, but nothing can equate two phrasings of one
stall, so an open field lets "policy rule unresolved" and "could not resolve
the policy rule" become two patterns that each accumulate half a counter and
never reach a threshold. The vocabulary is what makes the key space knowable.

The vocabularies are hardcoded, never loaded from Sibyl. Loading them would
make the signature module a second reader of memory instead of a pure
derivation, and caching them would be exactly the convenience cache the hard
rules forbid. This does not weaken the deletion test: the vocabulary is the
key space, not the memory. With Sibyl removed there are still no patterns, no
calibration, and every escalation goes to a human.

Values must be read out of the agent's control state at the point it stalled.
The branch that decided to escalate knows which branch it is, the tool
registry knows the tool's name, and the invocation knows the action's name.
None of these fields may be filled by asking a model to describe the
escalation.

### The escalation policy document

The policy is a structured document in which every rule carries a stable id.
`policy_rule` is a lookup key into it, never a quotation from it. The ids in
`POLICY_RULES` and the ids in the REFERENCE copy are two hands on the same
rope, so keep them in step: a rule added to the document raises here until it
is registered, which is the direction the drift should fail in.

Free text never enters the key. The raw context goes in the entity body so
FTS5 can serve as a fuzzy second pass when the exact key misses. Lookup
order: exact key, then `search_entities` anchored to the pattern category,
then branch on verdict. That body is also where user-supplied text lands,
which is why the search gates matter and why a `GATED` verdict is a signal
rather than an error to suppress.

## Memory layout

- WARM entity, category `escalation_pattern`, name = signature: the chosen
  resolution, times seen, times the human agreed, times overridden, derived
  confidence, last outcome, last seen.
- COLD journal, one event per escalation instance. Use the semantic channels:
  `evaluated` for what the agent considered, `acted` for what it did,
  `forward` for what it passed to the human, `extra` for the rest. This is the
  decision audit log.
- HOT state: current confidence threshold, pending escalation.
- REFERENCE: the escalation policy document, structured so every rule carries
  a stable id. Those ids are the `policy_rule` vocabulary, see above.

Validate the confidence threshold with `validate_confidence_threshold` from
`src/signature.py` wherever it is stored or read back, on the HOT state write
path and on the journal event. It enforces the 0.0 to 1.0 range and rejects
bools and NaN. It is not a signature input, see the membership rule above.

## The agent

`src/agent.py` is the producer of signature fields: a refund handler scoped to
the demo scenario. It evaluates a request against the policy document and, when
it stalls, reads the four fields out of the branch that gave up. No model call,
no customer text. `evaluate_policy` returns either a resolution string or a
`StallState`, and `StallState.signature_fields()` is the only input to
`escalation_signature`.

### The pattern counters count human decisions, not encounters

`times_seen` is the number of times a human was asked about this pattern, not
the number of times the situation occurred. `record_outcome` is therefore
called only when a human actually decided, never on an auto-handled request.

This is load-bearing, not bookkeeping. `times_seen` is the denominator of an
agreement rate. An auto-handled encounter has no human decision to pair with
an increment, so incrementing on it would raise the denominator while the
numerator stood still:

    (agreed + 1) / (seen + 2)

Confidence would fall every time the agent used what it had learned. A settled
pattern would decay back below the threshold, start escalating again, climb
back, and oscillate forever. Do not "fix" the counters to track encounters.

Encounters are counted in the journal, which records every escalation
instance, auto-handled ones included. If you need occurrence counts, read the
journal. The entity holds the agreement rate.

### Other agent decisions worth knowing

- **A cold-start decision seeds as agreement.** With nothing recalled there is
  nothing to disagree with, so the first human decision establishes the
  pattern with `human_agreed=True` rather than counting as an override.
  Otherwise every new pattern would seed at 0.2 confidence and effectively
  never recover. Agreement on later decisions means the human landed on the
  stored resolution.
- **Resolved requests are not journalled.** When the policy closes a request
  without stalling, no pattern is involved and no event is written. The
  journal is a decision audit log, not a request log. Journalling the resolved
  path would spend the cap on events with nothing to audit. Reverse this if
  the audit requirement changes to a complete request log, and re-measure
  against the cap first: the journal is the unbounded growth risk.
- **`tool_unavailable` takes precedence** over a rule's own trigger, because
  the agent could not evaluate the rule at all.
- **More than one matching rule is `conflicting_policy_rules`**, with the
  sorted-first rule id as `policy_rule`. `_matching_rule_ids` sorts before
  use, so reordering the condition checks cannot fork every stored pattern.
- `check_policy_consistency` reads the STORED policy out of REFERENCE, not the
  module constant. That is the point: the constant and `POLICY_RULES` can
  agree perfectly while the store holds a stale document from an older deploy,
  and the stored copy is what a human reviewing an escalation consults. It
  raises on drift in either direction, at startup rather than mid-request.
- `get_reference` returns the body as a raw STRING. It does not deserialize
  the way entity and state bodies do, even though `set_reference` accepts a
  dict and coerces it to JSON. The parse is the caller's.

## Deletion test

Removing the Sibyl layer must break the core function. With no patterns there
is no calibration, so the system escalates every time. Keep this true. Any
convenience cache that preserves behaviour without Sibyl fails the gate.

## Running the demo

`scripts/demo.py` is the scripted run behind the video. Three modes, each a
SEPARATE process invocation. That is deliberate: the claim being demonstrated
is that the only thing crossing the process boundary is Sibyl, and one
function call could not show it.

    $env:PYTHONIOENCODING="utf-8"
    .venv/Scripts/python.exe scripts/demo.py phase1
    .venv/Scripts/python.exe scripts/demo.py phase2
    .venv/Scripts/python.exe scripts/demo.py no-memory

| Mode | What it does |
| --- | --- |
| `phase1` | Fresh store. Publishes the policy, sets the threshold, runs three identical stalls with a scripted human agreeing. Confidence climbs 0.6667, 0.7500, 0.8000. Exits. |
| `phase2` | New process, same store. A fourth identical request auto-handles from memory with no human. |
| `no-memory` | The same fourth request against a tenant with no history. It escalates. The deletion test on screen. |

**`phase1` must run before `phase2`.** Phase 2 reads what phase 1 wrote; with
no store it exits non-zero and says so.

**`phase1` destroys and recreates its store without confirmation.** That is
what makes every take start from a true cold start, but a re-run mid-session
discards the state an earlier take recorded. It only ever touches the demo
store, never the dev or test tenants.

The threshold of 0.8 and the three-agreement ladder are not arbitrary:
`derive_confidence(3, 3)` is exactly 0.8, so the fourth request is the first
that can auto-handle. Changing `CONFIDENCE_PRIOR_WEIGHT` or the threshold
changes how many escalations the demo needs before it converges.

Set `ESCALATION_DEMO_DB` to point the demo at a different store. The smoke
test in `tests/test_demo.py` uses it to run all three modes under `tmp_path`
so it cannot clobber a take in progress. That test drives the script as three
subprocesses and pins the numbers the video depends on, so run the suite
before recording.

Demo data uses tenant `escalation-memory-demo` (and
`escalation-memory-demo-empty` for `no-memory`), separate from dev and test.
