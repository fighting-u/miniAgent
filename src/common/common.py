from typing import Any

from src.channels.base import InboundMessage, MessageDispatcher
from src.tools.tool import TextContent, ToolResult
from src.session.session import Session
from src.session.session_manager import SessionManager, serializable


def to_openai_tool_output(call_id: str, result: ToolResult) -> dict[str, Any]:
    """把自定义的统一工具结果规范转换为 Responses api格式的工具输出。"""
    output: str | list[dict[str, str]]
    if result.is_error:
        reason = next(block.text for block in result.content if isinstance(block, TextContent))
        output = f"工具执行失败，错误信息：{reason}"
    elif not result.content:
        output = ""
    elif len(result.content) == 1 and isinstance(result.content[0], TextContent):
        output = result.content[0].text
    else:
        output = []
        for block in result.content:
            if isinstance(block, TextContent):
                output.append({"type": "input_text", "text": block.text})
            else:
                output.append({"type": "input_image", "image_url": f"data:{block.mime_type};base64,{block.data}"})
    return {"type": "function_call_output", "call_id": call_id, "output": output}


async def persist_and_dispatch_items(
    inbound_message: InboundMessage, session: Session, items: list[Any],
    session_manager: SessionManager, message_dispatcher: MessageDispatcher,
    *, model_name: str | None = None, is_error: bool | None = None, output_preview: str | None = None,
) -> None:
    """按原顺序落盘消息条目，再序列化并发送给来源通道。"""
    for item in items:
        session_manager.append(session, item, model_name=model_name, is_error=is_error)
    await message_dispatcher.send(
        inbound_message, {"type": "items", "items": [{**item, "output": output_preview} if output_preview is not None else serializable(item) for item in items]},
    )
