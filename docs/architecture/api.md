# Job API: job creation and job metadata (P30, BUG-140)

`hermclaw/api/jobs.py` serves the job endpoints of Bauplan §34. Tokens carry the scopes `read`, `control` and
`admin` (`hermclaw/api/auth.py`). Errors come in one of two shapes:

* request bodies that do not match the contract get FastAPI's `422 {"detail": [...]}`;
* every other refusal is `{"error": {"code", "message", "details"}}`, with the HTTP status of the
  `HermclawError` (`hermclaw/api/app.py`).

The UI reads both shapes (`ui/src/api/client.ts` `errorFromBody`). The machine-readable contract is
`docs/contracts/openapi.json` together with `docs/contracts/schemas/JobCreate.schema.json`. Regenerate both with
`hermclaw schemas docs/contracts/schemas`; `tests/unit/test_contract_docs.py` fails when they are stale.

## 1. `POST /api/jobs` (control)

| field | meaning |
|---|---|
| `prompt` | the task, 3 to 50 000 characters |
| `title` | optional; defaults to the first line of the prompt |
| `repository` | `{name, url, base_branch}`. The API uses a repository that is already registered, or registers `url` on the fly. Registering a local `file://` URL or path needs `admin` |
| `constraints` | up to 50 lines. They become `job_inputs` and planner constraints |
| `priority` | 0 to 100, default 50 |
| `allow_network_research`, `auto_commit`, `create_merge_request` | per-job flags. The API stores them in `jobs.metadata` under the same names. Only these typed fields can set them |
| `metadata` | free-form client keys. The API stores and returns them. The runtime never trusts them. Reserved keys are refused (§2) |

The checks run in this order:

1. token and scope;
2. contract validation (unknown fields, lengths). Surrounding whitespace is stripped from strings and from metadata
   keys;
3. reserved metadata keys;
4. repository resolution.

The job row, the job inputs and the `job.created` event are written only after all of these pass, in one
transaction. A refused request therefore leaves no job, no auto-registered repository and no event behind.

## 2. Reserved job metadata keys

`jobs.metadata` also holds state that the runtime and the API write themselves and later trust. Before BUG-140 was
fixed, `create_job` stored `{**body.metadata, …}` without any filter. A principal with only the `control` scope could
then plant, for example:

* `ssh_approvals`: pre-approve SSH mutations, which otherwise needs the `admin` scope;
* `triage`: skip the fast router and steer the planner. A non-dict value crashed the job;
* `replan_requested`: a falsy value stopped dispatch for good and wedged the job.

The registry `hermclaw/core/job_metadata.py` now declares every such key once. Every writer uses its constant, and
`POST /api/jobs` refuses `metadata` that contains one of them.

| key | written by | trusted by |
|---|---|---|
| `ssh_approvals` | `sshtools/approvals.py` `grant_approval`/`revoke_approval`/`consume_approval` (`POST`/`DELETE /api/jobs/{id}/ssh-approvals`, admin) | `consume_approval`: authorises one mutating `ssh_command`/`ssh_upload` |
| `ssh_approvals_consumed` | `consume_approval` | `sshtools/tools.py` `_approver` (`approved_by` in `command_runs` and the `ssh.command` event), `GET …/ssh-approvals` |
| `triage` | `runtime/driver.py` (fast router result) | `driver._triage` (router skipped if present), planner constraint `needs_research`, final report |
| `replan_requested` | `POST /api/jobs/{id}/replan` | scheduler: dispatch stops while the key exists; forces a replan outside the replan budget |
| `replan_reason` | scheduler (start of a replan) | scheduler restart recovery of `replanning` jobs |
| `retry_requested` | `POST /api/jobs/{id}/retry` | none yet (marker) |
| `recovered_prepare_from` | scheduler startup recovery | none yet (marker) |
| `planner_model_alias`, `last_replan` | planner / replanner | none (provenance shown in the API and UI) |
| `planner_model_fallback`, `planner_fallback_alias` | planner `record_fallback` | none ("no silent model switch" provenance) |
| `allow_network_research`, `auto_commit`, `create_merge_request` | `POST /api/jobs`, from the typed fields | UI display |
| `memory_learned_at` | `MemoryLearner` (`hermclaw/memory/learner.py`, D-019) | learner: a finished job carrying it is not read again (BUG-308: reserved, so a client cannot keep its job out of learning) |
| `tournament` | operator CLI `hermclaw coder-tournament` only (`cli._create_job`) | driver: adopts the prescribed plan, no triage, no push; scheduler: replan limit; implement handler: coder alias, no review (`hermclaw/tournament/settings.py`) |

Matching rules:

* Keys are compared after Unicode NFKC normalisation and case folding, with whitespace and invisible format
  characters (such as U+200B) removed. `" SSH_Approvals"`, `"Triage"` and `"ssh_approvals\u200b"` are all refused.
* Keys that start with `_` or `hermclaw.` are reserved for future runtime keys. A runtime key added under one of
  these prefixes can never have been planted by a client, even on rows created before the key existed.
* Only top-level keys are checked. The runtime reads top-level keys only, so a nested `{"ticket": {"triage": …}}` is
  plain client data.
* Every other key remains client-settable. One example is `sandbox_image`: the runtime reads it only from
  *repository* metadata, which only the operator sets (`hermclaw repo-add --sandbox-image`) and which is checked
  against `policies.sandbox` (`runtime/images.py`). A job-level `sandbox_image` has no effect.

A refusal looks like this:

```json
HTTP 422
{"error": {"code": "JOB_METADATA_RESERVED_KEY",
           "message": "metadata contains runtime-reserved key(s): 'ssh_approvals'",
           "details": {"field": "metadata", "reserved_keys": ["ssh_approvals"], "reserved_prefixes": ["_", "hermclaw."]}}}
```

`details.reserved_keys` lists every offending key in the spelling the API received, after the contract stripped
the surrounding whitespace.

The rule applies to the `admin` scope as well. An SSH approval is granted only through the audited endpoint
`POST /api/jobs/{id}/ssh-approvals`, which writes an event and records `approved_by`. It is never granted through job
metadata.

### Adding a runtime key

1. Declare the constant in `hermclaw/core/job_metadata.py` and add it to `RESERVED_KEYS` with its owner. Then run
   `hermclaw schemas docs/contracts/schemas`: the `JobCreate.metadata` description lists the keys.
2. Write and read the key only through that constant.

`tests/unit/test_job_metadata.py` scans `hermclaw/` for job metadata writes and reads and fails when one of the
following is true:

* a written or read key is not reserved;
* a key is a string literal and not a registry constant;
* a registry entry has no writer.

The scan covers these write and read forms:

* `job.metadata_ = {…}`;
* `Job(metadata_={…})`;
* `merge_job_metadata`;
* the driver's `_set_meta`;
* the approvals' `metadata || patch`;
* `job.metadata_.get`/`[...]`/`has_key`/`K in …`, also on `(job.metadata_ or {})`, `dict(…)` and
  `Job.metadata_.has_key` in a query;
* a local name the function binds to a job's metadata, and then `meta.get(K)`/`meta[K]`/`K in meta`/
  `_list(meta, K)`. The name is bound by `meta = …select(Job.metadata_)…` (for example
  `(await session.scalar(select(Job.metadata_)…)) or {}`, or `.first()` and then `meta[0]`), by
  `meta = await _locked_metadata(…)`, or by an alias such as `meta = dict(job.metadata_ or {})`. This is how the SSH
  approval readers read (`sshtools/approvals.py`, `sshtools/tools.py` `_approver`, `api/approvals.py`).

Names are tracked per function. A name taken over from an enclosing function or passed as a parameter is not
tracked, and neither is a loop variable over result rows. Read the key in the function that loads the metadata.

## 3. Reads

`GET /api/jobs` and `GET /api/jobs/{id}` (scope `read`) return the full `metadata`, including the runtime keys.
`GET /api/jobs/{id}/ssh-approvals` (scope `read`) lists pending, approved and consumed approvals.

## Tests

* `tests/unit/test_job_metadata.py`: the registry, spelling variants and prefixes, the error format, the
  `JobCreate` schema description, and the guard scan (with sanity checks that it finds the known writers and readers,
  and the read forms above on a sample).
* `tests/integration/test_api_job_metadata.py`: every reserved key and spelling variant is refused with 422, and the
  test checks that no job, job input, repository or event was written. The check runs before repository
  resolution: a local URL with a control token, or an unknown repository name, still gets
  `JOB_METADATA_RESERVED_KEY`. Free-form keys are stored, and the typed fields win. A control token cannot pre-approve an SSH mutation; approving stays admin-only.
* `tests/integration/test_api.py`, `test_api_repository_registration.py`: lifecycle, scopes, repository
  registration.
