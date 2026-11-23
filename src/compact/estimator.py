"""上下文 token 估算纯函数，覆盖文本、图片、Responses 项及请求固定开销。"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from PIL import Image, UnidentifiedImageError

from src.prompt.developer import SYSTEM_PROMPT

if TYPE_CHECKING:
    from src.session.session import Session

# 上下文估算诊断日志记录器。
logger = logging.getLogger(__name__)

# 单张图片的 token 估算上限；远程 URL、文件引用或头部异常时也按此上限计。
MAX_IMAGE_TOKENS = 10_000

# 图片尺寸按 32x32 网格向上取整计算 token。
IMAGE_PATCH_SIZE = 32


def estimate_bytes(byte_count: int) -> int:
    """按 UTF-8 字节数向上取整估算 token 数量。"""
    if byte_count <= 0:
        return 0
    return (byte_count + 3) // 4


def estimate_text(text: str) -> int:
    """按文本的 UTF-8 字节长度估算 token 数量。"""
    if not text:
        return 0
    return estimate_bytes(len(text.encode("utf-8")))

# todo:?
def _to_plain(item: Any) -> Any:
    """将 SDK 响应对象转为与持久化一致的 JSON 兼容结构。"""
    return item.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(item, "model_dump") else item


def estimate_image_block(block: dict[str, Any]) -> int:
    """从 Data URL 图片头读取宽高估算单图 token，无法识别时按上限计且不联网。"""
    image_url = block.get("image_url")
    if not isinstance(image_url, str) or not image_url.startswith("data:"):
        logger.warning("图片块缺少本地 Data URL，按单图上限 %d 估算", MAX_IMAGE_TOKENS)
        return MAX_IMAGE_TOKENS
    header, separator, encoded = image_url.partition(",")
    if not separator or ";base64" not in header.lower():
        logger.warning("图片 Data URL 非 base64 编码，按单图上限 %d 估算", MAX_IMAGE_TOKENS)
        return MAX_IMAGE_TOKENS
    try:
        raw = base64.b64decode(encoded)
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
    except (binascii.Error, ValueError, OSError, UnidentifiedImageError) as error:
        logger.warning("无法解析图片头尺寸，按单图上限 %d 估算：%s", MAX_IMAGE_TOKENS, error)
        return MAX_IMAGE_TOKENS
    if width <= 0 or height <= 0:
        logger.warning("图片尺寸无效（%dx%d），按单图上限 %d 估算", width, height, MAX_IMAGE_TOKENS)
        return MAX_IMAGE_TOKENS
    patches = ((width + IMAGE_PATCH_SIZE - 1) // IMAGE_PATCH_SIZE) * ((height + IMAGE_PATCH_SIZE - 1) // IMAGE_PATCH_SIZE)
    return min(MAX_IMAGE_TOKENS, patches)


def _extract_image_tokens_from_blocks(blocks: list[Any]) -> tuple[list[Any], int]:
    """剥离内容块列表中的图片数据并单独累计图片 token，避免重复计算。"""
    sanitized: list[Any] = []
    image_tokens = 0
    for block in blocks:
        plain = _to_plain(block)
        if isinstance(plain, dict) and plain.get("type") == "input_image":
            image_tokens += estimate_image_block(plain)
            sanitized.append({key: value for key, value in plain.items() if key != "image_url"})
        else:
            sanitized.append(plain)
    return sanitized, image_tokens


def estimate_item(item: Any) -> int:
    """估算单个 Responses 上下文项的 token 消耗，兼容 SDK 对象与字典。"""
    plain = _to_plain(item)
    image_tokens = 0
    if isinstance(plain, dict):
        item_type = plain.get("type")
        if (item_type == "message" or (item_type is None and "role" in plain)) and isinstance(plain.get("content"), list):
            content, image_tokens = _extract_image_tokens_from_blocks(plain["content"])
            plain = {**plain, "content": content}
        elif item_type == "function_call_output" and isinstance(plain.get("output"), list):
            output, image_tokens = _extract_image_tokens_from_blocks(plain["output"])
            plain = {**plain, "output": output}
    serialized = json.dumps(plain, ensure_ascii=False, separators=(",", ":"))
    return estimate_text(serialized) + image_tokens


def estimate_items(items: Sequence[Any]) -> int:
    """累加估算一组 Responses 上下文项的 token 数量。"""
    return sum(estimate_item(item) for item in items)


def estimate_schema(tools: Sequence[dict[str, Any]]) -> int:
    """估算请求中工具定义列表的 token 消耗。"""
    if not tools:
        return 0
    return estimate_text(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))


def estimate_context(
    session: Session,
    *,
    tools: Sequence[dict[str, Any]],
) -> int:
    """以最近主响应校准估算，仅对尚未覆盖的历史项重新估算；无有效基准时走全量估算。"""
    token_usage = session.token_usage
    if token_usage.last_total_tokens is not None:
        return token_usage.last_total_tokens + estimate_items(session.messages[token_usage.covered_items:])
    return estimate_text(SYSTEM_PROMPT) + estimate_schema(tools) + estimate_items(session.messages)
