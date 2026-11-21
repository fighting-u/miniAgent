"""创建、追加和加载单进程使用的会话 JSONL 文件。"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from src.session.session import Session


# 会话 JSONL 目录名，由调用方与根目录拼接后传入。
SESSION_DIR_NAME = "sessions"


# 当前模块的会话存储日志记录器。
logger = logging.getLogger(__name__)

# 当前持久化记录对应的 LLM API 协议格式。
API_FORMAT = "openai_responses"

# 基本 Responses 上下文项类型；会话管理器按配置补充托管工具调用类型，未知类型明确报错。
BASE_MESSAGE_TYPES = frozenset({"message", "reasoning", "function_call", "function_call_output"})


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
                    payload = record.get("payload", {})
                    if payload.get("role") == "user":
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


def load(path: str | Path, *, message_types: frozenset[str] = BASE_MESSAGE_TYPES) -> Session:
    """校验文件并恢复原始上下文，移除附加 metadata，不改写历史。"""
    session = Session(Path(path).resolve())
    with session.path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("ordinal") != session.ordinal:
                    raise ValueError("ordinal 必须从 0 连续递增")
                if record.get("type") != "messages":
                    raise ValueError("当前仅支持恢复 messages，尚不支持 compact 记录")
                turn_id = record.get("turn_id")
                if type(turn_id) is not int or turn_id < 0:
                    raise ValueError("turn_id 必须为非负整数")
                if turn_id not in {session.turn_id, session.turn_id + 1}:
                    raise ValueError("turn_id 必须从 0 开始，保持不变或递增 1")
                payload = record.get("payload")
                if not isinstance(payload, dict) or payload.get("type") not in message_types:
                    raise ValueError("不支持的 payload 类型")
                message = dict(payload)
                metadata = message.pop("metadata", {})
                if not isinstance(metadata, dict) or metadata.get("api_format") != API_FORMAT:
                    raise ValueError("不支持的 api_format")
                # 没有换行的末条记录也拒绝追加，避免与下一条 JSON 粘连。
                if not line.endswith("\n"):
                    raise ValueError("记录末尾缺少换行，可能发生了不完整写入")
                _validate_call(session, message)
                _track_call(session, message)
                session.messages.append(message)
                session.ordinal += 1
                session.turn_id = turn_id
            except (ValueError, TypeError) as error:
                raise ValueError(f"会话文件第 {line_number} 行无效：{error}") from error
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


def _track_call(session: Session, message: dict[str, Any]) -> None:
    """在写入或加载成功后更新待完成工具调用。"""
    if message["type"] == "function_call":
        session.pending_calls[message["call_id"]] = message
    elif message["type"] == "function_call_output":
        del session.pending_calls[message["call_id"]]


def append(
    session: Session, item: Any, model_name: str | None = None, *,
    is_error: bool | None = None, message_types: frozenset[str] = BASE_MESSAGE_TYPES,
) -> None:
    """按 API 原字段追加一条记录，仅模型消息与推理附带模型名称。"""
    if session.turn_id < 0:
        raise ValueError("请先通过 start_turn 开始用户请求")
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
    record = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "ordinal": session.ordinal,
        "type": "messages",
        "payload": omit_none(payload),
        "turn_id": session.turn_id,
    }
    line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
    try:
        if not session.persisted:
            session.path.parent.mkdir(parents=True, exist_ok=True)
        # 首次写入独占创建，避免覆盖同名文件；恢复的空文件也按已有会话追加。
        with session.path.open("a" if session.persisted else "x", encoding="utf-8", newline="\n") as target:
            target.write(line)
            target.flush()
            os.fsync(target.fileno())
    except OSError:
        logger.exception("会话记录写入失败：%s", session.path)
        raise
    if not session.persisted:
        session.persisted = True
        logger.info("首次用户输入已保存，创建会话文件：%s", session.path)
    session.ordinal += 1
    _track_call(session, payload)


def start_turn(session: Session, text: str) -> dict[str, Any]:
    """为真实用户输入开启下一轮并持久化，返回可追加到历史的消息。"""
    if session.pending_calls:
        raise ValueError("开始新请求前，请先处理上次中断的工具调用")
    session.turn_id += 1
    message = {"type": "message", "role": "user", "content": text}
    append(session, message)
    return message


def recover_pending_tools(session: Session) -> list[dict[str, Any]]:
    """为中断且未落盘结果的调用补充未确认说明，不重新执行任何工具。"""
    # 延迟导入以避免 common 与会话管理器循环依赖。
    from src.common.common import to_openai_tool_output
    from src.tools.tool import text_result

    results = []
    for call_id in list(session.pending_calls):
        result = to_openai_tool_output(call_id, text_result("上次运行中断，未保存此工具的执行结果；是否执行及其效果无法确认。请根据当前用户请求重新判断，不要假定工具已成功。", is_error=True))
        append(session, result, is_error=True)
        results.append(result)
    return results
