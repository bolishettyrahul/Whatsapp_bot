"""Checks that exercise the built pipeline, as opposed to the dataset.

Split out of evaluation/main.py, which holds the assertions that need only the
data and the source: rule precision, regressions, leakage, coverage, schema,
and the hardcoded-id scan. Everything here constructs the real router and runs
messages through it.

Imported and called by evaluation/main.py; not a separate entry point.
"""

import collections
import importlib.util
import os
import sys

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODE)

ROOT = os.path.dirname(CODE)
DATA = os.path.join(ROOT, "dataset")


def load_pipeline():
    """Import code/main.py explicitly, by path.

    A bare `import main` is ambiguous: this package contains a main.py too, and
    which one wins depends on sys.path ordering. It silently resolved to the
    wrong one once evaluation/ was added to the path, so the ambiguity is
    removed rather than worked around.
    """
    path = os.path.join(CODE, "main.py")
    if os.path.getsize(path) == 0:
        raise RuntimeError(f"{path} is EMPTY -- the entry point has been truncated")
    spec = importlib.util.spec_from_file_location("router_pipeline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("choose", "main", "make_client"):
        if not hasattr(module, name):
            raise RuntimeError(f"{path} loaded but has no {name}() -- wrong file "
                               f"imported, or the entry point is incomplete")
    return module

def rate_limiting(check, section):
    """Pacing and backoff. No network, no key -- pure timing behaviour.

    Worth asserting rather than eyeballing: the whole point of the pacer is
    that it does something on a boundary that a short happy-path run never
    reaches, so a broken one would look fine right up until a real run.
    """
    section("9. RATE LIMITING: PACING AND BACKOFF")
    import time                                       # noqa: E402

    import llm                                        # noqa: E402

    # Requests-per-minute: the (rpm+1)-th call must block until the window rolls.
    pacer = llm.Pacer("TEST", rpm=5, tpm=0)
    start = time.monotonic()
    for _ in range(5):
        pacer.reserve(0)
    check(time.monotonic() - start < 0.2, "calls inside the request budget do not block")

    pacer.events[0] = (time.monotonic() - 59.5, 0)
    start = time.monotonic()
    pacer.reserve(0)
    waited = time.monotonic() - start
    print(f"  over-budget request waited {waited:.2f}s")
    check(0.3 < waited < 1.5, "a request over the RPM budget blocks until the window rolls",
          f"waited {waited:.2f}s")

    # Tokens-per-minute binds first on this workload, so it gets its own check.
    pacer = llm.Pacer("TEST", rpm=1000, tpm=10000)
    pacer.reserve(9000)
    pacer.events[0] = (time.monotonic() - 59.5, 9000)
    start = time.monotonic()
    pacer.reserve(2000)
    waited = time.monotonic() - start
    print(f"  over-budget tokens waited {waited:.2f}s")
    check(0.3 < waited < 1.5, "a request over the TPM budget blocks even when RPM is free",
          f"waited {waited:.2f}s")

    # 429 must be retried, honouring the server's own Retry-After.
    class RateLimited(Exception):
        status_code = 429

        class response:
            headers = {"retry-after": "0.3"}

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RateLimited()
        return "ok"

    check(llm.call_with_retry(flaky) == "ok" and attempts["n"] == 3,
          "a 429 is retried with backoff and eventually succeeds",
          f"{attempts['n']} attempts")

    # A bad key must NOT be retried into silence.
    class Unauthorized(Exception):
        status_code = 401

    tries = {"n": 0}

    def unauthorized():
        tries["n"] += 1
        raise Unauthorized()

    try:
        llm.call_with_retry(unauthorized)
        check(False, "a non-retryable status is raised, not swallowed")
    except Unauthorized:
        check(tries["n"] == 1, "a non-retryable status is raised immediately",
              f"{tries['n']} attempts")

    print(f"  configured: LLM {llm.setting('LLM_RPM')}rpm/{llm.setting('LLM_TPM')}tpm  "
          f"VISION {llm.setting('VISION_RPM')}rpm  ASR {llm.setting('ASR_RPM')}rpm")


def taxonomy_fact_threading(check, section):
    """Ambiguous model selections must use row facts, not option order.

    The offline fallback already passed facts to taxonomy.disambiguate(). This
    regression test exercises the model parser too, where omitted
    ``message_type`` previously resolved an ambiguous option by frequency.
    """
    section("10. MODEL PARSER: FACT-AWARE TAXONOMY RESOLUTION")
    import json                                       # noqa: E402

    import context                                    # noqa: E402
    import router                                     # noqa: E402
    import similarity                                 # noqa: E402
    import taxonomy                                   # noqa: E402

    data = context.Dataset()
    catalogue = taxonomy.Taxonomy(data.samples)

    def option_with(types):
        wanted = set(types)
        return next(o for o in catalogue.options if set(o.types) == wanted)

    parser = router.Router(None, catalogue, "test")
    cases = (
        (("greeting", "forward"), {"forwarded": True, "chain_forward": False}, "forward"),
        (("promotion", "spam"), {"is_business": True, "business_verified": False,
                                   "has_relationship": False}, "spam"),
    )
    for types, facts, expected in cases:
        option = option_with(types)
        raw = json.dumps({"reason_index": option.index})
        parsed, why = parser._parse(raw, facts=facts)
        check(parsed is not None and parsed[1] == expected,
              f"{expected} is selected from {types} using row facts", why or repr(parsed))

    personal_work = next(row for row in data.samples
                         if row["conversation_type"] == "personal"
                         and row["message_type"] == "urgent"
                         and "work context" in row["reason"].lower())
    facts = context.claim_facts(context.build(personal_work, data), personal_work)
    check(facts["work_context"],
          "a direct message between members of a shared work group has work context")

    # A future record is not valid evidence even if its text is a better match.
    message = {"user_id": "u", "created_at": "2026-08-02 12:00", "message_text": "alpha",
               "business_id": "", "group_id": "", "sender_user_id": "s"}
    history = [
        {"message_id": "past", "user_id": "u", "created_at": "2026-08-01 12:00",
         "message_text": "alpha", "business_id": "", "group_id": "", "sender_user_id": "s"},
        {"message_id": "future", "user_id": "u", "created_at": "2026-08-03 12:00",
         "message_text": "alpha", "business_id": "", "group_id": "", "sender_user_id": "s"},
    ]
    evidence = similarity.find_evidence(message, history, limit=2)
    check([row["message_id"] for row, _score, _event in evidence] == ["past"],
          "evidence retrieval excludes future messages")


def _ids(raw):
    """Evidence column -> a set. `none` is the empty set, not a member."""
    return {p.strip() for p in str(raw).split(";")
            if p.strip() and p.strip().lower() != "none"}


def _facts(row, data):
    """Delegate to the pipeline's own fact extraction, so the harness scores
    exactly what the pipeline reasoned over."""
    import context                                    # noqa: E402

    return context.claim_facts(context.build(row, data), row)


def _scorecard(check, pairs, evidence_pairs, confidences, corrects,
               reason_records, types, samples):
    """The five graded axes, each as a number rather than an impression."""
    import metrics                                    # noqa: E402

    action_pairs = [(g, p) for g, p, _gt, _pt in pairs]
    type_pairs = [(gt, pt) for _g, _p, gt, pt in pairs]

    import taxonomy as tax                          # noqa: E402

    print()
    print("  SCORECARD (the five graded axes)")
    a_f1 = metrics.macro_f1(action_pairs, ("notify", "digest", "mute"))
    t_f1 = metrics.macro_f1(type_pairs, types)
    type_ceiling, pair_ceiling, f1_ceiling, _pc = metrics.loo_ceiling(samples, tax.Taxonomy)
    pair_reachability = metrics.loo_pair_reachability(samples, tax.Taxonomy)
    total = len(action_pairs)
    print(f"    action      accuracy {sum(1 for g, p in action_pairs if g == p)}"
          f"/{total}   macro-F1 {a_f1:.3f}")
    print(f"    type        accuracy {sum(1 for g, p in type_pairs if g == p)}"
          f"/{total}   macro-F1 {t_f1:.3f}"
          f"      [LOO ceiling {type_ceiling}/{total}, macro-F1 {f1_ceiling:.3f}]")
    print(f"    both        {sum(1 for g, p, gt, pt in pairs if g == p and gt == pt)}"
          f"/{total}"
          f"                            [LOO ceiling {pair_ceiling}/{total}]")

    ev = metrics.evidence_scores(evidence_pairs)
    print(f"    evidence    exact {ev['exact']}/{ev['total']}   "
          f"precision {ev['precision']:.3f}  recall {ev['recall']:.3f}  "
          f"F1 {ev['f1']:.3f}  (correct 'none' x{ev['correct_abstentions']})")

    cal = metrics.calibration(confidences, corrects)
    print(f"    confidence  ECE {cal['ece']:.3f}  Brier {cal['brier']:.3f}  "
          f"mean {cal['mean_confidence']:.3f} vs accuracy {cal['accuracy']:.3f}"
          f"  (over by {cal['overconfidence']:+.3f})")
    print(f"                discrimination {cal['discrimination']:+.3f}  "
          f"[right {cal['mean_conf_when_right']:.3f} vs wrong "
          f"{cal['mean_conf_when_wrong']:.3f}], {cal['distinct_values']} distinct values")

    supported, contradicted, unchecked, details = metrics.reason_entailment(reason_records)
    checkable = supported + contradicted
    rate = supported / checkable if checkable else 0.0
    print(f"    reason      entailment {supported}/{checkable} supported "
          f"({rate:.0%}), {unchecked} make no checkable claim")
    if details:
        print("                contradicted: " + ", ".join(f"{k} x{v}"
                                                           for k, v in details.most_common()))

    all_zero_recall = metrics.zero_recall_classes(type_pairs, types)
    gated_zero_recall = metrics.zero_recall_classes(type_pairs, types,
                                                     pair_reachability)
    protocol_limited = [name for name in all_zero_recall
                        if name not in gated_zero_recall]
    if all_zero_recall:
        print("    type coverage  zero recall: " + ", ".join(all_zero_recall))
    if protocol_limited:
        print("                   LOO pair unavailable: " + ", ".join(protocol_limited))

    # A reason that asserts something false about the row is worse than a vague
    # one: it is a confident, checkable, wrong statement.
    check(contradicted == 0 or rate >= 0.80,
          "reason claims are supported by the row's own facts",
          f"{contradicted} contradicted")
    # Confidence that does not separate right from wrong carries no information,
    # however well-centred it happens to be.
    # Gated together on purpose. Discrimination bought by wrecking calibration
    # is not an improvement, and a well-centred constant is not information.
    check(cal["discrimination"] >= 0.02,
          "confidence discriminates right from wrong (>= +0.02)",
          f"discrimination {cal['discrimination']:+.3f}")
    check(cal["ece"] <= 0.05, "confidence stays calibrated (ECE <= 0.05)",
          f"ECE {cal['ece']:.3f}")
    check(not gated_zero_recall,
          "every LOO-reachable action/type category has at least one correct prediction",
          "zero recall: " + ", ".join(gated_zero_recall))

def media_layer(check, section):
    """Container sniffing and cache invalidation. Neither needs a key."""
    section("8. MEDIA: CONTAINER SNIFFING AND CACHE INVALIDATION")
    import context                                    # noqa: E402
    import media                                      # noqa: E402

    # V1 -- every filename in this dataset lies about its container.
    kinds = collections.Counter()
    mismatched = []
    for folder in ("images", "audio"):
        root = os.path.join(DATA, "media", folder)
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            extension, mime = media.sniff(path)
            kinds[extension] += 1
            if extension != os.path.splitext(name)[1].lower():
                mismatched.append(name)
    print(f"  real containers: {dict(sorted(kinds.items()))}")
    print(f"  filenames whose extension is wrong: {len(mismatched)}")
    check(mismatched, "sniffing detects mislabelled containers",
          "none found -- sniff() may be broken")
    check(all(media.sniff(os.path.join(DATA, "media", "images", n))[1]
              is not None for n in os.listdir(os.path.join(DATA, "media", "images"))),
          "every image resolves to a known MIME type")

    # Images the vision API cannot accept must be convertible, not skipped.
    unsupported = []
    for name in sorted(os.listdir(os.path.join(DATA, "media", "images"))):
        path = os.path.join(DATA, "media", "images", name)
        _extension, mime = media.sniff(path)
        if mime not in media.SUPPORTED_IMAGE_MIME:
            with open(path, "rb") as fh:
                converted, _new = media._transcode(fh.read(), mime, path)
            unsupported.append((name, mime, converted is not None))
    for name, mime, ok in unsupported:
        print(f"  {name} is {mime} -> convertible: {ok}")
    check(all(ok for _n, _m, ok in unsupported),
          "images in unsupported formats can be transcoded", str(unsupported))

    # V2 -- a stale entry must read as a miss, or a prompt edit silently
    # keeps serving output generated by the previous prompt.
    data = context.Dataset()
    cache = media.MediaCache()
    sample = next((m for m in data.messages if (m.get("media_id") or "").strip()), None)
    if sample:
        media_id = sample["media_id"].strip()
        kind = sample["media_type"].strip()
        path = data.media_path(sample)
        cache.store(media_id, "placeholder description", kind, path)
        check(cache.lookup(media_id, kind, path) == "placeholder description",
              "a fresh entry reads back")

        cache._store[media_id]["prompt_version"] = media.PROMPT_VERSION - 1
        check(cache.lookup(media_id, kind, path) is None,
              "an entry from an older prompt version is a miss")

        cache._store[media_id]["prompt_version"] = media.PROMPT_VERSION
        cache._store[media_id]["source_digest"] = "0" * 64
        check(cache.lookup(media_id, kind, path) is None,
              "an entry whose source file changed is a miss")

        cache._store[media_id]["source_digest"] = media._digest(path)
        cache._store[media_id]["model"] = "some-other-model"
        check(cache.lookup(media_id, kind, path) is None,
              "an entry from a different model is a miss")

    # Coverage is reported, not asserted: it is 0 until the cache is built,
    # and failing the whole harness for an unbuilt cache would be noise.
    described, total = media.MediaCache().coverage(data.messages)
    print(f"  media cache coverage: {described}/{total}"
          + ("  (run --build-media-cache)" if described < total else ""))


# --------------------------------------------------------------------------
def end_to_end(check, section, use_model=False):
    """Run the real pipeline over the 30 labelled rows and score it.

    LEAVE-ONE-OUT keeps the held-out row's reason/action/type out of the
    catalogue. Otherwise a prediction can select the exact answer it is being
    judged against; each row is instead scored against a catalogue built from
    the other 29 examples.

    Offline by default, so the number is reproducible without a key. With
    keys configured in .env and --model passed, the same harness scores the
    model path.
    """
    section("7. END-TO-END ON THE 30 LABELLED ROWS (leave-one-out)")
    import context                                    # noqa: E402
    import llm                                        # noqa: E402
    import media                                      # noqa: E402
    import postprocess                                # noqa: E402
    import router as router_mod                       # noqa: E402
    import rules                                      # noqa: E402
    import taxonomy                                   # noqa: E402

    import metrics                                    # noqa: E402

    pipeline = load_pipeline()

    data = context.Dataset()
    client, model = None, ""
    if use_model:
        client, model, why = pipeline.make_client()
        if client is None:
            print(f"  (--model requested but {why}; scoring the offline path)")
        else:
            print(f"  scoring the model path with {model}")
    vision, vision_model, _why = (llm.vision_client() if use_model else (None, "", None))
    asr, asr_model, _why = (llm.audio_client() if use_model else (None, "", None))
    cache = media.MediaCache(vision, vision_model, asr, asr_model,
                             offline_cache=True)
    fixer = postprocess.Postprocessor(taxonomy.Taxonomy(data.samples), data.history)

    action_ok = type_ok = both_ok = evidence_ok = 0
    predictions = []
    pairs = []      # (gold_action, pred_action, gold_type, pred_type)
    evidence_pairs = []
    confidences = []
    corrects = []
    reason_records = []
    tiers = collections.Counter()
    confusion = collections.Counter()
    type_confusion = collections.Counter()

    scored_rows = 0
    for row in data.samples:
        others = [s for s in data.samples if s["message_id"] != row["message_id"]]
        catalogue = taxonomy.Taxonomy(others)
        held_out = context.Dataset.__new__(context.Dataset)
        held_out.__dict__ = dict(data.__dict__)
        held_out.samples = others
        engine = (router_mod.Router(client, catalogue, model) if client else None)

        try:
            tier, action, mtype, reason, confidence, evidence, media_text = pipeline.choose(
                row, held_out, catalogue, cache, engine)
        except llm.QuotaExhausted as exhausted:
            # A partial eval is not a gate. Say so and stop, rather than
            # reporting a score computed over whatever happened to fit.
            print()
            print(f"  ! quota exhausted after {scored_rows}/{len(data.samples)} rows")
            print(f"  !   {str(exhausted)[:160]}")
            print("  ! scores below would be partial and are NOT a valid gate.")
            check(False, "model-path evaluation completed",
                  f"stopped at {scored_rows}/{len(data.samples)} rows")
            return
        scored_rows += 1
        (action, mtype, reason, confidence), evidence_ids = fixer.apply(
            (action, mtype, reason, confidence), row,
            data.businesses.get((row.get("business_id") or "").strip()),
            evidence, media_text)

        tiers[tier] += 1
        predictions.append(action)
        pairs.append((row["action"], action, row["message_type"], mtype))
        hit_a = action == row["action"]
        hit_t = mtype == row["message_type"]
        action_ok += hit_a
        type_ok += hit_t
        both_ok += hit_a and hit_t
        predicted_evidence = _ids(evidence_ids)
        gold_evidence = _ids(row["evidence_message_ids"])
        evidence_ok += predicted_evidence == gold_evidence

        evidence_pairs.append((predicted_evidence, gold_evidence))
        confidences.append(float(confidence))
        corrects.append(1 if hit_a else 0)
        reason_records.append((reason, _facts(row, data)))
        if not hit_a:
            confusion[f"{row['action']}->{action}"] += 1
        if not hit_t:
            type_confusion[f"{row['message_type']}->{mtype}"] += 1

    total = len(data.samples)
    print(f"  tier mix: " + "  ".join(f"T{t}={tiers[t]}" for t in sorted(tiers)))
    print(f"  action           {action_ok}/{total}  ({action_ok / total:.0%})")
    print(f"  message_type     {type_ok}/{total}  ({type_ok / total:.0%})")
    print(f"  both correct     {both_ok}/{total}  ({both_ok / total:.0%})")
    print(f"  evidence exact   {evidence_ok}/{total}  ({evidence_ok / total:.0%})")
    if confusion:
        print("  action errors: " + ", ".join(f"{k} x{v}" for k, v in confusion.most_common()))
    if type_confusion:
        print("  type errors:   " + ", ".join(f"{k} x{v}"
                                              for k, v in type_confusion.most_common(8)))
    # Distribution skew is the failure mode a single accuracy number hides:
    # a model that mutes everything scores well on a mute-heavy set.
    predicted = collections.Counter(p for p in predictions)
    gold_mix = collections.Counter(r["action"] for r in data.samples)
    print(f"  predicted mix: {dict(predicted)}")
    print(f"  gold mix:      {dict(gold_mix)}")

    metrics.render_per_class(pairs, 0, 1, "ACTION", ("notify", "digest", "mute"))
    metrics.render_per_class(pairs, 2, 3, "MESSAGE_TYPE", taxonomy.TYPES)
    _scorecard(check, pairs, evidence_pairs, confidences, corrects,
               reason_records, taxonomy.TYPES, data.samples)

    # A coin flip over three classes is 33%. Anything at or below that means
    # the pipeline is contributing nothing and the number must not be dressed up.
    check(action_ok / total > 0.34, "action accuracy beats chance (33%)",
          f"{action_ok / total:.0%}")
    check(evidence_ok / total >= 0.50, "evidence matches gold on at least half",
          f"{evidence_ok / total:.0%}")
