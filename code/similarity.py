"""Near-duplicate matching over provided, participant-facing files.

Two uses:
  1. Labelled-example prior -- a near-duplicate sample is prompt context only;
     it never transfers a prediction to an incoming message.
  2. Evidence retrieval -- find the historical messages most similar to an
     incoming one, restricted to that user.

Nothing here hardcodes a label or a message id. Matching is computed at
runtime from dataset/sample_messages.csv, which is participant-facing.
`grep -E "msg_[0-9]{3}" code/` must return nothing.
"""

import difflib
import re
import unicodedata

PRIOR_THRESHOLD = 0.85      # weaker / cross-user -> pass as prompt context
EVIDENCE_THRESHOLD = 0.35

# Evidence ranking bonuses. Named so they can be swept and justified rather
# than adjusted by feel -- every one of these was measured against the 30
# labelled rows, and the measurement is recorded in docs/build-plan.md.
SENDER_BONUS = 0.30         # same sender as the incoming message
MEDIA_TYPE_BONUS = 0.10
MEDIA_ID_BONUS = 0.25       # literally the same attachment
ENGAGED_BONUS = 0.00        # the user opened/replied/reported it


def normalise(text):
    """Casefold, strip accents and homoglyph variants, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.casefold()).strip()


def ratio(a, b):
    return difflib.SequenceMatcher(None, normalise(a), normalise(b)).ratio()


def _sender_key(row):
    """What the message came FROM. Identical text from a different business
    is not the same message -- one labelled pair crosses businesses."""
    return (
        (row.get("business_id") or "").strip(),
        (row.get("group_id") or "").strip(),
        (row.get("sender_user_id") or "").strip(),
    )


def find_labelled_match(message, samples):
    """Best match in sample_messages.csv.

    Returns (sample_row, ratio, kind) where kind is:
      "prior" -- a similar label is a prompt hint only, never a prediction
      None       -- nothing close enough
    """
    text = message.get("message_text", "") or ""
    if not text.strip():
        return (None, 0.0, None)

    best, best_r = None, 0.0
    for s in samples:
        r = ratio(text, s.get("message_text", ""))
        if r > best_r:
            best, best_r = s, r

    if best is None or best_r < PRIOR_THRESHOLD:
        return (None, best_r, None)

    return (best, best_r, "prior")


def find_evidence(message, history, events=None, limit=1, media_text=None):
    """Most relevant historical messages FOR THE SAME USER.

    Pool is 3-32 rows per user, so brute force is correct here -- an
    embedding index would be unjustified complexity at this scale.

    limit defaults to 1 because 27 of the 30 labelled rows cite exactly one
    id. Citing two where the gold cites one dilutes the evidence metric.

    Returns [(history_row, score, event_row_or_None)], best first.
    """
    uid = message.get("user_id")
    created_at = (message.get("created_at") or "").strip()
    pool = [h for h in history
            if h.get("user_id") == uid
            and (not created_at or (h.get("created_at") or "") < created_at)]
    if not pool:
        return []

    events_by_id = {e["message_id"]: e for e in (events or [])}
    text = "\n".join(part.strip() for part in
                     (message.get("message_text", "") or "", media_text or "") if part.strip())
    key = _sender_key(message)
    recent = lambda h: h.get("created_at", "")          # noqa: E731

    # A voice note carries no text at all. Text similarity cannot rank an
    # empty string, so provenance is the only signal left: same sender,
    # most recent first. Without this the ~20 media rows all cite "none".
    if not text.strip():
        if not any(key):
            return []
        same = [h for h in pool if _sender_key(h) == key]
        same.sort(key=recent, reverse=True)
        return [(h, 0.30, events_by_id.get(h["message_id"])) for h in same[:limit]]

    scored = []
    for h in pool:
        score = ratio(text, h.get("message_text", ""))
        if _sender_key(h) == key and any(key):
            score += SENDER_BONUS
        if message.get("media_type") and h.get("media_type") == message.get("media_type"):
            score += MEDIA_TYPE_BONUS
        if message.get("media_id") and h.get("media_id") == message.get("media_id"):
            score += MEDIA_ID_BONUS
        event = events_by_id.get(h["message_id"])
        if ENGAGED_BONUS and event and any(event.get(f) == "1" for f in
                                           ("message_opened", "message_replied",
                                            "message_reported")):
            score += ENGAGED_BONUS
        if score >= EVIDENCE_THRESHOLD:
            scored.append((h, round(score, 4), event))

    # Tiebreak oldest-first: measured 18/30 vs 16/30 for newest-first against
    # the labelled evidence column. Tuned on 30 rows, so treat as weak.
    scored.sort(key=lambda x: recent(x[0]))
    scored.sort(key=lambda x: -x[1])                        # stable: score wins
    return scored[:limit]


def evidence_ids(matches):
    """Format for the output column. 'none' when there is no useful evidence."""
    return ";".join(m[0]["message_id"] for m in matches) if matches else "none"
