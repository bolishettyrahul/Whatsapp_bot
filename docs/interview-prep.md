# Message Notification Router — Brutal Interview Prep

Format below: **Q** = the question as a hostile interviewer asks it. **Probe** = what they are
actually testing. **Trap** = the answer that gets you cut. **Must contain** = the skeleton of a
passing answer.

Ground truth about the dataset you are defending (know these cold, they are checkable):

| Fact | Value |
|---|---|
| Rows to route (`messages.csv`) | 110 |
| Split by channel | 63 group / 30 business / 17 personal |
| Media | 87 text-only, 15 image, 8 voice (19 distinct media ids) |
| Forwarded (`forwarded_count > 0`) | 32 of 110, max 11 hops |
| Historical messages | 412, each with **exactly one** matching row in `message_events.csv` (perfect 1:1 join, no orphans either way) |
| Solved examples | 30 in `sample_messages.csv` — 9 notify / 11 digest / 10 mute |
| Businesses | 110; **26 unverified**, **27 with `domain_used_by_sender != official_domain`** |
| Users | 54, each with a `do_not_disturb_window` (many cross midnight, e.g. `22:00-07:00`) |
| Group memberships | 401 rows carrying `group_muted_by_user` |
| Notification load | 756 user-day rows in `daily_notification_summary.csv` |

---

## 1. Design Principles

**Q1.** State your design principles in one sentence each. If you say "modular" or "scalable" I will
end the interview.
- **Probe:** whether principles were chosen *before* code or reverse-engineered after.
- **Must contain:** principles that a reviewer can *falsify* by reading the code. Examples that
  survive: (a) *safety is a veto, not a score* — scam classification overrides every engagement
  signal; (b) *every decision is traceable to a row id* — no verdict without evidence or an explicit
  `none`; (c) *deterministic spine, probabilistic leaves* — hard structured signals (opt-out,
  domain mismatch, DND) are code, taste judgments are LLM; (d) *the LLM never sees raw CSVs, only a
  compiled context object*.

**Q2.** You have three actions. Why is `digest` not just "low-confidence notify"? Defend the
existence of the third class.
- **Trap:** treating action as a 1-D urgency threshold with two cutoffs.
- **Must contain:** `digest` and `mute` differ on *value*, not urgency — a legitimate delivery update
  the user opted into is high-value/low-urgency (digest); a promo they opted out of is
  zero-value regardless of urgency (mute). Confidence is orthogonal: you can be 0.95 confident
  something belongs in the digest.

**Q3.** What is the cost asymmetry between your three error types, and where in the code is that
asymmetry actually encoded?
- **Must contain:** false-mute of an urgent message is the catastrophic error (user misses a real
  emergency); false-notify is annoying and compounds into dismissal fatigue; digest↔notify confusion
  is cheap. Then point at the line: a bias term, an asymmetric threshold, or an escalation rule.
  "It's in the prompt" is a weak but survivable answer only if you can quote the prompt line.

**Q4.** Why did you pick an LLM at all? A gradient-boosted tree on these ~25 structured columns
would be faster, cheaper, and calibratable. Convince me.
- **Must contain:** 110 rows and 30 labeled samples — nowhere near enough to fit a supervised model
  without overfitting. Plus the rubric scores `reason` text and multimodal content (image posters,
  voice notes) that has no structured representation. Honest concession: *if* we had 100k labeled
  rows, the right architecture is a tree on features with the LLM demoted to a feature extractor.

**Q5.** Which principle did you actually violate under time pressure, and why was it the right
trade?
- **Probe:** self-awareness. Everyone violates one at hour 20. Naming it buys more credibility than
  claiming purity.

---

## 2. Architecture — Phase by Phase

**Q6.** Whiteboard your pipeline. For each phase: input type, output type, failure mode, and
whether it is pure.
- **Must contain:** a phase list where each stage has a *typed* boundary, e.g.
  `load → normalize/index → media extraction (OCR/ASR) → retrieval of evidence → feature/signal
  computation → LLM reasoning → post-hoc rule enforcement → schema validation → writer`.
- **Trap:** describing it as "load the CSVs and ask the LLM." That is one phase, not an architecture.

**Q7.** Why is the deterministic rule layer **after** the LLM and not before it? Or if yours is
before — why did you let rules pre-empt reasoning?
- **Must contain:** a defensible position either way. *After* = the LLM produces a rich judgment and
  rules act as a safety net that can only downgrade toward `mute` or enforce contractual constraints
  (opted-out promo can never be `notify`). *Before* = short-circuits save cost/latency on obvious
  cases but you lose the reason text quality. Best answer: **both**, with the pre-filter only
  allowed to *skip* work, and the post-layer only allowed to *override*, never to invent.

**Q8.** Which phase is the accuracy bottleneck? Prove it with a number, not intuition.
- **Must contain:** an ablation on the 30 sample rows — e.g. "removing retrieval drops action
  accuracy from X to Y; removing OCR only affects the 15 image rows." If you never ablated, say so
  and say what you would ablate first.

**Q9.** Your context object per message — how large is it in tokens, and what did you deliberately
leave out?
- **Probe:** whether you understand context is a budget, not a dump.
- **Must contain:** what got cut and why (e.g. full 412-row history → top-k retrieved; full group
  roster → only this user's membership row).

**Q10.** Where does the pipeline hold mutable state? Walk me through everything that is not a pure
function of its inputs.
- **Must contain:** caches, the media extraction store, any counter for notification budget. Then
  the follow-up you must survive: *does routing message N change the decision for message N+1?*
  If yes (fatigue budget), you have order dependence — and you must have a defined ordering
  (sort by `created_at`) or your output is nondeterministic.

---

## 3. Routing Logic

**Q11.** Give me the exact decision procedure for a message. Not the vibes — the order of checks.
- **Must contain:** an ordered cascade with explicit precedence, e.g.
  1. Safety veto (scam/phishing indicators) → `mute` + `scam`
  2. Contractual veto (`allows_promotions == 0` or `promotions_opted_out_at` set) → never `notify`
  3. Urgency + relationship evidence → `notify`
  4. Everything legitimate and non-urgent → `digest`
  5. Repetition / prior dismissal pattern → downgrade

**Q12.** A message is scam-shaped **and** the user has an active order with that business. Which
wins, and why isn't that just you hardcoding your own prejudice?
- **Must contain:** safety wins. Justification: scammers *target* users with active orders — the
  relationship is the attack surface, not the alibi. This dataset is built to catch you here:
  `business_036` sends "Delivery failed. Pay small reattempt fee at amazonpay-delivery.in and enter
  OTP" — a real-looking delivery context with an off-domain payment + OTP request.

**Q13.** Rank your signals by weight and tell me which one you would delete if forced.
- **Must contain:** a real ranking. Strong candidates for top weight: domain mismatch +
  OTP/payment-link co-occurrence; sender role (`admin`) in group; opt-out status; prior
  `message_reported` / `muted_after_message` on similar history. Deletable: raw `forwarded_count`
  alone (correlated with spam but useless without content).

**Q14.** `forwarded_count = 11`. Is that a mute? 32 of your 110 rows are forwards.
- **Trap:** "high forward count means spam, mute it." That mutes forwarded emergency notices.
- **Must contain:** forwarding is a *prior*, not a verdict; it raises the bar for `notify` but the
  content class decides. A forwarded scam is `mute`/`scam`; a forwarded society water-outage notice
  from a group the user actively reads is still `digest` at minimum.

**Q15.** How do you assign `message_type` independently of `action`? Or are they entangled?
- **Probe:** the rubric scores them separately, so entanglement costs you points.
- **Must contain:** type is a property of the *content*; action is a property of content × user.
  A `promotion` can be `digest` (opted in, relevant) or `mute` (opted out). If your code derives
  type from action, you will systematically mislabel and you should admit it.

**Q16.** Justify one `confidence` number you emitted. What does 0.78 mean operationally?
- **Trap:** "the LLM said 0.78."
- **Must contain:** either (a) a defined scale tied to signal agreement — e.g. all three of
  content/behavior/sender signals agree → 0.85–0.95; two of three → 0.7–0.85; content-only → <0.7 —
  or (b) an honest admission that it is an uncalibrated LLM self-report, plus what you'd do about it
  (bin the samples, check empirical accuracy per bin, apply a monotone recalibration map).

---

## 4. Tools / Python Functions

**Q17.** List every tool you exposed to the model, with its exact signature and return type. Why is
each one a tool instead of pre-computed context?
- **Must contain:** the distinction — a tool exists when the *model* must decide whether the lookup
  is needed. If you always call it, it is not a tool, it is a pipeline stage, and making it a tool
  wastes a round trip.

**Q18.** What happens when the model calls a tool with a `user_id` that does not exist?
- **Must contain:** structured error return (`{"error": "unknown_user", "user_id": ...}`) that the
  model can recover from, not an exception that kills the row. Follow-up: how many retries before
  you fall back, and does the error stay in the transcript (it should — otherwise the model repeats
  the same bad call).

**Q19.** Your tool returns 412 history rows. Now what?
- **Must contain:** tools must return bounded, ranked, already-summarized payloads. Any tool that
  can return unbounded rows is a context-overflow bug waiting for the worst possible input.

**Q20.** Which of your functions is pure and unit-testable, and which needs a live API? What
percentage of your logic is testable without a network call?
- **Probe:** whether the design is even testable. A pipeline where 100% of logic lives inside one
  prompt has 0% unit coverage and you cannot regression-test it.

---

## 5. ID Matching & Evidence Retrieval

**Q21.** Walk me through every join in your system and what its cardinality is.
- **Must contain:** `messages.user_id → users` (N:1); `→ group_members(group_id,user_id)` composite
  key (N:1); `→ groups` (N:1); `→ business_accounts` (N:1); `→ user_business_history(user_id,
  business_id)` composite (N:0..1, **may be missing** — first contact from a business);
  `media_id → images.image_id / voice_notes.voice_note_id` (polymorphic, discriminated by
  `media_type`); `message_history.message_id → message_events.message_id` (verified 1:1, 412/412).

**Q22.** `media_id` points into two different tables depending on `media_type`. What breaks if
`media_type` is empty but `media_id` is populated?
- **Must contain:** you either validated the invariant or you have a silent-lookup-failure bug.
  Correct handling: resolve by id prefix (`img_` / `vn_`) as a fallback and log the inconsistency.

**Q23.** How do you pick `evidence_message_ids`? "Semantically similar" is not an answer — the rubric
scores whether the ids are *relevant*.
- **Must contain:** a concrete retrieval scheme with a stated scoring function, e.g. candidate set =
  history rows for **this same user** (never another user's history — that is a leak and a wrong
  answer), filtered by same sender / same business / same group, then ranked by a blend of
  lexical or embedding similarity and *outcome informativeness* — a past message the user
  **reported** or **muted after** is far stronger evidence for a `mute` than a past message they
  merely opened.
- **Killer follow-up:** *does your evidence justify the decision, or was it retrieved before the
  decision and pasted in afterwards?* Evidence chosen post-hoc to match a verdict is fabrication.
  The defensible design retrieves first, reasons over it, and cites the subset actually used.

**Q24.** When do you emit `none`, and what fraction of your rows have it? If it is near zero, you are
padding.
- **Must contain:** a similarity floor below which you emit `none` rather than cite a weak match, and
  awareness that citing irrelevant ids is scored *worse* than `none`.

**Q25.** Two messages, same user, same business, minutes apart. Do they get the same evidence and the
same action? Should they?
- **Probe:** repetition handling. The second one is arguably `digest`→`mute` as a duplicate, and this
  only works if you process in timestamp order and remember what you already routed.

---

## 6. Personalization

**Q26.** Same water-outage notice, two users in the same society group. When do they get different
actions, and name the exact columns that make them differ.
- **Must contain:** `group_members.group_muted_by_user`, `messages_read_30d`, `replies_sent_30d`,
  `notifications_dismissed_30d`, `role`; plus `users.do_not_disturb_window` and the user's
  `daily_notification_summary` load. Naming columns is the whole point of this question.

**Q27.** The group is muted by the user. A message directly mentions them and it is urgent. What do
you do — and where is the line?
- **Must contain:** mute state is a strong prior, not an absolute. A direct mention + urgency
  overrides it; a generic broadcast does not. Then the honest bit: **how did you detect the
  mention?** If you have no mention detector, say so and say the LLM infers addressing from text
  ("Tower B folks" is a segment address, "@Priya can you" is a direct one).

**Q28.** Message arrives at 23:40, user's DND is `22:00-07:00`. Walk me through your time logic.
- **Trap:** naive `start <= t <= end` comparison — it fails for every midnight-crossing window,
  which is most of this dataset.
- **Must contain:** the wrap-around check (`start > end → t >= start or t <= end`). Then the policy
  question: does DND *suppress* a notify or *defer* it to digest? Deferring is correct — muting a
  genuine emergency because of quiet hours is the catastrophic error from Q3. State whether you
  carved out an emergency override.

**Q29.** How do you use `daily_notification_summary.csv`? If the answer is "I didn't," why did the
organizers include 756 rows of it?
- **Must contain:** notification fatigue — a user with a high dismissed/sent ratio has demonstrated
  that your notify bar is too low *for them*, so raise it. This is the single most-skipped file in
  the dataset and the interviewer knows it.

**Q30.** A user has `messages_reported_30d = 4`. Is that a cautious user or a spam magnet? Your
system's answer changes the routing — which did you assume?
- **Probe:** whether you can distinguish a signal about the *user's sensitivity* from a signal about
  their *inbox quality*. Correct move: normalize by volume received (`daily_notification_summary`)
  before treating it as a personality trait.

**Q31.** Cold start: a business with no `user_business_history` row at all. What is the default?
- **Must contain:** absence of relationship is itself a signal — an unsolicited business message from
  an account the user has never transacted with is at best `digest`, and combined with unverified
  status or domain mismatch it is `mute`. Do not default to neutral.

---

## 7. Multimodal — OCR / ASR

**Q32.** 15 image rows and 8 voice rows. Exactly how did you extract content, and what is your error
rate?
- **Must contain:** the concrete path — vision model on the image vs. an OCR engine; ASR model on the
  audio. Plus: did you *check* the transcripts by hand? With only 19 media files, manually verifying
  all of them is a couple of hours and the interviewer expects you to have done it. "I spot-checked
  N of 19" is a real answer.

**Q33.** ASR mis-transcribes a Hindi/English code-switched voice note and drops the word "hospital."
The message becomes a casual chat. What does your system do?
- **Must contain:** graceful degradation — low ASR confidence should *lower* the routing confidence
  and bias toward `digest` rather than `mute`, because muting on a bad transcript is the
  catastrophic error. If you don't propagate ASR confidence, admit it and name it as the fix.

**Q34.** An image poster with text baked in — how do you keep the model from routing on the *image
aesthetics* instead of the content?
- **Must contain:** extract text to a canonical string, then route on the same text path as any
  other message, with a `media_derived: true` flag so the reason text stays honest.

**Q35.** Scam screenshots. A forwarded screenshot of a fake bank SMS. Which signals fire?
- **Must contain:** OCR text carries the phishing markers (OTP request, off-domain link, urgency
  framing) even though the sender is a personal contact. This is why media text must join the same
  safety layer, not bypass it.

---

## 8. Safety & Scam Detection

**Q36.** Define your scam detector precisely. What are its features?
- **Must contain:** the dataset's own strongest signal — `official_domain` vs `domain_used_by_sender`
  mismatch (**27 of 110 businesses**), `domain_used_by_sender_age_days` (freshly registered lookalike
  domains), `verified == 0` (**26 businesses**), `user_reports_30d`, `account_age_days`, plus content
  markers: OTP solicitation, "pay a small fee," payment link, artificial deadline, prize/refund bait.

**Q37.** Why not just mute every unverified sender? 26 businesses are unverified.
- **Must contain:** verification is a weak feature alone — small legitimate local businesses are
  unverified. It gains power only in conjunction with domain mismatch or content markers. If you
  used it as a standalone rule you over-muted and your sample eval should show it.

**Q38.** False-positive scam call: you muted a legitimate bank alert about actual fraud on the user's
account. Real-world consequence: financial loss. How does your design prevent this from being
silent?
- **Must contain:** mute is not deletion — muted items are still in the digest/archive, and the
  `reason` field makes the decision auditable. Then the operational answer: user-facing "why was
  this muted" + a one-tap correction that feeds back into the behavioral signals.

**Q39.** An attacker reads your `reason` strings. Have you just published your detection rules?
- **Probe:** adversarial thinking. Real answer: reasons should be user-legible without being a
  rule dump ("sender used a domain that doesn't match the official one" is fine; "score 0.83 on
  feature domain_age<30" is not).

---

## 9. Caching

**Q40.** What exactly is cached, keyed by what, and what invalidates it?
- **Must contain:** a key that includes **everything the output depends on** — for media extraction:
  the file content hash (not the filename); for LLM calls: a hash of the fully rendered prompt +
  model id + temperature + prompt version. If your key omits the prompt version, editing a prompt
  silently serves stale results and your "improvement" measures nothing. This is the #1 real bug in
  hackathon caches and the interviewer is fishing for it.

**Q41.** You changed the prompt at hour 18 and accuracy didn't move. Cache bug or a genuinely
useless change? How do you tell them apart in under two minutes?
- **Must contain:** check cache hit rate on the run; run one row with the cache disabled and diff.
  Having a `--no-cache` flag is the correct pre-built answer.

**Q42.** Is your cache correct under concurrency? Two threads miss on the same key simultaneously.
- **Must contain:** benign duplicate work (both compute, last write wins) is acceptable for a pure
  function; the *real* bug is a torn write — a half-written JSON file read by another thread. Fix:
  write to a temp file and `os.replace` (atomic on POSIX), or hold a lock around the write.

**Q43.** Why cache at all — is this a cost optimization or a determinism mechanism?
- **Must contain:** both, and they conflict. Caching gives you reproducible reruns during
  development, which is what makes eval iteration meaningful with a nonzero-temperature model. Say
  that out loud; it is a better answer than "to save money."

**Q44.** Persistent (on-disk) or in-process? Defend it.
- **Must contain:** on-disk for a 24-hour hackathon where you rerun the pipeline 50 times — the
  entire value is across-process. In-process only helps within one run and is nearly pointless here.

---

## 10. Threading & Concurrency

**Q45.** Yes, they can ask this. Do you parallelize, and what is the bottleneck you are actually
attacking?
- **Must contain:** the workload is **I/O-bound** (LLM API round trips), so Python's GIL is
  irrelevant — `ThreadPoolExecutor` or `asyncio` is correct and `multiprocessing` is the wrong tool
  and a red flag. 110 rows × ~2–5s serial ≈ 4–9 minutes; at 8 workers, under a minute.

**Q46.** With 8 workers you will hit rate limits. What is your policy?
- **Must contain:** bounded concurrency (semaphore/pool size, not `for row: Thread().start()`),
  exponential backoff with jitter on 429/5xx, a retry cap, and respecting `Retry-After`. Jitter
  matters — synchronized retries from 8 threads re-collide immediately.

**Q47.** Is your output deterministic with threads? Prove it.
- **Must contain:** results must be reassembled by `message_id`, not by completion order — collect
  into a dict and write in the input order of `messages.csv`. If you append to a shared list as
  futures complete, your `output.csv` row order is nondeterministic between runs, which makes diffing
  two runs useless.

**Q48.** You said routing uses a notification-fatigue budget that updates as you go. That is shared
mutable state across threads. How is that not a race?
- **Probe:** whether you actually thought it through, or whether you have a live bug.
- **Must contain:** either the budget is computed *up front* from `daily_notification_summary` and is
  therefore read-only (clean), or it is stateful and must be per-user-serialized — partition work by
  `user_id` so all of one user's messages run on one worker in timestamp order. The second is the
  honest answer if you have order-dependent logic.

**Q49.** One worker throws. What happens to the other 109 rows?
- **Must contain:** exceptions inside a future are silent until you call `.result()`. If you never
  collect results you lose rows *silently* and ship a short `output.csv`. Per-row try/except that
  emits a fallback prediction, plus a hard assertion that `len(output) == len(messages)` before
  writing.

---

## 11. Fallbacks

**Q50.** The LLM API is down for the whole scoring run. What does your submission produce?
- **Must contain:** a rules-only degraded path that still emits 110 schema-valid rows. Anything that
  crashes and produces a partial CSV scores zero on the missing rows.

**Q51.** The model returns malformed JSON. Show me the ladder.
- **Must contain:** an actual ordered ladder: (1) strict parse; (2) extract the first balanced
  `{...}` / strip code fences; (3) one repair retry with the parse error fed back to the model;
  (4) schema-coerce known fields; (5) rules-only fallback for that row. Each rung must be marked in
  the output (a `degraded` flag) so your eval doesn't silently score fallback rows as model rows.

**Q52.** The model returns valid JSON with `action: "notify_user"` — not in your enum.
- **Must contain:** validate against the enum, never trust the string. Map near-misses, and if it
  cannot be mapped, fall back rather than writing an invalid value — an out-of-enum `action` is an
  automatic zero on that row.

**Q53.** What is your fallback's *default* action, and why is that the least-bad guess?
- **Must contain:** `digest` — it is the modal safe class (11 of 30 samples), it never causes the
  catastrophic false-mute, and it never causes a false interrupt. Confidence should be low (e.g.
  0.3–0.4) and the `reason` should honestly state it is a degraded decision.

**Q54.** How many of your 110 final rows came from a fallback path? If you don't know, why don't you?
- **Probe:** instrumentation. Not knowing means your accuracy number is contaminated by an unknown
  fraction of rule-only rows.

---

## 12. Evaluation — Metrics, Iteration, Why Those Evals

**Q55.** You have 30 labeled rows. Any number you compute on them has a confidence interval roughly
±18 points. So why should I believe your accuracy claim at all?
- **Probe:** statistical honesty. This is the question that separates people who ran an eval from
  people who understand one.
- **Must contain:** you shouldn't believe a point estimate. The 30 samples are useful as a
  **regression guard and error-analysis corpus**, not as a leaderboard. Report the confusion matrix
  and the *named failure classes*, not a headline accuracy.

**Q56.** Which metrics did you use and why each one?
- **Must contain:** at minimum — action accuracy (matches rubric); **per-class recall on `notify`**
  (the asymmetric-cost class from Q3); message_type accuracy computed *separately* from action;
  evidence precision (fraction of cited ids a human agrees are relevant); calibration (mean
  confidence vs. empirical accuracy per bin — ECE if you want to name it). Justify each by pointing
  at the rubric line it maps to.

**Q57.** Why not just macro-F1 and be done?
- **Must contain:** macro-F1 hides the cost asymmetry — it weights a false-mute of an emergency the
  same as a digest/notify mixup. You need the confusion matrix because *which* cell you're wrong in
  is the whole story.

**Q58.** Describe one full iteration loop: what you measured, what you changed, what happened.
- **Must contain:** a concrete before→after with a named failure class, e.g. "the confusion matrix
  showed promotions from opted-out businesses landing in `digest`; I added the opt-out hard rule;
  that cell went to zero and nothing else regressed." A loop with no *regression check* is not a
  loop — that's the follow-up.

**Q59.** You iterated on the same 30 rows repeatedly. That is overfitting to your dev set. What did
you do about it?
- **Trap:** denying it. You did overfit; the question is whether you know.
- **Must contain:** acknowledgment, plus mitigations — prefer changes that are *principled* (a rule
  you can justify from the schema) over changes that are *sample-fitted*; hold out a slice; sanity-
  check the action distribution on the full 110 against the sample prior (roughly balanced thirds) —
  if your 110 predictions are 70% mute, you have drifted regardless of dev accuracy.

**Q60.** Did you use an LLM-as-judge for the `reason` field? What is that judge's bias?
- **Must contain:** if yes — same-family judges favor their own generations, and judges reward
  fluency over correctness; mitigate with a rubric-anchored judge prompt and human spot-checks. If
  no — say you spot-read reasons manually, which at 110 rows is entirely feasible and more credible.

**Q61.** What is your system's single biggest remaining weakness, measured?
- **Probe:** whether the eval produced knowledge or just a number. Name a specific confusion cell.

---

## 13. Code Walkthrough — "Which File Matters Most"

> **You cannot answer this section from this repo.** `code/main.py` and `code/evaluation/main.py`
> are both empty here. Fill this in from your real submission before the interview.

**Q62.** Walk me file by file. For each: responsibility, public surface, and why it is a separate
file.
- **Must contain:** one sentence per file, no rambling. If two files can't be distinguished in one
  sentence each, they should have been one file — say that before the interviewer does.

**Q63.** Which single file is most important, and what is the second-order answer?
- **First-order:** the orchestrator/router — it encodes the decision cascade, so it is where
  correctness lives.
- **Second-order (the answer that scores):** *most important to whom?* Most important for
  **accuracy** is the prompt/decision layer; most important for **not shipping zero** is the
  writer/validator that guarantees 110 schema-valid rows; most important for **improvement velocity**
  is the eval harness, because every gain came through it. Give all three framings — interviewers
  reward the reframe.

**Q64.** Show me the riskiest 20 lines in your codebase and tell me why they are risky.
- **Probe:** whether you know your own code's weak point. Everyone has one — usually the JSON parse,
  the DND time comparison, or the media-id resolution.

**Q65.** If I deleted your prompt file and gave you 30 minutes, could you rebuild it? What's in it
that isn't obvious?
- **Probe:** whether the prompt is engineered or accreted. Know your prompt's *structure*: role,
  the action/type enums with definitions, the decision precedence, the output schema, the
  few-shot examples (which ones, and why those).

**Q66.** How much of your final logic is in Python versus in the prompt? Is that ratio deliberate?
- **Must contain:** a stance. Rules in Python are testable and deterministic; rules in the prompt are
  soft and get silently ignored on hard inputs. Anything you *must* guarantee (opt-out, enum
  validity, DND) belongs in Python. Anything requiring judgment belongs in the prompt.

---

## 14. Real-World Deployment & Edge Cases

**Q67.** This runs on a CSV. WhatsApp does 100B messages/day. What is the *first* thing that breaks?
- **Must contain:** per-message LLM inference is economically and latency-infeasible — at ~1s and
  fractions of a cent per call this is billions of dollars and blows any notification SLA (a routing
  decision must land in tens of milliseconds).
- **The production reframe:** a tiered cascade — a cheap on-device/rules layer resolves the ~95% of
  obvious traffic, a small distilled classifier handles the middle, and the LLM is reserved for the
  genuinely ambiguous tail *or* is used offline to generate training labels and per-user preference
  profiles that the fast path consumes.

**Q68.** WhatsApp is end-to-end encrypted. Your entire system reads plaintext message bodies. Is your
product even legal to build?
- **Probe:** whether you understand the constraint that defines this product space. This is the
  single hardest question in the set and most candidates have no answer.
- **Must contain:** server-side content inspection is incompatible with E2EE. The routing must run
  **on-device**, which reverses every architecture decision: no server LLM, a small quantized
  on-device model, metadata-only signals on the server side, and federated/local personalization
  that never uploads message content. The dataset is a server-side simulation of what would be an
  on-device system.

**Q69.** Your model was tuned on this month's scam patterns. It is now six months later. What has
degraded and how would you know before your users tell you?
- **Must contain:** concept drift on scam phrasing and lookalike domains; monitoring via proxy
  metrics that need no labels — user-report rate on delivered notifications, dismissal rate,
  "unmute"/correction rate, and the distribution shift of predicted classes over time. Alert on the
  distribution, not on accuracy (you have no labels in production).

**Q70.** How do you learn from a user tapping "this was important" on something you muted?
- **Must contain:** that is your label pipeline. Immediate: raise the sender/group's personal prior.
  Careful part: one correction should not swing global behavior, and an adversary who can trigger
  your feedback loop can train your router to deliver their spam.

**Q71.** Edge case: user changes timezone mid-dataset. Your DND window is a local-time string with no
timezone anywhere in `users.csv`. What did you assume?
- **Must contain:** you assumed all timestamps and DND windows are in one consistent local timezone —
  state that as an explicit documented assumption. Production needs a per-user tz and UTC storage.

**Q72.** Edge case: a group of 184 members (`group_002`, 742 messages in 30 days) — that's ~25/day.
Notifying on 10% is 2–3 interrupts a day from one group. Does your system have a per-source rate
limit?
- **Must contain:** whether you modeled *volume* at all. Group size and `messages_30d` are in
  `groups.csv` precisely so the notify bar can scale with the group's noise level.

**Q73.** Edge case: the first message ever from a brand-new contact who is a real person with a real
emergency. Zero history, zero relationship, urgent content. Your evidence-driven system has nothing
to cite.
- **Must contain:** content urgency must be able to carry a `notify` alone, with `evidence_message_ids
  = none` and a moderate confidence. If your pipeline requires evidence to justify `notify`, you fail
  this case — and it is the highest-stakes case there is.

**Q74.** Edge case: coordinated spam — 50 different unverified businesses each send one message.
Nothing repeats per-sender. Your repetition logic sees nothing.
- **Must contain:** per-sender repetition detection is blind to this; you need content-level
  clustering across senders, or the aggregate load signal from `daily_notification_summary`.

**Q75.** A regulator asks why you muted a specific message six months ago. What can you produce?
- **Must contain:** `reason` + `evidence_message_ids` + `confidence` per row is exactly this audit
  trail — that is the real product justification for the output schema, beyond scoring. Add: prompt
  version and model id per decision, which you should be logging.

---

## 15. Rapid-Fire Kill Shots

Answer each in under 30 seconds.

1. Your action distribution on the 110 rows — what is it, and does it match the sample prior?
2. What percentage of your 110 rows cite `none` as evidence?
3. Which single dataset file did you not use, and admit why.
4. Temperature setting, and why.
5. If I reran your pipeline right now, would `output.csv` be byte-identical? If not, what varies?
6. Your worst prediction among the 30 samples — which one and why did it fail?
7. Would you rather ship 5% higher action accuracy or 20% better calibration? Defend it.
8. What did you cut for time that you would build first with another 24 hours?
9. Name a decision you made that you now think was wrong.
10. If the hidden test set has 10× the rows, does anything in your code break?

---

## Pre-Interview Checklist

- [ ] Can recite the pipeline phases with typed inputs/outputs, unprompted.
- [ ] Know the confusion matrix on the 30 samples by heart — not just the accuracy.
- [ ] Know the action distribution on all 110 predictions.
- [ ] Can name the exact columns behind every personalization claim.
- [ ] Have one number ready for every "how do you know" question.
- [ ] Have one honest failure and one honest cut-for-time prepared. Rehearse them — being ready with
      a real weakness reads as senior; being caught without one reads as junior.
- [ ] Can answer Q68 (E2EE). It is the question most candidates die on.
