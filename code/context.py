"""Deterministic context assembly. No LLM, no network.

Loads dataset/ once, then joins each incoming message to the user, group,
membership, business, relationship, and engagement rows that describe it.

The join is the whole personalization story: identical text is `digest` for
one user and `mute` for another, and the only thing that separates them is
in these tables.
"""

import csv
import os
from collections import Counter, defaultdict
from datetime import datetime

import prior as prior_mod
import rules

DATASET = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dataset"
)


def _read(name):
    with open(os.path.join(DATASET, name), encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _ratio(numerator, denominator):
    """Engagement rate, or None when the denominator is too small to mean
    anything. None is passed to the model as 'unknown', never as 0.0 --
    'never opened' and 'never sent' are different facts."""
    denominator = _int(denominator)
    if denominator < 3:
        return None
    return round(_int(numerator) / denominator, 3)


def in_dnd(timestamp, window):
    """Is this timestamp inside the user's do-not-disturb window?

    Windows wrap past midnight (22:00-07:00), so a naive start<=t<=end is
    wrong for every row in this dataset.
    """
    if not window or "-" not in str(window):
        return False
    try:
        start_s, end_s = str(window).split("-", 1)
        moment = datetime.strptime(str(timestamp).strip(), "%Y-%m-%d %H:%M").time()
        start = datetime.strptime(start_s.strip(), "%H:%M").time()
        end = datetime.strptime(end_s.strip(), "%H:%M").time()
    except ValueError:
        return False
    if start <= end:
        return start <= moment <= end
    return moment >= start or moment <= end          # wraps midnight


class Dataset:
    """Every participant-facing table, indexed for lookup."""

    def __init__(self):
        self.messages = _read("messages.csv")
        self.samples = _read("sample_messages.csv")
        self.history = _read("message_history.csv")
        self.events = _read("message_events.csv")

        self.users = {r["user_id"]: r for r in _read("users.csv")}
        self.groups = {r["group_id"]: r for r in _read("groups.csv")}
        self.businesses = {r["business_id"]: r for r in _read("business_accounts.csv")}
        self.images = {r["image_id"]: r["file_path"] for r in _read("images.csv")}
        self.voice = {r["voice_note_id"]: r["file_path"] for r in _read("voice_notes.csv")}

        membership_rows = _read("group_members.csv")
        self.membership = {(r["group_id"], r["user_id"]): r for r in membership_rows}
        self.relationship = {(r["user_id"], r["business_id"]): r
                             for r in _read("user_business_history.csv")}

        self.events_by_message = {(r["user_id"], r["message_id"]): r for r in self.events}
        self.history_by_id = {r["message_id"]: r for r in self.history}
        self._history_sender_counts = Counter(
            (r["user_id"], r.get("business_id", ""), r.get("group_id", ""),
             r.get("sender_user_id", ""))
            for r in self.history
        )

        self._group_admins = defaultdict(set)
        self._work_groups_by_user = defaultdict(set)
        for r in membership_rows:
            if r.get("role") == "admin":
                self._group_admins[r["group_id"]].add(r["user_id"])
            group = self.groups.get(r["group_id"], {})
            if rules.is_work_group(group.get("group_type")):
                self._work_groups_by_user[r["user_id"]].add(r["group_id"])

        self.prior = None          # built lazily; see build()
        self.load = defaultdict(lambda: [0, 0])
        for r in _read("daily_notification_summary.csv"):
            bucket = self.load[r["user_id"]]
            bucket[0] += _int(r["notifications_sent"])
            bucket[1] += _int(r["notifications_dismissed"])

    def media_path(self, message):
        mid = (message.get("media_id") or "").strip()
        rel = self.images.get(mid) or self.voice.get(mid)
        return os.path.join(DATASET, rel) if rel else None

    def sender_is_group_admin(self, message):
        gid = (message.get("group_id") or "").strip()
        sender = (message.get("sender_user_id") or "").strip()
        return bool(gid and sender and sender in self._group_admins[gid])

    def has_work_context(self, message):
        """Whether this message is in, or between members of, a work group."""
        gid = (message.get("group_id") or "").strip()
        if rules.is_work_group((self.groups.get(gid) or {}).get("group_type")):
            return True
        user_id = (message.get("user_id") or "").strip()
        sender = (message.get("sender_user_id") or "").strip()
        return bool(user_id and sender and
                    self._work_groups_by_user[user_id] & self._work_groups_by_user[sender])

    def reaction_profile(self, user_id, sender_key):
        """How this user has historically reacted to THIS sender.

        The single most predictive feature available: a user who dismissed
        the last four messages from a sender does not want the fifth.
        """
        counts = Counter()
        seen = 0
        for h in self.history:
            if h["user_id"] != user_id:
                continue
            if (h.get("business_id", ""), h.get("group_id", ""),
                    h.get("sender_user_id", "")) != sender_key:
                continue
            event = self.events_by_message.get((user_id, h["message_id"]))
            if not event:
                continue
            seen += 1
            for field in ("message_opened", "message_replied", "notification_dismissed",
                          "muted_after_message", "message_reported"):
                counts[field] += _int(event.get(field))
        if not seen:
            return None
        return {"messages_from_this_sender": seen,
                "opened": counts["message_opened"],
                "replied": counts["message_replied"],
                "dismissed": counts["notification_dismissed"],
                "muted_after": counts["muted_after_message"],
                "reported": counts["message_reported"]}

    def sender_history_count(self, message):
        """Number of previous messages from this exact sender to this user.

        This includes history without an interaction event: absent telemetry
        does not turn a familiar sender into a cold-start sender.
        """
        key = (
            message.get("user_id", ""),
            message.get("business_id", "") or "",
            message.get("group_id", "") or "",
            message.get("sender_user_id", "") or "",
        )
        return self._history_sender_counts[key]


def effective_text(message, media_text=None):
    """Message text plus anything a model read out of its attachment.

    A voice note has no `message_text` at all, so without this the whole
    feature layer sees an empty string for the rows that need it most.
    """
    parts = [(message.get("message_text") or "").strip(), (media_text or "").strip()]
    return "\n".join(part for part in parts if part)


def is_unfamiliar_sender(ctx):
    """A cold-start signal exposed to both routing policy and the model."""
    return bool(ctx.get("unfamiliar_sender"))


def build(message, data, media_text=None):
    """The full deterministic context for one incoming message."""
    uid = message.get("user_id", "")
    sender = (message.get("sender_user_id") or "").strip()
    user = data.users.get(uid, {})
    gid = (message.get("group_id") or "").strip()
    bid = (message.get("business_id") or "").strip()
    group = data.groups.get(gid)
    member = data.membership.get((gid, uid))
    business = data.businesses.get(bid)
    relation = data.relationship.get((uid, bid))
    sent, dismissed = data.load[uid]

    ctx = {
        "message": {k: message.get(k, "") for k in
                    ("message_id", "user_id", "conversation_type", "group_id",
                     "business_id", "sender_user_id", "created_at", "media_type",
                     "media_id", "forwarded_count")},
        "work_context": data.has_work_context(message),
        "user": {
            "do_not_disturb_window": user.get("do_not_disturb_window", ""),
            "arriving_inside_dnd": in_dnd(message.get("created_at"),
                                          user.get("do_not_disturb_window")),
            "messages_opened_30d": _int(user.get("messages_opened_30d")),
            "messages_replied_30d": _int(user.get("messages_replied_30d")),
            "notifications_dismissed_30d": _int(user.get("notifications_dismissed_30d")),
            "messages_reported_30d": _int(user.get("messages_reported_30d")),
            "reply_rate": _ratio(user.get("messages_replied_30d"),
                                 user.get("messages_opened_30d")),
            "daily_notifications_sent": sent,
            "daily_dismiss_rate": _ratio(dismissed, sent),
        },
        # Signals are computed over the transcript/description too, so a voice
        # note is not silently featureless. They are context for the model,
        # never verdicts -- and deliberately never reach Tier 0.
        "signals": rules.features(
            dict(message, message_text=effective_text(message, media_text)), business),
    }

    if group:
        ctx["group"] = {
            "group_name": group.get("group_name", ""),
            "group_type": group.get("group_type", ""),
            "member_count": _int(group.get("member_count")),
            "messages_30d": _int(group.get("messages_30d")),
            "sender_is_admin": data.sender_is_group_admin(message),
        }
    if member:
        ctx["membership"] = {
            "role": member.get("role", ""),
            "muted_by_user": _int(member.get("group_muted_by_user")) == 1,
            "messages_read_30d": _int(member.get("messages_read_30d")),
            "replies_sent_30d": _int(member.get("replies_sent_30d")),
            "notifications_dismissed_30d": _int(member.get("notifications_dismissed_30d")),
            "read_rate": _ratio(member.get("messages_read_30d"), group.get("messages_30d")
                                if group else 0),
        }
    if business:
        ctx["business"] = {
            "brand_name": business.get("brand_name", ""),
            "category": business.get("category", ""),
            "verified": _int(business.get("verified")) == 1,
            "official_domain": business.get("official_domain", ""),
            "domain_used_by_sender": business.get("domain_used_by_sender", ""),
            "account_age_days": _int(business.get("account_age_days")),
            "user_reports_30d": _int(business.get("user_reports_30d")),
            "sender_domain_age_days": _int(business.get("domain_used_by_sender_age_days")),
        }
    if relation:
        opted_out = (relation.get("promotions_opted_out_at") or "").strip()
        active = (not opted_out
                  and _int(relation.get("activity_count_180d")) > 0
                  and _int(relation.get("allows_promotions")) == 1)
        ctx["business_relationship"] = {
            "why_user_knows_account": relation.get("why_user_knows_account", ""),
            "last_activity_at": relation.get("last_activity_at", ""),
            "allows_promotions": _int(relation.get("allows_promotions")) == 1,
            "opted_out_of_promotions_at": opted_out or None,
            "activity_count_180d": _int(relation.get("activity_count_180d")),
            "messages_opened_30d": _int(relation.get("messages_opened_30d")),
            "messages_dismissed_30d": _int(relation.get("messages_dismissed_30d")),
            "user_has_active_relationship": active,
        }
    elif bid:
        ctx["business_relationship"] = None      # sender is a business the user does not know

    profile = data.reaction_profile(uid, (bid, gid, sender))
    sender_history_count = data.sender_history_count(message)
    ctx["sender_history_count"] = sender_history_count
    ctx["unfamiliar_sender"] = sender_history_count == 0
    if profile:
        ctx["past_reactions_to_this_sender"] = profile

    # The behavioural prior. Raw counts ("dismissed: 4") are much harder for a
    # model to weigh than smoothed rates plus an explicit distribution, and the
    # counts were demonstrably not being used well.
    if data.prior is None:
        data.prior = prior_mod.Prior(data)
    ctx["behavioural_prior"] = data.prior.score(message, ctx).as_dict()
    return ctx


def claim_facts(ctx, message=None, media_text=None):
    """The facts a catalogue reason might assert, drawn from an assembled ctx.

    Lives here so the pipeline and the eval harness check identical facts --
    two implementations of "is this business verified" would eventually
    disagree, and the disagreement would look like a scoring bug.
    """
    reactions = ctx.get("past_reactions_to_this_sender") or {}
    relationship = ctx.get("business_relationship") or {}
    signals = ctx.get("signals", {})
    text = effective_text(message or {}, media_text)
    return {
        "group_type": (ctx.get("group") or {}).get("group_type", ""),
        "work_context": bool(ctx.get("work_context")),
        "sender_is_admin": bool((ctx.get("group") or {}).get("sender_is_admin")),
        "business_verified": bool((ctx.get("business") or {}).get("verified")),
        "history": reactions.get("messages_from_this_sender", 0),
        "opted_out": bool(relationship.get("opted_out_of_promotions_at")),
        "dismissed": reactions.get("dismissed", 0) > 0,
        "muted_after": reactions.get("muted_after", 0) > 0,
        "forwarded": signals.get("forwarded_count", 0) > 0,
        "greeting_text": bool(rules.GREETING.search(text)),
        "injection": bool(signals.get("impersonates_system")),
        "asks_secret": bool(signals.get("asks_for_secret")),
        "allows_promotions": bool(relationship.get("allows_promotions")),
        "has_relationship": bool(relationship),
        "is_business": bool((message or {}).get("business_id", "").strip()),
        "chain_forward": bool(signals.get("chain_forward_language")),
    }
