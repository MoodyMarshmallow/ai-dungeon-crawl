import json

from pydantic_ai.messages import (
    PartStartEvent, PartDeltaEvent, TextPart, ThinkingPart, ToolCallPart,
    TextPartDelta, ThinkingPartDelta, ToolCallPartDelta,
)
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.exceptions import UnexpectedModelBehavior

from ..events import emit


class ObservedModel(WrapperModel):
    """Stream display events, then return the whole response for normal validation.

    Streaming never accepts or executes a partial tool call. Each invocation,
    including a repair, is a separate model request in the activity log.
    Only public text, reasoning content and tool arguments are exposed; never
    provider metadata, encrypted reasoning, signatures or credentials.
    """

    async def request(self, messages, model_settings, model_request_parameters):
        emit("model.started")
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters,
        ) as stream:
            async for event in stream:
                if isinstance(event, PartStartEvent):
                    part = event.part
                    if isinstance(part, (TextPart, ThinkingPart)):
                        emit("model.part", index=event.index,
                             kind="reasoning" if isinstance(part, ThinkingPart) else "text",
                             text=part.content, replace=True)
                    elif isinstance(part, ToolCallPart):
                        emit("model.part", index=event.index, kind="tool", name=part.tool_name,
                             text=part.args if isinstance(part.args, str) else json.dumps(part.args) if part.args else "",
                             replace=True)
                elif isinstance(event, PartDeltaEvent):
                    delta = event.delta
                    if isinstance(delta, (TextPartDelta, ThinkingPartDelta)):
                        if delta.content_delta:
                            emit("model.part", index=event.index,
                                 kind="reasoning" if isinstance(delta, ThinkingPartDelta) else "text",
                                 text=delta.content_delta, replace=False)
                    elif isinstance(delta, ToolCallPartDelta):
                        emit("model.part", index=event.index, kind="tool",
                             name=delta.tool_name_delta or "",
                             text=delta.args_delta or "", replace=False)
            response = stream.get()
        if response.finish_reason in {"length", "content_filter", "error"}:
            raise UnexpectedModelBehavior("Model stream did not complete; no script will run")
        emit("model.finished", finish_reason=response.finish_reason,
             input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens)
        return response
