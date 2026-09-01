# Escal

**An escalation memory layer for production agents.** When a support agent
hits something it cannot resolve and hands off to a human, that handoff is
normally forgotten the moment the ticket closes, so the hundredth identical
escalation costs a human exactly as much as the first. Escal persists what
stalled the agent, what the human decided, and whether that decision held up,
then recalls it on the next occurrence to decide whether to escalate again or
handle it directly.

The people with this problem are anyone running an agent with a human in the
loop: support, trust and safety, ops. Their escalation queue never gets
shorter, because the agent never learns from the queue.

## Demo video

**Link goes here before submission.** Placeholder, must not ship empty.

---

## Where memory is load-bearing

Every read and write goes through `src/memory.py`. Nothing else in the
project touches Sibyl. A judge can confirm the whole memory surface in one
file:

| Tier | Operation | Where |
| --- | --- | --- |
| WARM entity | **write** the pattern, counters, confidence | [`src/memory.py:518`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/src/memory.py#L518) in `record_outcome` |
| WARM entity | **read** the pattern by exact key | [`src/memory.py:367`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/src/memory.py#L367) in `recall_pattern` |
| WARM entity | **search** fallback when the key misses | [`src/memory.py:222`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/src/memory.py#L222) in `_search_patterns` |
| COLD journal | **write** one event per escalation | [`src/memory.py:570`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/src/memory.py#L570) in `journal_escalation` |
| HOT state | **write** the confidence threshold | [`src/memory.py:585`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/src/memory.py#L585) in `set_threshold` |
| HOT state | **read** the threshold | [`src/memory.py:596`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/src/memory.py#L596) in `get_threshold` |
| REFERENCE | **write** the escalation policy | [`src/agent.py:251`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/src/agent.py#L251) in `publish_policy` |
| REFERENCE | **read** the policy for the startup check | [`src/agent.py:269`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/src/agent.py#L269) in `check_policy_consistency` |

**The one function to read if you only read one:** `recall_pattern`
([`src/memory.py:341`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/src/memory.py#L341)). It is the decision point. Exact key first, then a
fallback search, then a branch on which of five reasons a zero result came
back with. Whether the agent escalates or acts turns entirely on what this
returns.

**The two-minute path through the repo:**

1. [`src/signature.py:390`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/src/signature.py#L390), `escalation_signature` derives a stable key from
   the agent's structured state, never from customer text, so two customers
   phrasing one problem differently land on one pattern.
2. [`src/memory.py:341`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/src/memory.py#L341), `recall_pattern` looks that key up.
3. [`src/agent.py:405`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/src/agent.py#L405), `handle_request` is the loop: resolve, stall, recall,
   auto-handle or escalate, record.

## How memory made this possible

Escal has no other state. Confidence is not a heuristic recomputed at runtime;
it is an accumulated count of how often a human agreed with a stored
resolution, and that count exists only in Sibyl. With no persistent store
there are no patterns, so there is nothing to calibrate against, so every
escalation goes to a human, forever. The agent still runs. It just never
learns.

There is deliberately no fallback path. No try/except degrades to a local
dict, no cache answers a recall the store could not, and `get_threshold`
raises rather than returning a default, because a hardcoded default would be
calibration the module invented rather than read.

## Setup

Python 3.10 or newer. Developed and verified on 3.14.

    git clone https://github.com/UEddy/Escal.git
    cd Escal

Create and activate a virtual environment.

macOS and Linux:

    python3 -m venv .venv
    source .venv/bin/activate

Windows PowerShell:

    python -m venv .venv
    .venv\Scripts\Activate.ps1

Install. `requirements.txt` is the single runtime dependency;
`requirements-dev.txt` adds pytest and is needed to run the tests.

    pip install -r requirements.txt
    pip install -r requirements-dev.txt

That is the whole setup. Every command below assumes the venv is active, so
plain `python` is the right interpreter on all platforms.

Verify:

    python -m pytest tests -q

Expect `225 passed`.

### You do not need a Sibyl account

**Verified, not assumed:** the full test suite and all three demo modes run
with no credentials present at all. Escal calls `MemoryClient.local(...)`,
which is local SQLite. It creates its own store and never requires activation.

`sibyl init` exists and opens a browser to activate a free account, but it is
**optional here**. Activating unlocks the `sibyl` CLI inspection commands
(`sibyl status`, `sibyl whoami`, `sibyl memory`) and server-side tier
verification for the storage cap. Nothing in this repo depends on any of that.
Without credentials the SDK simply behaves as free tier locally.

The free tier is sufficient regardless. Escal uses no paid-tier features:
Sibyl's `learn` and `lint` are gated on paid tiers and are not used, and all
confidence logic is hand-written in `derive_confidence`
([`src/memory.py:426`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/src/memory.py#L426)).

> **Windows only:** if console output raises `UnicodeEncodeError`, set
> `$env:PYTHONIOENCODING="utf-8"` first. This is a Windows cp1252 console
> issue and is not needed on macOS or Linux.

## The deletion test

Two tests, side by side in `tests/test_agent.py`, run the identical request
sequence through the identical code. The only variable is whether the store
has a history.

- `test_without_the_store_every_request_escalates` ([`tests/test_agent.py:484`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/tests/test_agent.py#L484))
  runs six requests, each against a fresh store. All six escalate. A human is
  asked six times.
- `test_the_same_agent_with_the_store_does_stop_escalating`
  ([`tests/test_agent.py:497`](https://github.com/UEddy/Escal/blob/ec91ba4395f20190b918ca315896e9712afd2ee8/tests/test_agent.py#L497)) runs the same six against one kept store. The
  human is asked fewer than six times, because the agent stops needing them.

Run just those two:

    python -m pytest tests/test_agent.py -k "without_the_store or same_agent_with_the_store" -v

Full suite, 225 tests:

    python -m pytest tests -q

## Running the demo

Three separate process invocations, not three function calls. The point being
demonstrated is that the only thing crossing the process boundary is Sibyl,
and one process could not show that.

    python scripts/demo.py phase1
    python scripts/demo.py phase2
    python scripts/demo.py no-memory

`phase1` must run before `phase2`. It resets its store so every run starts
cold, then handles three identical stalls with a scripted human agreeing each
time.

**What to look for:**

- **The same signature in every phase.** Four customers word the refund
  request differently and all four produce
  `esc.v2.policy-rule-unresolved.9ae9cdb8e397597c`. The key comes from the
  agent's control state, not from what anyone typed.
- **Confidence climbing 0.6667, 0.7500, 0.8000** across phase 1, printed with
  the threshold marked on a bar. Three human agreements reach exactly the 0.8
  threshold.
- **The process id in each header changes** while the tenant and store path
  stay the same. Two real processes, one store.
- **Phase 2 prints `AUTO HANDLED FROM MEMORY`, humans asked 0.** It recalls
  what phase 1 wrote and acts without a human.
- **`no-memory` runs that same fourth request against a tenant with no
  history and escalates.** The deletion test on screen.

## Measured storage cost

Not estimated. Written and measured by `scripts/spike_store.py` against a
real store, 100 pattern entities and 100 journal events:

| | bytes |
| --- | --- |
| Baseline, schema only | 282,624 |
| Per pattern entity | **4,342** |
| Per journal event | **2,990** |
| Free tier cap | 5,242,880 |

100 patterns plus 100 events use 19.4 percent of the cap.

Both per-record figures are roughly ten times the JSON payload they carry.
That is SQLite page granularity plus FTS5 index overhead, not wasted data,
which means the cost will not shrink much by trimming bodies. Budget on the
measured number.

**The cap is per account, not per file.** `_capcheck.aggregate_db_size` sums
every `memory.db` the machine resolves, WAL included, so real headroom is
lower than any single store suggests. Pattern entities are bounded by the
number of distinct signatures; the journal is the unbounded growth risk, at
roughly 1,400 further events before this store alone would reach the cap.

## Partner stacks

**None used. Multiplier 1.00.** Escal uses Sibyl Memory only
(`sibyl-memory-client==0.8.0`). No partner technologies were integrated, and
no multiplier is claimed. The only third-party import anywhere in `src/` or
`scripts/` is `sibyl_memory_client`; everything else is the Python standard
library.

## Prior work declaration

Everything in this repository was written during the Sep 1 to 10, 2026 build
window. Nothing pre-existing was brought in. There is no vendored code, no
adapted prior project, and no template. The full commit history is in the
repository and reflects the actual order the work was done in, starting from
an empty directory.

## Layout

    src/signature.py       Pure key derivation. No Sibyl import, no IO.
    src/memory.py          The Sibyl layer. Every read and write.
    src/agent.py           The refund agent that produces escalations.
    scripts/demo.py        The three-mode scripted demo.
    scripts/spike_*.py     Throwaway measurement spikes, kept for their findings.
    tests/                 225 tests, real store, no mocks.
    docs/api-actual.txt    Sibyl 0.8.0 API surface dumped from the installed
                           package. The PyPI README is stale; this is the
                           source of truth every design decision was checked
                           against.
    DESIGN.md              Design decisions and the verified Sibyl API surface.
    requirements.txt       Runtime dependency, pinned.
    requirements-dev.txt   Adds pytest, for running the suite.
    requirements-lock.txt  Full transitive record of the dev environment.

## License

MIT. See `LICENSE`.
