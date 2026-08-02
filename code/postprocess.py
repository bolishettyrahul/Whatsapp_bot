"""Tier 3: the veto that runs over every row, whatever produced it.

Three jobs, in order:
  1. Safety veto -- the same predicates rules.py uses for Tier 0, re-applied
     to the model's answer. A model talked into `notify` by an injection
     payload is corrected here.
  2. Action/type coherence -- a mute/scam row must not come back as
     notify/scam. The permitted pairs are DERIVED from the catalogue rather
     than written down, so they cannot drift away from the labelled data.
  3. Output hygiene -- confidence inside the observed band, evidence ids that
     exist and belong to this user.

Every change is counted and reported. A veto layer that silently rewrites the
model's output is indistinguishable from one that is broken.
"""

import collections

import context
import rules
import taxonomy as tx


class Postprocessor:
    def __init__(self, catalogue, history):
        self.catalogue = catalogue
        self.history_owner = {h["message_id"]: h["user_id"] for h in history}
        self.changes = collections.Counter()

        # Which actions has each message_type actually been seen with?
        seen = collections.defaultdict(set)
        for option in catalogue.options:
            for mtype in option.types:
                seen[mtype].add(option.action)
        self.actions_for_type = dict(seen)

        # Types the labelled data only ever mutes, and only ever notifies.
        self.always_mute = {t for t, a in seen.items() if a == {"mute"}}
        self.never_mute = {t for t, a in seen.items() if "mute" not in a and a == {"notify"}}

        # A reason string for each action, so a vetoed row still explains itself
        # in the catalogue's voice instead of ours.
        self.risk_reason = _pick(catalogue, "mute", "scam")

    def apply(self, prediction, message, business, evidence_ids, media_text=None):
        """prediction: (action, message_type, reason, confidence).
        Returns a corrected prediction plus a cleaned evidence string.

        `media_text` is the transcript or image description. It feeds the veto
        but never Tier 0: a veto only tightens toward `mute` and runs after the
        model has seen full context, so a false positive here is recoverable.
        A Tier 0 block is not, which is why noisy ASR is kept out of it.
        """
        action, mtype, reason, confidence = prediction
        text = context.effective_text(message, media_text)

        # 1. Safety veto. Content-level risk overrides any preference signal.
        if rules.impersonates_system(text) or rules.asks_for_secret(text):
            if action != "mute" or mtype != "scam":
                self.changes["safety_veto"] += 1
                action, mtype = "mute", "scam"
                reason = self.risk_reason or reason
                confidence = self.catalogue.confidence_for("mute")

        # 2. Coherence, derived from the catalogue.
        elif mtype in self.always_mute and action != "mute":
            self.changes["type_implies_mute"] += 1
            action = "mute"
            confidence = self.catalogue.confidence_for("mute")
        elif mtype in self.never_mute and action == "mute":
            # An urgent-typed row the labelled data never mutes. The model
            # judged risk without a rule agreeing; trust the type, not the mute.
            self.changes["type_never_muted"] += 1
            action = "notify"
            confidence = self.catalogue.confidence_for("notify")

        allowed = self.actions_for_type.get(mtype)
        if allowed and action not in allowed:
            self.changes["pair_unseen_in_labelled_data"] += 1   # reported, not forced

        # 3. Hygiene.
        clamped = round(min(tx.CONF_MAX, max(tx.CONF_MIN, float(confidence))), 2)
        if abs(clamped - float(confidence)) > 1e-9:
            self.changes["confidence_clamped"] += 1
        reason = " ".join(str(reason).split())

        return (action, mtype, reason, clamped), self.clean_evidence(evidence_ids, message)

    def clean_evidence(self, evidence_ids, message):
        """Drop ids that do not exist or belong to another user. `none` if empty."""
        kept = []
        for raw in evidence_ids or []:
            mid = str(raw).strip()
            if not mid or mid.lower() == "none":
                continue
            owner = self.history_owner.get(mid)
            if owner is None:
                self.changes["evidence_id_not_in_history"] += 1
            elif owner != message.get("user_id"):
                self.changes["evidence_id_wrong_user"] += 1
            else:
                kept.append(mid)
        return ";".join(kept) if kept else "none"


def _pick(catalogue, action, message_type):
    for option in catalogue.options:
        if option.action == action and message_type in option.types:
            return option.reason
    return None
