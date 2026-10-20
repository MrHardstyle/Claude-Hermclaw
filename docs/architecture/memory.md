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
| Coder | tool `recall(query, k)` (read-only, offered only when the knowledge store exists); scope `repo:<key>` of the job |
| Planner, replanner | `PlannerInput.knowledge`: the `planner_entries` active entries closest to the job text, as input key `knowledge` (3 % of the prompt budget); the system prompt says they are hints and the job and repository decide |
| Operator | page "Wissen" (`/api/memory`) |

## API

| Route | Scope | Purpose |
|---|---|---|
| `GET /api/memory?kind=&status=&q=&limit=` | read | list (newest change first), text filter on title/content |
| `GET /api/memory/search?q=&k=` | read | what a recall would get (active only, not counted) |
| `GET /api/memory/{id}` | read | one entry |
| `POST /api/memory` | control | new entry by the operator (`source = operator`, active by default) |
| `PATCH /api/memory/{id}` | control | change kind, title, text, status, scope, tags, expiry; a new text is embedded again |

The vector never leaves the server (`embedded: true/false` instead).

## Policy

`policies.memory` (`config/policies.example.yaml`): `enabled`, `recall_k`, `planner_entries`, `coder_entries`,
`min_score`, `max_entry_chars`, `learn`, `promote_after`.

## Guard rails

- Only `active` entries reach a model. An operator entry is active at once; a system entry starts as a candidate.
- A failure caused by a tool (the model had the right data and the tool still refused, BUG-301) becomes a `tool_bug`
  entry and a bug, never a lesson about the model.
- Entries are hints. The coder's system contract and the planner prompt keep the current files, outputs and the job
  text above them.
- No reasoning is stored: entries hold outcomes, causes and what helped.
