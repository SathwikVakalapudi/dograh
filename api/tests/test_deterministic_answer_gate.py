"""Tests for the pre-LLM deterministic answer fast path (Phase 1: party answers).

The gate is an accelerator, so most of these assert the *negative*: that it
declines and hands the turn back to the LLM. The single most important test is
`test_party_answer_on_unlisted_caste_node_defers_to_llm` — a party parser must
never be able to accept a party name as a caste.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pipecat.frames.frames import LLMContextFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection

from api.services.pipecat.deterministic_answer_gate import (
    DeterministicAnswerGate,
    parse_node_allowlist,
)
from api.services.workflow.answer_parsers import parse_party

# ─── Party parser ───────────────────────────────────────────────


class TestParseParty:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("कांग्रेस", "INC"),
            ("कांग्रेस।", "INC"),  # Sarvam ends utterances with a danda
            ("भाजपा॥", "BJP"),  # double danda
            ("मोदी जी।", "BJP"),
            ("Congress", "INC"),
            ("भाजपा", "BJP"),
            ("बीजेपी को", "BJP"),
            ("नरेंद्र मोदी", "BJP"),  # leader name resolves to party
            ("जयराम", "BJP"),
            ("नोटा", "Other"),
            ("किसी को नहीं", "Other"),
            ("निर्दलीय", "Other"),
        ],
    )
    def test_unambiguous_answers_resolve(self, text, expected):
        assert parse_party(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "हाँ",  # valid word, not a party
            "पता नहीं",
            "मुझे नहीं मालूम",
            "ब्राह्मण",  # a caste, not a party
        ],
    )
    def test_non_party_answers_defer(self, text):
        assert parse_party(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "भाजपा नहीं, कांग्रेस",  # correction — only the LLM can resolve intent
            "कांग्रेस या भाजपा",  # listing options
            "मोदी और राहुल",  # two leaders, two parties
        ],
    )
    def test_ambiguous_multi_party_answers_defer(self, text):
        """More than one party matched means we must not guess."""
        assert parse_party(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "सुख को।",       # what Sarvam actually returned on run 18
            "सुख को",
            "सुखु",
            "सुक्खु",
        ],
    )
    def test_stt_variants_of_the_himachal_cm_resolve_to_inc(self, text):
        """Sarvam splits or shortens सुक्खू, and the split form matched nothing.

        Run 18: the caller answered "सुख को।" -- the sitting Congress chief
        minister -- and it was recorded as "No answer". The canonical spelling
        is already in the keyword list; only these transcriptions are not.
        """
        assert parse_party(text) == "INC"

    @pytest.mark.parametrize(
        "text",
        [
            "सुख मिला",      # "found happiness" -- सुख is an everyday word
            "बहुत सुख है",
        ],
    )
    def test_bare_sukh_is_not_a_party(self, text):
        """सुख alone means happiness, so it must not be a keyword on its own.

        This is the risk the fix has to avoid: widening the CM's name far
        enough to swallow an ordinary Hindi word would make the fast path
        advance the survey on something that is not an answer at all.
        """
        assert parse_party(text) is None

    def test_matches_whole_words_only(self):
        """Substring matching would fire on 'हाथ' inside a longer word."""
        assert parse_party("हाथ") == "INC"
        assert parse_party("हाथी") is None


# ─── Allowlist parsing ──────────────────────────────────────────


class TestAllowlist:
    def test_parses_pairs(self):
        assert parse_node_allowlist("Q4 Vote Now:party,Q5 Vote 2022:party") == {
            "Q4 Vote Now": "party",
            "Q5 Vote 2022": "party",
        }

    @pytest.mark.parametrize("raw", [None, "", "   ", "no-colon-entry"])
    def test_empty_or_malformed_yields_nothing(self, raw):
        assert parse_node_allowlist(raw) == {}

    def test_unknown_parser_kind_is_dropped(self):
        """A typo must disable that entry, never fall back to some default."""
        assert parse_node_allowlist("Q7 Caste:caste,Q4 Vote Now:party") == {
            "Q4 Vote Now": "party"
        }


# ─── Gate behaviour ─────────────────────────────────────────────


def _edge(name: str, target: str):
    return SimpleNamespace(
        target=target,
        transition_speech=None,
        data=SimpleNamespace(
            transition_speech_type=None, transition_speech_recording_id=None
        ),
        get_function_name=lambda: name,
    )


def _node(name: str, edges, is_end: bool = False):
    return SimpleNamespace(name=name, is_end=is_end, out_edges=edges)


def _frame(user_text: str) -> LLMContextFrame:
    context = SimpleNamespace(
        messages=[
            {"role": "assistant", "content": "किस पार्टी को वोट देंगे?"},
            {"role": "user", "content": user_text},
        ]
    )
    return LLMContextFrame(context=context)


def _gate(node, allowlist=None):
    engine = Mock()
    engine._current_node = node
    engine._user_is_speaking = False
    engine._destination_has_recorded_opening = Mock(return_value=True)
    engine.execute_transition = AsyncMock(return_value="greeting")
    gate = DeterministicAnswerGate(
        engine=engine,
        allowlist={"Q4 Vote Now": "party"} if allowlist is None else allowlist,
    )
    pushed = []

    async def _capture(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    gate.push_frame = _capture
    return gate, engine, pushed


async def _run(gate, frame):
    """Exercise the real process_frame, not a re-implementation."""
    await gate.process_frame(frame, FrameDirection.DOWNSTREAM)


class TestGate:
    @pytest.mark.asyncio
    async def test_clean_party_answer_transitions_without_llm(self):
        node = _node("Q4 Vote Now", [_edge("q4_answered", "6"), _edge("end_call", "9")])
        gate, engine, pushed = _gate(node)

        await _run(gate, _frame("कांग्रेस"))

        engine.execute_transition.assert_awaited_once()
        assert engine.execute_transition.await_args.kwargs["transition_to_node"] == "6"
        assert pushed == [], "context frame must not reach the LLM"

    @pytest.mark.asyncio
    async def test_party_answer_on_unlisted_caste_node_defers_to_llm(self):
        """The failure mode this whole design exists to prevent.

        'कांग्रेस' answered to the caste question must reach the LLM so it can
        recover, exactly as it does in production today.
        """
        node = _node("Q7 Caste", [_edge("q7_answered", "9"), _edge("end_call", "9")])
        gate, engine, pushed = _gate(node)  # allowlist contains only Q4

        await _run(gate, _frame("कांग्रेस"))

        engine.execute_transition.assert_not_awaited()
        assert len(pushed) == 1

    @pytest.mark.asyncio
    async def test_ambiguous_answer_defers_to_llm(self):
        node = _node("Q4 Vote Now", [_edge("q4_answered", "6"), _edge("end_call", "9")])
        gate, engine, pushed = _gate(node)

        await _run(gate, _frame("भाजपा नहीं, कांग्रेस"))

        engine.execute_transition.assert_not_awaited()
        assert len(pushed) == 1

    @pytest.mark.asyncio
    async def test_empty_allowlist_is_inert(self):
        """Unconfigured deployments must behave as if the gate did not exist."""
        node = _node("Q4 Vote Now", [_edge("q4_answered", "6")])
        gate, engine, pushed = _gate(node, allowlist={})

        await _run(gate, _frame("कांग्रेस"))

        engine.execute_transition.assert_not_awaited()
        assert len(pushed) == 1

    @pytest.mark.asyncio
    async def test_non_context_frames_always_pass_through(self):
        node = _node("Q4 Vote Now", [_edge("q4_answered", "6")])
        gate, engine, pushed = _gate(node)

        frame = TTSSpeakFrame("hello")
        await _run(gate, frame)

        assert pushed == [frame]
        engine.execute_transition.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_end_node_defers_to_llm(self):
        node = _node("Q4 Vote Now", [_edge("q4_answered", "6")], is_end=True)
        gate, engine, pushed = _gate(node)

        await _run(gate, _frame("कांग्रेस"))

        engine.execute_transition.assert_not_awaited()
        assert len(pushed) == 1

    @pytest.mark.asyncio
    async def test_branching_node_defers_to_llm(self):
        """Two forward edges means the parse cannot say which branch to take."""
        node = _node(
            "Q4 Vote Now",
            [
                _edge("eligible", "6"),
                _edge("not_eligible", "9"),
                _edge("end_call", "9"),
            ],
        )
        gate, engine, pushed = _gate(node)

        await _run(gate, _frame("कांग्रेस"))

        engine.execute_transition.assert_not_awaited()
        assert len(pushed) == 1

    @pytest.mark.asyncio
    async def test_failed_transition_falls_back_to_llm(self):
        """A transition that blows up must not leave the caller in silence."""
        node = _node("Q4 Vote Now", [_edge("q4_answered", "6"), _edge("end_call", "9")])
        gate, engine, pushed = _gate(node)
        engine.execute_transition = AsyncMock(side_effect=RuntimeError("boom"))

        await _run(gate, _frame("कांग्रेस"))

        assert len(pushed) == 1, "frame must be handed to the LLM after failure"

    @pytest.mark.asyncio
    async def test_no_user_message_defers_to_llm(self):
        node = _node("Q4 Vote Now", [_edge("q4_answered", "6")])
        gate, engine, pushed = _gate(node)

        frame = LLMContextFrame(
            context=SimpleNamespace(messages=[{"role": "assistant", "content": "hi"}])
        )
        await _run(gate, frame)

        engine.execute_transition.assert_not_awaited()
        assert len(pushed) == 1

    @pytest.mark.asyncio
    async def test_destination_without_recording_defers_to_llm(self):
        """Swallowing the frame would leave the caller in silence.

        Only the tool-call path can afford a missing recording: it leaves
        run_llm unset and LLM #2 speaks the question instead.
        """
        node = _node("Q4 Vote Now", [_edge("q4_answered", "6"), _edge("end_call", "9")])
        gate, engine, pushed = _gate(node)
        engine._destination_has_recorded_opening = Mock(return_value=False)

        await _run(gate, _frame("कांग्रेस"))

        engine.execute_transition.assert_not_awaited()
        assert len(pushed) == 1

    @pytest.mark.asyncio
    async def test_user_speaking_defers_to_llm(self):
        """Never start a recording over a caller who resumed talking."""
        node = _node("Q4 Vote Now", [_edge("q4_answered", "6"), _edge("end_call", "9")])
        gate, engine, pushed = _gate(node)
        engine._user_is_speaking = True

        await _run(gate, _frame("कांग्रेस"))

        engine.execute_transition.assert_not_awaited()
        assert len(pushed) == 1

    def test_last_user_text_picks_the_most_recent_turn(self):
        frame = LLMContextFrame(
            context=SimpleNamespace(
                messages=[
                    {"role": "user", "content": "पहला"},
                    {"role": "assistant", "content": "सवाल"},
                    {"role": "user", "content": "दूसरा"},
                ]
            )
        )
        assert DeterministicAnswerGate._last_user_text(frame) == "दूसरा"


# ─── Engine contract ────────────────────────────────────────────

class TestEngineContract:
    """The gate calls into PipecatEngine by name. Those calls are not exercised
    by the mocked tests above, so a missing method only surfaces on a live call
    -- which is exactly what happened: the gate matched, raised
    AttributeError: 'PipecatEngine' object has no attribute 'execute_transition',
    and silently fell back to the LLM on every fast-path hit.
    """

    def test_engine_exposes_everything_the_gate_calls(self):
        import inspect

        from api.services.workflow.pipecat_engine import PipecatEngine

        for attr in (
            "execute_transition",
            "_destination_has_recorded_opening",
            "_user_is_speaking",
            "_current_node",
        ):
            assert hasattr(PipecatEngine, attr) or attr in inspect.getsource(
                PipecatEngine.__init__
            ), f"PipecatEngine is missing {attr!r}, which DeterministicAnswerGate calls"

    def test_execute_transition_accepts_the_kwargs_the_gate_passes(self):
        import inspect

        from api.services.workflow.pipecat_engine import PipecatEngine

        params = inspect.signature(PipecatEngine.execute_transition).parameters
        for kwarg in (
            "transition_to_node",
            "transition_speech",
            "transition_speech_type",
            "transition_speech_recording_id",
        ):
            assert kwarg in params, f"execute_transition cannot accept {kwarg!r}"

    @pytest.mark.asyncio
    async def test_match_completes_the_transition_without_reaching_the_llm(self):
        """A successful fast path must both transition and withhold the frame.

        The earlier bug still logged "matched", so asserting on the log would
        have passed while every transition failed. Assert the effect instead.
        """
        node = _node("Q4 Vote Now", [_edge("q4_answered", "6"), _edge("end_call", "9")])
        gate, engine, pushed = _gate(node)

        await _run(gate, _frame("कांग्रेस"))

        engine.execute_transition.assert_awaited_once()
        assert engine.execute_transition.await_args.kwargs["transition_to_node"] == "6"
        assert pushed == [], "frame reached the LLM despite a successful transition"
