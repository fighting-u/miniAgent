from memory import memory_catalog
from skill import SKILL_LOADER
from tools import WORKDIR


def build_system_prompt(relevant_memories: str = "") -> str:
    """构建系统提示词，注入技能目录、记忆目录和本轮自动召回内容。"""
    recalled_section = (
        f"\n\n与本轮请求相关的已召回记忆：\n{relevant_memories}"
        if relevant_memories
        else ""
    )
    return (
        f"你是一个运行在 Windows 环境，命令行工具为PowerShell的智能体。当前工作目录为 {WORKDIR}。\n"
        "三步以上的复杂任务或者一次提出多个任务时，建议使用 update_plan 工具先创建计划。\n\n"
        f"可用技能：\n{SKILL_LOADER.catalog()}\n\n"
        "当某项技能适用于当前任务时，先使用 load_skill 工具读取它的完整说明，再按照说明执行。\n"
        f"\n可用长期记忆：\n{memory_catalog()}\n\n"
        "当记忆目录中的某条摘要可能有助于当前任务时，使用 load_memory 工具按文件名读取完整内容。\n"
        "当历史或工具结果已转存到本地时，只按需要使用关键词检索或分段读取相关片段，不要一次读回完整大文件。\n"
        "当你认为上下文较长并且大部分信息不再需要时，可以调用 compact 工具主动压缩上下文。"
        f"{recalled_section}"
    )
