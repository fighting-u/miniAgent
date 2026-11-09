import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from uuid6 import uuid7


# 会话文件保存到项目根目录，不受启动命令的当前目录影响。
SESSION_DIR = Path(__file__).resolve().parent / "sessions"

# 当前持久化记录对应的 LLM API 协议格式。
API_FORMAT = "openai_responses"

# 本阶段支持的 Responses 上下文项类型，未知类型明确报错。
MESSAGE_TYPES = {"message", "reasoning", "function_call", "function_call_output"}


def omit_none(value: Any) -> Any:
    """递归省略字典中值为 None 的字段，保留列表元素和其他空值。"""
    if isinstance(value, dict):
        return {key: omit_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [omit_none(item) for item in value]
    return value


class SessionStore:
    """创建、追加和加载单进程使用的会话 JSONL 文件。"""

    def __init__(self, path: Path, ordinal: int = 0, turn_id: int = -1) -> None:
        """保存文件路径、下一条序号和最近一次用户请求序号。"""
        self.path = path
        self.ordinal = ordinal
        self.turn_id = turn_id
        # 尚未收到结果的工具调用；用于中断恢复，绝不自动重放工具。
        self.pending_calls: dict[str, dict[str, Any]] = {}

    @classmethod
    def create(cls, root: Path = SESSION_DIR) -> "SessionStore":
        """按本地创建日期和 UUIDv7 创建空文件，不覆盖已有会话。"""
        now = datetime.now().astimezone()
        path = root / now.strftime("%Y/%m/%d") / f"{uuid7()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8"):
            pass
        return cls(path.resolve())

    @classmethod
    def list_sessions(cls, root: Path = SESSION_DIR) -> list[dict[str, Any]]:
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

    @classmethod
    def delete(cls, path: str | Path, root: Path = SESSION_DIR) -> None:
        """删除已确认的单个会话文件，限制在会话日期目录内，不递归删除目录。"""
        target = Path(path).resolve()
        relative = target.relative_to(root.resolve())
        if len(relative.parts) != 4 or target.suffix != ".jsonl" or not target.is_file():
            raise ValueError("只能删除会话日期目录内的 JSONL 文件")
        target.unlink()

    @classmethod
    def load(cls, path: str | Path) -> tuple["SessionStore", list[dict[str, Any]]]:
        """校验文件并恢复原始上下文，移除附加 metadata，不改写历史。"""
        store = cls(Path(path).resolve())
        messages = []
        with store.path.open("r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict) or record.get("ordinal") != store.ordinal:
                        raise ValueError("ordinal 必须从 0 连续递增")
                    if record.get("type") != "messages":
                        raise ValueError("当前仅支持恢复 messages，尚不支持 compact 记录")
                    turn_id = record.get("turn_id")
                    if type(turn_id) is not int or turn_id < 0:
                        raise ValueError("turn_id 必须为非负整数")
                    if turn_id not in {store.turn_id, store.turn_id + 1}:
                        raise ValueError("turn_id 必须从 0 开始，保持不变或递增 1")
                    payload = record.get("payload")
                    if not isinstance(payload, dict) or payload.get("type") not in MESSAGE_TYPES:
                        raise ValueError("不支持的 payload 类型")
                    message = dict(payload)
                    metadata = message.pop("metadata", {})
                    if not isinstance(metadata, dict) or metadata.get("api_format") != API_FORMAT:
                        raise ValueError("不支持的 api_format")
                    # 没有换行的末条记录也拒绝追加，避免与下一条 JSON 粘连。
                    if not line.endswith("\n"):
                        raise ValueError("记录末尾缺少换行，可能发生了不完整写入")
                    store._validate_call(message)
                    store._track_call(message)
                    messages.append(message)
                    store.ordinal += 1
                    store.turn_id = turn_id
                except (ValueError, TypeError) as error:
                    raise ValueError(f"会话文件第 {line_number} 行无效：{error}") from error
        return store, messages

    def _validate_call(self, message: dict[str, Any]) -> None:
        """检查工具调用与输出的 call_id，防止恢复出无法配对的上下文。"""
        if message["type"] not in {"function_call", "function_call_output"}:
            return
        call_id = message.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("工具记录缺少有效 call_id")
        if message["type"] == "function_call" and call_id in self.pending_calls:
            raise ValueError(f"重复的工具调用：{call_id}")
        if message["type"] == "function_call_output" and call_id not in self.pending_calls:
            raise ValueError(f"工具输出没有对应调用：{call_id}")

    def _track_call(self, message: dict[str, Any]) -> None:
        """在写入或加载成功后更新待完成工具调用。"""
        if message["type"] == "function_call":
            self.pending_calls[message["call_id"]] = message
        elif message["type"] == "function_call_output":
            del self.pending_calls[message["call_id"]]

    def append(self, item: Any, model_name: str | None = None) -> None:
        """按 API 原字段追加一条记录，仅模型消息与推理附带模型名称。"""
        if self.turn_id < 0:
            raise ValueError("请先通过 start_turn 开始用户请求")
        if hasattr(item, "model_dump"):
            payload = item.model_dump(mode="json", by_alias=True, exclude_none=True)
        elif isinstance(item, dict):
            payload = dict(item)
        else:
            raise TypeError("会话项必须为字典或 SDK 响应对象")
        if "type" not in payload and "role" in payload:
            payload["type"] = "message"
        if payload.get("type") not in MESSAGE_TYPES:
            raise ValueError(f"不支持的会话项类型：{payload.get('type')}")
        self._validate_call(payload)
        metadata = {"api_format": API_FORMAT}
        if model_name is not None and payload["type"] in {"message", "reasoning"}:
            metadata["model_name"] = model_name
        payload["metadata"] = metadata
        record = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "ordinal": self.ordinal,
            "type": "messages",
            "payload": omit_none(payload),
            "turn_id": self.turn_id,
        }
        line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as target:
            target.write(line)
            target.flush()
            os.fsync(target.fileno())
        self.ordinal += 1
        self._track_call(payload)

    def start_turn(self, text: str) -> dict[str, Any]:
        """为真实用户输入开启下一轮并持久化，返回可追加到历史的消息。"""
        if self.pending_calls:
            raise ValueError("开始新请求前，请先处理上次中断的工具调用")
        self.turn_id += 1
        message = {"type": "message", "role": "user", "content": text}
        self.append(message)
        return message

    def recover_pending_tools(self) -> list[dict[str, Any]]:
        """为中断且未落盘结果的调用补充未确认说明，不重新执行任何工具。"""
        results = []
        for call_id in list(self.pending_calls):
            result = {
                "type": "function_call_output",
                "call_id": call_id,
                "output": "上次运行中断，未保存此工具的执行结果；是否执行及其效果无法确认。请根据当前用户请求重新判断，不要假定工具已成功。",
            }
            self.append(result)
            results.append(result)
        return results
