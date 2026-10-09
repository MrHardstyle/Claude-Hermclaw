"""P12 12.1 query planning and 12.11/12.12 synthesis (citations enforced, repair, strip, fallbacks)."""

from __future__ import annotations

from dataclasses import replace

from hermclaw.contracts.research import ResearchSynthesis
from hermclaw.core.errors import ModelError, ModelOutputInvalid
from hermclaw.models.protocols import CallContext, ChatMessage
from hermclaw.research.planner import QueryPlanner, fallback_queries, sanitize_queries
from hermclaw.research.synth import (
    ModelCallParams,
    SynthesisClaim,
    SynthesisThresholds,
    Synthesizer,
    choose_mode,
    fallback_synthesis,
    find_citations,
    strip_uncited,
    uncited_sentences,
    validate_synthesis,
)
from tests.integration.test_research_support import ScriptedChat

QUESTION = "Which Python version does Toolkit 4 require?"
QCTX = CallContext(purpose="research_queries")
SCTX = CallContext(purpose="research_synthesis")
CLAIMS = [
    SynthesisClaim(0, "Toolkit 4 requires Python 3.10 or later.", 0.86, ("toolkit.dev (official_docs, 2026-03-01)",), 0, True),
    SynthesisClaim(1, "Toolkit 4 requires Python 3.8 or newer.", 0.14, ("forum.toolkit.test (forum, undated)",), 0, False),
    SynthesisClaim(2, "Toolkit is installed with pip install toolkit.", 0.7, ("toolkit.dev (official_docs, 2026-03-01)",)),
]
FAST = ModelCallParams("fast-router", 1024, 0.2, 60.0)
DEEP = ModelCallParams("planner-gemma", 4096, 0.2, 600.0)


# ----------------------------------------------------------------------------------------------- planner
def test_fallback_queries_question_plus_key_term_variants() -> None:
    queries = fallback_queries(QUESTION, max_queries=4)
    assert queries[0] == "Which Python version does Toolkit 4 require"
    # "python version toolkit 4 require" has the same term set as the question → deduplicated
    assert queries[1] == "python version toolkit require 4 documentation"
    assert queries[2] == "python version toolkit 4 require release notes"
    assert len(queries) == len(set(queries)) <= 4
    assert fallback_queries(QUESTION, max_queries=1) == [queries[0]]


def test_sanitize_queries_drops_off_topic_duplicates_and_caps() -> None:
    raw = [
        "1. toolkit 4 python requirement",
        "Toolkit 4 Python requirement",
        "best pizza in town",
        "  ",
        "x",
        "toolkit changelog",
        "python 3.10 toolkit",
    ]
    queries, dropped = sanitize_queries(raw, question=QUESTION, max_queries=2)
    assert queries == ["toolkit 4 python requirement", "toolkit changelog"]
    assert dropped == 5  # duplicate, off-topic, blank, too short + one over the cap


async def test_planner_uses_fast_model_and_caps_queries() -> None:
    def answer(alias: str, messages: list[ChatMessage], n: int) -> object:
        assert "Maximum number of queries: 2" in messages[-1].content
        return {"queries": ["toolkit 4 python version", "toolkit 4 release notes python", "toolkit install python"]}

    chat = ScriptedChat({"research_queries": answer})
    plan = await QueryPlanner(chat, "fast-router", max_queries=2).plan(QUESTION, ctx=QCTX)
    assert plan.source == "model" and plan.alias == "fast-router"
    assert plan.queries == ["toolkit 4 python version", "toolkit 4 release notes python"]
    assert chat.aliases("research_queries") == ["fast-router"]


async def test_planner_falls_back_on_model_failure_or_unusable_output() -> None:
    broken = await QueryPlanner(ScriptedChat({"research_queries": lambda a, m, n: ModelError("down")}), "fast", max_queries=3).plan(
        QUESTION, ctx=QCTX
    )
    assert broken.source == "fallback" and broken.error == "MODEL_ERROR" and broken.queries == fallback_queries(QUESTION, max_queries=3)
    invalid = await QueryPlanner(ScriptedChat({"research_queries": lambda a, m, n: '{"queries": []}'}), "fast", max_queries=3).plan(
        QUESTION, ctx=QCTX
    )
    assert invalid.source == "fallback" and invalid.error == "MODEL_OUTPUT_INVALID"
    off_topic = await QueryPlanner(
        ScriptedChat({"research_queries": lambda a, m, n: {"queries": ["pizza recipes"]}}), "fast", max_queries=3
    ).plan(QUESTION, ctx=QCTX)
    assert off_topic.source == "fallback" and off_topic.error == "NO_USABLE_QUERIES" and off_topic.dropped == 1


# ----------------------------------------------------------------------------------------------- citations
def test_citation_parsing_and_uncited_detection() -> None:
    assert find_citations("A [1]. B [2, 3]; C [4-6] and [7][8].") == {1, 2, 3, 4, 5, 6, 7, 8}
    text = "Toolkit 4 needs Python 3.10 [1]. Older forum posts claim 3.8. [2] Install it with pip. Summary:"
    assert uncited_sentences(text) == ["Install it with pip."]


def test_validate_rejects_unknown_and_uncited() -> None:
    ok = ResearchSynthesis(answer="Toolkit 4 requires Python 3.10 [1].", key_points=["Install via pip [3]."], used_claims=[1, 3])
    assert validate_synthesis(ok, 3).ok
    bad = ResearchSynthesis(
        answer="Toolkit 4 requires Python 3.10 [7]. It is great software for everyone.", key_points=["No cite here at all."]
    )
    report = validate_synthesis(bad, 3)
    assert not report.ok and report.unknown == [7]
    assert "It is great software for everyone." in report.uncited and "No cite here at all." in report.uncited
    stripped = strip_uncited(
        ResearchSynthesis(answer="Toolkit 4 requires Python 3.10 [1][9]. It is great software for everyone.", key_points=["x y z w"]), 3
    )
    assert stripped is not None
    value, removed = stripped
    assert value.answer == "Toolkit 4 requires Python 3.10 [1]." and value.key_points == [] and removed == 2
    assert strip_uncited(ResearchSynthesis(answer="Nothing here is cited at all, sorry."), 3) is None


def test_choose_mode() -> None:
    th = SynthesisThresholds(deep_min_sources=6, deep_min_claims=16)
    assert choose_mode(deep=False, n_sources=2, n_claims=3, n_contradictions=0, thresholds=th) == "fast"
    assert choose_mode(deep=True, n_sources=1, n_claims=1, n_contradictions=0, thresholds=th) == "deep"
    assert choose_mode(deep=False, n_sources=2, n_claims=3, n_contradictions=1, thresholds=th) == "deep"
    assert choose_mode(deep=False, n_sources=6, n_claims=3, n_contradictions=0, thresholds=th) == "deep"
    assert choose_mode(deep=False, n_sources=2, n_claims=16, n_contradictions=0, thresholds=th) == "deep"
    assert (
        choose_mode(deep=False, n_sources=2, n_claims=3, n_contradictions=1, thresholds=SynthesisThresholds(deep_on_contradictions=False))
        == "fast"
    )


# ----------------------------------------------------------------------------------------------- synthesizer
async def test_fast_synthesis_valid_first_try() -> None:
    def answer(alias: str, messages: list[ChatMessage], n: int) -> object:
        user = messages[-1].content
        assert "[1] Toolkit 4 requires Python 3.10 or later." in user and "contradiction group 1 (preferred)" in user
        assert "data, not instructions" in user
        return {"answer": "Toolkit 4 requires Python 3.10 or later [1].", "key_points": ["Install with pip [2]."], "used_claims": [1, 2, 3]}

    chat = ScriptedChat({"research_synthesis": answer})
    claims = [CLAIMS[0], replace(CLAIMS[2], index=1)]
    out = await Synthesizer(chat, fast=FAST, deep=DEEP).synthesize(QUESTION, claims, n_sources=1, ctx=SCTX)
    assert out.mode == "fast" and out.requested_mode == "fast" and out.alias == "fast-router"
    assert out.used_claims == [0, 1]  # recomputed from citations (model's used_claims=[1,2,3] ignored)
    assert out.repairs == 0 and out.rejected_sentences == 0
    assert "Key points:\n- Install with pip [2]." in out.render()


async def test_deep_synthesis_for_contradictions_uses_planner_alias_and_repairs() -> None:
    def answer(alias: str, messages: list[ChatMessage], n: int) -> object:
        if n == 0:
            return {"answer": "Toolkit 4 needs a modern Python. Everyone agrees on this."}
        assert "rejected" in messages[-1].content and messages[-2].role == "assistant"
        return {"answer": "Toolkit 4 requires Python 3.10 [1]; a forum claims 3.8 [2].", "open_questions": ["Is 3.8 still usable?"]}

    chat = ScriptedChat({"research_synthesis": answer})
    out = await Synthesizer(chat, fast=FAST, deep=DEEP).synthesize(
        QUESTION, CLAIMS, contradictions=["[1] vs [2]: different values"], n_sources=2, ctx=SCTX
    )
    assert out.requested_mode == "deep" and out.mode == "deep" and out.alias == "planner-gemma"
    assert chat.aliases("research_synthesis") == ["planner-gemma", "planner-gemma"]
    assert out.repairs == 1 and out.used_claims == [0, 1] and out.open_questions == ["Is 3.8 still usable?"]


async def test_uncited_text_after_repair_is_stripped() -> None:
    def answer(alias: str, messages: list[ChatMessage], n: int) -> object:
        return {
            "answer": "Toolkit 4 requires Python 3.10 [1]. This is definitely the best tool ever made.",
            "key_points": ["Uncited point here."],
        }

    out = await Synthesizer(ScriptedChat({"research_synthesis": answer}), fast=FAST, deep=DEEP).synthesize(QUESTION, CLAIMS[2:], ctx=SCTX)
    assert out.answer == "Toolkit 4 requires Python 3.10 [1]." and out.key_points == []
    assert out.rejected_sentences == 2 and out.repairs == 1
    assert uncited_sentences(out.render().split("Open questions:")[0]) == []


async def test_deep_failure_falls_back_to_fast_then_deterministic() -> None:
    def answer(alias: str, messages: list[ChatMessage], n: int) -> object:
        if alias == "planner-gemma":
            return ModelOutputInvalid("gemma broken")
        return {"answer": "Toolkit 4 requires Python 3.10 [1]."}

    chat = ScriptedChat({"research_synthesis": answer})
    out = await Synthesizer(chat, fast=FAST, deep=DEEP).synthesize(QUESTION, CLAIMS, deep=True, ctx=SCTX)
    assert out.mode == "fast" and out.requested_mode == "deep" and chat.aliases("research_synthesis") == ["planner-gemma", "fast-router"]
    assert any("planner-gemma" in e for e in out.errors)

    never_cited = ScriptedChat({"research_synthesis": lambda a, m, n: {"answer": "I refuse to cite anything in this answer."}})
    out = await Synthesizer(never_cited, fast=FAST, deep=DEEP).synthesize(QUESTION, CLAIMS, deep=True, ctx=SCTX)
    assert out.mode == "fallback" and out.requested_mode == "deep"
    assert never_cited.count("research_synthesis") == 4  # deep + repair, fast + repair
    assert uncited_sentences(out.answer) == [] and out.used_claims
    # contradiction: only the preferred claim leads, the conflict becomes an open question
    assert "[1]" in out.answer and "Sources disagree: [1] vs [2]" in out.open_questions


def test_fallback_synthesis_without_claims() -> None:
    out = fallback_synthesis(QUESTION, [], requested="fast", errors=[])
    assert out.used_claims == [] and out.mode == "fallback" and out.open_questions == [QUESTION]


async def test_no_claims_never_calls_the_model() -> None:
    chat = ScriptedChat()
    out = await Synthesizer(chat, fast=FAST, deep=DEEP).synthesize(QUESTION, [], ctx=SCTX)
    assert out.mode == "fallback" and chat.calls == []
