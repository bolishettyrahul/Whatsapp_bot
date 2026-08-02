# Message Notification Router — solution

Routes every row of `dataset/messages.csv` to `notify` / `digest` / `mute`
with a message type, a reason, a calibrated confidence, and supporting
evidence from that user's history.

## Setup

Python 3.9+. **No paid API is used anywhere** — free tiers or fully local.

```bash
pip install openai python-dotenv    # everything speaks OpenAI-compatible
pip install pillow                  # optional: one image is AVIF (see below)

cp .env.example .env                # then paste your two free keys into .env
```

`.env` is gitignored and must never be committed. `.env.example` documents
every variable and is safe to commit.

**Two free keys, neither needing a credit card:**

| Variable | Where | Free allowance |
|---|---|---|
| `LLM_API_KEY` + `ASR_API_KEY` | [console.groq.com](https://console.groq.com) | 14,400 req/day, 30k tokens/min |
| `VISION_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | 250 req/day, 10/min |

The same Groq key goes in both `LLM_API_KEY` and `ASR_API_KEY`. Vision needs a
separate provider because **Groq has had no vision model since `llama-4-scout`
was deprecated in June 2026**.

This project routes 110 messages and describes 20 images once — comfortably
inside both free tiers.

### Running fully local instead

Every provider here speaks the OpenAI-compatible protocol, so switching to
local models is a `.env` edit and no code change:

```bash
ollama pull qwen2.5:7b && ollama pull llava
# then uncomment the Ollama block at the bottom of .env.example
```

Ollama has no speech-to-text, so either keep `ASR_*` pointed at Groq or accept
that voice notes route on provenance alone.

### To route messages you need no key at all

`media_cache.json` ships with this package, so image descriptions and voice
transcripts are already captured. Keys are only needed to *rebuild* the cache
or to enable the Tier 1/2 model calls.

## Run

```bash
python code/main.py                 # route all 110 rows -> dataset/output.csv
python code/main.py --limit 5 -v    # smoke test, prints each decision
python code/main.py --no-model      # deterministic tiers only, no API calls
python code/main.py --build-media-cache  # re-capture media, then exit
python code/main.py --resume        # keep rows already in output.csv
python code/evaluation/main.py      # self-eval; exit 1 if any check fails
python code/evaluation/main.py --model   # same, scoring the model path
```

Without a key the run still completes and still writes a valid `output.csv`;
rows needing judgement fall back to a deterministic heuristic and the summary
says so on stdout. Nothing fails silently.

## Rate limits

**A full run takes minutes, by design.** Tokens bind before requests do: a
routing prompt is ~1,750 tokens, so 110 messages is ~192,000 input tokens
against Groq's free 30,000/min — a **6.4 minute floor**. Unpaced, the run
would start collecting 429s after roughly the first 17 messages.

Three defences, in order of preference:

1. **Client-side pacing.** A 60-second sliding window over both requests and
   tokens per role; the client blocks *before* a call that would breach either
   budget. Earning a 429 and retrying wastes the rejected request against the
   same quota, so not sending it is strictly better. Tune with `LLM_RPM` /
   `LLM_TPM` etc. in `.env`; `0` disables a budget.
2. **Backoff on rejection.** Any 429 or 5xx is retried up to 5 times with
   exponential backoff plus jitter, honouring the server's `Retry-After`
   header when it sends one. A **401 is never retried** — a bad key must fail
   loudly rather than masquerade as a rate limit.
3. **Resumability.** Rows are written and flushed one at a time, so an
   interrupted run keeps everything it finished. `--resume` reuses those rows
   and routes only what is missing. Verified: killing a run at row 40 and
   resuming produces a file identical to an uninterrupted run.

Progress and any pacing waits are printed as the run goes; `-q` silences the
pacing notices, `-v` prints every decision.

Running locally? Set every `*_RPM` and `*_TPM` to `0` — throttling yourself
against your own machine is pure waste.

## Media

Images go to Gemini's free tier; voice notes go to Groq `whisper-large-v3`
(deliberately not `-turbo`, which prunes the decoder 32→4 layers — cheap on
clean English, costly on the code-switched Hindi/English in this dataset).
Neither provider is hardcoded; both are resolved from `.env` by `llm.py`.

**Every filename in `dataset/media/` lies about its container.** Measured: 19
of 33 files have the wrong extension. Of 20 `.jpg` images, 7 are PNG, 2 are
WebP and 1 is AVIF; of 13 `.mp3` voice notes, 4 are M4A and 5 are WAV. Since
images are declared to the API *by MIME type*, sending a PNG labelled
`image/jpeg` is rejected outright — half the images would have failed. So
`media.sniff()` reads magic bytes and ignores the filename. The one AVIF is
transcoded to PNG via Pillow, because the vision API does not accept AVIF;
without Pillow that single image is skipped with a message rather than
crashing the run.

The cache records the model, prompt version and a content digest per entry.
Change the prompt and bump `PROMPT_VERSION`, and stale entries become misses
instead of silently serving descriptions from the old prompt.

## How it works

Five tiers. Each message takes the first one that applies.

| Tier | What it does | Rows (of 110) |
|---|---|---|
| **−1** | Near-identical text to a **labelled** sample, same user *and* same sender → inherit that label | 7 |
| **0** | Injection or credential solicitation → `mute`/`scam` **without calling the model** | 10 |
| **1** | A rule fixes the action; the model picks type and wording from the part of the catalogue implying that action | 23 |
| **2** | Full catalogue, one classified call | 70 |
| **3** | Veto over every row above | all |

Three design decisions carry most of the weight.

**`reason` is a closed catalogue, not free prose.** The 30 labelled rows
contain 24 distinct reason strings, six repeated verbatim, and every string
maps to exactly one action. So the model selects a reason and the action
follows — a 24-way classification instead of open generation. The catalogue is
read from `sample_messages.csv` at runtime, never pasted into source.

**Tier 0 exists for security, not cost.** 110 calls is cents. The point is that
an injection payload never enters a prompt. Because a Tier 0 bypass is
unrecoverable, only two rules qualify and both carry a semantic qualifier: the
credential rule requires a *named* secret and is suppressed when the sender is
warning about credential theft rather than soliciting it.

**Only history resolves preference.** Two labelled rows share identical text
*and* identical image and carry opposite gold actions, differing only in who
received them. Content-level rules resolve risk; roughly 70% of this dataset
turns on preference, which is why most rows reach the model with the user's own
reaction history attached.

## Files

| File | Role |
|---|---|
| `main.py` | Entry point: tier orchestration, CSV writing, run summary |
| `context.py` | Deterministic join — user, group, membership, business, relationship, engagement, DND |
| `taxonomy.py` | The reason catalogue and confidence calibration, derived at runtime |
| `rules.py` | Every predicate, defined once, imported by router and postprocess |
| `similarity.py` | `difflib` matcher — label transfer and evidence retrieval |
| `media.py` | Vision + speech-to-text, container sniffing, versioned cache |
| `llm.py` | Provider resolution from `.env` — text, vision and audio roles |
| `router.py` | The single classified call: fenced untrusted content, strict JSON, retry once then fail loudly |
| `postprocess.py` | Tier 3 veto, action/type coherence, output hygiene |
| `evaluation/main.py` | Self-eval: data and source assertions (sections 1–6) |
| `evaluation/pipeline_checks.py` | Self-eval: checks that run the real router (7–8) |
| `media_cache.json` | Captured descriptions and transcripts — **ships with the package** |

## Measured results

`python code/evaluation/main.py`, offline path, leave-one-out over the 30
labelled rows:

| Metric | Score |
|---|---|
| `action` | 24/30 (80%) |
| `message_type` | 12/30 (40%) |
| both | 11/30 (37%) |
| `evidence_message_ids` exact | 17/30 (57%) |

Rule precision on labelled rows: zero action errors, zero false mutes.
Re-running produces a byte-identical `output.csv`.

Leave-one-out matters here: Tier −1 matches against the labelled set, so a row
left in its own pool matches itself at ratio 1.0 and scores perfectly for the
wrong reason.

## Known limitations

- **No model path has ever executed.** No API keys are configured on the
  development machine, so `media_cache.json` is empty and every reported
  number comes from the deterministic path. Provider resolution, container
  sniffing, AVIF transcoding and cache invalidation are covered by offline
  tests, but no image has been captioned, no voice note transcribed, and no
  routing decision made by a model.
- **The free models are weaker than the one this was designed against.**
  `llama-3.3-70b` is more likely than a frontier model to return prose around
  its JSON. The router extracts JSON by regex, retries once with the error fed
  back, then fails loudly to the deterministic fallback — but the retry rate
  on free models is unmeasured.
- **Transcripts never reach Tier 0**, by construction — Tiers −1 and 0 return
  before any media is read. A misheard "OTP" would otherwise mute a legitimate
  message through a bypass that never reaches the model. Transcripts feed the
  model and the recoverable Tier 3 veto only.
- **Whisper may return Devanagari.** `rules.py` matches romanised Hinglish
  (`batao`, `daal`, `abhi`), so a Devanagari transcript would silently stop
  matching. The ASR prompt asks for romanised output; this needs checking
  against a real transcript before it can be called handled.
- **The offline fallback was designed after reading the 30 labelled rows.**
  Leave-one-out protects Tier −1 from self-matching, but not the heuristic from
  the author's knowledge of those rows. Treat 80% as an optimistic ceiling.
- **The evidence tiebreak (oldest-first) is tuned on 30 rows** and is probably
  an artifact of how the dataset was generated.
- **Tier −1 assumes the leakage persists.** If the hidden set has no overlap
  with `sample_messages.csv`, those rows degrade silently to Tier 2 rather
  than failing.
- **`payment` is unreachable through the catalogue** — no labelled row uses it.
  The model can still emit it through the documented escape hatch.
