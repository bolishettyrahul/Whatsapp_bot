"""The output vocabulary, derived at runtime from sample_messages.csv.

Measured fact that drives this module: the 30 labelled rows contain only 24
distinct `reason` strings, six of them repeated verbatim, and **every reason
string maps to exactly one `action`**. `reason` is a closed catalogue, not
free prose.

So the model's job is not "write an explanation". It is "choose the reason
that describes this message", after which `action` is derived deterministically
and `message_type` is derived too, except for two genuinely ambiguous entries.
That turns open generation into a 24-way classification, which is more
consistent and directly serves the metric on reason quality.

Nothing here is hardcoded. The catalogue is read from a participant-facing
file every run; if the organisers ship different samples, it changes with them.
"""

import collections
import csv
import os
import statistics

DATASET = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dataset"
)

ACTIONS = ("notify", "digest", "mute")
TYPES = ("personal", "urgent", "event", "payment", "business_update", "promotion",
         "greeting", "forward", "spam", "scam", "unknown")

# Observed confidence band across all 30 labelled rows. Predictions outside it
# are not better calibrated than the answer key, they are just louder.
CONF_MIN, CONF_MAX = 0.78, 0.91


class Option:
    """One catalogue entry: a reason and what it implies."""

    def __init__(self, index, reason, actions, types, confidences):
        self.index = index
        self.reason = reason
        self.action = actions[0]                    # 1:1 in the labelled data
        self.types = types                          # >1 only where truly ambiguous
        self.message_type = types[0] if len(types) == 1 else None
        self.confidence = round(statistics.mean(confidences), 2)

    def __repr__(self):
        return f"<Option {self.index} {self.action}/{self.types} {self.reason[:40]!r}>"


class Taxonomy:
    def __init__(self, samples=None):
        if samples is None:
            with open(os.path.join(DATASET, "sample_messages.csv"), encoding="utf-8") as fh:
                samples = list(csv.DictReader(fh))

        grouped = collections.OrderedDict()
        for row in samples:
            reason = (row.get("reason") or "").strip()
            if not reason:
                continue
            entry = grouped.setdefault(reason, {"actions": [], "types": [], "conf": []})
            entry["actions"].append(row["action"])
            entry["types"].append(row["message_type"])
            try:
                entry["conf"].append(float(row["confidence"]))
            except (TypeError, ValueError):
                pass

        self.options = []
        for index, (reason, entry) in enumerate(grouped.items(), start=1):
            actions = _by_frequency(entry["actions"])
            if len(actions) > 1:
                # Would break the derive-action-from-reason contract. Nothing in
                # the labelled data does this; fail loudly rather than guess.
                raise ValueError(f"reason maps to multiple actions {actions}: {reason!r}")
            self.options.append(Option(index, reason, actions,
                                       _by_frequency(entry["types"]),
                                       entry["conf"] or [_action_mean(samples, actions[0])]))

        self.by_index = {o.index: o for o in self.options}
        self._action_conf = {a: _action_mean(samples, a) for a in ACTIONS}

    def confidence_for(self, action, option=None):
        """Calibrated confidence: the catalogue entry's own observed mean where
        one exists, otherwise the mean for that action, always inside the band."""
        value = option.confidence if option else self._action_conf.get(action, 0.82)
        return round(min(CONF_MAX, max(CONF_MIN, value)), 2)

    def prompt_block(self, allowed_actions=None):
        """The catalogue as the model sees it.

        `allowed_actions` narrows it for Tier 1, where a rule has already fixed
        the action and the model is only choosing the type and wording. Showing
        options it may not pick invites exactly the contradiction we are
        avoiding: a reason that implies an action the row cannot have.
        """
        lines = []
        for o in self.options:
            if allowed_actions and o.action not in allowed_actions:
                continue
            types = "|".join(o.types)
            lines.append(f"{o.index}. [{o.action} / {types}] {o.reason}")
        return "\n".join(lines)

    def resolve(self, index, message_type=None, facts=None):
        """Map a chosen catalogue index to (action, message_type, reason, option).

        Returns None when the index is not in the catalogue, so the caller can
        treat it as a schema violation rather than silently defaulting.
        """
        option = self.by_index.get(index)
        if option is None:
            return None
        chosen = option.message_type
        if chosen is None:                          # ambiguous entry
            if message_type in option.types:
                chosen = message_type               # an explicit choice wins
            else:
                chosen = disambiguate(option, facts)
        return option.action, chosen, option.reason, option


def _by_frequency(values):
    return [v for v, _ in collections.Counter(values).most_common()]


def _action_mean(samples, action):
    values = [float(r["confidence"]) for r in samples
              if r.get("action") == action and r.get("confidence")]
    return statistics.mean(values) if values else 0.82


# Each catalogue reason asserts something checkable about the row it is used
# for. "A verified business is sending..." is false if the business is not
# verified, and a confident false statement is worse than a vague true one.
#
# Used in two places, which is why it lives here with the catalogue: main.py
# prefers an option whose claim holds, and the eval harness scores how often
# the claim actually held.
CLAIMS = (
    ("school admin", lambda f: f.get("group_type") == "school_group"),
    ("group admin", lambda f: f.get("sender_is_admin")),
    ("work context", lambda f: f.get("work_context")),
    ("verified business", lambda f: f.get("business_verified")),
    ("first message from the sender", lambda f: f.get("history") == 0),
    ("sender is unfamiliar", lambda f: f.get("history") == 0),
    ("opted out of or repeatedly dismissed",
     lambda f: f.get("opted_out") or f.get("dismissed")),
    ("ignored, dismissed, or muted",
     lambda f: f.get("dismissed") or f.get("muted_after")),
    ("repeated forwards or greetings",
     lambda f: f.get("forwarded") or f.get("greeting_text")),
    ("tries to instruct the router", lambda f: f.get("injection")),
    ("asks for urgent otp", lambda f: f.get("asks_secret")),
    ("opted into", lambda f: f.get("allows_promotions")),
)


def claim_of(reason):
    """The checkable claim a reason makes, or None."""
    lowered = (reason or "").lower()
    return next((c for c in CLAIMS if c[0] in lowered), None)


def claim_holds(reason, facts):
    """True when the reason asserts nothing checkable, or asserts something
    that is actually true of this row. False only on a contradiction."""
    claim = claim_of(reason)
    return True if claim is None else bool(claim[1](facts or {}))


# Two catalogue entries carry two types each, and falling back to `types[0]`
# picks by frequency order -- arbitrary, and under leave-one-out it is whichever
# of the pair happened to survive. These are the real discriminators.
def disambiguate(option, facts=None):
    """Choose within an ambiguous entry using the row's own facts.

    Falls back to the frequency-ordered first type when there are no facts to
    go on, which is the previous behaviour.
    """
    types = option.types
    if len(types) < 2 or not facts:
        return types[0]

    # greeting vs forward: a forward is defined by having been forwarded.
    if {"greeting", "forward"} <= set(types):
        return "forward" if (facts.get("forwarded")
                             or facts.get("chain_forward")) else "greeting"

    # promotion vs spam. `spam` requires an actual BUSINESS sender the user
    # does not know. Absence of a business relationship is not evidence of
    # spam when there is no business at all -- a neighbour selling a kurta set
    # in a group is promotion, and treating it as spam was a real misfire.
    if {"promotion", "spam"} <= set(types):
        if not facts.get("is_business"):
            return "promotion"
        known = facts.get("business_verified") or facts.get("has_relationship")
        return "promotion" if known else "spam"

    return types[0]
