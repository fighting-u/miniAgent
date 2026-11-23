"""本地上下文摘要压缩器，负责阈值检测、超窗裁剪与用户原话保留。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from openai import APIStatusError

from src.common.common import extract_total_tokens, truncate_middle_to_budget
from src.prompt.compact import COMPACT_PROMPT, SUMMARY_PREFIX
from src.compact.estimator import estimate_context, estimate_text
from src.models.registry import ModelInfo

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from src.config import Config
    from src.session.session import Session
    from src.session.session_manager import SessionManager

# 上下文压缩诊断日志记录器。
logger = logging.getLogger(__name__)

# 纯图片用户消息在压缩重建时的占位文本，避免兼容端点拒绝空内容 user 消息。
OMITTED_IMAGE_PLACEHOLDER = "[历史图片已省略]"

# 服务端明确表示上下文超窗的错误码集合。
CONTEXT_LENGTH_ERROR_CODES = frozenset({"context_length_exceeded", "string_above_max_length"})

# 兼容端点在 HTTP 400 下可能返回的超窗描述关键字（小写匹配）。
CONTEXT_LENGTH_ERROR_PHRASES = (
    "context_length_exceeded",
    "maximum context length",
    "context length",
    "range of input length",
    "input is too long",
    "too many tokens",
)


def _item_attr(item: Any, name: str, default: Any = None) -> Any:
    """统一读取字典项或 SDK 对象的属性。"""
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def is_context_length_error(error: APIStatusError) -> bool:
    """判断 APIStatusError 是否为上下文长度超限错误，排除鉴权、限流和服务端异常。"""
    codes: list[str] = []
    messages: list[str] = [str(error)]
    if isinstance(error.code, str):
        codes.append(error.code)
    body = error.body
    if isinstance(body, dict):
        if isinstance(body.get("code"), str):
            codes.append(body["code"])
        if isinstance(body.get("message"), str):
            messages.append(body["message"])
        nested = body.get("error")
        if isinstance(nested, dict):
            if isinstance(nested.get("code"), str):
                codes.append(nested["code"])
            if isinstance(nested.get("message"), str):
                messages.append(nested["message"])
    if any(code in CONTEXT_LENGTH_ERROR_CODES for code in codes):
        return True
    if error.status_code == 400:
        combined = " ".join(messages).lower()
        return any(phrase in combined for phrase in CONTEXT_LENGTH_ERROR_PHRASES)
    return False


def remove_oldest_unit(compaction_messages: list[Any]) -> int:
    """从工作副本删除最旧的一个历史单元（含配对 call_id），若删除后无剩余历史则不修改并返回 0。"""
    history_len = len(compaction_messages) - 1  # 末项固定为本次摘要指令。
    if history_len <= 1:
        return 0
    first = compaction_messages[0]
    if _item_attr(first, "type") == "function_call":
        call_id = _item_attr(first, "call_id")
        for index in range(1, history_len):
            candidate = compaction_messages[index]
            if _item_attr(candidate, "type") == "function_call_output" and _item_attr(candidate, "call_id") == call_id:
                # 至少保留最后一个有内容的历史单元加摘要指令作为最小有效请求。
                if history_len == 2:
                    return 0
                del compaction_messages[index]
                del compaction_messages[0]
                return 2
    del compaction_messages[0]
    return 1


def _extract_user_message(item: Any) -> tuple[str, dict[str, Any]] | None:
    """从真实 user 消息提取用于预算计算的文本和保留形态，非 user 项返回 None。"""
    item_type = _item_attr(item, "type")
    if item_type not in {None, "message"} or _item_attr(item, "role") != "user":
        return None
    content = _item_attr(item, "content")
    if isinstance(content, str):
        return content, {"type": "message", "role": "user", "content": content}
    if isinstance(content, list):
        text_blocks: list[dict[str, Any]] = []
        texts: list[str] = []
        for block in content:
            plain = block.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(block, "model_dump") else block
            if isinstance(plain, dict) and plain.get("type") != "input_image" and isinstance(plain.get("text"), str):
                text_blocks.append(dict(plain))
                texts.append(plain["text"])
        if not text_blocks:
            return OMITTED_IMAGE_PLACEHOLDER, {"type": "message", "role": "user", "content": OMITTED_IMAGE_PLACEHOLDER}
        return "\n".join(texts), {"type": "message", "role": "user", "content": text_blocks}
    return None


def retain_user_messages(original: Sequence[Any], summary_index: int | None, budget_tokens: int) -> list[dict[str, Any]]:
    """从原历史末尾向前按预算选取真实用户消息，排除旧摘要并恢复时间顺序。"""
    kept: list[dict[str, Any]] = []
    remaining = budget_tokens
    for index in range(len(original) - 1, -1, -1):
        if index == summary_index or remaining <= 0:
            continue
        extracted = _extract_user_message(original[index])
        if extracted is None:
            continue
        text, preserved = extracted
        tokens = estimate_text(text)
        if tokens <= remaining:
            kept.append(preserved)
            remaining -= tokens
            if remaining <= 0:
                break
        else:
            truncated = truncate_middle_to_budget(text, remaining)
            if truncated is not None:
                kept.append({"type": "message", "role": "user", "content": truncated})
            break
    kept.reverse()
    return kept


class ContextCompactor:
    """全局共享单个实例，封装自动检测与手动压缩；工作副本与窗口阈值均按会话动态计算。"""

    def __init__(self, client: AsyncOpenAI, config: Config, session_manager: SessionManager) -> None:
        """接收共享依赖，初始化压缩后保留用户原文的 token 预算。"""
        self.client = client
        self.config = config
        self.session_manager = session_manager
        self.keep_user_tokens = config.compact_keep_user_tokens  # 所有会话共用的用户原文保留预算。

    def _window_policy(self, session: Session) -> tuple[ModelInfo, int, int]:
        """按会话模型计算有效窗口和自动压缩阈值，供自动检测使用。"""
        model_info = session.model_info
        if model_info is None:
            raise ValueError(f"会话 {session.id} 未设置 model_info，无法执行上下文压缩。")
        effective_window = model_info.context_window * self.config.effective_window_percent // 100
        limit = model_info.context_window * self.config.auto_compact_percent // 100
        return model_info, effective_window, limit

    def format_usage_summary(self, session: Session, tokens_before_turn: int) -> str:
        """根据轮前累计值计算本轮消耗，有窗口用量时一并展示。"""
        total = session.token_usage.total_reported_tokens
        token_text = f"Token 消耗：本次 {total - tokens_before_turn:,} · 会话累计 {total:,}"
        used = session.token_usage.last_total_tokens
        if used is None:
            return token_text
        _, effective_window, _ = self._window_policy(session)
        used_text = str(used) if used < 1000 else f"{used / 1000:.0f}k"
        window_text = f"上下文窗口使用情况：已用约 {used / effective_window:.0%}（{used_text} / {effective_window / 1000:.0f}k tokens）"
        return f"{window_text}\n{token_text}"

    async def maybe_compact(
        self,
        session: Session,
        *,
        phase: str,
        tools: Sequence[dict[str, Any]],
        on_status: Callable[[str], Awaitable[None]] | None = None,
    ) -> bool:
        """供轮前和轮内调用，在非空历史达到阈值时安装一次摘要并通知状态。"""
        if not session.persisted or not session.messages:
            return False
        model_info, effective_window, limit = self._window_policy(session)
        estimated = estimate_context(session, tools=tools)
        if estimated < limit:
            return False
        logger.info(
            "触发自动上下文压缩：session=%s model=%s phase=%s estimated=%d limit=%d effective_window=%d",
            session.id,
            model_info.name,
            phase,
            estimated,
            limit,
            effective_window,
        )
        if on_status is not None:
            await on_status("上下文接近窗口上限，正在自动压缩...")
        await self.compact(session, phase=phase)
        if on_status is not None:
            await on_status("上下文已自动压缩。")
        return True

    async def compact(self, session: Session, *, phase: str) -> None:
        """从同一份原历史生成摘要和保留项，待磁盘提交成功后切换窗口。"""
        original = list(session.messages)
        compaction_messages: list[Any] = [*original, {"type": "message", "role": "user", "content": COMPACT_PROMPT}]
        dropped_items = 0
        # codex就这样做的，直接先请求报错了才移除，并且每次只移除1项；并不会先移除到预估不会超窗后再请求。
        # 猜测原因1.本来配置的最大窗口就不是真的最大窗口；2.预估不准确，尽量多地保留
        while True:
            try:
                response = await self.client.responses.create(model=session.model_info.name, input=compaction_messages)
                break
            except APIStatusError as error:
                if not is_context_length_error(error):
                    logger.warning("摘要请求失败（非超窗错误）：session=%s phase=%s error=%s", session.id, phase, error)
                    raise
                removed = remove_oldest_unit(compaction_messages)
                if not removed:
                    # todo: 配合ui进行提示
                    logger.error("摘要请求裁剪至最小历史单元后仍超窗：session=%s phase=%s dropped_items=%d", session.id, phase, dropped_items)
                    raise
                dropped_items += removed
                logger.warning(
                    "摘要请求超窗，已裁剪最旧历史单元并重试：session=%s phase=%s removed=%d total_dropped=%d remaining_history=%d",
                    session.id,
                    phase,
                    removed,
                    dropped_items,
                    len(compaction_messages) - 1,
                )

        summary_text = (response.output_text or "").strip()
        if response.status != "completed" or not summary_text:
            self.session_manager.record_usage(session, "compact", response.usage)
            logger.error("摘要响应未完成或正文为空：session=%s phase=%s status=%s", session.id, phase, response.status)
            raise ValueError("压缩模型未返回完整的文本摘要，已保留原上下文。")

        kept = retain_user_messages(original, session.summary_index, self.keep_user_tokens)
        summary = {"type": "message", "role": "user", "content": f"{SUMMARY_PREFIX}\n{summary_text}"}
        self.session_manager.install_compacted(session, [*kept, summary], extract_total_tokens(response.usage))
        logger.info(
            "上下文压缩完成：session=%s model=%s phase=%s original_items=%d dropped_items=%d kept_user_items=%d checkpoint_ordinal=%d",
            session.id,
            session.model_info.name,
            phase,
            len(original),
            dropped_items,
            len(kept),
            session.ordinal - 1,
        )
