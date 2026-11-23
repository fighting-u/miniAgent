"""主模型的固定指令和运行时 developer 消息模板。"""

# 每次主模型请求共用的系统指令。
SYSTEM_PROMPT = "你是一个运行在 Windows 环境、使用 PowerShell 命令行工具的智能体。"

# 首次创建会话和压缩后置于历史首条的环境与技能说明。
SESSION_PROMPT = (
    "当前工作目录为 {cwd}。\n"
    "文件工具允许访问以下目录及其子目录：{allowed_dirs}。相对路径仍以当前工作目录为基准。\n"
    "三步以上的复杂任务或者一次提出多个任务时，建议使用 update_plan 工具先创建计划。\n\n"
    "可用技能：\n{skills}\n\n"
    "当某项技能适用于当前任务时，先使用 load_skill 工具读取它的完整说明，再按照说明执行。"
)

# 开启长期记忆时紧跟环境说明的目录快照。
MEMORY_CATALOG_PROMPT = (
    "可用长期记忆：\n{catalog}\n\n"
    "当记忆目录中的某条摘要可能有助于当前任务时，使用 load_memory 工具按文件名读取完整内容。"
)

# 自动召回非空时，在本轮用户消息前提供的记忆正文。
RECALLED_MEMORY_PROMPT = "与本轮请求相关的已召回记忆：\n{memories}"

# 连续多批工具调用尚未建立计划时的提醒。
CREATE_PLAN_REMINDER = (
    "系统提醒：当前已连续进行 {interval} 批工具调用，但尚未创建任务计划。"
    "如果当前任务复杂或步骤较多，请使用 update_plan 创建计划。"
)

# 已有计划但连续多批工具调用没有更新时的提醒。
UPDATE_PLAN_REMINDER = (
    "系统提醒：已连续 {interval} 批工具调用未更新任务计划。"
    "请核对实际进度，如有变化请使用 update_plan 更新计划。\n当前计划：{plan}"
)
