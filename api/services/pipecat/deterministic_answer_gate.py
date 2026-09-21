"""Skip LLM #1 when the caller's answer is unambiguous.

Sits between the user context aggregator and the LLM, in the same position the
voicemail detector's ``LLMGate`` occupies, and follows the same contract: only
``LLMContextFrame`` is ever withheld, every other frame passes through
immediately so turn detection and speaking events keep flowing.

On each ``LLMContextFrame`` it asks a node-scoped parser whether the last user
message is an unambiguous answer for the node the conversation is on. If it is,
the transition runs directly and the frame is dropped, so no inference happens.
If it is not — ambiguous, empty, unrecognised, or the node has no parser — the
frame is forwarded untouched and the normal LLM path runs exactly as before.

The gate is inert unless ``DETERMINISTIC_ANSWER_NODES`` names nodes, so an
unconfigured deployment behaves identically to one without this processor.
"""

from typing import TYPE_CHECKING, Optional

from loguru import logger

from api.services.workflow.answer_parsers import PARSERS
from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

if TYPE_CHECKING:
    from api.services.workflow.pipecat_engine import PipecatEngine


def parse_node_allowlist(raw: Optional[str]) -> dict[str, str]:
    """Parse ``DETERMINISTIC_ANSWER_NODES`` into ``{node_name: parser_kind}``.

    Accepts ``"Q4 Vote Now:party,Q5 Vote 2022:party"``. Entries naming an
    unknown parser are dropped with a warning rather than failing the call.
    """
    allowlist: dict[str, str] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        node_name, _, kind = entry.partition(":")
        node_name, kind = node_name.strip(), kind.strip()
        if not node_name or kind not in PARSERS:
            logger.warning(
                f"Ignoring deterministic-answer entry {entry!r}: unknown parser {kind!r}"
            )
            continue
        allowlist[node_name] = kind
    return allowlist


class DeterministicAnswerGate(FrameProcessor):
    """Bypass the LLM for unambiguous answers on explicitly allowlisted nodes.

    Args:
        engine: The live ``PipecatEngine``; supplies the current node and runs
            the transition.
        allowlist: ``{node_name: parser_kind}``. A node absent from this mapping
            is never parsed, which is what keeps a party parser away from the
            caste question.
    """

    def __init__(self, *, engine: "PipecatEngine", allowlist: dict[str, str], **kwargs):
        super().__init__(**kwargs)
        self._engine = engine
        self._allowlist = allowlist

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        try:
            edge = self._match(frame)
        except Exception as e:
            # The fast path is an optimization; nothing it does may cost a turn.
            logger.error(f"Deterministic answer gate failed, deferring to LLM: {e}")
            edge = None

        if edge is None:
            await self.push_frame(frame, direction)
            return

        logger.info(
            f"Deterministic answer matched on node "
            f"{self._engine._current_node.name!r}: taking edge "
            f"{edge.get_function_name()!r} without an LLM call"
        )
        try:
            await self._engine.execute_transition(
                transition_to_node=edge.target,
                transition_speech=edge.transition_speech,
                transition_speech_type=edge.data.transition_speech_type,
                transition_speech_recording_id=edge.data.transition_speech_recording_id,
            )
        except Exception as e:
            # The transition itself failed after we swallowed the frame. Hand the
            # turn back to the LLM rather than leaving the caller in silence.
            logger.error(f"Deterministic transition failed, falling back to LLM: {e}")
            await self.push_frame(frame, direction)

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def _match(self, frame: LLMContextFrame):
        """Return the edge to take, or None to defer to the LLM."""
        if not self._allowlist:
            return None

        node = self._engine._current_node
        if node is None or node.is_end:
            return None

        kind = self._allowlist.get(node.name)
        if kind is None:
            return None

        text = self._last_user_text(frame)
        if not text:
            return None

        answer = PARSERS[kind](text)
        if answer is None:
            return None

        edge = self._proceed_edge(node)
        if edge is None:
            return None

        # Swallowing the context frame means nothing downstream will speak, so
        # the fast path may only commit when the destination is certain to open
        # with its recording. The tool-call path has no such requirement: it
        # leaves run_llm unset and LLM #2 generates the question instead. These
        # are the same two conditions execute_transition itself checks before
        # queueing an opening.
        if self._engine._user_is_speaking:
            return None
        if not self._engine._destination_has_recorded_opening(edge.target):
            return None

        return edge

    @staticmethod
    def _last_user_text(frame: LLMContextFrame) -> Optional[str]:
        """The most recent user turn already committed by the aggregator."""
        for message in reversed(getattr(frame.context, "messages", []) or []):
            role = (
                message.get("role")
                if isinstance(message, dict)
                else getattr(message, "role", None)
            )
            if role != "user":
                continue
            content = (
                message.get("content")
                if isinstance(message, dict)
                else getattr(message, "content", None)
            )
            if isinstance(content, str):
                return content
            # Multimodal turns arrive as a parts list; take the text parts only.
            if isinstance(content, list):
                parts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                return " ".join(parts) or None
            return None
        return None

    @staticmethod
    def _proceed_edge(node):
        """The single forward edge, or None when the choice is not obvious.

        A parsed answer only says "this was a clean reply"; it carries no
        opinion about which branch to take. So the fast path commits only when
        the node has exactly one non-terminal outgoing edge, leaving anything
        with a real branch (a screener, say) to the LLM.
        """
        candidates = [e for e in node.out_edges if e.get_function_name() != "end_call"]
        if len(candidates) != 1:
            return None
        return candidates[0]
