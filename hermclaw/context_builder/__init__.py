"""Context Builder (Bauplan §18, Phase 16): persistent state -> fresh, budgeted context per coder turn."""

from hermclaw.context_builder.budget import BudgetPlan, SectionBudgets, plan_budget, split_elastic
from hermclaw.context_builder.builder import ContextBuilder, StepBrief, TurnContextInput
from hermclaw.context_builder.config import ContextBuilderConfig
from hermclaw.context_builder.failure import first_error_line, preserve_failure, truncate_middle
from hermclaw.context_builder.history import HistoryLimits, TurnRecord, render_history, strip_reasoning
from hermclaw.context_builder.render import CorrectionItem, ToolPromptSpec
from hermclaw.context_builder.report import BuiltContext, ContextReport, DroppedItem, SectionReport, record_context_report
from hermclaw.context_builder.sections import SECTION_ORDER, SectionName
from hermclaw.context_builder.tokens import CHARS_PER_TOKEN, char_cost, estimate_tokens

__all__ = [
    "CHARS_PER_TOKEN",
    "SECTION_ORDER",
    "BudgetPlan",
    "BuiltContext",
    "ContextBuilder",
    "ContextBuilderConfig",
    "ContextReport",
    "CorrectionItem",
    "DroppedItem",
    "HistoryLimits",
    "SectionBudgets",
    "SectionName",
    "SectionReport",
    "StepBrief",
    "ToolPromptSpec",
    "TurnContextInput",
    "TurnRecord",
    "char_cost",
    "estimate_tokens",
    "first_error_line",
    "plan_budget",
    "preserve_failure",
    "record_context_report",
    "render_history",
    "split_elastic",
    "strip_reasoning",
    "truncate_middle",
]
