"""Provider-agnostic model access. No vendor is hardcoded.

Groq, Gemini, OpenRouter, Together and Ollama all expose the same
OpenAI-compatible surface -- `/chat/completions`, `/audio/transcriptions`, and
image parts inside a chat message. So one client covers hosted free tiers and
fully local models, and switching between them is a `.env` edit rather than a
code change.

Three independent roles, each configured by a base URL, a key and a model:

    LLM_*      routing decisions      (Tier 1 and Tier 2)
    VISION_*   image descriptions
    ASR_*      voice-note transcripts

They are separate because no single free provider covers all three. Groq has
the most generous free text tier but **dropped vision** when llama-4-scout was
deprecated in June 2026, so images go to Gemini's free tier instead. Point all
three at http://localhost:11434/v1 to run entirely on Ollama.

Keys come from the environment, or from a .env file at the repo root that is
loaded into the environment at startup. Never from an argument, never from
source. A real environment variable always wins over .env.
"""

import collections
import os
import random
import re
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(ROOT, ".env")

# Sensible free-tier defaults. Every one is overridable from .env.
#
# The rate limits matter more than they look. At ~1,750 tokens per routing
# prompt, 110 messages is ~192,000 input tokens. Against Groq's free 30,000
# tokens/min that is a 6.4 minute floor, and an unpaced run would start
# getting 429s after roughly the first 17 messages. TOKENS are the binding
# constraint here, not requests.
DEFAULTS = {
    "LLM_BASE_URL": "https://api.groq.com/openai/v1",
    "LLM_MODEL": "llama-3.3-70b-versatile",
    "LLM_RPM": "28",              # Groq free tier is ~30; leave headroom
    "LLM_TPM": "28000",           # Groq free tier is 30,000
    "VISION_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "VISION_MODEL": "gemini-2.5-flash",
    "VISION_RPM": "9",            # Gemini free tier is 10/min, 250/day
    "VISION_TPM": "0",            # 0 disables the token budget for this role
    "ASR_BASE_URL": "https://api.groq.com/openai/v1",
    "ASR_MODEL": "whisper-large-v3",
    "ASR_RPM": "18",
    "ASR_TPM": "0",
}

# Statuses worth retrying. 429 is the rate limit; the 5xx family is transient.
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 6
MAX_BACKOFF = 60.0

# Local providers need a key to satisfy the SDK but ignore its value.
_PLACEHOLDER_KEYS = {"ollama", "local", "none", "not-needed"}

_loaded = False


def load_env():
    """Load .env into os.environ once, without overriding real variables."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not os.path.exists(ENV_FILE):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_env_manually()
        return
    load_dotenv(ENV_FILE, override=False)


def _load_env_manually():
    """Minimal fallback so the project works without python-dotenv."""
    with open(ENV_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip("'\"")
            if key and value and key not in os.environ:
                os.environ[key] = value


def setting(name):
    load_env()
    return os.environ.get(name) or DEFAULTS.get(name, "")


def estimate_tokens(text):
    """Rough token count. ~3.6 chars/token holds well enough for pacing.

    Deliberately an estimate: asking the provider costs a request, which is the
    thing we are trying to conserve. Erring high is safe -- it only slows us.
    """
    return int(len(text or "") / 3.6) + 1


class Pacer:
    """Client-side throttle so we never earn a 429 in the first place.

    Keeps a 60-second sliding window of requests and tokens, and blocks before
    a call that would breach either budget. Retrying after a rejection wastes
    the rejected request against the same quota, so pacing beats reacting.

    `tpm=0` disables the token budget for roles where it does not apply
    (image and audio endpoints bill differently).
    """

    def __init__(self, role, rpm, tpm, announce=None):
        self.role = role
        self.rpm = max(0, int(rpm or 0))
        self.tpm = max(0, int(tpm or 0))
        self.announce = announce
        self.events = collections.deque()      # (timestamp, tokens)
        self.waited = 0.0

    def _prune(self, now):
        while self.events and now - self.events[0][0] >= 60.0:
            self.events.popleft()

    def reserve(self, tokens=0):
        """Block until this call fits inside both budgets."""
        if not self.rpm and not self.tpm:
            return
        while True:
            now = time.monotonic()
            self._prune(now)
            requests = len(self.events)
            used = sum(t for _ts, t in self.events)

            over_requests = self.rpm and requests + 1 > self.rpm
            over_tokens = self.tpm and used + tokens > self.tpm
            if not over_requests and not over_tokens:
                self.events.append((now, tokens))
                return

            delay = 60.0 - (now - self.events[0][0]) + 0.05
            delay = max(0.05, min(delay, 60.0))
            self.waited += delay
            if self.announce:
                reason = "request budget" if over_requests else "token budget"
                self.announce(f"    pacing {self.role}: {reason} full, "
                              f"waiting {delay:.1f}s")
            time.sleep(delay)


def _status_of(exc):
    for attribute in ("status_code", "http_status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


class QuotaExhausted(RuntimeError):
    """A limit that waiting-in-process cannot fix -- typically per-day.

    Distinct from a per-minute 429, which backoff genuinely solves. Retrying a
    daily cap just burns attempts and then fails anyway, several minutes later
    and with a less useful message.
    """


def _quota_message(exc):
    """The provider's own text, which names which limit was hit."""
    for attribute in ("message", "body"):
        value = getattr(exc, attribute, None)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            error = value.get("error") or {}
            if isinstance(error, dict) and error.get("message"):
                return error["message"]
    return str(exc)


def _is_daily(exc):
    text = _quota_message(exc).lower()
    return any(marker in text for marker in
               ("per day", "tokens per day", "tpd", "requests per day", "rpd",
                "daily limit", "quota exceeded"))


def _retry_after(exc, cap=MAX_BACKOFF):
    """The server's own backoff instruction, in seconds, or None.

    `cap=None` returns the raw figure, which the caller uses to decide whether
    the wait is short enough to sit through at all.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    raw = None
    for name in ("retry-after", "Retry-After", "x-ratelimit-reset-tokens",
                 "x-ratelimit-reset-requests"):
        if headers.get(name):
            raw = headers[name]
            break

    if raw is None:
        # Groq states the wait in the body, not a header: "try again in 6m56.4s".
        found = re.search(r"try again in ((?:\d+(?:\.\d+)?[hms])+)",
                          _quota_message(exc), re.I)
        if not found:
            return None
        raw = found.group(1)

    seconds = _parse_duration(raw)
    if seconds is None:
        return None
    return seconds if cap is None else min(cap, seconds)


def _parse_duration(raw):
    """Accept `45`, `45s`, `6m56.4s`, `1h2m3s`."""
    text = str(raw).strip()
    try:
        return float(text)
    except ValueError:
        pass
    total, matched = 0.0, False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", text, re.I):
        total += float(value) * {"h": 3600, "m": 60, "s": 1}[unit.lower()]
        matched = True
    return total if matched else None


def call_with_retry(call, pacer=None, tokens=0, label="", announce=None):
    """Run `call`, pacing before it and backing off on a retryable failure.

    Raises the last exception once attempts are exhausted, rather than
    returning a default -- a silent fallback here would hide a broken key or a
    daily quota behind plausible-looking output.
    """
    last = None
    for attempt in range(MAX_ATTEMPTS):
        if pacer:
            pacer.reserve(tokens)
        try:
            return call()
        except Exception as exc:                   # noqa: BLE001 - re-raised below
            status = _status_of(exc)
            retryable = status in RETRYABLE_STATUS or status is None and _is_transport(exc)

            # A daily cap, or a wait longer than we are willing to sleep, is
            # not something backoff fixes. Stop now with a message that says
            # what to do, instead of burning attempts and failing anyway.
            wait = _retry_after(exc, cap=None)
            if status == 429 and (_is_daily(exc) or (wait or 0) > MAX_BACKOFF):
                raise QuotaExhausted(_quota_message(exc)) from exc

            if not retryable or attempt == MAX_ATTEMPTS - 1:
                raise
            delay = _retry_after(exc) or min(MAX_BACKOFF, 2.0 ** attempt)
            delay += random.uniform(0, 0.5)        # jitter, de-synchronise retries
            if announce:
                announce(f"    {label or 'call'} got {status or type(exc).__name__}; "
                         f"retry {attempt + 1}/{MAX_ATTEMPTS - 1} in {delay:.1f}s")
            time.sleep(delay)
            last = exc
    raise last                                     # unreachable; kept explicit


def _is_transport(exc):
    name = type(exc).__name__
    return "Connection" in name or "Timeout" in name


def pacer_for(role, announce=None):
    return Pacer(role, setting(f"{role}_RPM"), setting(f"{role}_TPM"), announce)


def _client(role):
    """(client, model, why_unavailable). `why` is None when usable."""
    load_env()
    base_url = setting(f"{role}_BASE_URL")
    model = setting(f"{role}_MODEL")
    key = os.environ.get(f"{role}_API_KEY", "").strip()

    if not key:
        return None, model, (f"{role}_API_KEY is not set (see .env.example). "
                             f"Would have called {model} at {base_url}")
    try:
        import openai
    except ImportError:
        return None, model, "the openai package is not installed"

    return openai.OpenAI(base_url=base_url, api_key=key), model, None


def text_client():
    """For routing decisions."""
    return _client("LLM")


def vision_client():
    """For image descriptions. A different provider from text by necessity."""
    return _client("VISION")


def audio_client():
    """For voice-note transcription."""
    return _client("ASR")


def describes_local(role):
    """Is this role pointed at something running on this machine?"""
    base_url = setting(f"{role}_BASE_URL")
    return "localhost" in base_url or "127.0.0.1" in base_url


def summary():
    """One line per role, for the run banner. Never prints a key."""
    load_env()          # must precede the first os.environ read below --
    # without it the FIRST role is reported before .env has been loaded and
    # always shows "no key", while later roles show "key set".
    lines = []
    for role in ("LLM", "VISION", "ASR"):
        key = os.environ.get(f"{role}_API_KEY", "").strip()
        if not key:
            state = "no key"
        elif key.lower() in _PLACEHOLDER_KEYS:
            state = "local"
        else:
            state = "key set"
        lines.append(f"  {role:7s} {setting(f'{role}_MODEL'):32s} "
                     f"{setting(f'{role}_BASE_URL'):55s} [{state}]")
    return "\n".join(lines)
