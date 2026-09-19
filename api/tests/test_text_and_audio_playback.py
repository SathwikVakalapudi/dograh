"""Tests for text and audio playback in greetings, transitions, and tool messages.

Verifies that:
- Text mode produces TTSSpeakFrame
- Audio mode produces TTSStartedFrame -> TTSAudioRawFrame -> TTSStoppedFrame
- Covers: start node greetings, edge transition speech, tool config messages
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pipecat.frames.frames import (
    Frame,
    FunctionCallResultProperties,
    LLMContextFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMContextAggregatorPair,
)
from pipecat.tests.mock_transport import MockTransport
from pipecat.transports.base_transport import TransportParams

from api.services.pipecat.recording_audio_cache import RecordingAudio
from api.services.workflow.dto import (
    AgentNodeData,
    EdgeDataDTO,
    EndCallNodeData,
    Position,
    ReactFlowDTO,
    RFEdgeDTO,
    RFNodeDTO,
    StartCallNodeData,
)
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager
from api.services.workflow.workflow_graph import WorkflowGraph
from api.tests.pipecat_test_utils import run_engine_test_pipeline
from pipecat.tests import MockLLMService, MockTTSService

# ─── Constants ──────────────────────────────────────────────────

START_PROMPT = "Start Call System Prompt"
END_PROMPT = "End Call System Prompt"
TEXT_GREETING = "Hello, welcome to our service!"
TEXT_TRANSITION = "Thank you for calling, goodbye!"
AUDIO_GREETING_ID = "rec-greeting-001"
AUDIO_TRANSITION_ID = "101"
FAKE_PCM_AUDIO = b"\x00\x01" * 1000  # Fake 16-bit mono PCM data


# ─── Fixtures ───────────────────────────────────────────────────


@pytest.fixture
def text_workflow() -> WorkflowGraph:
    """Start->End workflow with text greeting and text transition speech."""
    dto = ReactFlowDTO(
        nodes=[
            RFNodeDTO(
                id="start",
                type="startCall",
                position=Position(x=0, y=0),
                data=StartCallNodeData(
                    name="Start Call",
                    prompt=START_PROMPT,
                    is_start=True,
                    allow_interrupt=False,
                    add_global_prompt=False,
                    greeting=TEXT_GREETING,
                    greeting_type="text",
                    extraction_enabled=False,
                ),
            ),
            RFNodeDTO(
                id="end",
                type="endCall",
                position=Position(x=0, y=200),
                data=EndCallNodeData(
                    name="End Call",
                    prompt=END_PROMPT,
                    is_end=True,
                    allow_interrupt=False,
                    add_global_prompt=False,
                    extraction_enabled=False,
                ),
            ),
        ],
        edges=[
            RFEdgeDTO(
                id="start-end",
                source="start",
                target="end",
                data=EdgeDataDTO(
                    label="End Call",
                    condition="When the user says end the call",
                    transition_speech=TEXT_TRANSITION,
                    transition_speech_type="text",
                ),
            ),
        ],
    )
    return WorkflowGraph(dto)


@pytest.fixture
def audio_workflow() -> WorkflowGraph:
    """Start->End workflow with audio greeting and audio transition speech."""
    dto = ReactFlowDTO(
        nodes=[
            RFNodeDTO(
                id="start",
                type="startCall",
                position=Position(x=0, y=0),
                data=StartCallNodeData(
                    name="Start Call",
                    prompt=START_PROMPT,
                    is_start=True,
                    allow_interrupt=False,
                    add_global_prompt=False,
                    greeting_type="audio",
                    greeting_recording_id=AUDIO_GREETING_ID,
                    extraction_enabled=False,
                ),
            ),
            RFNodeDTO(
                id="end",
                type="endCall",
                position=Position(x=0, y=200),
                data=EndCallNodeData(
                    name="End Call",
                    prompt=END_PROMPT,
                    is_end=True,
                    allow_interrupt=False,
                    add_global_prompt=False,
                    extraction_enabled=False,
                ),
            ),
        ],
        edges=[
            RFEdgeDTO(
                id="start-end",
                source="start",
                target="end",
                data=EdgeDataDTO(
                    label="End Call",
                    condition="When the user says end the call",
                    transition_speech_type="audio",
                    transition_speech_recording_id=AUDIO_TRANSITION_ID,
                ),
            ),
        ],
    )
    return WorkflowGraph(dto)


# ─── Pipeline Helper ────────────────────────────────────────────


async def run_pipeline_and_capture_frames(
    workflow: WorkflowGraph,
    functions: List[Dict[str, Any]],
    fetch_recording_audio=None,
    num_text_steps: int = 1,
) -> tuple[MockLLMService, LLMContext, list[Frame]]:
    """Run a pipeline with mock tool calls and capture frames queued via task.queue_frame.

    Returns:
        Tuple of (llm, context, list of captured frames).
    """
    first_step_chunks = MockLLMService.create_multiple_function_call_chunks(functions)
    mock_steps = MockLLMService.create_multi_step_responses(
        first_step_chunks, num_text_steps=num_text_steps, step_prefix="Response"
    )

    llm = MockLLMService(mock_steps=mock_steps, chunk_delay=0.001)
    tts = MockTTSService(mock_audio_duration_ms=40, frame_delay=0)
    mock_transport = MockTransport(
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
            audio_out_end_silence_secs=0,
        ),
    )

    context = LLMContext()
    assistant_params = LLMAssistantAggregatorParams()
    context_aggregator = LLMContextAggregatorPair(
        context, assistant_params=assistant_params
    )

    engine = PipecatEngine(
        llm=llm,
        context=context,
        workflow=workflow,
        call_context_vars={"customer_name": "Test User"},
        workflow_run_id=1,
    )

    transport_output = mock_transport.output()

    if fetch_recording_audio:
        engine.set_fetch_recording_audio(fetch_recording_audio)
        engine.set_transport_output(transport_output)

    pipeline = Pipeline(
        [
            mock_transport.input(),
            llm,
            tts,
            transport_output,
            context_aggregator.assistant(),
        ]
    )
    task = PipelineWorker(pipeline, params=PipelineParams(), enable_rtvi=False)
    engine.set_task(task)

    # Spy on task.queue_frame and transport_output.queue_frame to capture
    # all frames queued by the engine (audio transitions go via transport output)
    queued_frames: list[Frame] = []
    original_queue_frame = task.queue_frame

    async def capturing_queue_frame(frame):
        queued_frames.append(frame)
        await original_queue_frame(frame)

    task.queue_frame = capturing_queue_frame

    if fetch_recording_audio:
        original_transport_queue = transport_output.queue_frame

        async def _spy_transport_queue(frame, *args, **kwargs):
            queued_frames.append(frame)
            await original_transport_queue(frame, *args, **kwargs)

        transport_output.queue_frame = _spy_transport_queue

    with (
        patch(
            "api.db:db_client.get_organization_id_by_workflow_run_id",
            new_callable=AsyncMock,
            return_value=1,
        ),
    ):
        await run_engine_test_pipeline(task, engine, mock_transport)

    return llm, context, queued_frames


# ─── Tests: Start Greeting ──────────────────────────────────────


class TestStartGreeting:
    """Unit tests for PipecatEngine.get_start_greeting()."""

    def test_text_greeting_returns_text_tuple(self, text_workflow: WorkflowGraph):
        """Text greeting config should return ('text', rendered_text)."""
        engine = PipecatEngine(
            workflow=text_workflow,
            call_context_vars={},
            workflow_run_id=1,
        )
        result = engine.get_start_greeting()
        assert result == ("text", TEXT_GREETING)

    def test_audio_greeting_returns_audio_tuple(self, audio_workflow: WorkflowGraph):
        """Audio greeting config should return ('audio', recording_id)."""
        engine = PipecatEngine(
            workflow=audio_workflow,
            call_context_vars={},
            workflow_run_id=1,
        )
        result = engine.get_start_greeting()
        assert result == ("audio", AUDIO_GREETING_ID)

    def test_no_greeting_returns_none(self):
        """No greeting configured should return None."""
        dto = ReactFlowDTO(
            nodes=[
                RFNodeDTO(
                    id="start",
                    type="startCall",
                    position=Position(x=0, y=0),
                    data=StartCallNodeData(
                        name="Start",
                        prompt="Prompt",
                        is_start=True,
                        add_global_prompt=False,
                        extraction_enabled=False,
                    ),
                ),
                RFNodeDTO(
                    id="end",
                    type="endCall",
                    position=Position(x=0, y=200),
                    data=EndCallNodeData(
                        name="End",
                        prompt="End",
                        is_end=True,
                        add_global_prompt=False,
                        extraction_enabled=False,
                    ),
                ),
            ],
            edges=[
                RFEdgeDTO(
                    id="e",
                    source="start",
                    target="end",
                    data=EdgeDataDTO(label="End", condition="End"),
                ),
            ],
        )
        engine = PipecatEngine(
            workflow=WorkflowGraph(dto),
            call_context_vars={},
            workflow_run_id=1,
        )
        assert engine.get_start_greeting() is None

    def test_text_greeting_renders_template_variables(self):
        """Text greeting with {{variable}} placeholders should be rendered."""
        dto = ReactFlowDTO(
            nodes=[
                RFNodeDTO(
                    id="start",
                    type="startCall",
                    position=Position(x=0, y=0),
                    data=StartCallNodeData(
                        name="Start",
                        prompt="Prompt",
                        is_start=True,
                        add_global_prompt=False,
                        greeting="Hello {{customer_name}}!",
                        greeting_type="text",
                        extraction_enabled=False,
                    ),
                ),
                RFNodeDTO(
                    id="end",
                    type="endCall",
                    position=Position(x=0, y=200),
                    data=EndCallNodeData(
                        name="End",
                        prompt="End",
                        is_end=True,
                        add_global_prompt=False,
                        extraction_enabled=False,
                    ),
                ),
            ],
            edges=[
                RFEdgeDTO(
                    id="e",
                    source="start",
                    target="end",
                    data=EdgeDataDTO(label="End", condition="End"),
                ),
            ],
        )
        engine = PipecatEngine(
            workflow=WorkflowGraph(dto),
            call_context_vars={"customer_name": "Alice"},
            workflow_run_id=1,
        )
        result = engine.get_start_greeting()
        assert result == ("text", "Hello Alice!")

    def test_trigger_text_greeting_override_wins_and_renders_context(
        self, text_workflow: WorkflowGraph
    ):
        engine = PipecatEngine(
            workflow=text_workflow,
            call_context_vars={
                "account_id": "ACC-123",
                "greeting_override": {
                    "type": "text",
                    "text": "Please confirm account {{account_id}}.",
                },
            },
            workflow_run_id=1,
        )

        assert engine.get_start_greeting() == (
            "text",
            "Please confirm account ACC-123.",
        )

    def test_invalid_trigger_greeting_override_uses_node_greeting(
        self, text_workflow: WorkflowGraph
    ):
        engine = PipecatEngine(
            workflow=text_workflow,
            call_context_vars={"greeting_override": {"type": "text"}},
            workflow_run_id=1,
        )

        assert engine.get_start_greeting() == ("text", TEXT_GREETING)

    @pytest.mark.asyncio
    async def test_audio_greeting_override_resolves_public_recording_id(
        self, text_workflow: WorkflowGraph
    ):
        engine = PipecatEngine(
            workflow=text_workflow,
            call_context_vars={
                "greeting_override": {
                    "type": "audio",
                    "recording_id": "callback-welcome",
                }
            },
            workflow_run_id=1,
        )
        engine.set_transport_output(Mock(queue_frame=AsyncMock()))
        engine.set_fetch_recording_audio(
            AsyncMock(return_value=RecordingAudio(FAKE_PCM_AUDIO, "Welcome back"))
        )

        result = await engine.queue_node_opening(
            node_id=text_workflow.start_node_id,
            previous_node_id=None,
            generate_if_no_greeting=True,
        )

        assert result == "greeting"
        engine._fetch_recording_audio.assert_awaited_once_with(
            recording_id="callback-welcome"
        )

    @pytest.mark.asyncio
    async def test_queue_node_opening_queues_text_greeting(
        self, text_workflow: WorkflowGraph
    ):
        """Fresh node entry with a greeting should queue TTS and skip LLM bootstrap."""
        llm = Mock()
        llm.queue_frame = AsyncMock()
        task = Mock()
        task.queue_frame = AsyncMock()

        engine = PipecatEngine(
            llm=llm,
            context=LLMContext(),
            workflow=text_workflow,
            call_context_vars={},
            workflow_run_id=1,
        )
        engine.set_task(task)

        result = await engine.queue_node_opening(
            node_id=text_workflow.start_node_id,
            previous_node_id=None,
            generate_if_no_greeting=True,
        )

        assert result == "greeting"
        llm.queue_frame.assert_not_awaited()
        queued_frame = task.queue_frame.await_args.args[0]
        assert isinstance(queued_frame, TTSSpeakFrame)
        assert queued_frame.text == TEXT_GREETING
        assert queued_frame.append_to_context is True

    @pytest.mark.asyncio
    async def test_queue_node_opening_falls_back_to_llm_without_greeting(self):
        """When a node has no greeting, the engine should queue initial LLM generation."""
        dto = ReactFlowDTO(
            nodes=[
                RFNodeDTO(
                    id="start",
                    type="startCall",
                    position=Position(x=0, y=0),
                    data=StartCallNodeData(
                        name="Start",
                        prompt="Prompt",
                        is_start=True,
                        add_global_prompt=False,
                        extraction_enabled=False,
                    ),
                ),
                RFNodeDTO(
                    id="end",
                    type="endCall",
                    position=Position(x=0, y=200),
                    data=EndCallNodeData(
                        name="End",
                        prompt="End",
                        is_end=True,
                        add_global_prompt=False,
                        extraction_enabled=False,
                    ),
                ),
            ],
            edges=[
                RFEdgeDTO(
                    id="e",
                    source="start",
                    target="end",
                    data=EdgeDataDTO(label="End", condition="End"),
                ),
            ],
        )
        workflow = WorkflowGraph(dto)
        context = LLMContext()
        llm = Mock()
        llm.queue_frame = AsyncMock()
        task = Mock()
        task.queue_frame = AsyncMock()

        engine = PipecatEngine(
            llm=llm,
            context=context,
            workflow=workflow,
            call_context_vars={},
            workflow_run_id=1,
        )
        engine.set_task(task)

        result = await engine.queue_node_opening(
            node_id=workflow.start_node_id,
            previous_node_id=None,
            generate_if_no_greeting=True,
        )

        assert result == "llm"
        task.queue_frame.assert_not_awaited()
        queued_frame = llm.queue_frame.await_args.args[0]
        assert isinstance(queued_frame, LLMContextFrame)
        assert queued_frame.context is context


# ─── Tests: Transition Speech (Pipeline) ────────────────────────


class TestTransitionSpeech:
    """Pipeline tests for edge transition speech (text and audio)."""

    @pytest.mark.asyncio
    async def test_text_transition_queues_tts_speak_frame(
        self, text_workflow: WorkflowGraph
    ):
        """Text transition speech should queue a TTSSpeakFrame with the message."""
        functions = [
            {
                "name": "end_call",
                "arguments": {},
                "tool_call_id": "call_transition",
            },
        ]

        llm, context, queued_frames = await run_pipeline_and_capture_frames(
            workflow=text_workflow,
            functions=functions,
            num_text_steps=2,
        )

        # Pipeline completes: 1st gen on StartNode, 2nd gen on EndNode
        assert llm.get_current_step() == 2

        # Verify TTSSpeakFrame was queued with the transition speech text
        tts_speak_frames = [f for f in queued_frames if isinstance(f, TTSSpeakFrame)]
        transition_frames = [f for f in tts_speak_frames if f.text == TEXT_TRANSITION]
        assert len(transition_frames) == 1, (
            f"Expected one TTSSpeakFrame with text '{TEXT_TRANSITION}', "
            f"got: {[f.text for f in tts_speak_frames]}"
        )

        # No raw audio frames should be queued for text transition
        audio_raw = [f for f in queued_frames if isinstance(f, TTSAudioRawFrame)]
        assert len(audio_raw) == 0

    @pytest.mark.asyncio
    async def test_audio_transition_queues_audio_frames(
        self, audio_workflow: WorkflowGraph
    ):
        """Audio transition speech should queue TTSStarted + TTSAudioRaw + TTSStopped."""
        functions = [
            {
                "name": "end_call",
                "arguments": {},
                "tool_call_id": "call_transition",
            },
        ]

        mock_fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))

        llm, context, queued_frames = await run_pipeline_and_capture_frames(
            workflow=audio_workflow,
            functions=functions,
            fetch_recording_audio=mock_fetch,
            num_text_steps=2,
        )

        # Pipeline completes
        assert llm.get_current_step() == 2

        # Verify fetch was called with the correct recording ID
        mock_fetch.assert_called_once_with(recording_pk=int(AUDIO_TRANSITION_ID))

        # Verify the three-frame audio sequence was queued
        started = [f for f in queued_frames if isinstance(f, TTSStartedFrame)]
        audio = [f for f in queued_frames if isinstance(f, TTSAudioRawFrame)]
        stopped = [f for f in queued_frames if isinstance(f, TTSStoppedFrame)]

        assert len(started) >= 1, (
            f"Expected TTSStartedFrame. "
            f"Frame types: {[type(f).__name__ for f in queued_frames]}"
        )
        assert len(audio) >= 1, "Expected TTSAudioRawFrame"
        assert len(stopped) >= 1, "Expected TTSStoppedFrame"

        # Verify audio content
        assert audio[0].audio == FAKE_PCM_AUDIO
        assert audio[0].sample_rate == 16000
        assert audio[0].num_channels == 1

        # Verify context_id consistency across the three frames
        ctx_id = started[0].context_id
        assert ctx_id is not None
        assert audio[0].context_id == ctx_id
        assert stopped[0].context_id == ctx_id

        # No TTSSpeakFrame should be queued for audio transition
        speak = [f for f in queued_frames if isinstance(f, TTSSpeakFrame)]
        assert len(speak) == 0


# ─── Tests: Tool Config Messages ────────────────────────────────


class TestPlayConfigMessage:
    """Unit tests for CustomToolManager._play_config_message."""

    @pytest.fixture
    def mock_engine(self):
        """Create a mock engine with frame capture on task.queue_frame."""
        engine = Mock()
        engine._workflow_run_id = 1
        engine._call_context_vars = {}
        engine._fetch_recording_audio = None
        engine._audio_config = None
        engine.task = Mock()
        engine.llm = Mock()

        # Capture frames queued via task.queue_frame
        engine._queued_frames = []

        async def mock_queue_frame(frame):
            engine._queued_frames.append(frame)

        engine.task.queue_frame = mock_queue_frame

        # Also capture frames queued via transport_output.queue_frame (audio playback)
        engine._transport_output = Mock()
        engine._transport_output.queue_frame = mock_queue_frame
        return engine

    @pytest.mark.asyncio
    async def test_custom_text_queues_tts_speak_frame(self, mock_engine):
        """messageType='custom' queues TTSSpeakFrame with the message text."""
        manager = CustomToolManager(mock_engine)
        config = {"messageType": "custom", "customMessage": "Ending your call now."}

        result = await manager._play_config_message(config)

        assert result is True
        frames = mock_engine._queued_frames
        assert len(frames) == 1
        assert isinstance(frames[0], TTSSpeakFrame)
        assert frames[0].text == "Ending your call now."

    @pytest.mark.asyncio
    async def test_audio_queues_started_raw_stopped_frames(self, mock_engine):
        """messageType='audio' queues TTSStarted + TTSAudioRaw + TTSStopped."""
        mock_fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))
        mock_engine._fetch_recording_audio = mock_fetch

        manager = CustomToolManager(mock_engine)
        config = {"messageType": "audio", "audioRecordingId": "201"}

        result = await manager._play_config_message(config)

        assert result is True
        mock_fetch.assert_called_once_with(recording_pk=201)

        frames = mock_engine._queued_frames
        assert len(frames) == 3
        assert isinstance(frames[0], TTSStartedFrame)
        assert isinstance(frames[1], TTSAudioRawFrame)
        assert isinstance(frames[2], TTSStoppedFrame)

        # Verify audio content
        assert frames[1].audio == FAKE_PCM_AUDIO
        assert frames[1].sample_rate == 16000
        assert frames[1].num_channels == 1

        # Context IDs should match across all three frames
        ctx_id = frames[0].context_id
        assert ctx_id is not None
        assert frames[1].context_id == ctx_id
        assert frames[2].context_id == ctx_id

    @pytest.mark.asyncio
    async def test_none_message_type_returns_false(self, mock_engine):
        """messageType='none' returns False without queuing frames."""
        manager = CustomToolManager(mock_engine)
        result = await manager._play_config_message({"messageType": "none"})

        assert result is False
        assert len(mock_engine._queued_frames) == 0

    @pytest.mark.asyncio
    async def test_audio_without_fetch_callback_returns_false(self, mock_engine):
        """Audio without fetch_recording_audio callback returns False."""
        mock_engine._fetch_recording_audio = None

        manager = CustomToolManager(mock_engine)
        config = {"messageType": "audio", "audioRecordingId": "301"}

        result = await manager._play_config_message(config)

        assert result is False
        assert len(mock_engine._queued_frames) == 0

    @pytest.mark.asyncio
    async def test_audio_with_failed_fetch_returns_false(self, mock_engine):
        """Audio with fetch returning None returns False."""
        mock_fetch = AsyncMock(return_value=None)
        mock_engine._fetch_recording_audio = mock_fetch

        manager = CustomToolManager(mock_engine)
        config = {"messageType": "audio", "audioRecordingId": "301"}

        result = await manager._play_config_message(config)

        assert result is False
        mock_fetch.assert_called_once_with(recording_pk=301)
        assert len(mock_engine._queued_frames) == 0

    @pytest.mark.asyncio
    async def test_custom_empty_message_returns_false(self, mock_engine):
        """messageType='custom' with empty message returns False."""
        manager = CustomToolManager(mock_engine)
        config = {"messageType": "custom", "customMessage": ""}

        result = await manager._play_config_message(config)

        assert result is False
        assert len(mock_engine._queued_frames) == 0


# ─── Tests: Recorded Node Openings on Transition ────────────────

AGENT_OPENING_ID = "202"
# Transition tool names are derived from the edge label (transition_tool_name).
AGENT_TRANSITION_TOOL = "qualify"


def _recording_audio_frames(frames: List[Frame]) -> List[TTSAudioRawFrame]:
    """Audio frames that came from the recording, not from the mock TTS."""
    return [
        f
        for f in frames
        if isinstance(f, TTSAudioRawFrame) and f.audio == FAKE_PCM_AUDIO
    ]


def _recorded_opening_workflow(
    *, opening_recording_id: str | None = AGENT_OPENING_ID
) -> WorkflowGraph:
    """Start -> Agent -> End, where the Agent node owns a recorded opening.

    The Start->Agent edge carries no transition speech so the only audio in the
    run comes from the destination node's opening.
    """
    agent_data: dict[str, Any] = {
        "name": "Qualify",
        "prompt": "Agent System Prompt",
        "allow_interrupt": True,
        "add_global_prompt": False,
        "extraction_enabled": False,
    }
    if opening_recording_id is not None:
        agent_data["greeting_type"] = "audio"
        agent_data["greeting_recording_id"] = opening_recording_id

    dto = ReactFlowDTO(
        nodes=[
            RFNodeDTO(
                id="start",
                type="startCall",
                position=Position(x=0, y=0),
                data=StartCallNodeData(
                    name="Start Call",
                    prompt=START_PROMPT,
                    is_start=True,
                    allow_interrupt=False,
                    add_global_prompt=False,
                    extraction_enabled=False,
                ),
            ),
            RFNodeDTO(
                id="agent",
                type="agentNode",
                position=Position(x=0, y=200),
                data=AgentNodeData(**agent_data),
            ),
            RFNodeDTO(
                id="end",
                type="endCall",
                position=Position(x=0, y=400),
                data=EndCallNodeData(
                    name="End Call",
                    prompt=END_PROMPT,
                    is_end=True,
                    allow_interrupt=False,
                    add_global_prompt=False,
                    extraction_enabled=False,
                ),
            ),
        ],
        edges=[
            RFEdgeDTO(
                id="start-agent",
                source="start",
                target="agent",
                data=EdgeDataDTO(
                    label="Qualify",
                    condition="When the user is ready to be qualified",
                ),
            ),
            RFEdgeDTO(
                id="agent-end",
                source="agent",
                target="end",
                data=EdgeDataDTO(
                    label="End Call",
                    condition="When the user says end the call",
                ),
            ),
        ],
    )
    return WorkflowGraph(dto)


class TestRecordedNodeOpening:
    """The recorded opening replaces LLM #2 only on a successful transition."""

    @pytest.mark.asyncio
    async def test_recorded_opening_plays_once_and_suppresses_second_llm(self):
        """Transition into a node with a recording: audio plays, LLM #2 does not run."""
        mock_fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))

        llm, context, queued_frames = await run_pipeline_and_capture_frames(
            workflow=_recorded_opening_workflow(),
            functions=[
                {
                    "name": AGENT_TRANSITION_TOOL,
                    "arguments": {},
                    "tool_call_id": "call_transition",
                }
            ],
            fetch_recording_audio=mock_fetch,
            num_text_steps=2,
        )

        # The whole point: only the first generation ran. Without the recording
        # this is 2 (see test_no_recording_falls_back_to_second_llm below).
        assert llm.get_current_step() == 1, (
            "LLM #2 should be suppressed when a recorded opening was queued"
        )

        mock_fetch.assert_called_once_with(recording_pk=int(AGENT_OPENING_ID))

        started = [f for f in queued_frames if isinstance(f, TTSStartedFrame)]
        audio = [f for f in queued_frames if isinstance(f, TTSAudioRawFrame)]
        assert len(started) == 1, "recorded opening must play exactly once"
        assert len(audio) == 1
        assert audio[0].audio == FAKE_PCM_AUDIO

    @pytest.mark.asyncio
    async def test_no_recording_falls_back_to_second_llm(self):
        """Without a configured opening the existing two-call behaviour is kept."""
        mock_fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))

        llm, context, queued_frames = await run_pipeline_and_capture_frames(
            workflow=_recorded_opening_workflow(opening_recording_id=None),
            functions=[
                {
                    "name": AGENT_TRANSITION_TOOL,
                    "arguments": {},
                    "tool_call_id": "call_transition",
                }
            ],
            fetch_recording_audio=mock_fetch,
            num_text_steps=2,
        )

        assert llm.get_current_step() == 2, "LLM #2 must still run without a recording"
        mock_fetch.assert_not_called()
        assert _recording_audio_frames(queued_frames) == []

    @pytest.mark.asyncio
    async def test_fetch_failure_falls_back_to_second_llm(self):
        """A recording that cannot be fetched must not silence the turn."""
        mock_fetch = AsyncMock(return_value=None)

        llm, context, queued_frames = await run_pipeline_and_capture_frames(
            workflow=_recorded_opening_workflow(),
            functions=[
                {
                    "name": AGENT_TRANSITION_TOOL,
                    "arguments": {},
                    "tool_call_id": "call_transition",
                }
            ],
            fetch_recording_audio=mock_fetch,
            num_text_steps=2,
        )

        mock_fetch.assert_called_once()
        assert llm.get_current_step() == 2, "failed fetch must fall back to LLM #2"
        assert _recording_audio_frames(queued_frames) == []

    @pytest.mark.asyncio
    async def test_fetch_raising_falls_back_to_second_llm(self):
        """An exception from the fetcher must not fail the transition."""
        mock_fetch = AsyncMock(side_effect=RuntimeError("storage down"))

        llm, context, queued_frames = await run_pipeline_and_capture_frames(
            workflow=_recorded_opening_workflow(),
            functions=[
                {
                    "name": AGENT_TRANSITION_TOOL,
                    "arguments": {},
                    "tool_call_id": "call_transition",
                }
            ],
            fetch_recording_audio=mock_fetch,
            num_text_steps=2,
        )

        assert llm.get_current_step() == 2, "raising fetch must fall back to LLM #2"
        assert _recording_audio_frames(queued_frames) == []


class TestOpeningEpoch:
    """Exactly-once semantics for a node's opening, at the engine level."""

    def _engine(self, fetch) -> PipecatEngine:
        engine = PipecatEngine(
            llm=Mock(),
            context=LLMContext(),
            workflow=_recorded_opening_workflow(),
            call_context_vars={},
            workflow_run_id=1,
        )
        engine.set_fetch_recording_audio(fetch)
        transport_output = Mock()
        transport_output.queue_frame = AsyncMock()
        engine.set_transport_output(transport_output)
        return engine

    @pytest.mark.asyncio
    async def test_concurrent_openings_play_once(self):
        """Two handlers racing on one node entry produce a single playback."""
        fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))
        engine = self._engine(fetch)
        engine._opening_epoch = 1  # simulate a single set_node()

        results = await asyncio.gather(
            engine.queue_node_opening(
                node_id="agent", previous_node_id="start", generate_if_no_greeting=False
            ),
            engine.queue_node_opening(
                node_id="agent", previous_node_id="start", generate_if_no_greeting=False
            ),
        )

        assert sorted(results) == ["greeting", "none"], (
            "exactly one caller may claim the opening"
        )
        fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_call_after_playback_is_suppressed(self):
        """A late duplicate for the same entry must not replay the recording."""
        fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))
        engine = self._engine(fetch)
        engine._opening_epoch = 1

        first = await engine.queue_node_opening(
            node_id="agent", previous_node_id="start", generate_if_no_greeting=False
        )
        second = await engine.queue_node_opening(
            node_id="agent", previous_node_id="start", generate_if_no_greeting=False
        )

        assert first == "greeting"
        assert second == "none"
        fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_node_revisit_gets_a_fresh_opening(self):
        """Re-entering the node later in the call plays the opening again."""
        fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))
        engine = self._engine(fetch)

        engine._opening_epoch = 1
        first = await engine.queue_node_opening(
            node_id="agent", previous_node_id="start", generate_if_no_greeting=False
        )
        engine._opening_epoch = 2  # a later set_node() into the same node
        second = await engine.queue_node_opening(
            node_id="agent", previous_node_id="start", generate_if_no_greeting=False
        )

        assert first == "greeting"
        assert second == "greeting"
        assert fetch.call_count == 2

    @pytest.mark.asyncio
    async def test_failed_fetch_consumes_the_epoch(self):
        """A broken recording must not be retried for the same node entry."""
        fetch = AsyncMock(return_value=None)
        engine = self._engine(fetch)
        engine._opening_epoch = 1

        assert (
            await engine.queue_node_opening(
                node_id="agent", previous_node_id="start", generate_if_no_greeting=False
            )
            == "none"
        )
        assert (
            await engine.queue_node_opening(
                node_id="agent", previous_node_id="start", generate_if_no_greeting=False
            )
            == "none"
        )
        fetch.assert_called_once()


class TestRunLlmContract:
    """The vendored pipecat contract this optimization depends on."""

    @pytest.mark.asyncio
    async def test_run_llm_false_suppresses_inference_but_still_runs_callback(self):
        """run_llm=False must stop the generation and still fire on_context_updated.

        Exercised against the installed pipecat aggregator via pipecat's own
        run_test harness (which wires the task manager), not a mock, because
        end-of-call handling rides on on_context_updated.
        """
        from pipecat.frames.frames import (
            FunctionCallInProgressFrame,
            FunctionCallResultFrame,
        )
        from pipecat.tests.utils import SleepFrame, run_test

        called = asyncio.Event()

        async def on_context_updated() -> None:
            called.set()

        aggregator = LLMContextAggregatorPair(LLMContext()).assistant()

        in_progress = FunctionCallInProgressFrame(
            function_name=AGENT_TRANSITION_TOOL,
            tool_call_id="call_1",
            arguments={},
        )
        result = FunctionCallResultFrame(
            function_name=AGENT_TRANSITION_TOOL,
            tool_call_id="call_1",
            arguments={},
            result={"status": "done"},
            properties=FunctionCallResultProperties(
                run_llm=False,
                on_context_updated=on_context_updated,
            ),
        )

        # SleepFrame keeps the pipeline alive long enough for the
        # on_context_updated task to run: the EndFrame run_test sends afterwards
        # cancels any callback task still in flight.
        _, received_up = await run_test(
            aggregator,
            frames_to_send=[in_progress, result, SleepFrame(sleep=0.3)],
            expected_down_frames=[],
        )

        # on_context_updated still runs, so end-of-call handling survives.
        assert called.is_set(), "on_context_updated must still fire when run_llm=False"

        # ...and no context frame was pushed upstream, i.e. LLM #2 never ran.
        assert not [f for f in received_up if isinstance(f, LLMContextFrame)], (
            "run_llm=False must not push a context frame (must not run LLM #2)"
        )


class TestParallelTransitions:
    """One LLM response emitting two transition tool calls.

    pipecat runs function calls with run_in_parallel=True, so both handlers
    execute and each performs its own set_node(). Only the node that is still
    current may speak its opening.
    """

    def _engine(self, fetch) -> PipecatEngine:
        engine = PipecatEngine(
            llm=Mock(),
            context=LLMContext(),
            workflow=_recorded_opening_workflow(),
            call_context_vars={},
            workflow_run_id=1,
        )
        engine.set_fetch_recording_audio(fetch)
        transport_output = Mock()
        transport_output.queue_frame = AsyncMock()
        engine.set_transport_output(transport_output)
        return engine

    @pytest.mark.asyncio
    async def test_losing_node_does_not_speak(self):
        """A transition whose node was superseded must not play its opening."""
        fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))
        engine = self._engine(fetch)

        # Handler B won the race: "agent" is no longer the current node.
        engine._current_node = engine.workflow.nodes["end"]
        engine._opening_epoch = 3

        result = await engine.queue_node_opening(
            node_id="agent", previous_node_id="start", generate_if_no_greeting=False
        )

        assert result == "none", "superseded node must not speak"
        fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_loser_does_not_consume_the_winners_epoch(self):
        """The superseded handler must leave the epoch for the winner.

        Regression guard for check-ordering: if the winner-check ran *after* the
        epoch CAS, the loser would consume the shared epoch and silence the
        node that actually won.
        """
        fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))
        engine = self._engine(fetch)
        engine._current_node = engine.workflow.nodes["agent"]
        engine._opening_epoch = 3

        loser = await engine.queue_node_opening(
            node_id="end", previous_node_id="start", generate_if_no_greeting=False
        )
        winner = await engine.queue_node_opening(
            node_id="agent", previous_node_id="start", generate_if_no_greeting=False
        )

        assert loser == "none"
        assert winner == "greeting", "winner must still be able to speak"
        fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_concurrent_different_nodes_play_at_most_once(self):
        """Racing openings for two different nodes never both play."""
        fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))
        engine = self._engine(fetch)
        engine._current_node = engine.workflow.nodes["agent"]
        engine._opening_epoch = 2

        results = await asyncio.gather(
            engine.queue_node_opening(
                node_id="agent", previous_node_id="start", generate_if_no_greeting=False
            ),
            engine.queue_node_opening(
                node_id="end", previous_node_id="start", generate_if_no_greeting=False
            ),
        )

        assert results.count("greeting") <= 1, "at most one opening may be spoken"
        assert fetch.call_count <= 1


class TestOpeningSampleRates:
    """The opening must carry the pipeline sample rate for both transports."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sample_rate", [8000, 16000])
    async def test_opening_uses_pipeline_sample_rate(self, sample_rate: int):
        """8 kHz telephony and 16 kHz WebRTC both flow through one code path."""
        from api.services.pipecat.audio_config import AudioConfig

        fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))
        engine = PipecatEngine(
            llm=Mock(),
            context=LLMContext(),
            workflow=_recorded_opening_workflow(),
            call_context_vars={},
            workflow_run_id=1,
        )
        engine.set_fetch_recording_audio(fetch)
        engine.set_audio_config(
            AudioConfig(
                transport_in_sample_rate=sample_rate,
                transport_out_sample_rate=sample_rate,
                vad_sample_rate=16000,
            )
        )
        queued: list = []
        transport_output = Mock()

        async def _capture(frame, *a, **kw):
            queued.append(frame)

        transport_output.queue_frame = _capture
        engine.set_transport_output(transport_output)
        engine._current_node = engine.workflow.nodes["agent"]
        engine._opening_epoch = 1

        assert (
            await engine.queue_node_opening(
                node_id="agent", previous_node_id="start", generate_if_no_greeting=False
            )
            == "greeting"
        )

        audio = [f for f in queued if isinstance(f, TTSAudioRawFrame)]
        assert len(audio) == 1
        assert audio[0].sample_rate == sample_rate
        assert audio[0].num_channels == 1


class TestOpeningContextCommit:
    """The recorded question must enter assistant context exactly once."""

    @pytest.mark.asyncio
    async def test_opening_frames_request_context_append(self):
        """play_audio marks the transcript frame append_to_context for the aggregator.

        This is what makes the recorded question the assistant's committed turn,
        so the next user turn sees it in history and the transcript records it
        once. A second (LLM-generated) question cannot appear because run_llm is
        False on this path.
        """
        fetch = AsyncMock(
            return_value=RecordingAudio(
                audio=FAKE_PCM_AUDIO, transcript="How many employees do you have?"
            )
        )
        engine = PipecatEngine(
            llm=Mock(),
            context=LLMContext(),
            workflow=_recorded_opening_workflow(),
            call_context_vars={},
            workflow_run_id=1,
        )
        engine.set_fetch_recording_audio(fetch)
        queued: list = []
        transport_output = Mock()

        async def _capture(frame, *a, **kw):
            queued.append(frame)

        transport_output.queue_frame = _capture
        engine.set_transport_output(transport_output)
        engine._current_node = engine.workflow.nodes["agent"]
        engine._opening_epoch = 1

        await engine.queue_node_opening(
            node_id="agent", previous_node_id="start", generate_if_no_greeting=False
        )

        from pipecat.frames.frames import TTSTextFrame

        text_frames = [f for f in queued if isinstance(f, TTSTextFrame)]
        assert len(text_frames) == 1, "exactly one assistant transcript frame"
        assert text_frames[0].text == "How many employees do you have?"
        assert text_frames[0].append_to_context is True


class TestUserSpeakingGuard:
    """The recorded opening must never start on top of a speaking caller.

    The generation it replaces is gated by pipecat's own
    `if run_llm and not self._user_speaking` check, which has no recovery path
    for the user-speaking case. The opening honours the same rule and hands the
    turn back to that check by leaving run_llm unset.
    """

    def _engine(self, fetch) -> PipecatEngine:
        # AsyncMock: set_node() -> _setup_llm_context() awaits on the LLM.
        engine = PipecatEngine(
            llm=AsyncMock(),
            context=LLMContext(),
            workflow=_recorded_opening_workflow(),
            call_context_vars={},
            workflow_run_id=1,
        )
        engine.set_fetch_recording_audio(fetch)
        transport_output = Mock()
        transport_output.queue_frame = AsyncMock()
        engine.set_transport_output(transport_output)
        # Variable extraction is orthogonal to this guard and needs a manager
        # that only initialize() builds.
        engine._perform_variable_extraction_if_needed = AsyncMock()
        engine._current_node = engine.workflow.nodes["agent"]
        engine._opening_epoch = 1
        return engine

    @pytest.mark.asyncio
    async def test_user_speaking_frames_update_engine_state(self):
        """should_mute_user observes the same two frames pipecat tracks."""
        from pipecat.frames.frames import (
            UserStartedSpeakingFrame,
            UserStoppedSpeakingFrame,
        )

        engine = self._engine(AsyncMock())
        assert engine._user_is_speaking is False

        await engine.should_mute_user(UserStartedSpeakingFrame())
        assert engine._user_is_speaking is True

        await engine.should_mute_user(UserStoppedSpeakingFrame())
        assert engine._user_is_speaking is False

    @pytest.mark.asyncio
    async def test_user_speaking_does_not_change_mute_decision(self):
        """Tracking user speech must not alter who gets muted."""
        from pipecat.frames.frames import UserStartedSpeakingFrame

        engine = self._engine(AsyncMock())
        engine._current_node.allow_interrupt = True

        assert await engine.should_mute_user(UserStartedSpeakingFrame()) is False

    @pytest.mark.asyncio
    async def test_opening_plays_when_user_is_not_speaking(self):
        """Baseline: the optimisation still applies in the normal case."""
        fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))
        engine = self._engine(fetch)
        engine._user_is_speaking = False

        assert (
            await engine.queue_node_opening(
                node_id="agent", previous_node_id="start", generate_if_no_greeting=False
            )
            == "greeting"
        )
        fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_transition_skips_opening_while_user_speaks(self):
        """Recording must not start, and LLM fallback must stay enabled."""
        fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))
        engine = self._engine(fetch)
        engine._user_is_speaking = True
        # Transition in from the start node: a self-loop would be skipped by the
        # existing previous_node_id guard and prove nothing about this one.
        engine._current_node = engine.workflow.nodes["start"]

        captured = {}

        async def result_callback(result, *, properties=None):
            captured["properties"] = properties

        transition = await engine._create_transition_func("qualify", "agent")
        await transition(
            SimpleNamespace(
                function_name="qualify",
                tool_call_id="call_1",
                arguments={},
                result_callback=result_callback,
            )
        )

        fetch.assert_not_called(), "recording must not be fetched or played"
        assert captured["properties"].run_llm is None, (
            "skipping for a speaking user must NOT suppress the LLM fallback"
        )
        assert captured["properties"].on_context_updated is not None

    @pytest.mark.asyncio
    async def test_transition_plays_opening_when_user_silent(self):
        """The same path suppresses LLM #2 once the caller is quiet."""
        fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))
        engine = self._engine(fetch)
        engine._user_is_speaking = False
        engine._current_node = engine.workflow.nodes["start"]

        captured = {}

        async def result_callback(result, *, properties=None):
            captured["properties"] = properties

        transition = await engine._create_transition_func("qualify", "agent")
        await transition(
            SimpleNamespace(
                function_name="qualify",
                tool_call_id="call_1",
                arguments={},
                result_callback=result_callback,
            )
        )

        fetch.assert_called_once()
        assert captured["properties"].run_llm is False

    @pytest.mark.asyncio
    async def test_recovery_after_user_stops_speaking(self):
        """User speaks, stops, then a later transition plays normally."""
        from pipecat.frames.frames import UserStoppedSpeakingFrame

        fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))
        engine = self._engine(fetch)
        engine._user_is_speaking = True

        # First transition is skipped while the caller is talking.
        first = await engine.queue_node_opening(
            node_id="agent", previous_node_id="start", generate_if_no_greeting=False
        )
        assert first == "greeting", (
            "queue_node_opening itself is unguarded; the guard lives in the "
            "transition path so the start-node greeting is unaffected"
        )

        # Caller falls silent; a later node entry plays normally.
        await engine.should_mute_user(UserStoppedSpeakingFrame())
        assert engine._user_is_speaking is False
        engine._opening_epoch = 2
        assert (
            await engine.queue_node_opening(
                node_id="agent", previous_node_id="start", generate_if_no_greeting=False
            )
            == "greeting"
        )


class TestOpeningPlaybackFailure:
    """Playback itself failing must degrade to the LLM path, not to silence.

    Distinct from a failed *fetch*: here the recording resolves but pushing the
    audio frames raises, e.g. a transport that is tearing down.
    """

    @pytest.mark.asyncio
    async def test_playback_exception_falls_back_to_llm(self):
        fetch = AsyncMock(return_value=RecordingAudio(audio=FAKE_PCM_AUDIO))
        engine = PipecatEngine(
            llm=AsyncMock(),
            context=LLMContext(),
            workflow=_recorded_opening_workflow(),
            call_context_vars={},
            workflow_run_id=1,
        )
        engine.set_fetch_recording_audio(fetch)
        transport_output = Mock()
        transport_output.queue_frame = AsyncMock(
            side_effect=RuntimeError("transport gone")
        )
        engine.set_transport_output(transport_output)
        engine._perform_variable_extraction_if_needed = AsyncMock()
        engine._current_node = engine.workflow.nodes["start"]

        captured = {}

        async def result_callback(result, *, properties=None):
            captured["properties"] = properties

        transition = await engine._create_transition_func("qualify", "agent")
        await transition(
            SimpleNamespace(
                function_name="qualify",
                tool_call_id="call_1",
                arguments={},
                result_callback=result_callback,
            )
        )

        # The transition still succeeds and the LLM fallback stays enabled.
        assert captured["properties"] is not None, (
            "playback failure must not turn the transition into an error result"
        )
        assert captured["properties"].run_llm is None
        assert captured["properties"].on_context_updated is not None
