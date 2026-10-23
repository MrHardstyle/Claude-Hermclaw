# Live job fixtures

Starting points of the live job battery (`config/acceptance-jobs.example.yaml`). `scripts/ops/acceptance-fixtures.sh`
pushes each directory as the branch `acceptance/<name>` of the test repository. The fixtures are deliberately plain
(standard library, built-in test runners) and contain no hints for the runtime; `service` has a known bug for J6.

`form` (contact form with faulty validation, `node --test`) and `legacy` (inherited CSV ledger tool with several
bugs, `unittest`) are the starting points of K2 and M2 in the test cycle battery (`config/acceptance-jobs-cycle.example.yaml`).

`tasks-v1` is the task manager (`tasks_app`, SQLite, JSON API, page, pytest) that the difficulty ladder delivered for A8
(run #2 on 0.1.0rc78, 23/23 hidden checks), kept as it was delivered: the inherited code of ladder level A10.
