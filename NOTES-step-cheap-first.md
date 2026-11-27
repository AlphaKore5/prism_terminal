# STEP cheap-first: server rows and wiring

Engine module: core/step_cheap.py (entry point `ask()`). The GUI branch
feat/step-cheap-first-gui calls `step_cheap.ask(report, question,
licence_id=..., model=..., attachments=..., history=...)`; the plan it
returns is the same `{"changes", "advice"}` shape `apply_plan` /
`predicted_parts` already take.

## SERVER BRANCH MUST CARRY THIS (not done here; the server is not edited)

Without a `step.fast` row the server refuses the role (INVALID_REQUEST), also
when a model is named, so every Haiku-pick question that needs AI fails.
In license_server `app/ai_seed.py`, `_ROLES`:

    "step.fast": (("haiku-5.5",),) * 3,

and a role-map row on the live database (seeding only adds missing rows).
The model behind it must be able to read images (haiku-5.5 is flagged so).

| role | model sent by the client | max_tokens | notes |
|---|---|---|---|
| `step.fast` (new) | `anthropic/claude-haiku-5.5` (the Haiku pick); none from a caller with no pick | 1500 | strict JSON, `json_mode`, temperature 0; may carry one picture |
| `reasoner` (existing) | `anthropic/claude-sonnet-5.5` | 1500 | Sonnet pick, or the one escalation; no picture |
| `vision` (existing) | `anthropic/claude-sonnet-5.5` | 1500 | the same strong call when a picture is attached |

A named model is checked against the server's `ai_models` allow-list and gets
no fallback model. Price: bill at the named model's per-token rate. The server
should also reject a STEP call when the licence is over its STEP file cap
(below): the client check is only courtesy, a customer can edit a local file.

## Follow-ups: what is sent

Recent exchanges, newest kept first, up to HISTORY_CHARS (3000); a follow-up
that refers back, or names a hole only an older turn spoke of, adds up to
HISTORY_OLDER_CHARS (1500) of older ones, best word matches first. Each answer
is cut to 600 characters. Worst case about 4,500 characters (about 1,100
input tokens) on top of the measurements. A follow-up containing words like
"other", "that", "same" and no hole tag is not answered locally.

## Caps

STEP_AI_FILES_PER_MONTH = 300, STEP_AI_FILES_PER_DAY = 40 (config
constants at the top of step_cheap.py). A "file" is one measured STEP file
(file name + part sizes/volumes); the first answered AI question on it counts,
further questions about it that day/month are free. Local answers, plain
edits and calls that fail are never counted (if the cheap call answers and
the escalation then fails, nothing is counted although the cheap call was
billed). Haiku and Sonnet picks count the same. Stored in ~/.prism/step_ai_usage.json, keyed by a
hash of the licence id. The customer sees a plain sentence, no credits.

## Measuring the saving

Each answer appends a line to ~/.prism/step_ai_log.jsonl: path
(local / cheap / escalated / strong (Sonnet pick) / unverified / capped), calls, credits (from the
server's charged_mc), a hashed licence id, file key. No question text and no
geometry. `step_cheap.summarize_log()` gives answers, total and average
credits per path. Escalation rate must stay under about 10 percent or the
saving shrinks (escalated costs cheap + Sonnet).

## Limits to know

- Validators check against the measured report (itself measured from the
  file); applying a plan still re-measures the built copy, as before.
- The numeric-grounding check rejects any number in the text that is not a
  measured figure or part of a change. That is strict on purpose; it can
  raise the escalation rate. Tune on real logs. It applies to the Sonnet
  pick too: an answer that fails is withheld after being billed. Numbers from
  earlier answers in the chat are allowed.
