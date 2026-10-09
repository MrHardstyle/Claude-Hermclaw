"""Stagnation detection (P20, Bauplan §20): fingerprints, detector, escalation ladder and persistence.

The coder-loop hook lives in :mod:`hermclaw.stagnation.monitor` (imported explicitly to keep this package free of a
dependency on the coder package).
"""

from hermclaw.stagnation.actions import (
    DirectiveKind,
    EscalationDecision,
    LadderContext,
    Recommendation,
    ReplanHint,
    StagnationCause,
    choose_recommendation,
    classify_cause,
    decide,
    replan_hint,
)
from hermclaw.stagnation.detector import (
    DetectorTuning,
    Observation,
    RepeatedSignal,
    SignalKind,
    StagnationDetector,
    StagnationLevel,
    StagnationVerdict,
    level_for,
)
from hermclaw.stagnation.fingerprints import (
    ErrorSignature,
    action_fingerprint,
    changed_files_fingerprint,
    decision_label,
    diff_hash,
    error_signature,
    extract_failing_tests,
    failing_tests_fingerprint,
    normalise_text,
    tool_sequence,
)
from hermclaw.stagnation.persistence import load_detector, load_state, prior_escalations, record_events, save_state

__all__ = [
    "DetectorTuning",
    "DirectiveKind",
    "ErrorSignature",
    "EscalationDecision",
    "LadderContext",
    "Observation",
    "Recommendation",
    "RepeatedSignal",
    "ReplanHint",
    "SignalKind",
    "StagnationCause",
    "StagnationDetector",
    "StagnationLevel",
    "StagnationVerdict",
    "action_fingerprint",
    "changed_files_fingerprint",
    "choose_recommendation",
    "classify_cause",
    "decide",
    "decision_label",
    "diff_hash",
    "error_signature",
    "extract_failing_tests",
    "failing_tests_fingerprint",
    "level_for",
    "load_detector",
    "load_state",
    "normalise_text",
    "prior_escalations",
    "record_events",
    "replan_hint",
    "save_state",
    "tool_sequence",
]
