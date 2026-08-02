"""Deterministic predicates over a message.

Single source of truth: imported by both the router (to decide tiers) and
postprocess (to veto the model). No regex is defined twice.

Design rule, established by measurement (see docs/repo-analysis.md 3.6):
regexes here are FEATURE EXTRACTORS and narrow high-precision blocks only.
They are never a general classifier. The same word carries opposite gold
labels -- "stadium road is blocked" is notify, "profile will be blocked"
is scam -- so any rule without a semantic qualifier is wrong.
"""

import re

# --- Tier 0 -----------------------------------------------------------------

# Router-manipulation. Six such rows exist in messages.csv plus one in the
# labelled samples; all wrap a real scam payload in fake system framing.
INJECTION = re.compile(
    r"routing override"
    r"|system note for (the )?notification router"
    r"|internal router metadata"
    r"|assistant instruction"
    r"|ignore (all )?previous"
    r"|ignore sender risk"
    r"|set action\s*=|action\s*=\s*(notify|digest|mute)"
    r"|confidence\s*=\s*1"
    r"|mark (this )?(message )?as (notify|urgent)"
    r"|classify (this )?as",
    re.I,
)

# A NAMED secret. Bare "code" is deliberately excluded: a legitimate delivery
# message reading "share pickup code only after courier arrives" was muted as
# scam by a bare-"code" pattern. A pickup code handed to a courier at the door
# is not a secret to transmit.
SECRET = (
    r"(otp|password|pin|cvv|login code|verification code|security code"
    r"|one[- ]time (code|password)|6 ?digit code)"
)

# Hinglish verbs (batao/daal) and urgency (abhi) are folded in here rather
# than kept as a separate rule, which is what unified language coverage.
CREDENTIAL_SOLICITATION = re.compile(
    rf"(send|share|confirm|enter|reply with|provide|submit|batao|daal).{{0,30}}{SECRET}"
    rf"|{SECRET}.{{0,25}}(now|immediately|abhi|to keep|to avoid|to release|to restore)"
    rf"|verify {SECRET}"
    rf"|complete.{{0,15}}kyc",
    re.I,
)

# A sender WARNING about credential theft is the opposite of one soliciting it.
# A verified brand's safety advisory saying it never asks for OTP is gold
# digest in the labelled set; without this guard it is muted as scam.
WARNS = re.compile(
    r"never ask|do not share|don'?t share|will not ask|beware|advisory"
    r"|be careful|fraud awareness|report suspicious|only after",
    re.I,
)

# --- Tier 1 -----------------------------------------------------------------

QR_PAYMENT_PRESSURE = re.compile(
    r"scan\W{0,10}(this\W)?qr\W{0,25}(and\W)?pay"
    r"|qr\W{0,20}(pay|payment)\W{0,20}(immediately|now|today)",
    re.I,
)

# Romanised-Hindi function words. Used ONLY to tag language for the prompt --
# never to route. Hinglish spans the full label range in this dataset,
# including two urgent society notices and a legitimate admin reminder.
HINGLISH_MARKERS = re.compile(
    r"\b(aapka|aap|apka|karo|karna|kare|karein|kijiye|hai|hain|hoga|gaya|gayi"
    r"|nahi|nahin|abhi|warna|kripya|bhej|bhejo|jaldi|turant|sabko|sabka"
    r"|bhagwan|beta|bhai|mera|meri|apna|apne|khata|bachane|kar dena)\b",
    re.I,
)

CHAIN_FORWARD = re.compile(
    r"share kar|forward kar|sab groups|sabko bhej|forward (this|as received)"
    r"|share.{0,20}(groups|everyone|ten people)|blessing|positive energy",
    re.I,
)

HIGH_FORWARD_THRESHOLD = 5

# Group types that establish a work relationship between their members. This
# is deliberately metadata-based: a direct message can be work-related when
# sender and recipient share a work group even though the direct conversation
# itself has no group_id.
WORK_GROUP_TYPES = frozenset(("coworker", "college_faculty", "tech_community"))

# --- message_type hints -----------------------------------------------------
# FEATURES ONLY, never actions. These exist because the offline fallback mapped
# every group message to `event`, which scored 0 correct out of 7 predictions.
# A word list is a weak classifier, but "any group message is an event" is a
# worse one.

GREETING = re.compile(
    r"good (morning|night|evening)|happy (new year|diwali|holi|eid|birthday|anniversary)"
    r"|shubh|namaste|greetings|warm wishes|blessed|god bless|have a (great|peaceful|nice)",
    re.I,
)

# A scheduled occasion with a time or place, which is what `event` means here.
SCHEDULED = re.compile(
    r"\b(meeting|meet|trip|match|party|event|rehearsal|practice|assembly|ceremony"
    r"|reunion|picnic|function|celebration|drive|camp|session|class|exam|test"
    r"|pickup|drop|bus|circular|schedule|timing|agenda|venue|school"
    r"|sports day|postponed|clubhouse|cultural fest)\b",
    re.I,
)

PROMOTIONAL = re.compile(
    r"\b(offer|sale|discount|deal|coupon|voucher|cashback|promo|% ?off|flat \d+"
    r"|limited time|shop now|buy now|order now|price drop|bestseller|new arrival)\b",
    re.I,
)

# Commercial spam is not the same thing as a legitimate promotion. The latter
# can be useful to some recipients; the former uses improbable earnings or
# credit claims to pull a recipient to an untrusted account. Keep this narrow:
# an unverified sender alone is a risk feature, not enough to call every
# message from it spam.
COMMERCIAL_SPAM = re.compile(
    r"\b(?:earn|make)\s*(?:₹|rs\.?|inr)?\s*\d[\d,]*\s*(?:a|per)?\s*day\b"
    r"|\b(?:typing|data entry)\s+jobs?\b"
    r"|\bwork from home\b.{0,40}\b(?:earn|income|daily)\b"
    r"|\bpre[- ]?approved\b.{0,30}\b(?:loan|lakh|lac|credit)\b"
    r"|\b(?:instant|guaranteed)\b.{0,25}\b(?:loan|income|return|credit)\b",
    re.I,
)

SUSPICIOUS_LINK = re.compile(r"\b(?:https?://|www\.)\S+", re.I)

# `urgent` means a bounded window with a consequence, not merely "important".
# Paired with RELAXED below, exactly like CREDENTIAL_SOLICITATION is paired
# with WARNS: the raw pattern alone fires on "when you get 5 mins can you
# call?", which is gold `personal`. The guard is what makes it usable.
TIME_PRESSURE = re.compile(
    r"\b(in|within|starts? in|only|max(?:imum)?) ?\d+ ?(min|minute|hour)s?\b"
    r"|\b\d+ ?(min|minute|hour)s? (max|left|only)\b"
    r"|\b(right now|come online now|immediately|asap|urgent|emergency)\b"
    r"|\b(escalation|deadline|cut ?off|last date|expires? today)\b"
    r"|\b(pulled (to|forward)|moved up|brought forward)\b"
    # An actual outage needs attention even without a countdown. Scope this
    # to down/outage/incident so ordinary mentions of a server do not escalate.
    r"|\b(?:server|service|production|prod|system)\s+(?:is\s+)?(?:down|outage)\b"
    r"|\b(?:production|prod)\s+incident\b",
    re.I,
)

# Explicit de-escalation. A sender saying "no pressure" is telling you it is
# not urgent, and that statement outranks any clock-shaped phrase nearby.
RELAXED = re.compile(
    r"no pressure|nothing dramatic|nothing urgent|no rush|no hurry"
    r"|whenever (you|it)|when you get|when free|at your convenience"
    # "can wait" is deliberately absent: "the tanker guy can wait 20 mins max"
    # is a bounded window, i.e. the opposite of de-escalation. Every other
    # token here is unambiguous about the SENDER lowering urgency.
    r"|no need to (respond|reply)|talk tomorrow|tomorrow morning"
    r"|not urgent|take your time",
    re.I,
)


def is_time_critical(text):
    """A bounded window with a consequence, and no de-escalation language."""
    return bool(TIME_PRESSURE.search(text)) and not RELAXED.search(text)


PAYMENT_DUE = re.compile(
    r"\b(due|overdue|invoice|bill|maintenance|instal?ment|fee|premium|emi|rent)\b"
    r".{0,40}\b(pay|payment|paid|due|by|before)\b"
    r"|\bpay\b.{0,30}\b(due|by|before|today|tomorrow)\b",
    re.I,
)

# Untrusted business signature. Replaces domain_mismatch, which is unusable:
# only one labelled row has a mismatch (verified brand on a link shortener,
# gold digest), and a blank official_domain blocks a true positive.
UNTRUSTED_MAX_ACCOUNT_AGE = 60
UNTRUSTED_MIN_REPORTS = 20


def _int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def asks_for_secret(text):
    """Solicits a named secret and is not warning about solicitation."""
    return bool(CREDENTIAL_SOLICITATION.search(text)) and not WARNS.search(text)


def impersonates_system(text):
    return bool(INJECTION.search(text))


def is_hinglish(text, min_markers=2):
    return len(HINGLISH_MARKERS.findall(text)) >= min_markers


def is_work_group(group_type):
    return (group_type or "").strip() in WORK_GROUP_TYPES


def untrusted_business(business):
    """business: a row from business_accounts.csv, or None."""
    if not business:
        return False
    verified = business.get("verified", "")
    return (
        (verified is False or str(verified).strip() == "0")
        and _int(business.get("account_age_days"), 10**6) < UNTRUSTED_MAX_ACCOUNT_AGE
        and _int(business.get("user_reports_30d")) > UNTRUSTED_MIN_REPORTS
    )


def is_spam(text, business=None):
    """Whether an untrusted business makes a high-risk commercial pitch.

    A URL is meaningful here only in combination with an untrusted sender;
    legitimate business links and unverified-but-benign notices are not spam
    solely because they contain a link.
    """
    if not untrusted_business(business):
        return False
    commercial_text = COMMERCIAL_SPAM.search(text) or SUSPICIOUS_LINK.search(text)
    display_name = str(business.get("display_name", "")).lower()
    brand_name = str(business.get("brand_name", "")).lower()
    category = str(business.get("category", "")).lower()
    # Voice notes can have no local transcript. A sender explicitly presenting
    # itself as an unknown loan desk is still a commercial-risk signal, based
    # on its provenance rather than pretending empty audio has benign content.
    anonymous_loan_sender = (
        category == "finance"
        and (brand_name in {"", "unknown"} or "loan" in display_name)
    )
    return bool(commercial_text or anonymous_loan_sender)


def domain_mismatch(business):
    """FEATURE ONLY -- never a routing trigger. A blank official_domain is not
    evidence: at least one legitimate, long-established pharmacy account in
    business_accounts.csv leaves that field empty."""
    if not business:
        return False
    official = str(business.get("official_domain", "")).strip()
    used = str(business.get("domain_used_by_sender", "")).strip()
    return bool(official and used and official != used)


def classify(message, business=None):
    """Return (rule_name, tier, action, message_type) or None.

    tier 0 -> decided in code, never sent to the model
    tier 1 -> action fixed, model assigns message_type
    """
    text = message.get("message_text", "") or ""

    if impersonates_system(text):
        return ("R1_injection", 0, "mute", "scam")
    if asks_for_secret(text):
        return ("R2_credential_solicitation", 0, "mute", "scam")

    if untrusted_business(business):
        return ("R3_untrusted_business", 1, "mute", None)
    if _int(message.get("forwarded_count")) >= HIGH_FORWARD_THRESHOLD:
        return ("R4_high_forward", 1, "mute", None)
    if QR_PAYMENT_PRESSURE.search(text) and not WARNS.search(text):
        return ("R5_qr_payment_pressure", 1, "mute", None)
    return None


def features(message, business=None):
    """Booleans passed INTO the prompt as context. Not verdicts."""
    text = message.get("message_text", "") or ""
    return {
        "asks_for_secret": asks_for_secret(text),
        "impersonates_system": impersonates_system(text),
        "qr_payment_pressure": bool(QR_PAYMENT_PRESSURE.search(text)),
        "chain_forward_language": bool(CHAIN_FORWARD.search(text)),
        "is_hinglish": is_hinglish(text),
        "forwarded_count": _int(message.get("forwarded_count")),
        "business_unverified": bool(business) and str(business.get("verified", "")).strip() == "0",
        "business_domain_mismatch": domain_mismatch(business),
        "business_account_age_days": _int(business.get("account_age_days")) if business else None,
        "business_reports_30d": _int(business.get("user_reports_30d")) if business else None,
        "commercial_spam": is_spam(text, business),
    }
