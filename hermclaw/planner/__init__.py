"""Gemma planner and replanner (P14 + P24, Bauplan §3.2, §15, §16).

Public entry points:

* ``Planner(chat, sessionmaker, config).create_plan(job_id, PlannerInput)`` – first plan version of a job.
* ``Replanner(chat, sessionmaker, config).replan(job_id, ReplanTrigger, PlannerInput)`` – new plan version that
  preserves completed steps.

Both return a ``PlanResult``. Invalid model output after the repair budget raises ``PlannerError``
(``PLANNER_INVALID_OUTPUT``); an exhausted replan budget raises ``ReplanLimitReached``.
"""

from hermclaw.planner.enrich import RiskPolicy, enrich_plan
from hermclaw.planner.errors import PlanConflict, PlannerError, ReplanLimitReached
from hermclaw.planner.inputs import ContextSnippet, PlannerInput, PlannerSettings
from hermclaw.planner.persist import PlanResult
from hermclaw.planner.planner import Planner, plan_json_schema
from hermclaw.planner.replan_contract import REPLAN_REASONS, ReplanContract, ReplanReason, ReplanStep, ReplanTrigger
from hermclaw.planner.replanner import Replanner, replan_json_schema

__all__ = [
    "REPLAN_REASONS",
    "ContextSnippet",
    "PlanConflict",
    "PlanResult",
    "Planner",
    "PlannerError",
    "PlannerInput",
    "PlannerSettings",
    "ReplanContract",
    "ReplanLimitReached",
    "ReplanReason",
    "ReplanStep",
    "ReplanTrigger",
    "Replanner",
    "RiskPolicy",
    "enrich_plan",
    "plan_json_schema",
    "replan_json_schema",
]
