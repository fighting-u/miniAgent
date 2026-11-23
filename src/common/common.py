import logging
from typing import Any

from src.tools.tool import TextContent, ToolResult


# 记录模型响应的用量字段异常。
logger = logging.getLogger(__name__)


def extract_total_tokens(usage: Any) -> int | None:
    """从响应 usage 对象或字典中读取 total_tokens，缺失或无效时返回 None。"""
    if usage is None:
        logger.error("模型响应缺少 usage，无法记录本次 token 用量。")
        return None
    total = usage.get("total_tokens") if isinstance(usage, dict) else getattr(usage, "total_tokens", None)
    if type(total) is int and total >= 0:
        return total
    logger.error("模型响应的 usage.total_tokens 缺失或无效：%r", total)
    return None


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
