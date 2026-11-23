"""创建、追加和加载单进程使用的会话 JSONL 文件。"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from src.common.common import to_openai_tool_output
from src.compact.constants import SUMMARY_PREFIX
from src.session.session import Session, SessionTokenUsage
from src.tools.tool import text_result


# 会话 JSONL 目录名，由调用方与根目录拼接后传入。
SESSION_DIR_NAME = "sessions"


# 当前模块的会话存储日志记录器。
logger = logging.getLogger(__name__)

# 当前持久化记录对应的 LLM API 协议格式。
API_FORMAT = "openai_responses"

# 基本 Responses 上下文项类型；会话管理器按配置补充托管工具调用类型，未知类型明确报错。
BASE_MESSAGE_TYPES = frozenset({"message", "reasoning", "function_call", "function_call_output"})

# 从文件尾部逆向扫描压缩检查点时的单次二进制分块字节数。
BACKWARD_SCAN_CHUNK_SIZE = 65536

# 允许写入的 token_usage 记录来源类型。
TOKEN_USAGE_SOURCES = frozenset({"main", "compact"})


def omit_none(value: Any) -> Any:
    """递归省略字典中值为 None 的字段，保留列表元素和其他空值。"""
    if isinstance(value, dict):
        return {key: omit_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [omit_none(item) for item in value]
    return value


def create(root: Path, session_id: str = "") -> Session:
    """沿用给定 UUID 或生成 UUIDv7，按本地日期准备会话，不创建目录或文件。"""
    now = datetime.now().astimezone()
    path = root / now.strftime("%Y/%m/%d") / f"{session_id}.jsonl"
    return Session(path.resolve(), persisted=False)


def list_sessions(root: Path) -> list[dict[str, Any]]:
    """按最近写入时间列出会话，仅读取首条用户问题作为列表预览。"""
    sessions = []
    for path in root.glob("*/*/*/*.jsonl"):
        try:
            updated_at = path.stat().st_mtime
            preview = "（空会话）"
            with path.open("r", encoding="utf-8") as source:
                for line in source:
                    record = json.loads(line)
                    if record.get("type") != "messages":
                        continue
                    payload = record.get("payload")
                    if isinstance(payload, dict) and payload.get("role") == "user":
                        content = payload.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(block.get("text", "") for block in content
                                               if isinstance(block, dict))
                        preview = " ".join(str(content).split())
                        preview = preview[:40] + ("…" if len(preview) > 40 else "")
                        break
        except OSError:
            continue
        except (ValueError, AttributeError, TypeError):
            preview = "（记录格式异常）"
        sessions.append({"path": path.resolve(), "updated_at": updated_at, "preview": preview})
    return sorted(sessions, key=lambda item: (item["updated_at"], item["path"].name), reverse=True)


def delete(path: str | Path, root: Path) -> None:
    """删除已确认的单个会话文件，限制在会话日期目录内，不递归删除目录。"""
    target = Path(path).resolve()
    relative = target.relative_to(root.resolve())
    if len(relative.parts) != 4 or target.suffix != ".jsonl" or not target.is_file():
        raise ValueError("只能删除会话日期目录内的 JSONL 文件")
    target.unlink()


def _find_last_compacted_offset(path: Path) -> int | None:
    """从文件尾部按二进制块逆向查找最近一条 compacted 记录的字节偏移。"""
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        position = source.tell()
        if position == 0:
            return None
        tail = b""
        while position > 0:
            read_size = min(BACKWARD_SCAN_CHUNK_SIZE, position)
            position -= read_size
            source.seek(position)
            buffer = source.read(read_size) + tail
            parts = buffer.split(b"\n")
            tail = parts[0]
            offsets: list[tuple[int, bytes]] = []
            cursor = position + len(parts[0]) + 1
            for part in parts[1:]:
                offsets.append((cursor, part))
                cursor += len(part) + 1
            for offset, raw_line in reversed(offsets):
                if not raw_line or b'"compacted"' not in raw_line:
                    continue
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(record, dict) and record.get("type") == "compacted":
                    return offset
        if tail and b'"compacted"' in tail:
            try:
                record = json.loads(tail.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return None
            if isinstance(record, dict) and record.get("type") == "compacted":
                return 0
    return None


def _validate_compacted_payload(payload: Any) -> list[dict[str, Any]]:
    """校验 compacted 检查点的有效历史列表及末项摘要格式。"""
    messages: list[dict[str, Any]] = []
    for item in payload:
        messages.append(dict(item))
    last_content = messages[-1].get("content")
    if not isinstance(last_content, str) or not last_content.startswith(SUMMARY_PREFIX + "\n"):
        raise ValueError("compacted.payload 末项必须为带固定前缀的摘要文本")
    return messages


def _apply_checkpoint(session: Session, record: dict[str, Any]) -> None:
    """校验并应用最近一条 compacted 检查点记录，初始化有效历史与累计用量。"""
    ordinal = record.get("ordinal")
    turn_id = record.get("turn_id")
    metadata = record.get("metadata")
    total_reported = metadata.get("total_reported_tokens")
    messages = _validate_compacted_payload(record.get("payload"))
    session.messages = messages
    session.summary_index = len(messages) - 1
    session.pending_calls = {}
    session.ordinal = ordinal + 1
    session.turn_id = turn_id
    session.token_usage = SessionTokenUsage(total_reported_tokens=total_reported)


def _replay_token_usage(session: Session, payload: Any) -> None:
    """校验并应用 token_usage 记录，更新累计消耗及主响应计数基准。"""
    if not isinstance(payload, dict):
        raise ValueError("token_usage.payload 必须为对象")
    source = payload.get("source")
    if source not in TOKEN_USAGE_SOURCES:
        raise ValueError(f"不支持的 token_usage 来源：{source}")
    total_tokens = payload.get("total_tokens")
    if type(total_tokens) is not int or total_tokens < 0:
        raise ValueError("token_usage.total_tokens 必须为非负整数")
    session.token_usage.total_reported_tokens += total_tokens
    if source == "main":
        session.token_usage.last_total_tokens = total_tokens
        session.token_usage.covered_items = len(session.messages)


def load(path: str | Path, *, message_types: frozenset[str] = BASE_MESSAGE_TYPES) -> Session:
    """校验文件并从最近检查点（或文件头）恢复有效上下文，移除附加 metadata，不改写历史。"""
    session = Session(Path(path).resolve())
    checkpoint_offset = _find_last_compacted_offset(session.path)
    with session.path.open("rb") as source:
        if checkpoint_offset is not None:
            source.seek(checkpoint_offset)
            raw_checkpoint = source.readline()
            try:
                if not raw_checkpoint.endswith(b"\n"):
                    raise ValueError("记录末尾缺少换行，可能发生了不完整写入")
                _apply_checkpoint(session, json.loads(raw_checkpoint.decode("utf-8")))
            except (UnicodeDecodeError, ValueError, TypeError) as error:
                raise ValueError(f"会话文件检查点无效：{error}") from error

        line_offset = 1
        for raw_line in source:
            try:
                # 没有换行的末条记录也拒绝追加，避免与下一条 JSON 粘连。
                if not raw_line.endswith(b"\n"):
                    raise ValueError("记录末尾缺少换行，可能发生了不完整写入")
                record = json.loads(raw_line.decode("utf-8"))
                if not isinstance(record, dict) or record.get("ordinal") != session.ordinal:
                    raise ValueError("ordinal 必须连续递增")
                turn_id = record.get("turn_id")
                if type(turn_id) is not int or turn_id < 0:
                    raise ValueError("turn_id 必须为非负整数")
                if turn_id not in {session.turn_id, session.turn_id + 1}:
                    raise ValueError("turn_id 必须保持不变或递增 1")
                record_type = record.get("type")
                if record_type == "messages":
                    payload = record.get("payload")
                    if not isinstance(payload, dict) or payload.get("type") not in message_types:
                        raise ValueError("不支持的 payload 类型")
                    message = dict(payload)
                    metadata = message.pop("metadata", {})
                    if not isinstance(metadata, dict) or metadata.get("api_format") != API_FORMAT:
                        raise ValueError("不支持的 api_format")
                    _validate_call(session, message)
                    _update_pending_tool_calls(session, message)
                    session.messages.append(message)
                elif record_type == "token_usage":
                    _replay_token_usage(session, record.get("payload"))
                else:
                    raise ValueError(f"不支持的记录类型：{record_type}")
                session.ordinal += 1
                session.turn_id = turn_id
            except (UnicodeDecodeError, ValueError, TypeError) as error:
                prefix = f"检查点后第 {line_offset} 行" if checkpoint_offset is not None else f"第 {line_offset} 行"
                raise ValueError(f"会话文件{prefix}无效：{error}") from error
            line_offset += 1
    return session


def _validate_call(session: Session, message: dict[str, Any]) -> None:
    """检查工具调用与输出的 call_id，防止恢复出无法配对的上下文。"""
    if message["type"] not in {"function_call", "function_call_output"}:
        return
    call_id = message.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("工具记录缺少有效 call_id")
    if message["type"] == "function_call" and call_id in session.pending_calls:
        raise ValueError(f"重复的工具调用：{call_id}")
    if message["type"] == "function_call_output" and call_id not in session.pending_calls:
        raise ValueError(f"工具输出没有对应调用：{call_id}")


def _update_pending_tool_calls(session: Session, message: dict[str, Any]) -> None:
    """在写入或加载成功后更新待完成工具调用。"""
    if message["type"] == "function_call":
        session.pending_calls[message["call_id"]] = message
    elif message["type"] == "function_call_output":
        del session.pending_calls[message["call_id"]]


def _write_records(session: Session, records: list[dict[str, Any]]) -> None:
    """序列化多条 JSONL 记录，一次打开文件写入并只 fsync 一次，落盘成功后更新 persisted 与 ordinal。"""
    content = "".join(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n" for record in records)
    try:
        if not session.persisted:
            session.path.parent.mkdir(parents=True, exist_ok=True)
        # 首次写入独占创建，避免覆盖同名文件；恢复的空文件也按已有会话追加。
        with session.path.open("a" if session.persisted else "x", encoding="utf-8", newline="\n") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
    except OSError:
        logger.exception("会话记录写入失败：%s 条数=%s", session.path, len(records))
        raise
    if not session.persisted:
        session.persisted = True
        logger.info("首次用户输入已保存，创建会话文件：%s", session.path)
    session.ordinal += len(records)


def append_items(
    session: Session, items: list[Any], model_name: str | None = None, *,
    is_error: bool | None = None, message_types: frozenset[str] = BASE_MESSAGE_TYPES,
) -> None:
    """按 API 原字段批量追加记录，整批校验通过后一次落盘；仅模型消息与推理附带模型名称。"""
    if session.turn_id < 0:
        raise ValueError("请先通过 persist_user_message 写入用户输入并开启新轮次")
    timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    records = []
    for index, item in enumerate(items):
        if hasattr(item, "model_dump"):
            payload = item.model_dump(mode="json", by_alias=True, exclude_none=True)
        elif isinstance(item, dict):
            payload = dict(item)
        else:
            raise TypeError("会话项必须为字典或 SDK 响应对象")
        if "type" not in payload and "role" in payload:
            payload["type"] = "message"
        if payload.get("type") not in message_types:
            raise ValueError(f"不支持的会话项类型：{payload.get('type')}")
        _validate_call(session, payload)
        metadata: dict[str, Any] = {"api_format": API_FORMAT}
        if is_error is not None:
            metadata["is_error"] = is_error
        if model_name is not None and payload["type"] in {"message", "reasoning"}:
            metadata["model_name"] = model_name
        payload["metadata"] = metadata
        records.append({
            "timestamp": timestamp,
            "ordinal": session.ordinal + index,
            "turn_id": session.turn_id,
            "type": "messages",
            "payload": omit_none(payload),
        })
    _write_records(session, records)
    # 与单条写入一致：落盘成功后再更新待完成工具调用。
    for record in records:
        _update_pending_tool_calls(session, record["payload"])


def append_token_usage(
    session: Session,
    *,
    source: str,
    total_tokens: int,
) -> None:
    """持久化单次主请求或失败摘要请求的实际 token 用量。"""
    payload: dict[str, Any] = {"source": source, "total_tokens": total_tokens}
    record = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "ordinal": session.ordinal,
        "turn_id": session.turn_id,
        "type": "token_usage",
        "payload": payload,
    }
    _write_records(session, [record])


def append_compacted(
    session: Session,
    replacement: list[dict[str, Any]],
    *,
    summary_tokens: int | None,
    total_reported_tokens: int,
) -> None:
    """校验并持久化一条 compacted 检查点替换记录。"""
    if session.turn_id < 0:
        raise ValueError("提交压缩检查点前必须先开始用户请求")
    if session.pending_calls:
        raise ValueError("存在未完成的工具调用，无法提交压缩检查点")
    if type(total_reported_tokens) is not int or total_reported_tokens < 0:
        raise ValueError("total_reported_tokens 必须为非负整数")
    if summary_tokens is not None and (type(summary_tokens) is not int or summary_tokens < 0):
        raise ValueError("summary_tokens 必须为非负整数")
    validated = _validate_compacted_payload(replacement)
    metadata: dict[str, Any] = {
        "api_format": API_FORMAT,
        "total_reported_tokens": total_reported_tokens,
    }
    if summary_tokens is not None:
        metadata["summary_tokens"] = summary_tokens
    record = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "ordinal": session.ordinal,
        "turn_id": session.turn_id,
        "type": "compacted",
        "payload": omit_none(validated),
        "metadata": metadata,
    }
    _write_records(session, [record])


def persist_user_message(session: Session, text: str) -> dict[str, Any]:
    """递增 turn_id 并持久化真实用户输入，返回可追加到内存历史的消息。"""
    if session.pending_calls:
        raise ValueError("写入新用户输入前，请先处理上次中断的工具调用")
    session.turn_id += 1
    message = {"type": "message", "role": "user", "content": text}
    append_items(session, [message])
    return message


def recover_pending_tools(session: Session) -> list[dict[str, Any]]:
    """为中断且未落盘结果的调用补充未确认说明并一次批量落盘，不重新执行任何工具。"""
    results = [
        to_openai_tool_output(call_id, text_result("上次运行中断，未保存此工具的执行结果；是否执行及其效果无法确认。请根据当前用户请求重新判断，不要假定工具已成功。", is_error=True))
        for call_id in session.pending_calls
    ]
    append_items(session, results, is_error=True)
    return results
