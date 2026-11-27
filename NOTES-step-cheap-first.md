# STEP cheap-first: server rows and wiring

Engine module: core/step_cheap.py (entry point `ask()`). Nothing is wired
into the GUI yet: StepAskWorker/StepPlanWorker (prism_gui/addons/step/
workers.py) must call `step_cheap.ask(report, question, licence_id=...)`
instead of making two `reasoner` calls; the plan it returns is the same
`{"changes", "advice"}` shape `apply_plan` / `predicted_parts` already take.

## Server role-map rows needed (the server owns role -> model)

| role | level mapping | model class | max_tokens | notes |
|---|---|---|---|---|
| `step.fast` (new) | same level as `classify` / BOQ agents (cheap tier, Haiku-class or cheaper) | cheap, strict JSON, `json_mode` honoured | 1500 | temperature 0 is sent |
| `reasoner` (existing) | unchanged (Sonnet 5.5) | escalation only | 1500 | now called at most once per question, and only after validation fails |

Price: bill `step.fast` at the cheap-tier per-token rate. Target 2 credits a
call (about 3k tokens in, 0.6k out); the deep-cost report assumes about 12
credits a file in total. The server should also reject a `step.fast` call
when the licence is over its STEP file cap (below): the client check is only
courtesy, a customer can edit a local file.

Request fields the client already sends: addon `step`, feature `step.ask`,
json_mode true, temperature 0.0, max_tokens 1500.

## Caps

STEP_AI_FILES_PER_MONTH = 300, STEP_AI_FILES_PER_DAY = 40 (config
constants at the top of step_cheap.py). A "file" is one measured STEP file
(file name + part sizes/volumes); the first AI call on it counts, further
questions about it that day/month are free. Local answers and plain
edits are never counted. Stored in ~/.prism/step_ai_usage.json, keyed by a
hash of the licence id. The customer sees a plain sentence, no credits.

## Measuring the saving

Each answer appends a line to ~/.prism/step_ai_log.jsonl: path
(local / cheap / escalated / unverified / capped), calls, credits (from the
server's charged_mc), a hashed licence id, file key. No question text and no
geometry. `step_cheap.summarize_log()` gives answers, total and average
credits per path. Escalation rate must stay under about 10 percent or the
saving shrinks (escalated costs cheap + Sonnet).

## Limits to know

- Validators check against the measured report (itself measured from the
  file); applying a plan still re-measures the built copy, as before.
- The numeric-grounding check rejects any number in the text that is not a
  measured figure or part of a change. That is strict on purpose; it can
  raise the escalation rate. Tune on real logs.
