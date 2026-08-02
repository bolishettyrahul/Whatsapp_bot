"""The deterministic fallback: how to route with no model available.

Split out of main.py, which is orchestration. This module is policy -- which
action, which type, which catalogue entry -- and it runs whenever no key is
configured or the model failed twice.

Action and type are decided SEPARATELY. They used to be entangled, with each
branch hardcoding a type, which is why `greeting` was never predicted: a
forwarded greeting hit the forward branch and could never come back.
"""

import context
import rules
import taxonomy


def without_model(message, ctx, catalogue, allowed, media_text=None):
    """Deterministic fallback: no key configured, or the model failed twice.

    Action and type are decided SEPARATELY. They were entangled before, with
    each branch hardcoding a type, which is why `greeting` was never predicted:
    a forwarded greeting hit the forward branch and could never come back. The
    catalogue's ambiguous entries (greeting|forward, promotion|spam) exist
    precisely so content can choose within a fixed action.
    """
    action = fallback_action(message, ctx, media_text)
    if allowed and action not in allowed:
        action = next(iter(allowed))

    guess = guess_type(message, ctx, action, media_text)

    # `payment` has no labelled reason in sample_messages.csv, even though it
    # is a legal output type. Do not erase a clear payment classification just
    # because the reason catalogue cannot express it yet.
    if guess == "payment" and action in {"digest", "notify"}:
        reason = ("A legitimate payment has a near-term deadline and needs attention."
                  if action == "notify" else
                  "A legitimate payment reminder can be reviewed before its stated deadline.")
        return action, guess, reason, catalogue.confidence_for(action)

    # Likewise, a predicate-confirmed commercial spam pitch should retain its
    # type even if every catalogue reason with that type makes an unrelated
    # historical-preference claim.
    if guess == "spam" and action == "mute":
        return action, guess, "An untrusted sender is making a high-risk commercial pitch.", \
            catalogue.confidence_for(action)
    # Among the options for this action, prefer one whose factual claim is
    # actually true of this row. Emitting "A verified business is sending..."
    # for an unverified sender is a confident false statement, and 7 of 30 rows
    # did exactly that before this check existed.
    facts = context.claim_facts(ctx, message)
    option = (option_for(catalogue, action, guess, facts)
              or option_for(catalogue, action, None, facts)
              or option_for(catalogue, action, guess)
              or option_for(catalogue, action))
    if option is None:
        return (action, guess or "unknown", "Routed without model judgement.",
                catalogue.confidence_for(action))
    chosen = guess if guess in option.types else taxonomy.disambiguate(option, facts)
    return (option.action, chosen, option.reason,
            catalogue.confidence_for(option.action, option))


def fallback_action(message, ctx, media_text=None):
    """Which action, ignoring type entirely.

    The behavioural prior wins wherever it is confident -- it is calibrated
    against population base rates rather than hand-tuned, and standalone it
    scores 23/30 against this cascade's 23/30 while getting notify recall
    8/9 instead of 4/9. The cascade only runs when the prior abstains.
    """
    verdict = ctx.get("behavioural_prior") or {}
    if verdict.get("confident"):
        return verdict["most_likely"]

    reactions = ctx.get("past_reactions_to_this_sender") or {}
    ignored = reactions.get("dismissed", 0) + reactions.get("muted_after", 0)
    opened = reactions.get("opened", 0)
    signals = ctx.get("signals", {})
    relationship = ctx.get("business_relationship") or {}
    text = context.effective_text(message, media_text)

    if signals.get("chain_forward_language") or signals.get("forwarded_count", 0) >= 5:
        return "mute"
    if relationship.get("opted_out_of_promotions_at"):
        return "mute"
    if ignored > 2 * max(opened, 1):
        return "mute"
    # A greeting from a group admin is still a greeting. It should not inherit
    # the admin-notification prior unless it also contains event or urgency
    # content that needs an immediate response.
    if (rules.GREETING.search(text) and not rules.SCHEDULED.search(text)
            and not rules.is_time_critical(text)):
        return "digest"
    if (ctx.get("group") or {}).get("sender_is_admin"):
        return "notify"
    return "digest"


def guess_type(message, ctx, action=None, media_text=None):
    """Content-shaped message_type for the offline fallback.

    Replaces a `conversation_type` lookup that sent every group message to
    `event` -- 7 predictions, 0 correct. Ordered most-specific first; the
    conversation type is only the last resort rather than the first answer.
    """
    text = context.effective_text(message, media_text)

    # `urgent` only competes inside `notify`. Scoped deliberately: an
    # unscoped urgency regex was tried earlier and cost a point, because
    # clock-shaped phrases appear all over the dataset. Within notify the
    # question is narrow -- is this a bounded window, or a casual request?
    if action == "notify" and rules.is_time_critical(text):
        return "urgent"

    # Consider commercial spam before the generic promotion rule, otherwise
    # words such as "offer" hide an untrusted high-yield or loan pitch.
    if (ctx.get("signals", {}).get("commercial_spam")
            or rules.is_spam(text, (ctx.get("business") or None))):
        return "spam"

    if rules.GREETING.search(text):
        return "greeting"
    if rules.PROMOTIONAL.search(text):
        return "promotion"
    if rules.PAYMENT_DUE.search(text):
        return "payment"
    if rules.SCHEDULED.search(text):
        return "event"
    conversation = message.get("conversation_type")
    if conversation == "business":
        return "business_update"
    # A safe casual message from someone the user knows is personal; the same
    # low-priority message from a cold-start personal contact is unknown.
    # Do not override notify: urgent/safety signals have already won upstream.
    if (conversation == "personal" and action == "digest"
            and context.is_unfamiliar_sender(ctx)):
        return "unknown"
    if conversation == "personal":
        return "personal"
    # A group message with no scheduling, commercial or greeting signal is
    # ordinary chatter between people, not an "event".
    return "personal"


def option_for(catalogue, action, message_type=None, facts=None):
    """Best catalogue option for this action (and type, if given).

    With `facts`, options are ranked rather than merely filtered:

        1. makes a checkable claim that is TRUE of this row
        2. makes no checkable claim
        3. makes a claim contradicted by this row -- never selected

    Ranking matters as much as filtering. "The sender is unfamiliar, but the
    message shows no urgency..." is a better reason for a genuine cold-start
    row than a generic "safe casual chat", and both were previously eligible
    with the generic one winning on catalogue order alone.
    """
    best = None
    for option in catalogue.options:
        if option.action != action:
            continue
        if message_type is not None and message_type not in option.types:
            continue
        if facts is None:
            return option
        claim = taxonomy.claim_of(option.reason)
        if claim is None:
            rank = 1                                # no claim: acceptable
        elif claim[1](facts):
            rank = 2                                # claim holds: preferred
        else:
            continue                                # contradicted: never
        if rank == 2:
            return option
        if best is None:
            best = option
    return best


def index_of(catalogue, action, message_type):
    option = option_for(catalogue, action, message_type)
    return option.index if option else None
