import logging
from typing import Any

from openai.types.responses import Response

from src.compact.estimator import estimate_text
from src.tools.tool import TextContent, ToolResult


# 记录模型响应的用量字段异常。
logger = logging.getLogger(__name__)


def log_llm_usage(purpose: str, response: Response) -> None:
    """逐笔记录模型返回的 token 用量，保留服务端提供的各项明细。"""
    usage = response.usage
    details = usage.model_dump_json(exclude_none=True, exclude_unset=True) if usage is not None else "服务端未返回用量"
    logger.info("LLM 用量 | 用途=%s model=%s usage=%s", purpose, response.model, details)


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


def _take_prefix_by_bytes(text: str, max_bytes: int) -> str:
    """沿 Unicode 字符边界截取不超过指定 UTF-8 字节数的前缀。"""
    if max_bytes <= 0:
        return ""
    used = 0
    chars: list[str] = []
    for char in text:
        size = len(char.encode("utf-8"))
        if used + size > max_bytes:
            break
        chars.append(char)
        used += size
    return "".join(chars)


def _take_suffix_by_bytes(text: str, max_bytes: int) -> str:
    """沿 Unicode 字符边界截取不超过指定 UTF-8 字节数的后缀。"""
    if max_bytes <= 0:
        return ""
    used = 0
    chars: list[str] = []
    for char in reversed(text):
        size = len(char.encode("utf-8"))
        if used + size > max_bytes:
            break
        chars.append(char)
        used += size
    chars.reverse()
    return "".join(chars)


def truncate_middle_to_budget(text: str, budget_tokens: int) -> str | None:
    """按 token 预算保留文本首尾各占预算的二分之一，并插入中文省略标记；连标记都放不下时返回 None。"""
    if budget_tokens <= 0:
        return None
    if estimate_text(text) <= budget_tokens:
        return text
    total_bytes = len(text.encode("utf-8"))
    max_bytes = budget_tokens * 4
    probe_marker = f"…中间已省略 {total_bytes} 字节…"
    marker_bytes = len(probe_marker.encode("utf-8"))
    if marker_bytes > max_bytes:
        return None
    available_bytes = max_bytes - marker_bytes
    head_bytes = available_bytes // 2
    tail_bytes = available_bytes - head_bytes
    head = _take_prefix_by_bytes(text, head_bytes)
    tail = _take_suffix_by_bytes(text, tail_bytes)
    omitted_bytes = max(0, total_bytes - len(head.encode("utf-8")) - len(tail.encode("utf-8")))
    result = f"{head}…中间已省略 {omitted_bytes} 字节…{tail}"
    return result if estimate_text(result) <= budget_tokens else None
