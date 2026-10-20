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
   becomes a correction instead.
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

A change to a `protected` path or a failed hidden check whose name contains `security` disqualifies the run (0 points).
Runs that ended on an infrastructure error (engine, GitLab, model host unreachable, grading could not run) are retried
up to twice and never scored; the report lists them separately.

## Operation

```
sudo -u hermclaw hermclaw coder-tournament /var/lib/hermclaw/tournament/coder-tournament.yaml \
     --task A1 --task A3 --reps 1 --out /var/lib/hermclaw/tournament/prelim
```

`--model` and `--task` narrow the run. `runs.jsonl` in the output directory makes a run resumable; `report.md` and
`report.json` are rewritten after every job. A job that exceeds `limit_minutes` is cancelled like a cancel from the API.
