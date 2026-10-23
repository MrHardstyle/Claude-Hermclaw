# Knowledge (`hermclaw/memory`, D-019)

Hermclaw keeps what it learns: lessons from earlier jobs, research findings, incidents, suspected tool bugs and the
conventions of a repository. Planner and coder look entries up; the operator reads, writes, corrects and retires them
on the "Wissen" page of the control room. The models stay the same, the system around them learns.

## Entries

`memory_entries` (migration 0003):

| Field | Meaning |
|---|---|
| `kind` | `lesson`, `research`, `incident`, `tool_bug`, `convention`, `note` |
| `title`, `content` | short Markdown text; a model sees `[kind] title` and the text, clipped to `policies.memory.max_entry_chars`, secrets masked |
| `status` | `candidate` (waits for evidence or the operator), `active` (reaches models), `retired` (kept, never used) |
| `source` | `system`, `operator`, `research` |
| `scope` | `NULL` = everywhere, `repo:<repository key>`, `model:<alias>` |
| `signature` | normalised failure pattern of an incident; one entry per signature |
| `occurrences`, `evidence.jobs` | jobs that showed the incident (each job counted once) |
| `recalls` | how often a model got the entry |
| `job_id`, `evidence` | origin and examples |
| `embedding`, `embedding_model` | vector of `title + content`; vectors of another model are not compared |
| `expires_at` | optional end of validity (research facts age) |

Nothing is deleted through the API: a wrong entry is retired and stays visible.

## Search

`MemoryStore.search(query, k, scopes, kinds, statuses=("active",))`:

1. Exact cosine KNN over the entries embedded by the current embedding model (the knowledge is small; no HNSW tuning
   needed), similarity at least `policies.memory.min_score`.
2. Entries without such a vector (embedding model was unavailable, or another model) are matched by the share of the
   query's words they contain (at least a third), so the knowledge stays usable without the embedding model.
3. Merged, nearest first, `k` at most; each returned entry counts one recall (the page's own search does not count).

`scopes` limits scoped entries: an entry with a scope is only found when its scope is asked for; global entries always.

## Who reads it

| Reader | How |
|---|---|
| Coder | from the first turn, in `STEP GOAL`: every active lesson and convention at least `coder_start_min_score` (0.5) similar to the step goal and the job text, nearest first, `coder_start_entries` (5) at most (BUG-325: the planner had the PowerShell JSON convention, the coder never saw it; BUG-328: five PowerShell entries scored 0.52–0.63 for A9, unrelated ones at most 0.43, and a fixed two left out the two that mattered); tool `recall(query, k)` (read-only, offered only when the knowledge store exists); scope `repo:<key>` of the job. A refused tool call brings the knowledge about it along, once per tool and error code in an attempt: the active entry with the signature `tool:<tool>:<code>`, then the closest lessons, `coder_entries` at most. A command or test run that failed (`COMMAND_FAILED`, `TESTS_FAILED`, `TEST_ERROR`) is looked up by its key error lines (`stagnation.fingerprints.key_error_lines`); the closest active lessons, conventions and research findings come with its output, each entry once per attempt, appended after the output was clipped. Such a match needs `min_score`, or up to 0.1 below it an identifier the failure and the entry share (`Get-Service`, `CommandNotFoundException`, `NOTES_API_TOKEN`; common exception names do not count): measured with embeddinggemma, the Pester failure scored 0.50 against its lesson while unrelated failures reached 0.54 |
| Planner, replanner | `PlannerInput.knowledge`: the `planner_entries` active entries closest to the job text, as input key `knowledge` (3 % of the prompt budget); the system prompt says they are hints and the job and repository decide |
| Operator | page "Wissen" (`ui/src/pages/Wissen.tsx`, `/api/memory`): filters, edit form, activate/retire, recall probe |

## Learning after jobs (`hermclaw/memory/learner.py`)

The scheduler process runs `MemoryLearner` next to the workspace janitor: every `learn_interval_seconds` it reads up to
`learn_batch` finished jobs (`succeeded`/`failed`, not older than `learn_since_days`) that carry no
`metadata.memory_learned_at` yet, and stamps them afterwards (a JSON merge, no row version).

| Source | Entry | Signature |
|---|---|---|
| a tool refused with one error code at least `min_repeats` times in the job (a command or test that ran and failed – `errors.RUN_FAILED` – and sandbox errors are not refusals, BUG-306) | `incident`: how often, models, typical message, **what helped** (the calls up to the next success of the same tool on the same file in that attempt, e.g. `read_file → replace_text`) or that nothing did | `tool:<tool>:<code>` |
| `stagnation.detected` with directive `diagnose` or `stop` (one per stall kind and job) | `incident`: cause (`edit_mechanics`, `loop`, `knowledge`, …), step, model, turn, reasons, repeated signal, how the step ended | `stall:<cause>:<tool or signal>:<code>` |
| failed job | `incident` with the job's error | `job:<error code>` |
| completed research run | `research` (source `research`, active, expires after `research_days`, scope `repo:<name>` when the job has a repository – a question about one repository is no knowledge for another, BUG-307): question, synthesis, claims with confidence ≥ 0.6, sources; the same question updates the entry | `research:<hash of the question>` |

An incident is counted once per job (`occurrences`, `evidence.jobs`, the last five examples). Its text is rewritten from
the latest examples while it is a system entry; at `promote_after` jobs **and** with something that helped in at least
half of the recent cases it becomes active. A refusal that at least two models could not get past in `promote_after`
jobs, with at least half of the recent cases unresolved, is a **tool-bug suspicion**: a `tool_bug` candidate and one
open bug record `AUTO-<hash>` (component `tools`, P3) for the operator. A guard's refusal (`COMMAND_REPEATED`,
`READ_REPEATED`, `NO_CHANGE`, `CHECK_STILL_FAILING`, policy refusals) is never a suspicion; a scope verdict on a command
is. The model of an attempt comes from `step_attempts.model_alias` (set by the coder handler), for older attempts from
their coder turns in `model_invocations`. Nothing in a tool is changed by itself.

Dry run (2026-10-21, copy of the production database, 200 finished jobs): 192 incidents, 205 stall examples, 13 research
entries, 8 active incidents and exactly two suspicions – `replace_text:TEXT_NOT_FOUND` (BUG-301) and
`run_test:COMMAND_SCOPE_VIOLATION` (Pester report, BUG-305), both real tool bugs found by hand before.

### Lessons from fixes (`hermclaw/memory/distill.py`, D-025)

After the incident pass, the learner reads finished **and cancelled** jobs (not older than `lessons_since_days`) without
`metadata.memory_lessons_at`. A job's fixes are, corrections first, at most `lessons_per_job`:

- a correction attempt that completed (it passed the checks) with at least one item a fix can teach from – a failing
  test, start, import, syntax, compile or command check, a `blocker`/`major` review finding, a coverage gap or a stall
  (not lint, secrets, diff, scope or the turn budget, BUG-323): those items are the failure, plus the first red run of
  the attempt; its successful edits are the change;
- inside any other attempt, a red `run_test`/`run_command` (`errors.RUN_FAILED`, not a linter or formatter) followed
  by successful edits and the same command green again.

Each fix goes to one structured call of the coder profile (`lesson_alias` overrides it), purpose `lesson`, with the
step goal, the failure and the edits (redacted, clipped). The model answers `{general, title, mistake, fix,
applies_to}` and sets `general` false for a requirement of this task, a typo, a wrong expected value or a lint fix. A
general lesson is searched among lessons and conventions (also candidates and retired ones) at `lesson_similarity`
(0.72: the same lesson in other words scored 0.76–0.83 with embeddinggemma, different ones at most 0.58).
A match counts the job (`note_job`), and a system candidate seen in `promote_after` jobs becomes active. A retired
match stays retired. Without a match the lesson is a new candidate (`source = system`, tag `learned`, signature
`lesson:<hash of the title>`). The calls hold the model lease (`lesson_wait_seconds`). While the GPU is busy the pass
ends and the job waits for the next pass. The tournament does not count purpose `lesson` as a call of the job.

The operator's word wins: an entry whose title or text was edited on the page becomes `source = operator` and is not
rewritten, a retired entry keeps counting but stays retired.

## API

| Route | Scope | Purpose |
|---|---|---|
| `GET /api/memory?kind=&status=&q=&limit=` | read | list (newest change first), text filter on title/content |
| `GET /api/memory/search?q=&k=` | read | what a recall would get (active only, not counted) |
| `GET /api/memory/{id}` | read | one entry |
| `POST /api/memory` | control | new entry by the operator (`source = operator`, active by default) |
| `PATCH /api/memory/{id}` | control | change kind, title, text, status, scope, tags, expiry; a new text is embedded again |

The vector never leaves the server (`embedded: true/false` instead).

`hermclaw memory-import FILE` adds operator entries from a YAML list (`kind`, `title`, `content`, optional `scope`,
`tags`, `status`; active by default) without an API token; a title that exists is skipped. The repository keeps such
lists under `config/knowledge/` (first: `rc62-tournament.yaml`, four lessons of the coder tournament, each naming the
run it comes from; the Pester stub was checked in the PowerShell sandbox image).

## Policy

`policies.memory` (`config/policies.example.yaml`): `enabled`, `recall_k`, `planner_entries`, `coder_entries`,
`min_score`, `max_entry_chars`, `learn`, `promote_after`, `learn_interval_seconds`, `learn_batch`, `learn_since_days`,
`min_repeats`, `research_days`, `lessons`, `lessons_per_job`, `lesson_alias`, `lesson_similarity`,
`lessons_since_days`, `lesson_wait_seconds` (D-025).

## Guard rails

- Only `active` entries reach a model. An operator entry is active at once; a system entry starts as a candidate.
- A failure caused by a tool (the model had the right data and the tool still refused, BUG-301) becomes a `tool_bug`
  entry and a bug, never a lesson about the model.
- Entries are hints. The coder's system contract and the planner prompt keep the current files, outputs and the job
  text above them.
- No reasoning is stored: entries hold outcomes, causes and what helped.
