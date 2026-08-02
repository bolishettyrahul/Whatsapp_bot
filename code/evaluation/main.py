"""Self-evaluation harness.

Consolidates six exploratory probes into repeatable checks:
  rule_probe / refined_probe -> rule precision on the labelled samples
  miss_probe                 -> which gold-mute rows no rule catches
  hinglish_probe             -> multilingual coverage + the legitimate-row guard
  leakage_probe              -> sample<->scored overlap, keyword-vs-intent
  dup_user_probe             -> same-user transfer audit, full rule sweep

Run:  python code/evaluation/main.py
Exit code 1 if any assertion fails.

Sections 1-6 here need only the data and the source. The checks that construct
the real router and push messages through it live in pipeline_checks.py.

The 30 labelled rows in sample_messages.csv are the ONLY feedback loop that
exists before submission. Nothing is trained; they are an answer key we are
allowed to read.
"""

import collections
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# NOTE: do not insert this file's own directory ahead of code/. Both
# directories contain a main.py, and putting evaluation/ first makes
# `import main` resolve to this file instead of the pipeline entry point.
# pipeline_checks is importable regardless, because Python already puts the
# running script's directory on sys.path.

import pipeline_checks  # noqa: E402
import rules            # noqa: E402
import similarity       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "dataset")

ACTIONS = {"notify", "digest", "mute"}
TYPES = {"personal", "urgent", "event", "payment", "business_update", "promotion",
         "greeting", "forward", "spam", "scam", "unknown"}
HEADER = ["message_id", "action", "message_type", "reason", "confidence", "evidence_message_ids"]
CONF_LO, CONF_HI = 0.78, 0.91

failures = []


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def check(condition, label, detail=""):
    ok = bool(condition)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)
    return ok


def section(title):
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


# --------------------------------------------------------------------------
def rule_precision(samples, businesses):
    """Every rule fire on a labelled row must agree with the gold action.

    A false MUTE is the most expensive error in this dataset -- it suppresses
    a message the user needed -- so it is asserted separately.
    """
    section("1. RULE PRECISION ON THE 30 LABELLED SAMPLES")
    stats = collections.defaultdict(lambda: [0, 0, 0])
    false_mutes = []

    for m in samples:
        hit = rules.classify(m, businesses.get(m.get("business_id")))
        if not hit:
            continue
        name, _tier, action, mtype = hit
        s = stats[name]
        s[0] += 1
        s[1] += (action == m["action"])
        if mtype is not None:
            s[2] += (mtype == m["message_type"])
        if action == "mute" and m["action"] != "mute":
            false_mutes.append((name, m["message_id"], m["action"], m["message_type"],
                                m["message_text"][:70]))

    for name in sorted(stats):
        fired, act_ok, type_ok = stats[name]
        print(f"  {name:30s} fired={fired:2d}  action={act_ok}/{fired}  type={type_ok}/{fired}")
        check(act_ok == fired, f"{name}: no action errors")

    check(not false_mutes, "zero false mutes on gold notify/digest rows", str(false_mutes))

    missed = [m for m in samples
              if m["action"] == "mute" and not rules.classify(m, businesses.get(m.get("business_id")))]
    print(f"\n  gold-mute rows no rule catches: {len(missed)} "
          f"(expected -- these need history, not content)")
    for m in missed:
        print(f"    {m['message_id']} {m['message_type']:10s} {m['message_text'][:58]!r}")


# --------------------------------------------------------------------------
def regressions(scored, samples, businesses):
    """Named rows that previously broke, or that must never break."""
    section("2. REGRESSION SET")
    by_id = {m["message_id"]: m for m in scored}
    s_by_id = {m["message_id"]: m for m in samples}

    def fired(mid, pool):
        m = pool.get(mid)
        return rules.classify(m, businesses.get(m.get("business_id"))) if m else None

    # The false positive that was actually found: a legitimate delivery message
    # reading "share pickup code only after courier arrives".
    legit_delivery = [mid for mid in by_id
                      if "pickup code" in (by_id[mid].get("message_text") or "").lower()]
    print(f"  legitimate 'pickup code' rows: {len(legit_delivery)}")
    for mid in legit_delivery:
        hit = fired(mid, by_id)
        check(hit is None or hit[2] != "mute",
              f"legitimate delivery row {mid} is not muted", str(hit))

    # A brand advisory that NAMES otp must not be read as soliciting one.
    advisories = [mid for mid in s_by_id if rules.WARNS.search(s_by_id[mid].get("message_text", ""))]
    print(f"  advisory rows naming a credential: {len(advisories)}")
    for mid in advisories:
        check(not rules.asks_for_secret(s_by_id[mid]["message_text"]),
              f"advisory {mid} not read as credential solicitation")

    # Injection rows must all be caught.
    inj = [m["message_id"] for m in scored if rules.impersonates_system(m.get("message_text", ""))]
    print(f"  injection rows detected: {len(inj)} -> {', '.join(inj)}")
    check(len(inj) >= 5, "injection cluster detected (expected >= 5)", f"found {len(inj)}")

    # Hinglish: the scams must fire, the legitimate rows must not be muted by rule.
    hing = [m for m in scored if rules.is_hinglish(m.get("message_text", ""))]
    print(f"  hinglish rows: {len(hing)}")
    for m in hing:
        hit = rules.classify(m, businesses.get(m.get("business_id")))
        verdict = hit[2] if hit else "-> model"
        flag = "solicits" if rules.asks_for_secret(m["message_text"]) else "benign"
        print(f"    {m['message_id']} {verdict:8s} {flag:9s} {m['message_text'][:50]!r}")
        if not rules.asks_for_secret(m["message_text"]) and hit and hit[1] == 0:
            check(False, f"hinglish row {m['message_id']} hard-blocked without soliciting a secret")
    check(hing, "hinglish rows present and inspected")


# --------------------------------------------------------------------------
def leakage_and_intent(scored, samples):
    section("3. LEAKAGE AND KEYWORD-VS-INTENT")

    transfers, priors = [], []
    for m in scored:
        match, r, kind = similarity.find_labelled_match(m, samples)
        if kind == "transfer":
            transfers.append((m["message_id"], match["message_id"], round(r, 3)))
        elif kind == "prior":
            priors.append((m["message_id"], match["message_id"], round(r, 3)))

    print(f"  direct label transfers: {len(transfers)}")
    print(f"  labelled priors (prompt context only): {len(priors)}")
    for p in priors:
        print(f"    {p[0]} ~ {p[1]} (r={p[2]}) -> prompt context only")
    check(not transfers, "labelled examples never transfer actions or types")
    check(priors, "similar labelled examples remain available as non-binding context")

    # Intent proof: one word, opposite gold actions, inside the labelled set.
    print("\n  keyword-inversion evidence (same word, different gold action):")
    inverted = 0
    for kw in ["otp", "blocked", "payment", "offer", "verify", "urgent", "reminder"]:
        rows = [s for s in samples if kw in (s.get("message_text") or "").lower()]
        acts = collections.Counter(r["action"] for r in rows)
        if len(acts) > 1:
            inverted += 1
            print(f"    '{kw}' -> {dict(acts)}  *** INVERTED ***")
        elif rows:
            print(f"    '{kw}' -> {dict(acts)}")
    check(inverted >= 2,
          "keyword inversion demonstrated (proves rules alone cannot decide)",
          f"only {inverted} inverted")


# --------------------------------------------------------------------------
def coverage(scored, businesses):
    section("4. TIER COVERAGE ON THE 110 SCORED ROWS")
    counts = collections.Counter()
    for m in scored:
        hit = rules.classify(m, businesses.get(m.get("business_id")))
        counts[f"tier{hit[1]}:{hit[0]}" if hit else "tier2:model"] += 1
    for k, v in sorted(counts.items()):
        print(f"  {k:40s} {v:3d}")
    deterministic = sum(v for k, v in counts.items() if not k.startswith("tier2"))
    pct = deterministic / len(scored) if scored else 0
    print(f"\n  action decided in code: {deterministic}/{len(scored)} ({pct:.0%})")
    check(0.15 <= pct <= 0.45,
          "rule coverage in the expected 15-45% band",
          f"{pct:.0%} -- outside band suggests a rule broke or over-fires")


# --------------------------------------------------------------------------
def output_schema(scored):
    section("5. OUTPUT SCHEMA")
    with open(os.path.join(DATA, "output.csv"), encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    check(header == HEADER, "header matches required column order", str(header))

    rows = load("output.csv")
    check(len(rows) == len(scored), f"row count == {len(scored)}", str(len(rows)))
    check([r["message_id"] for r in rows] == [m["message_id"] for m in scored],
          "message_id order matches messages.csv")

    filled = [r for r in rows if (r.get("action") or "").strip()]
    if not filled:
        print("  (predictions not generated yet -- value checks skipped)")
        return

    check(all(r["action"] in ACTIONS for r in filled), "all actions in enum")
    check(all(r["message_type"] in TYPES for r in filled), "all message_types in enum")
    confs = [float(r["confidence"]) for r in filled if r.get("confidence")]
    if confs:
        out = [c for c in confs if not (CONF_LO - 1e-9 <= c <= CONF_HI + 1e-9)]
        check(not out, f"confidence within observed band {CONF_LO}-{CONF_HI}", str(out[:5]))

    history_ids = {h["message_id"] for h in load("message_history.csv")}
    bad = [r["message_id"] for r in filled
           for e in r["evidence_message_ids"].split(";")
           if e.strip() and e.strip().lower() != "none" and e.strip() not in history_ids]
    check(not bad, "all evidence ids exist in message_history.csv", str(bad[:5]))


# --------------------------------------------------------------------------
def no_hardcoded_ids():
    """AGENTS.md 6.3 forbids hardcoded labels.

    A runtime similarity retriever over a participant-facing file is a model.
    `if message_id == "...": return "mute"` is a hardcoded label. This scans
    every source file for a dataset id appearing anywhere -- comment, string
    or expression -- because a reviewer running the same grep should get a
    clean result without having to judge intent line by line.
    """
    section("6. NO HARDCODED DATASET IDS IN SOURCE")
    code_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pattern = re.compile(r"\b(msg_\d{3}|sample_msg_\d+|message_\d{4}|"
                         r"business_\d{3}|group_\d{3}|u_\d{3}|img_\d{3}|vn_\d{3})\b")
    hits = []
    for folder, _dirs, files in os.walk(code_dir):
        if "__pycache__" in folder:
            continue
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(folder, fname)
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    for match in pattern.finditer(line):
                        hits.append(f"{os.path.relpath(path, code_dir)}:{lineno}: {match.group(0)}")

    for h in hits:
        print(f"    {h}")
    check(not hits, "no dataset ids anywhere in code/", f"{len(hits)} occurrence(s)")


# --------------------------------------------------------------------------
def main():
    scored = load("messages.csv")
    samples = load("sample_messages.csv")
    businesses = {b["business_id"]: b for b in load("business_accounts.csv")}

    print(f"messages.csv={len(scored)}  sample_messages.csv={len(samples)}  "
          f"business_accounts.csv={len(businesses)}")

    rule_precision(samples, businesses)
    regressions(scored, samples, businesses)
    leakage_and_intent(scored, samples)
    coverage(scored, businesses)
    output_schema(scored)
    no_hardcoded_ids()
    pipeline_checks.media_layer(check, section)
    pipeline_checks.rate_limiting(check, section)
    pipeline_checks.taxonomy_fact_threading(check, section)
    pipeline_checks.end_to_end(check, section, use_model="--model" in sys.argv)

    section("SUMMARY")
    if failures:
        print(f"  {len(failures)} FAILED:")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
