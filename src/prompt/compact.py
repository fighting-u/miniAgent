# Codex 原版摘要指令，仅用于本次摘要请求，不写入会话历史。
COMPACT_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work.
"""

# 摘要指令的中文意译。
COMPACT_PROMPT_zh = """你正在压缩当前上下文，生成一个检查点。请写一份交接摘要，供另一个语言模型接手续做当前任务。

请涵盖以下内容：
- 当前进展和已经作出的关键决定
- 重要背景、约束条件或用户偏好
- 尚待完成的工作，并明确下一步该做什么
- 继续工作所需的关键数据、示例或参考资料

请写得简洁、条理清楚，让接手的模型能够顺利继续工作。"""

# Codex 原版摘要前缀，写在检查点末项的摘要正文之前。
SUMMARY_PREFIX = """Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:"""

# 摘要前缀的中文意译。
SUMMARY_PREFIX_zh = (
    "另一个语言模型已经着手解决这个问题，并总结了它的思考过程。你也可以访问它使用过的工具的状态。"
    "请利用这些信息，在已有工作的基础上继续，避免重复劳动。以下是那个模型生成的摘要，请结合其中的信息进行分析："
)

# 读取检查点时识别英文和中文摘要前缀。
SUMMARY_PREFIXES = (SUMMARY_PREFIX, SUMMARY_PREFIX_zh)
