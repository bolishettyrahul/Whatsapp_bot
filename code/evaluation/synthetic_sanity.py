"""Disposable, stratified 50-case sanity evaluation.

This is not a claim about the organiser's hidden labels.  It is a deliberately
authored holdout: 50 new messages, with four or more cases for every allowed
message type, scored against an independently specified expected action/type.
It reuses only existing media IDs whose captions/transcripts are already in
the packaged cache.  The temporary CSV is removed in ``finally``.

Run:
    python code/evaluation/synthetic_sanity.py
    python code/evaluation/synthetic_sanity.py --model
"""

import argparse
import collections
import csv
import os
import sys

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(CODE)
DATASET = os.path.join(ROOT, "dataset")
sys.path.insert(0, CODE)

import context  # noqa: E402
import llm  # noqa: E402
import main as pipeline  # noqa: E402
import media  # noqa: E402
import metrics  # noqa: E402
import postprocess  # noqa: E402
import rules  # noqa: E402
import router as router_mod  # noqa: E402
import taxonomy  # noqa: E402

HEADER = ["message_id", "user_id", "conversation_type", "group_id", "business_id",
          "sender_user_id", "created_at", "message_text", "media_type", "media_id",
          "forwarded_count", "action", "message_type", "reason", "confidence",
          "evidence_message_ids"]

# 50 rows total. Every allowed message type has at least four rows; the extra
# six weight the high-frequency, preference-sensitive categories.
PLAN = {
    "personal": (6, ("digest",)),
    "urgent": (4, ("notify",)),
    "event": (5, ("notify",)),
    "payment": (4, ("digest",)),
    "business_update": (5, ("digest",)),
    "promotion": (6, ("digest",)),
    "greeting": (4, ("digest",)),
    "forward": (4, ("mute",)),
    "spam": (4, ("mute",)),
    "scam": (4, ("mute",)),
    "unknown": (4, ("digest",)),
}

TEXT = {
    "personal": "Can we confirm the plan for Saturday after dinner?",
    "urgent": "Production service is down; please join the incident call now.",
    "event": "The school event timing has changed; please check the updated notice.",
    "payment": "Your maintenance payment is due today. Please pay before 5 PM.",
    "business_update": "Your booked service has been updated and is ready to review.",
    "promotion": "This week's offer gives you 25% off selected items.",
    "greeting": "Good morning everyone. Wishing you a peaceful day.",
    "forward": "Forward this to every group for good luck and positive energy.",
    "spam": "Earn Rs 5000 per day from home typing jobs. Visit https://fast-income.example.",
    "scam": "Your account will be blocked today. Share the OTP now to verify it.",
    "unknown": "Hello, I found your number on the volunteer list. Are you coordinating today?",
}


def _find(rows, predicate):
    return next((row for row in rows if predicate(row)), rows[0])


def _anchors(data):
    """Choose real context only at runtime; no dataset IDs are embedded here."""
    def has_prior(row, action):
        verdict = context.build(row, data)["behavioural_prior"]
        return verdict["confident"] and verdict["most_likely"] == action

    trusted_business = _find(
        data.messages,
        lambda row: row.get("conversation_type") == "business"
        and str((data.businesses.get(row.get("business_id")) or {}).get("verified")) == "1"
        and has_prior(row, "digest"),
    )
    untrusted_business = _find(
        data.messages,
        lambda row: rules.untrusted_business(data.businesses.get(row.get("business_id"))),
    )
    group_admin = _find(
        data.messages,
        lambda row: row.get("conversation_type") == "group"
        and data.sender_is_group_admin(row) and has_prior(row, "notify"),
    )
    group = _find(data.messages, lambda row: row.get("conversation_type") == "group"
                  and not data.sender_is_group_admin(row) and has_prior(row, "digest"))
    personal = _find(data.messages, lambda row: row.get("conversation_type") == "personal"
                     and has_prior(row, "digest"))
    return {"business": trusted_business, "untrusted": untrusted_business,
            "group_admin": group_admin, "group": group, "personal": personal}


def _anchor_for(message_type, anchors):
    if message_type in {"payment", "business_update", "promotion"}:
        return anchors["business"]
    if message_type == "spam":
        return anchors["untrusted"]
    if message_type in {"event", "urgent"}:
        return anchors["group_admin"]
    if message_type in {"greeting", "forward"}:
        return anchors["group"]
    return anchors["personal"]


def _cached_media(data, cache):
    """Cached media records grouped by broad semantic cue and media kind."""
    rows = []
    seen = set()
    for source in data.messages + data.samples:
        media_id = (source.get("media_id") or "").strip()
        kind = (source.get("media_type") or "").strip()
        if not media_id or media_id in seen or kind not in {"image", "voice"}:
            continue
        caption = cache.describe(source, data)
        if caption:
            rows.append((source, caption.lower()))
            seen.add(media_id)
    if not rows:
        raise RuntimeError("No usable packaged media entries were found.")
    return rows


def _media_for(message_type, entries, ordinal):
    """Attach cached media only when its caption matches the authored intent."""
    cues = {
        "urgent": ("incident", "failing", "missing person"),
        "event": ("school", "field trip", "pickup", "airport"),
        "promotion": ("promotional", "offer", "cashback", "sale", "cards"),
        "scam": ("blocked", "otp"),
        "business_update": ("financial chart",),
        "personal": ("blue jacket",),
        "spam": ("land plot",),
    }.get(message_type, ())
    matches = [row for row in entries if any(cue in row[1] for cue in cues)]
    if not matches:
        return None
    source, _caption = matches[ordinal % len(matches)]
    return source


def make_cases(data, cache):
    anchors = _anchors(data)
    cached = _cached_media(data, cache)
    cases = []
    ordinal = 0
    for message_type, (count, actions) in PLAN.items():
        for index in range(count):
            source = _anchor_for(message_type, anchors)
            row = {key: source.get(key, "") for key in HEADER[:11]}
            row["message_id"] = f"sanity_{ordinal + 1:03d}"
            row["created_at"] = f"2026-08-02 {9 + (ordinal % 9):02d}:00"
            row["message_text"] = TEXT[message_type]
            row["forwarded_count"] = "7" if message_type == "forward" else "0"

            # Unknown rows must be genuine cold starts, not merely a new
            # message from somebody with an existing history.
            if message_type in {"unknown", "scam"}:
                row["conversation_type"] = "personal"
                row["group_id"] = row["business_id"] = ""
                row["sender_user_id"] = f"synthetic_sender_{ordinal:03d}"

            attachment = _media_for(message_type, cached, index)
            if attachment:
                row["media_type"] = attachment["media_type"]
                row["media_id"] = attachment["media_id"]
                row["message_text"] += " See the attached item for the full details."
            else:
                row["media_type"] = row["media_id"] = ""

            row["action"] = actions[index % len(actions)]
            row["message_type"] = message_type
            row["reason"] = "Synthetic holdout expectation authored independently of the router."
            row["confidence"] = "0.84"
            row["evidence_message_ids"] = "none"
            cases.append(row)
            ordinal += 1
    assert len(cases) == 50
    return cases


def write_temp_csv(cases):
    path = os.path.join(DATASET, ".synthetic_sanity_50.csv")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(cases)
    return path


def _ids(value):
    return {item for item in str(value or "").split(";") if item and item != "none"}


def run(use_model=False):
    data = context.Dataset()
    catalogue = taxonomy.Taxonomy(data.samples)
    client = model = None
    if use_model:
        client, model, why = pipeline.make_client()
        if client is None:
            print(f"! model unavailable ({why}); running the offline path")
    vision, vision_model, _ = llm.vision_client() if use_model else (None, "", None)
    asr, asr_model, _ = llm.audio_client() if use_model else (None, "", None)
    cache = media.MediaCache(vision, vision_model, asr, asr_model, offline_cache=True)
    cases = make_cases(data, cache)
    path = write_temp_csv(cases)
    print(f"created temporary 50-row holdout: {path}")

    try:
        engine = router_mod.Router(client, catalogue, model) if client else None
        fixer = postprocess.Postprocessor(catalogue, data.history)
        results, action_pairs, type_pairs = [], [], []
        confidences, corrects, evidence_valid = [], [], 0
        attached = resolved = 0

        for row in cases:
            if row["media_id"]:
                attached += 1
                resolved += bool(cache.describe(row, data))
            tier, action, mtype, reason, confidence, evidence, media_text = pipeline.choose(
                row, data, catalogue, cache, engine)
            (action, mtype, reason, confidence), evidence_text = fixer.apply(
                (action, mtype, reason, confidence), row,
                data.businesses.get((row.get("business_id") or "").strip()), evidence, media_text)
            action_pairs.append((row["action"], action))
            type_pairs.append((row["message_type"], mtype))
            correct = row["action"] == action and row["message_type"] == mtype
            confidences.append(float(confidence))
            corrects.append(int(correct))
            evidence_ids = _ids(evidence_text)
            owners = {h["message_id"]: h["user_id"] for h in data.history}
            evidence_valid += all(owners.get(mid) == row["user_id"] for mid in evidence_ids)
            results.append((row, action, mtype, tier, confidence, evidence_text))

        action_ok = sum(gold == predicted for gold, predicted in action_pairs)
        type_ok = sum(gold == predicted for gold, predicted in type_pairs)
        both_ok = sum(a[0] == a[1] and t[0] == t[1]
                      for a, t in zip(action_pairs, type_pairs))
        print("\nSYNTHETIC HOLDOUT (authored expectations, not organiser ground truth)")
        print(f"  media cache resolved: {resolved}/{attached} attached cases")
        print(f"  action accuracy:       {action_ok}/50 ({action_ok / 50:.0%})  "
              f"macro-F1 {metrics.macro_f1(action_pairs, taxonomy.ACTIONS):.3f}")
        print(f"  type accuracy:         {type_ok}/50 ({type_ok / 50:.0%})  "
              f"macro-F1 {metrics.macro_f1(type_pairs, taxonomy.TYPES):.3f}")
        print(f"  action + type:         {both_ok}/50 ({both_ok / 50:.0%})")
        print(f"  evidence valid-owner:  {evidence_valid}/50 ({evidence_valid / 50:.0%})")
        calibration = metrics.calibration(confidences, corrects)
        print(f"  confidence: ECE {calibration['ece']:.3f}, "
              f"discrimination {calibration['discrimination']:+.3f}")
        metrics.render_per_class(
            [(row["action"], action, row["message_type"], mtype)
             for row, action, mtype, _tier, _confidence, _evidence in results],
            2, 3, "MESSAGE_TYPE", taxonomy.TYPES)

        action_misses = collections.defaultdict(list)
        misses = collections.defaultdict(list)
        for row, action, mtype, tier, _confidence, _evidence in results:
            if row["action"] != action:
                action_misses[row["message_type"]].append(
                    f"{row['message_id']}->{action} (T{tier})")
            if row["message_type"] != mtype:
                misses[row["message_type"]].append(f"{row['message_id']}->{mtype} (T{tier})")
        print("\n  action misses by expected class:")
        for message_type in taxonomy.TYPES:
            if action_misses[message_type]:
                print(f"    {message_type:16s} {', '.join(action_misses[message_type])}")
        print("\n  type misses by expected class:")
        for message_type in taxonomy.TYPES:
            if misses[message_type]:
                print(f"    {message_type:16s} {', '.join(misses[message_type])}")
        return 0
    finally:
        if os.path.exists(path):
            os.remove(path)
            print(f"\nremoved temporary holdout: {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the disposable 50-case sanity holdout.")
    parser.add_argument("--model", action="store_true", help="use configured router/media providers")
    args = parser.parse_args()
    sys.exit(run(args.model))
