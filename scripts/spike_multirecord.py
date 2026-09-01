"""Throwaway spike, second pass. Probes multi_record_search on the two
properties that disqualified client.search() as the fallback recall path.

NOT part of the system. Nothing here is imported by src/. Reuses the database
and tenant written by spike_store.py, and writes nothing of its own to the
escalation tenants.

Run spike_store.py first, then:
    $env:PYTHONIOENCODING="utf-8"
    .venv/Scripts/python.exe scripts/spike_multirecord.py

Questions:
  1. Does it enforce AND across query tokens, or does a token with zero
     corpus support pass through the way it does in client.search()? If a
     nonsense token fails to suppress the query, a NO_MATCH from this
     function is weak evidence and it cannot anchor confidence accumulation.
  2. What exactly is in a returned row? The fallback has to recover a
     signature from the result, so it needs `name` or an equivalent.
  3. Call signature, API stability, and whether it needs client state or
     kwargs the client methods do not.
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.multi_record import multi_record_search

TENANT = "escalation-memory-spike"
PATTERN_CATEGORY = "escalation_pattern"
DB_PATH = (
    Path(os.environ.get("TEMP", ".")) / "claude" / "escalation-memory-spike" / "spike.db"
)

# Tokens known to appear in the spike corpus (raw_context bodies).
REAL = ["refund", "chargeback", "damaged", "identity", "shipping"]
# Tokens that appear nowhere. Verified at runtime before use.
NONSENSE = ["flibbertigibbet", "zzzznonexistenttoken", "quixotrophic", "blorpwidget"]


def banner(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def verdict_of(results) -> str:
    return results.verdict.code.value


def line(label: str, results, extra: str = "") -> None:
    v = results.verdict
    print(f"  {label:<46} rows={len(results):<4} {v.code.value:<17}{extra}")


def main() -> None:
    if not DB_PATH.exists():
        sys.exit(f"spike db missing at {DB_PATH}. Run scripts/spike_store.py first.")

    client = MemoryClient.local(str(DB_PATH), tenant_id=TENANT)
    print(f"db     : {DB_PATH}")
    print(f"tenant : {client.get_tenant()}")

    # ------------------------------------------------------------------
    banner("Q3: call signature, stability, required state")
    sig = inspect.signature(multi_record_search)
    print(f"  signature      : multi_record_search{sig}")
    print(f"  limit default  : "
          f"{sig.parameters['limit'].default}   "
          f"(client.search default is 20; this is different)")

    import sibyl_memory_client as pkg
    import sibyl_memory_client.multi_record as mr

    print(f"  in package dir : {'multi_record_search' in dir(pkg)}")
    print(f"  submodule in dir(pkg): {'multi_record' in dir(pkg)}")
    print(f"  module __all__ : {getattr(mr, '__all__', 'NOT DEFINED')}")
    print(f"  module file    : {Path(mr.__file__).name}")
    print(f"  leading underscore on name: {multi_record_search.__name__.startswith('_')}")

    # Does any shipped client method route through it? If the SDK's own
    # surface does not call it, nothing in the package protects its shape.
    client_src = Path(
        pkg.client.__file__ if hasattr(pkg, "client") else ""
    ).read_text(encoding="utf-8")
    print(f"  referenced in client.py: "
          f"{'multi_record_search' in client_src}")
    print(f"  imported by client.py  : "
          f"{'from .multi_record import' in client_src}")

    # ------------------------------------------------------------------
    banner("Q1: AND enforcement")

    print("\n-- step 1: confirm each nonsense token has zero support --")
    usable = []
    for tok in NONSENSE:
        r_mr = multi_record_search(client, tok, limit=20)
        r_se = client.search_entities(tok, category=PATTERN_CATEGORY)
        r_s = client.search(tok)
        supported = bool(r_mr) or bool(r_se) or bool(r_s)
        print(f"  {tok:<22} multi_record={len(r_mr)}/{verdict_of(r_mr):<14} "
              f"search_entities={len(r_se)} search={len(r_s)}  "
              f"{'HAS SUPPORT, skipping' if supported else 'no support, usable'}")
        if not supported:
            usable.append(tok)

    print("\n-- step 2: confirm each real token DOES return rows --")
    for tok in REAL:
        r = multi_record_search(client, tok, limit=20)
        line(f"multi_record({tok!r})", r)

    print("\n-- step 3: mixed queries, varying the real:nonsense ratio --")
    print("     the disqualifying result is rows>0 with an OK verdict, which")
    print("     is what client.search() did with the same input")
    combos = []
    if usable:
        n = usable[0]
        combos += [
            (f"{n} refund", "1 nonsense : 1 real"),
            (f"refund {n}", "1 real : 1 nonsense, order swapped"),
            (f"{n} refund chargeback", "1 nonsense : 2 real"),
            (f"{n} refund chargeback damaged identity", "1 nonsense : 4 real"),
        ]
        if len(usable) >= 2:
            combos += [
                (f"{usable[0]} {usable[1]} refund", "2 nonsense : 1 real"),
                (f"{usable[0]} {usable[1]} refund chargeback damaged",
                 "2 nonsense : 3 real"),
            ]
        if len(usable) >= 3:
            combos.append(
                (f"{usable[0]} {usable[1]} {usable[2]} refund",
                 "3 nonsense : 1 real")
            )

    verdicts_seen = {}
    for query, description in combos:
        r_mr = multi_record_search(client, query, limit=20)
        r_se = client.search_entities(query, category=PATTERN_CATEGORY)
        r_s = client.search(query)
        verdicts_seen[description] = (len(r_mr), verdict_of(r_mr))
        print(f"\n  {description}")
        print(f"    query: {query!r}")
        print(f"    multi_record_search  rows={len(r_mr):<4} {verdict_of(r_mr)}")
        print(f"    search_entities      rows={len(r_se):<4} {verdict_of(r_se)}"
              f"   (the strict baseline)")
        print(f"    search               rows={len(r_s):<4} {verdict_of(r_s)}"
              f"   (the leaky one)")

    print("\n-- verdict summary for the mixed queries --")
    leaked = [d for d, (n, _) in verdicts_seen.items() if n > 0]
    if leaked:
        print("  LEAKED (returned rows despite an unsupported token):")
        for d in leaked:
            print(f"    {d}: {verdicts_seen[d]}")
    else:
        print("  No mixed query returned rows. The unsupported token")
        print("  suppressed every one of them.")

    # ------------------------------------------------------------------
    banner("Q2: row shape")

    r = multi_record_search(client, "refund", limit=5)
    if not r:
        print("  no rows to inspect")
        return

    row = r[0]
    mr_keys = sorted(row.keys())
    se_row = client.search_entities("refund", category=PATTERN_CATEGORY)[0]
    se_keys = sorted(se_row.keys())
    s_row = client.search("refund")[0]
    s_keys = sorted(s_row.keys())

    print(f"  multi_record_search keys : {mr_keys}")
    print(f"  search_entities keys     : {se_keys}")
    print(f"  search keys              : {s_keys}")
    print()
    print(f"  matches search_entities shape : {mr_keys == se_keys}")
    print(f"  matches search shape          : {mr_keys == s_keys}")
    print(f"  => third distinct shape       : "
          f"{mr_keys != se_keys and mr_keys != s_keys}")

    print()
    print("  can the signature be recovered from a row?")
    print(f"    has 'name'      : {'name' in row}")
    print(f"    has 'key'       : {'key' in row}")
    print(f"    key value       : {str(row.get('key'))[:60]!r}")
    print(f"    category value  : {str(row.get('category'))[:40]!r}")
    print(f"    tier value      : {str(row.get('tier'))[:20]!r}")
    print(f"    body type       : {type(row.get('body')).__name__}")

    # Is `key` the entity name for entity-tier rows, or the row id?
    print()
    print("  is `key` the signature for entity-tier rows?")
    for hit in r:
        if hit.get("tier") == "entity":
            k = hit.get("key")
            looked_up = client.get_entity(PATTERN_CATEGORY, k) if k else None
            print(f"    key={str(k)[:46]:<48} get_entity -> "
                  f"{'FOUND' if looked_up else 'not found'}")
            break
    else:
        print("    no entity-tier row in this result")

    print()
    print("  tier distribution in the result (fallback must filter):")
    from collections import Counter
    print(f"    {Counter(h.get('tier') for h in r)}")

    print()
    print("  is there a category/tier filter kwarg like search_entities has?")
    params = set(sig.parameters)
    print(f"    parameters: {sorted(params)}")
    print(f"    has 'category': {'category' in params}")
    print(f"    has 'tiers'   : {'tiers' in params}")


if __name__ == "__main__":
    main()
