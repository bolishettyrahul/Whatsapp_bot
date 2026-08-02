"""Message Notification Router -- entry point.

    python code/main.py                      # route all 110 rows -> dataset/output.csv
    python code/main.py --limit 5 -v         # smoke test, printing each decision
    python code/main.py --no-model           # deterministic tiers only
    python code/main.py --build-media-cache  # capture media once, then exit

Model access is configured in .env -- see .env.example. No vendor is named in
code: llm.py resolves a base URL, key and model per role, so Groq, Gemini,
OpenRouter and a local Ollama are all the same path. Without keys the run still
completes and still writes a valid output.csv, but the rows that need judgement
fall back to a deterministic heuristic and the summary says so.

Pipeline, per message:

  Tier 0   injection or credential solicitation -> mute/scam without asking the
           model. The point is security: an injection payload never reaches a
           prompt. Text-only history can still be cited as supporting evidence.
  Tier 1   a rule fixes the action; the model chooses type and wording from the
           part of the catalogue that implies that action.
  Tier 2   the full catalogue. Most of the dataset lands here, because most of
           it turns on preference and only history resolves preference.
  Tier 3   veto over everything above (postprocess.py).
"""

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import confidence as confidence_mod                  # noqa: E402
import context                                       # noqa: E402
import fallback                                      # noqa: E402
import llm                                           # noqa: E402
import media                                         # noqa: E402
import postprocess                                   # noqa: E402
import router as router_mod                          # noqa: E402
import rules                                         # noqa: E402
import similarity                                    # noqa: E402
import taxonomy                                      # noqa: E402

HEADER = ["message_id", "action", "message_type", "reason", "confidence",
          "evidence_message_ids"]
OUTPUT = os.path.join(context.DATASET, "output.csv")


def make_client():
    """(client, model, why_unavailable) for routing decisions.

    No vendor is named here. llm.py resolves the provider from .env, so Groq,
    Gemini, OpenRouter and a local Ollama are all the same code path. Keys come
    from the environment only -- AGENTS.md 6.3.
    """
    return llm.text_client()


def choose(message, data, catalogue, cache, router, verbose=False):
    """Route one message.

    Returns (tier, action, type, reason, confidence, evidence, media_text).

    Note the ordering: Tier 0 returns BEFORE any media is read. That is
    structural, not incidental -- it is what guarantees a noisy transcript can
    never reach the unrecoverable hard block. For every other message, media is
    described before evidence retrieval so a transcript/caption can inform it.
    """
    business = data.businesses.get((message.get("business_id") or "").strip())

    # Tier 0 -- decided in code, never sent to the model.
    hit = rules.classify(message, business)
    if hit and hit[1] == 0:
        _name, _tier, action, mtype = hit
        option = fallback.option_for(catalogue, action, mtype)
        # Safety does not need a model or media, but a text-only historical
        # match can still make the mute decision auditable. Retrieval itself
        # is deterministic and cannot execute untrusted message content.
        evidence_rows = similarity.find_evidence(message, data.history, data.events,
                                                  limit=1)
        evidence = [row["message_id"] for row, _score, _event in evidence_rows]
        return (0, action, mtype, option.reason if option else "",
                confidence_mod.adjust(catalogue.confidence_for(action, option),
                                      0, action), evidence, None)

    description = cache.describe(message, data) if cache else None
    evidence_rows = similarity.find_evidence(message, data.history, data.events,
                                              limit=1, media_text=description)
    evidence = [row["message_id"] for row, _score, _event in evidence_rows]
    match, score, kind = similarity.find_labelled_match(message, data.samples)
    ctx = context.build(message, data, description)
    if kind == "prior":                     # a hint, never a transferred prediction
        ctx["labelled_prior"] = {
            "note": "A near-identical labelled example is shown for context only; "
                    "do not copy its action or type because user context can differ.",
            "similarity": round(score, 3),
            "their_action": match["action"], "their_message_type": match["message_type"],
        }
    if description:
        ctx["attached_media_described"] = True

    allowed = {hit[2]} if hit else None     # Tier 1: rule fixed the action
    tier = 1 if hit else 2

    if router:
        result = router.route(message, ctx, evidence_rows, description, allowed)
        if result:
            action, mtype, reason, confidence, _option = result
            confidence = confidence_mod.adjust(
                confidence, tier, action, ctx.get("behavioural_prior"),
                retried=getattr(router, "last_retried", False))
            return (tier, action, mtype, reason, confidence, evidence, description)
        if verbose:
            print(f"  ! {message['message_id']}: model failed twice, using fallback")

    action, mtype, reason, confidence = fallback.without_model(
        message, ctx, catalogue, allowed, description)
    confidence = confidence_mod.adjust(confidence, tier, action,
                                       ctx.get("behavioural_prior"))
    return (tier, action, mtype, reason, confidence, evidence, description)


def _already_done(path, messages):
    """Rows from a previous run that can be reused verbatim.

    Only complete rows for message_ids we are actually routing count, so a
    stale or truncated file cannot smuggle in a wrong answer.
    """
    if not os.path.exists(path):
        return {}
    wanted = {m["message_id"] for m in messages}
    done = {}
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        if next(reader, None) != HEADER:
            return {}
        for row in reader:
            if len(row) == len(HEADER) and row[0] in wanted and row[1].strip():
                done[row[0]] = row
    return done


def main(argv=None):
    parser = argparse.ArgumentParser(description="Route WhatsApp messages.")
    parser.add_argument("--limit", type=int, help="route only the first N messages")
    parser.add_argument("--model", default="",
                        help="override LLM_MODEL from .env for this run")
    parser.add_argument("--out", default=OUTPUT)
    parser.add_argument("--no-model", action="store_true",
                        help="deterministic tiers only, no API calls")
    parser.add_argument("--build-media-cache", action="store_true",
                        help="caption images and transcribe voice notes, then exit")
    parser.add_argument("--max-calls", type=int, default=400, help="hard cost ceiling")
    parser.add_argument("--resume", action="store_true",
                        help="keep rows already present in the output file")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="suppress pacing and retry notices")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    data = context.Dataset()
    catalogue = taxonomy.Taxonomy(data.samples)
    messages = data.messages[:args.limit] if args.limit else data.messages

    offline = args.no_model
    if offline:
        client = vision = asr = None
        model = vision_model = asr_model = ""
        why = vision_why = asr_why = "disabled by --no-model"
    else:
        client, model, why = make_client()
        vision, vision_model, vision_why = llm.vision_client()
        asr, asr_model, asr_why = llm.audio_client()
        if args.verbose:
            print("providers:\n" + llm.summary())
    if args.model:                       # --model overrides .env for routing
        model = args.model
    cache = media.MediaCache(vision, vision_model, asr, asr_model,
                             announce=None if args.quiet else print,
                             offline_cache=True)

    if args.build_media_cache:
        if vision is None:
            print(f"! images cannot be captioned: {vision_why}")
        if asr is None:
            print(f"! voice notes cannot be transcribed: {asr_why}")
        captured, skipped, failed = cache.build_all(data.messages, data)
        described, total = cache.coverage(data.messages)
        print(f"\ncaptured {captured}, already cached {skipped}, failed {failed}")
        print(f"media cache now covers {described}/{total} media rows "
              f"-> {media.CACHE_FILE}")
        return 0 if failed == 0 else 1

    if client is None:
        print(f"! running WITHOUT a model ({why}).")
        print("! Tier 0 is unaffected; the rest use the deterministic fallback.")
    else:
        print(f"routing with {model}")
    announce = print if not args.quiet else None
    pacer = llm.pacer_for("LLM", announce)
    engine = (router_mod.Router(client, catalogue, model, args.max_calls,
                               pacer=pacer, announce=announce)
              if client else None)
    fixer = postprocess.Postprocessor(catalogue, data.history)

    done = _already_done(args.out, messages) if args.resume else {}
    if done:
        print(f"resuming: {len(done)} of {len(messages)} rows already present")

    started = time.time()
    tiers, rows = {}, []
    # Written incrementally and flushed per row. A paced run against a free
    # tier takes minutes, and losing all of it to an interruption at row 100
    # is the difference between --resume being useful and being decorative.
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for index, message in enumerate(messages, start=1):
            cached_row = done.get(message["message_id"])
            if cached_row:
                rows.append(cached_row)
                writer.writerow(cached_row)
                fh.flush()
                continue

            try:
                tier, action, mtype, reason, confidence, evidence, media_text = choose(
                    message, data, catalogue, cache, engine, args.verbose)
            except llm.QuotaExhausted as exhausted:
                # A daily cap. Everything routed so far is already on disk and
                # flushed, so stop cleanly and say how to pick it up rather
                # than dying with a traceback over completed work.
                print(f"\n! daily quota exhausted at row {index}/{len(messages)}")
                print(f"!   {exhausted}")
                print(f"! {len(rows)} rows are saved in {args.out}.")
                print("! Resume with:  python code/main.py --resume")
                print("! Or switch LLM_* in .env to another free provider and resume now.")
                cache.save()
                return 2
            (action, mtype, reason, confidence), evidence_ids = fixer.apply(
                (action, mtype, reason, confidence), message,
                data.businesses.get((message.get("business_id") or "").strip()),
                evidence, media_text)
            tiers[tier] = tiers.get(tier, 0) + 1
            row = [message["message_id"], action, mtype, reason,
                   f"{confidence:.2f}", evidence_ids]
            rows.append(row)
            writer.writerow(row)
            fh.flush()

            if args.verbose:
                print(f"  T{tier:>2} {message['message_id']} {action:6s} {mtype:15s} "
                      f"{confidence:.2f} {evidence_ids}")
            elif engine and index % 10 == 0:
                elapsed = time.time() - started
                rate = index / elapsed if elapsed else 0
                left = (len(messages) - index) / rate if rate else 0
                print(f"  {index}/{len(messages)} routed  "
                      f"({elapsed / 60:.1f}m elapsed, ~{left / 60:.1f}m left)")

    cache.save()

    described, total = cache.coverage(messages)
    print(f"\nwrote {len(rows)} rows -> {args.out}")
    print("tiers: " + "  ".join(f"T{t}={tiers[t]}" for t in sorted(tiers)))
    print(f"media described: {described}/{total}")
    if engine:
        print(f"model calls: {engine.calls} (+{cache.calls} vision)  "
              f"retries: {engine.retries}  hard failures: {engine.failures}")
        tokens_in = sum(entry["input_tokens"] for entry in engine.log)
        tokens_out = sum(entry["output_tokens"] for entry in engine.log)
        print(f"tokens: {tokens_in} in / {tokens_out} out")
    if fixer.changes:
        print("tier 3 corrections: " + ", ".join(f"{k}={v}" for k, v in
                                                 sorted(fixer.changes.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
