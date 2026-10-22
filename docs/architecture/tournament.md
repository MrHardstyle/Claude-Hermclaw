# Coder tournament (`hermclaw coder-tournament`, D-018)

Compares local coder models on the model host under identical conditions. Only the coder is measured: the planner,
triage, the heavy review and replanning are taken out of the loop, so a result says something about the coder model
and nothing about the planner or the reviewer.

## Modules

| Module | Purpose |
|---|---|
| `hermclaw/tournament/spec.py` | YAML specification (`TournamentSpec`, `TaskSpec`, `HiddenCheck`), weights `WEIGHTS` |
| `hermclaw/tournament/settings.py` | `TournamentSettings` in `jobs.metadata.tournament`; `tournament_of(metadata)` |
| `hermclaw/tournament/runner.py` | `TournamentRunner`: creates one job per model × task × repetition, waits, grades, records |
| `hermclaw/tournament/scoring.py` | `RunRecord`, `score_runs`, `summarise`, `render_markdown` |
| `tests/live/tournament/` | Shipped specification `coder-tournament.yaml` and the hidden checks `hidden/<task>/` |

## A tournament job

The runner creates an ordinary job whose reserved metadata entry `tournament` holds
`TournamentSettings(plan, coder_alias, review=False, replans=0, task, run)`. The runtime reacts to it in four places:

1. **Driver, prepare:** no triage and no planner model. `Planner.adopt_plan` takes the prescribed plan through the same
   schema, semantic and enrichment checks as a model plan (`plan_versions.source = prescribed`). An invalid plan fails
   the job like a planner failure, so every plan of a specification is checked once before the tournament.
2. **Implement handler:** the coder and its context budget use `coder_alias` (a profile of role `coder`), the lease is
   taken for that alias, and with `review=False` no heavy review runs; stagnation that would ask the review for advice
   becomes a correction instead. The requirement-coverage check (D-024) runs on `coder_alias` as well: it measures
   the same model, and its call and correction count in `model_calls` and `corrections`.
3. **Scheduler:** the replan limit is `min(policy.max_replans_per_job, tournament.replans)` – 0 by default, so a step
   that exhausts its attempts ends the job.
4. **Driver, finalize:** no push and no merge request. The result stays in the job workspace for grading.

## Grading

After the job ends the runner copies the workspace (`changed_files`: diff against the base branch plus untracked files),
adds the hidden checks as `_hidden/` and the untouched base tree as `_hidden/_base/`, and runs the hidden command in the
sandbox image named by the task (remote executor on `.222`, the copy is removed afterwards). The candidate never sees the
hidden checks; they test the job text, not the prescribed plan.

## Scoring (weights 50/15/10/10/10/5)

| Part | Points |
|---|---|
| correct | share of passed hidden checks × 50 |
| first_try | 15 when the job succeeded without a correction and every hidden check passed |
| effort | 10 × best model-call count of a correct run of the task / own count – correct runs only |
| duration | 10 × best duration of a correct run of the task / own duration – correct runs only |
| changes | 10, minus 25 % per changed file outside `expected_changes` |
| completed | 5 when the job status is `succeeded` |

A change to a `protected` path, a failed hidden check whose name contains `security`, or an **incomplete program**
disqualifies the run (0 points). A program is incomplete when at least half of the hidden checks erred in setup – they
could not even exercise it (the server did not start, the module did not import); a running program with wrong results
is graded by the share of passed checks (BUG-304). `Grade.errors`/`RunRecord.hidden_errors` hold that count; records
written before it existed get it from the stored grading output (`grading/<model>-<task>-<rep>-<job>.txt`).
Runs that ended on an infrastructure error (engine, GitLab, model host unreachable, grading could not run) are retried
up to twice and never scored; the report lists them separately. Hidden checks the sandbox stopped at their time limit did
run the delivered program, which hung: they are graded from the pytest progress line plus one failure for the hanging
check, never repeated (BUG-316). Checks that run the candidate's own tests wait at most 300 s, below the 600 s limit
of the whole grading run.

## Operation

```
sudo -u hermclaw hermclaw coder-tournament /var/lib/hermclaw/tournament/coder-tournament.yaml \
     --task A1 --task A3 --reps 1 --out /var/lib/hermclaw/tournament/prelim
```

`--model` and `--task` narrow the run. `runs.jsonl` in the output directory makes a run resumable; `report.md` and
`report.json` are rewritten after every job. A job that exceeds `limit_minutes` is cancelled like a cancel from the API.

## System runs and the difficulty ladder (D-020)

`--model system` runs a task as any job: no tournament metadata, so the planner plans (a task's `plan` is ignored, a
task may have none), the configured coder profile codes, the review and replans work as in production. Only the grading
is the tournament's: the same hidden checks, `runs.jsonl`, `report.md`.

`--ladder` climbs the tasks in the order of `--task` (easiest first) for one model: each level runs once; a fully
correct run (no knock-out) climbs to the next level, anything else stops the climb. The next call runs the open level
again as a new repetition – after the fix of what made it fail – and climbs on when it succeeds. `ladder.md` shows the
levels climbed, the open level and the rest:

```
sudo -u hermclaw hermclaw coder-tournament /var/lib/hermclaw/tournament/coder-tournament.yaml --model system --ladder \
     --task A1 --task A6 --task A5 --task A2 --task A3 --task A4 --task A7 --task A9 --task A8 \
     --out /var/lib/hermclaw/tournament/ladder
```

The order follows the rc62 tournament (share of fully correct runs over all models, then duration). Harder levels
are added as new tasks with hidden checks at the end of the order. Ladder jobs use the model host like tournament
jobs: never at the same time as a tournament or a GPU benchmark.
