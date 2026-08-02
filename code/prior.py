"""Behavioural prior over notify / digest / mute. Deterministic, no network.

Why this exists: the labelled data separates the three actions on behaviour
almost cleanly, and nothing in the pipeline was using that directly.

    feature        notify  digest   mute
    open_rate        0.99    1.00   0.20
    reply_rate       0.80    0.10   0.00
    dismiss_rate     0.01    0.00   0.80
    muted_after      0.01    0.00   0.80
    sender_is_admin  0.44    0.09   0.00

`dismiss_rate`/`muted_after` isolate mute; `reply_rate` alone separates notify
from digest -- the axis the pipeline was failing on worst.

**Shrinkage is the point.** 31 of the 110 rows have only 2 prior messages from
the sender, so a raw rate is noise: one dismissal out of two reads as 0.50.
Each rate is pulled toward a population base rate with a small number of
pseudo-observations, so thin evidence stays near the base rate and only real
volume moves it.

The weights below are round numbers chosen from the table above, NOT fitted to
the labels -- with 9/11/10 rows per class, fitting would be memorisation. The
base rates come from message_events.csv alone; no gold label enters the prior.
"""

import math

PSEUDO = 2.0            # pseudo-observations; n=2 barely moves off the base rate

# Weights are log-odds contributions. Magnitudes track the separation table:
# a feature that swings 0.8 between classes gets ~3, one that swings 0.4 gets ~1.
W_DISMISS = 3.0         # 0.80 for mute vs 0.00 elsewhere -- the strongest signal
W_MUTED_AFTER = 3.0     # same magnitude; the user silenced the sender outright
W_REPORTED = 2.0        # rare but unambiguous
W_UNOPENED = 1.5        # open_rate 0.20 for mute vs ~1.00 for the others
W_OPTED_OUT = 1.0       # explicit preference, but only about promotions
W_GROUP_MUTED = 1.0     # the user silenced the whole group
W_HIGH_FORWARD = 1.0    # forwarded_count >= 5 averages 2.0 on gold mute rows

W_REPLY = 3.0           # 0.80 for notify vs 0.10 digest -- the notify/digest axis
W_ADMIN = 1.0           # 0.44 vs 0.09; real but far weaker than replying

# Scores are DEVIATIONS FROM THE POPULATION NORM, not raw magnitudes.
#
# Raw magnitudes were structurally unfair: mute accumulates from four features
# (up to 9.5) while notify has two (max 4.0), so with a fixed digest baseline
# notify required reply_rate > 0.90 while gold notify rows average 0.80 --
# notify could essentially never win, which is exactly the failure observed.
#
# Centring each action on its own base-rate score fixes that: an average sender
# scores 0 everywhere, gets a uniform distribution, and is correctly reported as
# NOT confident. Only behaviour that departs from the norm moves the prior.
DIGEST_SCORE = 0.0

MIN_EVIDENCE = 2        # one prior message is an anecdote, not a pattern
MIN_MARGIN = 0.25       # see Verdict.confident for why this number

ACTIONS = ("notify", "digest", "mute")


class Verdict:
    """A distribution plus enough context to decide whether to trust it."""

    def __init__(self, probabilities, n_evidence, explanation):
        self.probabilities = probabilities
        self.n_evidence = n_evidence
        self.explanation = explanation
        ranked = sorted(probabilities.items(), key=lambda kv: -kv[1])
        self.top = ranked[0][0]
        self.margin = round(ranked[0][1] - ranked[1][1], 4)
        # Thin evidence is not an opinion. Below this the prior may inform the
        # prompt but must never trigger an override or an escalation.
        #
        # margin 0.25 sits in the middle of a FLAT region: thresholds of 0.20,
        # 0.25 and 0.30 all give 79% accuracy on the labelled rows, while 0.15
        # gives 71% and 0.50 gives 85% on only 13 rows. Choosing the middle of
        # a plateau rather than a peak is the anti-overfitting move -- a peak on
        # 30 rows is usually noise.
        self.confident = n_evidence >= MIN_EVIDENCE and self.margin >= MIN_MARGIN

    def as_dict(self):
        return {
            "probabilities": {k: round(v, 3) for k, v in self.probabilities.items()},
            "most_likely": self.top,
            "margin": self.margin,
            "messages_of_history": self.n_evidence,
            "confident": self.confident,
            "why": self.explanation,
        }

    def __repr__(self):
        return f"<Verdict {self.top} m={self.margin} n={self.n_evidence}>"


class Prior:
    def __init__(self, dataset):
        self.base = _base_rates(dataset.events)
        # Each action's score at exactly-average behaviour. Subtracting these
        # makes every score a deviation from the norm, on a comparable scale.
        self.mute_at_norm = (
            W_DISMISS * self.base["notification_dismissed"]
            + W_MUTED_AFTER * self.base["muted_after_message"]
            + W_REPORTED * self.base["message_reported"]
            + W_UNOPENED * (1.0 - self.base["message_opened"])
        )
        self.notify_at_norm = W_REPLY * self.base["message_replied"]

    def score(self, message, ctx):
        """Return a Verdict for one message given its assembled context."""
        reactions = ctx.get("past_reactions_to_this_sender") or {}
        n = reactions.get("messages_from_this_sender", 0)

        # Cold start: no history with this sender. Abstain rather than guess --
        # 7 of the 110 rows land here and a confident guess would be invented.
        if not n:
            uniform = {a: 1 / 3 for a in ACTIONS}
            return Verdict(uniform, 0, "no prior messages from this sender")

        rates = {
            "open": _smooth(reactions.get("opened", 0), n, self.base["message_opened"]),
            "reply": _smooth(reactions.get("replied", 0), n, self.base["message_replied"]),
            "dismiss": _smooth(reactions.get("dismissed", 0), n,
                               self.base["notification_dismissed"]),
            "muted_after": _smooth(reactions.get("muted_after", 0), n,
                                   self.base["muted_after_message"]),
            "reported": _smooth(reactions.get("reported", 0), n,
                                self.base["message_reported"]),
        }

        signals = ctx.get("signals", {})
        relationship = ctx.get("business_relationship") or {}
        membership = ctx.get("membership") or {}
        group = ctx.get("group") or {}

        opted_out = 1.0 if relationship.get("opted_out_of_promotions_at") else 0.0
        group_muted = 1.0 if membership.get("muted_by_user") else 0.0
        high_forward = 1.0 if signals.get("forwarded_count", 0) >= 5 else 0.0
        admin = 1.0 if group.get("sender_is_admin") else 0.0

        mute = (W_DISMISS * rates["dismiss"]
                + W_MUTED_AFTER * rates["muted_after"]
                + W_REPORTED * rates["reported"]
                + W_UNOPENED * (1.0 - rates["open"])
                + W_OPTED_OUT * opted_out
                + W_GROUP_MUTED * group_muted
                + W_HIGH_FORWARD * high_forward) - self.mute_at_norm
        notify = (W_REPLY * rates["reply"] + W_ADMIN * admin) - self.notify_at_norm

        probabilities = _softmax({"notify": notify, "digest": DIGEST_SCORE, "mute": mute})
        return Verdict(probabilities, n, _explain(rates, n, opted_out, group_muted,
                                                  high_forward, admin))


def _smooth(successes, n, base_rate):
    """Beta-Binomial shrinkage toward the population base rate.

    n=2 barely moves off the base rate; n=20 is trusted almost entirely.
    """
    return (successes + PSEUDO * base_rate) / (n + PSEUDO)


def _base_rates(events):
    """Population rates from message_events.csv. No labels involved."""
    fields = ("message_opened", "message_replied", "notification_dismissed",
              "muted_after_message", "message_reported")
    totals = dict.fromkeys(fields, 0)
    for row in events:
        for field in fields:
            totals[field] += 1 if str(row.get(field, "")).strip() == "1" else 0
    count = max(1, len(events))
    return {field: totals[field] / count for field in fields}


def _softmax(scores):
    top = max(scores.values())
    exponentials = {k: math.exp(v - top) for k, v in scores.items()}
    total = sum(exponentials.values())
    return {k: v / total for k, v in exponentials.items()}


def _explain(rates, n, opted_out, group_muted, high_forward, admin):
    parts = [f"{n} prior message(s) from this sender",
             f"opened {rates['open']:.0%}", f"replied {rates['reply']:.0%}",
             f"dismissed {rates['dismiss']:.0%}"]
    if rates["muted_after"] > 0.2:
        parts.append(f"muted the sender after {rates['muted_after']:.0%}")
    if opted_out:
        parts.append("opted out of promotions")
    if group_muted:
        parts.append("group is muted")
    if high_forward:
        parts.append("heavily forwarded")
    if admin:
        parts.append("sender is a group admin")
    return "; ".join(parts)
