"""集中维护可选模型的上下文窗口等静态元信息。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    """声明单个模型的上下文窗口等静态元信息。"""

    name: str  # 模型名称，与 config.yaml 中的 model 字段一致。
    context_window: int  # 模型原始上下文窗口 token 上限。


# 已声明的模型静态元信息注册表；新增可选模型时在此补充。
MODELS: dict[str, ModelInfo] = {
    "qwen3.7-flash": ModelInfo(name="qwen3.7-flash", context_window=272_000),
}


def get_model_info(model: str) -> ModelInfo:
    """按模型名查找静态元信息，未声明时显式报错。"""
    info = MODELS.get(model)
    if info is None:
        raise ValueError(f"模型 {model!r} 未在 src/models/registry.py 中注册，请补充其上下文窗口声明。")
    return info
