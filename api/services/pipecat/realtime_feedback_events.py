"""Shared helpers for building and ordering realtime feedback events."""

from typing import Any

from pipecat.utils.enums import RealtimeFeedbackType


def build_node_transition_event(
    *,
    node_id: str | None,
    node_name: str | None,
    previous_node_id: str | None,
    previous_node_name: str | None,
    allow_interrupt: bool = False,
) -> dict[str, Any]:
    return {
        "type": RealtimeFeedbackType.NODE_TRANSITION.value,
        "payload": {
            "node_id": node_id,
            "node_name": node_name,
            "previous_node_id": previous_node_id,
            "previous_node_name": previous_node_name,
            "allow_interrupt": allow_interrupt,
        },
    }


def build_user_transcription_event(
    *,
    text: str,
    final: bool,
    timestamp: str | None = None,
    end_timestamp: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": text,
        "final": final,
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp
    if end_timestamp is not None:
        payload["end_timestamp"] = end_timestamp
    if user_id is not None:
        payload["user_id"] = user_id
    return {
        "type": RealtimeFeedbackType.USER_TRANSCRIPTION.value,
        "payload": payload,
    }


def build_bot_text_event(
    *,
    text: str,
    timestamp: str | None = None,
    end_timestamp: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": text}
    if timestamp is not None:
        payload["timestamp"] = timestamp
    if end_timestamp is not None:
        payload["end_timestamp"] = end_timestamp
    return {
        "type": RealtimeFeedbackType.BOT_TEXT.value,
        "payload": payload,
    }


def build_function_call_start_event(
    *,
    function_name: str | None,
    tool_call_id: str | None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "function_name": function_name,
        "tool_call_id": tool_call_id,
    }
    if arguments is not None:
        payload["arguments"] = arguments
    return {
        "type": RealtimeFeedbackType.FUNCTION_CALL_START.value,
        "payload": payload,
    }


def serialize_realtime_feedback_tool_result(result: Any) -> str | None:
    """Normalize function-call results to the string shape stored in logs."""
    if result is None:
        return None
    return str(result)


def build_function_call_end_event(
    *,
    function_name: str | None,
    tool_call_id: str | None,
    result: Any,
) -> dict[str, Any]:
    return {
        "type": RealtimeFeedbackType.FUNCTION_CALL_END.value,
        "payload": {
            "function_name": function_name,
            "tool_call_id": tool_call_id,
            "result": serialize_realtime_feedback_tool_result(result),
        },
    }


def build_ttfb_metric_event(
    *,
    ttfb_seconds: float,
    processor: str | None,
    model: str | None,
) -> dict[str, Any]:
    return {
        "type": RealtimeFeedbackType.TTFB_METRIC.value,
        "payload": {
            "ttfb_seconds": ttfb_seconds,
            "processor": processor,
            "model": model,
        },
    }


LATENCY_BREAKDOWN_EVENT_TYPE = "rtf-latency-breakdown"
"""Wire type for the per-service latency breakdown.

A literal rather than a ``RealtimeFeedbackType`` member: the installed pipecat
build does not define ``LATENCY_BREAKDOWN``.
"""

LATENCY_BREAKDOWN_SCHEMA_VERSION = 1


def build_latency_breakdown_event(breakdown: Any) -> dict[str, Any]:
    """Build the per-service breakdown that accompanies ``rtf-latency-measured``.

    ``breakdown`` is pipecat's ``LatencyBreakdown``. Duration fields are named
    ``duration_secs`` by pipecat and are passed through unchanged, so consumers
    must not expect ``duration``. Individual ``ttfb`` entries are preserved so
    each measurement can be attributed to its processor.
    """
    return {
        "type": LATENCY_BREAKDOWN_EVENT_TYPE,
        "payload": {
            "schema_version": LATENCY_BREAKDOWN_SCHEMA_VERSION,
            "user_turn_secs": breakdown.user_turn_secs,
            "user_turn_start_time": breakdown.user_turn_start_time,
            "ttfb": [entry.model_dump() for entry in breakdown.ttfb],
            "text_aggregation": (
                breakdown.text_aggregation.model_dump()
                if breakdown.text_aggregation
                else None
            ),
            "function_calls": [call.model_dump() for call in breakdown.function_calls],
        },
    }


def build_pipeline_error_event(
    *,
    error: str,
    fatal: bool,
    processor: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": error,
        "fatal": fatal,
    }
    if processor is not None:
        payload["processor"] = processor
    if extra_payload:
        payload.update(extra_payload)
    return {
        "type": RealtimeFeedbackType.PIPELINE_ERROR.value,
        "payload": payload,
    }


def stamp_realtime_feedback_event(
    event: dict[str, Any],
    *,
    timestamp: str | None = None,
    turn: int | None = None,
    node_id: str | None = None,
    node_name: str | None = None,
) -> dict[str, Any]:
    stamped = dict(event)
    if timestamp is not None:
        stamped["timestamp"] = timestamp
    if turn is not None:
        stamped["turn"] = turn
    if node_id is not None:
        stamped["node_id"] = node_id
    if node_name is not None:
        stamped["node_name"] = node_name
    return stamped


def realtime_feedback_event_sort_key(event: dict[str, Any]) -> str:
    payload_timestamp = (event.get("payload") or {}).get("timestamp")
    return event.get("timestamp") or payload_timestamp or ""
