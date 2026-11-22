这次我直接按 Codex 源码的执行顺序讲，而不是先介绍各种模块。

整个过程可以划分为 6 个阶段：

1. **判断是否需要压缩**

   检查 token 阈值、模型切换、用户主动触发，以及是否存在后台压缩结果。

2. **选择压缩实现分支**

   根据模型服务提供的能力，选择本地 LLM 摘要、远端 Compaction V2，或特殊的 TokenBudget 重置。

3. **准备压缩请求**

   从当前有效历史构造输入，处理文本、图片、工具返回和不可用的附件。

4. **执行压缩并构建新的历史**

   获取摘要或压缩输出，筛选需要保留的原始消息，按预算裁剪。

5. **安装压缩结果并写入 JSONL**

   将 `replacement_history` 替换为当前有效历史，同时把完整检查点追加到日志。

6. **恢复并准备下一次 LLM 请求**

   读取最新有效检查点，重放后续消息，再执行模型输入标准化。

我这次固定查看仓库提交 `e787cf7024fa66e4ec26c6c2ba9c122e045dedb9`，因此下面的行号和链接对应同一源码版本，不受 `main` 后续更新影响。

为避免混淆，后面统一使用三个概念：

- `history`：Codex 内存中的有效模型历史。
- `rollout`：持久化的完整会话记录，通常存为 JSONL。
- `prompt.input`：经过处理、实际提交给模型服务的历史内容。

Python 代码会尽量对应 Rust 函数的逻辑结构，但会省略事件通知、遥测、Guardian 和其他无关运行状态。伪代码不是可以直接运行的 Python，也不会为了适配你的项目而改变 Codex 的算法。

# **第一阶段：如何触发压缩？**

## **第 1 步：计算上下文使用情况**

**源码：** [context_window.rs:56–120](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/session/context_window.rs?utm_source=chatgpt.com#L56-L120)

核心函数：`context_window_token_status_with_config()`

Codex 先获取当前上下文 token 使用量：

```
active_tokens = await session.get_total_token_usage()
```

随后计算自动压缩范围。代码支持两种口径：

1. `Total`：整个有效上下文的 token 使用量。
2. `BodyAfterPrefix`：减去当前上下文窗口初始前缀的 token 量，仅计算后续增长部分。

接着检查两类限制：

- 自动压缩预算 `auto_compact_scope_limit`。
- 模型实际可用的完整上下文窗口 `full_context_window_limit`。

其中完整窗口还考虑 `effective_context_window_percent`。

大致 Python 逻辑：

```
async def context_window_token_status(session, config, model):
    active_tokens = await session.get_total_token_usage()

    if config.limit_scope == "Total":
        scope_tokens = active_tokens
        scope_limit = model.auto_compact_token_limit()

    else:  # BodyAfterPrefix
        window = await session.auto_compact_window_snapshot()

        baseline = (
            window.prefill_input_tokens
            if window.prefill_input_tokens is not None
            else active_tokens
        )

        scope_tokens = max(0, active_tokens - baseline)

        scope_limit = (
            config.model_auto_compact_token_limit
            or model.auto_compact_token_limit()
        )

    full_limit = (
        model.context_window
        * model.effective_context_window_percent
        // 100
    )

    fallback_buffer = config.token_budget.fallback_buffer_tokens
    buffered_limit = (
        scope_limit + fallback_buffer
        if scope_limit is not None else None
    )

    full_limit_reached = (
        full_limit is not None
        and active_tokens >= full_limit
    )

    token_limit_reached = (
        buffered_limit is not None
        and scope_tokens >= buffered_limit
    ) or full_limit_reached

    post_turn_due = (
        config.post_turn_compact_threshold_percent > 0
        and (
            token_limit_reached
            or active_tokens * 100 >= (
                full_limit
                * config.post_turn_compact_threshold_percent
            )
        )
    )

    return {
        "token_limit_reached": token_limit_reached,
        "turn_end_compaction_threshold_reached": post_turn_due,
        "active_context_tokens": active_tokens,
    }
```

这里的空值判断在伪代码中有所简化，关键是两个不同的布尔值：

- `token_limit_reached`：上下文已经达到强制压缩条件。
- `turn_end_compaction_threshold_reached`：一轮结束时，达到可提前压缩的阈值。

它们决定后面不同的触发时机。

## **第 2 步：根据当前处于哪个阶段，决定是否压缩**

源码中并非只在一个位置检查，而是分成几种情况。

### **情况 A：新一轮用户请求开始前**

**源码：** [turn.rs:227–318](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/session/turn.rs?utm_source=chatgpt.com#L227-L318)、[1751–1781](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/session/turn.rs?utm_source=chatgpt.com#L1751-L1781)

`run_turn()` 会在正常记录本轮用户输入之前，调用 `run_pre_sampling_compact()`。

这个函数执行：

1. 先检查是否需要因为切换模型而压缩。
2. 获取上下文 token 状态。
3. 检查是否有已经完成的后台压缩候选。
4. 达到阈值或有候选时，调用 `run_auto_compact()`。

```
async def run_pre_sampling_compact(session, turn_context, client):
    await maybe_run_previous_model_inline_compact(
        session, turn_context, client
    )

    status = await context_window_token_status(
        session, turn_context.config, turn_context.model
    )

    candidate = await async_compaction.take_ready(session)

    if status["token_limit_reached"] or candidate is not None:
        await run_auto_compact(
            session=session,
            phase="PreTurn",
            candidate=candidate,
        )
```

**注意：这里的压缩发生在新用户输入正式加入历史之前。**

源码还特意保留待输入的消息，防止压缩失败时丢失本轮用户请求。

### **情况 B：一轮执行过程中，工具调用之后**

**源码：** [turn.rs:742–853](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/session/turn.rs?utm_source=chatgpt.com#L742-L853)

假设 Agent 正在执行：

```
LLM 请求 1
    ↓
调用 read_file("a.py")
    ↓
工具返回 20000 行代码
    ↓
还需要继续请求 LLM
```

这时工具输出可能把上下文推到阈值以上。

Codex 在一次模型采样和相关工具处理完成之后，计算 token 状态。

如果本轮还有后续工作，并且达到限制、请求了新上下文窗口，或者有可安装的后台压缩结果，就尝试 `MidTurn` 压缩。

```
needs_follow_up = model_needs_follow_up or has_pending_input

status = await context_window_token_status(...)

candidate = (
    await async_compaction.take_ready(session)
    if needs_follow_up else None
)

should_roll_over = (
    auto_compaction_enabled
    and needs_follow_up
    and (
        session.has_new_context_window_request()
        or status["token_limit_reached"]
        or candidate is not None
    )
)

if should_roll_over:
    installed = await run_auto_compact(
        session=session,
        phase="MidTurn",
        candidate=candidate,
    )

    if installed:
        # 继续当前 Agent 循环
        # 后续 LLM 请求将使用压缩后的历史
        continue
```

也就是说，**工具返回内容已经进入历史，压缩才开始；压缩完成后，当前 Agent 轮次可以继续执行。**

### **情况 C：一轮对话结束后**

**源码：** [turn.rs:930–956](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/session/turn.rs?utm_source=chatgpt.com#L930-L956)

如果本轮已经结束，Codex 还可能执行 `PostTurn` 压缩。

条件包括：

- 启用了自动摘要压缩。
- 配置了 `model_post_turn_compact_threshold_percent`。
- 没有待处理的用户输入。
- 当前任务未取消。
- 达到轮末阈值，或存在可用的后台压缩候选。

```
if turn_finished and auto_summarization_enabled:
    if config.post_turn_compact_threshold_percent > 0:
        if not has_pending_input and not cancelled:
            status = await context_window_token_status(...)
            candidate = await async_compaction.take_ready(session)

            if (
                status["turn_end_compaction_threshold_reached"]
                or candidate is not None
            ):
                await run_auto_compact(
                    session=session,
                    phase="PostTurn",
                    candidate=candidate,
                )
```

这一分支与 `MidTurn` 最大的区别是：**当前轮次不再需要使用压缩后的上下文继续生成答案，而是为后续轮次准备新历史。**

### **情况 D：切换模型**

**源码：** [turn.rs:1783–1877](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/session/turn.rs?utm_source=chatgpt.com#L1783-L1877)

切换模型时也可能压缩。

有两个触发原因：

1. 两个模型的 `comp_hash` 都存在且不同，表示压缩兼容性发生变化。
2. 换成上下文窗口更小的模型，而当前上下文超出了新模型适用的限制。

一个值得注意的实现细节是：**Codex 尝试使用旧模型压缩旧上下文，然后由新模型使用生成的替换历史。**

```
async def maybe_run_previous_model_inline_compact(session, new_model):
    previous = session.previous_turn_settings

    if previous is None:
        return

    hash_changed = (
        previous.comp_hash is not None
        and new_model.comp_hash is not None
        and previous.comp_hash != new_model.comp_hash
    )

    if not hash_changed:
        if not switched_to_smaller_model_with_overflow(
            previous.model, new_model, session
        ):
            return

    await run_auto_compact(
        session=session,
        compaction_model=previous.model,
        replacement_model=new_model,
        phase="PreTurn",
    )
```

### **情况 E：用户主动** `/compact`

**源码：** [tasks/compact.rs:27–94](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/tasks/compact.rs?utm_source=chatgpt.com#L27-L94)

手动触发走 `CompactTask`。

它不需要等待自动 token 阈值，而是直接进行能力分支选择：

```
async def run_manual_compact(session, context):
    if context.features.token_budget:
        await run_manual_token_budget_reset(session)

    elif context.provider.remote_compaction == "V2":
        await run_remote_compact_task(session)

    else:
        await run_local_compact_task(
            session,
            prompt=context.config.compact_prompt
                   or SUMMARIZATION_PROMPT,
        )
```

## **第 3 步：还有一个后台预压缩分支**

**源码：** [async_compaction.rs:42–174](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/session/async_compaction.rs?utm_source=chatgpt.com#L42-L174)

这一部分容易被忽略。

在启用 `AsyncCompaction` 特性、远端压缩为 V2，并满足配置的提前触发条件时，Codex 可以：

1. 在上下文还没有达到强制压缩阈值时，捕获当前历史版本。
2. 后台向模型服务发起一次压缩请求。
3. 正常 Agent 主循环继续执行。
4. 后台收到压缩结果之后，将其保存为一个候选。
5. 主循环在合适位置检查候选是否可用。
6. 如果候选对应的历史版本仍有效，则尝试安装；否则不能盲目替换当前历史。

伪代码：

```
async def maybe_start_background_compaction(session, status):
    if not session.features.async_compaction:
        return

    if session.provider.remote_compaction != "V2":
        return

    # 已到强制压缩条件时，不在这里另起预压缩
    if status["token_limit_reached"]:
        return

    if not reached_async_compaction_threshold(status):
        return

    snapshot = session.history.clone()
    version = session.history.version

    session.background_compaction = asyncio.create_task(
        remote_compact_snapshot(snapshot, version)
    )


async def take_ready(session):
    task = session.background_compaction

    if task is None or not task.done():
        return None

    candidate = task.result()

    if candidate.history_version != session.history.version:
        return None

    return candidate
```

这个异步候选机制比较复杂，后面讲远端 V2 时还会涉及**历史前缀是否依旧有效、如何处理候选生成后新增的消息**。

到这里，第一阶段就结束了。

# **第二阶段：选择压缩算法分支**

## **第 4 步：统一进入** `run_auto_compact()`

**源码：** [turn.rs:1886–1997](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/session/turn.rs?utm_source=chatgpt.com#L1886-L1997)

这是自动压缩的重要分发入口。

Python 对应：

```
async def run_auto_compact(
    session,
    step_context,
    replacement_step_context,
    client_session,
    reason,
    phase,
    candidate,
):
    if not session.inline_compaction_allowed(step_context):
        return False

    world_state = await session.build_world_state_for_step(
        replacement_step_context,
        new_window=True,
    )

    if session.features.token_budget:
        await run_token_budget_compaction(
            session, replacement_step_context, world_state
        )
        return True

    if step_context.provider.remote_compaction == "V2":
        return await run_inline_remote_auto_compact_task(
            session,
            step_context,
            replacement_step_context,
            client_session,
            world_state,
            reason,
            phase,
            candidate,
        )

    return await run_inline_auto_compact_task(
        session,
        step_context,
        replacement_step_context,
        world_state,
        reason,
        phase,
    )
```

三个分支差别很大：

| 分支        | 是否调用模型     | 压缩结果                     |
| ----------- | ---------------- | ---------------------------- |
| TokenBudget | 否               | 重置并建立新的上下文窗口     |
| Local       | 是，普通模型采样 | 文本摘要 + 保留消息          |
| Remote V2   | 是，服务端压缩   | `Compaction` item + 保留消息 |

注意：`Remote V2` 不仅是向 LLM 发送“请总结历史”。它使用特殊的压缩触发 item，等待服务端返回真正的 `Compaction` 类型输出。

下面分别展开。

# **第三阶段 A：Local 本地摘要压缩 Pipeline**

源码主入口：

[compact.rs:123–477](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact.rs?utm_source=chatgpt.com#L123-L477)

## **A1. 创建摘要提示词**

**源码：** `compact.rs:123–153`

从配置获取 `compact_prompt`。如果没有自定义配置，则使用内置 `SUMMARIZATION_PROMPT`。

```
prompt_text = (
    config.compact_prompt
    or SUMMARIZATION_PROMPT
)

compaction_input = ResolvedUserInput.text(prompt_text)
```

这个提示词要求模型提取后续继续任务需要的信息。

## **A2. 克隆历史，追加压缩提示词**

**源码：** `compact.rs:285–303`

注意 Codex 在这里克隆当前历史。

```
compaction_message = resolved_user_input_into_message(
    compaction_input,
    local_image_preparation="Process",
)

history = await session.clone_history()

history.record_items(
    [compaction_message],
    truncation_policy,
)
```

由此形成：

```
原来的有效 history：
    user_1
    assistant_1
    tool_call_1
    tool_output_1
    user_2
    assistant_2

用于压缩的 history：
    user_1
    assistant_1
    tool_call_1
    tool_output_1
    user_2
    assistant_2
    compact_prompt
```

**这里的压缩提示词是给压缩模型使用的输入，并不是后面一定会永久保留的历史。**

## **A3. 将历史转换为压缩模型真正接收的 input**

**源码：** `compact.rs:305–353`

首先：

```
turn_input = await history.clone().for_prompt(
    model.input_modalities,
    session.image_store,
)

attach_executed_tool_calls(turn_input)
```

`for_prompt()` 会处理：

1. 工具调用与结果的配对关系。
2. 模型不支持的图片内容。
3. 服务端文件引用是否仍然可访问。
4. 不适合直接发送的历史 item。

假设原始消息是：

```
{
    "role": "user",
    "content": [
        {"type": "input_text", "text": "分析截图"},
        {"type": "input_image", "image_url": "..."},
    ],
}
```

如果模型支持图片，并且图片引用可用，那么图片会随着这条历史消息一起进入压缩请求。

**所以 Local 压缩的摘要模型可以直接看到仍在有效历史中的图片，而不只是它们的文字描述。**

随后构建正常 `Prompt`，发送一次模型请求。

```
prompt = Prompt(
    input=turn_input,
    base_instructions=base_instructions,
)

response = await drain_to_completed(
    session=session,
    prompt=prompt,
    client_session=client_session,
)
```

## **A4. 处理压缩模型的失败与上下文溢出**

**源码：** `compact.rs:344–391`

这一段非常值得关注。

如果压缩模型本身也报告 `ContextWindowExceeded`：

- 只要历史还可以继续缩短，就调用 `history.remove_first_item()`。
- 然后重新构造压缩请求。
- 直到成功，或者无法继续删减。

其他可重试的错误则按照服务端的重试规则重试。

```
retries = 0

while True:
    turn_input = await history.clone().for_prompt(
        model.input_modalities,
        image_store,
    )

    try:
        response = await request_compaction_llm(turn_input)
        break

    except ContextWindowExceeded:
        if len(turn_input) <= 1:
            raise

        history.remove_first_item()
        retries = 0
        continue

    except RetryableStreamError as error:
        if retries >= max_retries:
            raise

        retries += 1
        await wait_for_retry(error, retries)
```

值得强调：**这是压缩请求自身的溢出恢复，不是最终历史的 token 预算裁剪。** 二者发生在不同阶段。

另外，删除的是用于压缩的 `history` 副本，不是立即删除原始 JSONL 记录。

## **A5. 获取压缩摘要**

**源码：** `compact.rs:393–407`

这里有一个容易忽略的分支：

- `PostTurn`：直接从压缩响应的输出中提取最后一条 assistant 文本。
- 其他阶段：从重新克隆的会话历史中提取最后一条 assistant 文本。

```
history_snapshot = await session.clone_history()

if phase == "PostTurn":
    summary_suffix = last_assistant_message(
        compaction_response.output
    )
    if not summary_suffix.strip():
        raise CompactionError("Missing summary")
else:
    summary_suffix = (
        last_assistant_message(history_snapshot.raw_items())
        or ""
    )

summary_text = SUMMARY_PREFIX + "\n" + summary_suffix
```

这里不是无缘无故分别取值：本地压缩的采样结果和会话历史的写入时机有关。不同阶段采用不同提取路径，避免使用不正确的消息。

## **A6. 选择压缩后仍要保留的原始消息**

**源码：** [compact.rs:733–858](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact.rs?utm_source=chatgpt.com#L733-L858)

这一部分是 Local 压缩最重要的核心算法。

不是直接：

```
new_history = [summary]
```

而是先调用：

```
new_history = build_compacted_history(
    history_snapshot,
    summary_text,
)
```

内部按以下顺序执行。

**A6.1：首先收集必须保留的消息。**

调用 `retained_client_items()`，选择标记为 `is_always_retained` 的记录。

这些记录不局限于普通用户消息，可能包括需要跨窗口保留的受保护内容。

**A6.2：再从新到旧遍历真实用户消息。**

预算是 `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`。

- 不处理旧的压缩摘要。
- 已被特殊保留的消息不再重复插入。
- 尽可能保留较新的用户输入。

**A6.3：区分纯文本消息与含图片消息。**

当整个消息的内容都是 `InputText`，并且 token 预算足够时，原样保留。

否则进入文本提取和裁剪逻辑：

- 遍历原始内容。
- 仅收集 `InputText`。
- 不复制普通消息中的 `InputImage`。
- 文本超预算时截断。
- 如果一条消息只有图片，没有可保留的文本，则使用空文本作为回退。

**A6.4：恢复消息原顺序，追加摘要。**

虽然选择消息时从新到旧遍历，最终保留历史仍按原消息位置排序。

下面是与这段 Rust 非常接近的 Python 逻辑：

```
COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000


def build_compacted_history(history, summary_text):
    items = history.annotated_items()

    # 第一次选择：特殊标记的跨窗口保留项
    selected = retained_client_items(
        items,
        should_retain=lambda entry: entry.is_always_retained(),
    )

    remaining = COMPACT_USER_MESSAGE_MAX_TOKENS

    # 第二次选择：真实用户消息，从新到旧
    for position, message in reversed(
        list(annotated_user_messages(items))
    ):
        if remaining == 0:
            break

        tokens = approx_token_count(message.flattened_text)

        if position in selected:
            remaining = max(0, remaining - tokens)
            continue

        original = message.original
        content = original["content"]
        metadata = clone_metadata(message.metadata)

        preserve_content = (
            tokens <= remaining
            and all(p["type"] == "input_text" for p in content)
        )

        if preserve_content:
            new_content = copy(content)

        else:
            # 仅选择文本，并保留对应的内容来源标注
            parts = [
                copy_with_annotation(p)
                for p in user_input_parts(content)
                if p["type"] == "input_text"
            ]

            if not parts:
                parts = [{"type": "input_text", "text": ""}]

            new_content = truncate_annotated_text(
                parts,
                token_budget=remaining,
            )

            if tokens > remaining:
                metadata.mark_retained_sources_incomplete()

        selected[position] = envelope(
            item={
                "role": "user",
                "content": new_content,
                "id": original.get("id"),
            },
            metadata=metadata,
        )

        remaining = max(0, remaining - tokens)

    # selected 按原始位置排序
    new_history = [
        selected[pos]
        for pos in sorted(selected)
    ]

    new_history.append(
        make_compaction_summary(
            summary_text or "(no summary available)"
        )
    )

    return new_history
```

这里 `retained_client_items()` 本身也有 64,000 token 选择预算；它与后续的 20,000 token 普通用户消息预算分别执行，不能简单相加成一个统一预算。

### **图片在这个分支中的结果**

假设之前有：

```
user_1:
    input_text: "分析这张图"
    input_image: image_A

assistant_1:
    "这是一张网络拓扑图..."

user_2:
    input_text: "继续分析路由..."
```

压缩模型在 A3 阶段可能看到图片 A。

但是生成新历史时，普通 `user_1` 属于混合媒体消息，走 A6.3 的文本提取分支。

结果可能是：

```
retained_user_1:
    input_text: "分析这张图"

retained_user_2:
    input_text: "继续分析路由..."

summary:
    "此前分析了一张网络拓扑图，主要包括..."
```

**图片 A 不会因为模型在压缩时看过它，就自动留在新的模型上下文里。**

不过，带有特殊跨窗口保留标记的消息由另一套选择规则处理，可能完整保留媒体部分。因此不能认为 Local 压缩绝对会删除所有旧图片。

## **A7. 处理尚未被正常采样的 Agent 消息**

**源码：** `compact.rs:413–443`，以及 [compact_pending_input.rs:12–49](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact_pending_input.rs?utm_source=chatgpt.com#L12-L49)

这是特殊的多 Agent 消息保护分支。

如果存在已经到达、但尚未被正常模型采样消费的 Agent 消息：

1. 找出这些消息。
2. 避免与已选择的历史重复。
3. 按原始顺序放回。
4. 对保留历史施加相应预算。
5. 将压缩摘要插入在这些待消费消息之前。

这样不会把尚未真正处理的消息直接视为“已经总结完毕”。

```
pending_sequence = pending_mail_sequence(history)

if pending_sequence is not None:
    summary = new_history.pop()

    new_history = remove_duplicate_pending_mail(
        new_history, pending_sequence
    )

    new_history.extend(
        retained_pending_mail(history, pending_sequence)
    )

    new_history.sort(key=history_sequence)

    new_history = truncate_retained_messages(
        new_history,
        token_budget=20_000,
    )

    new_history = insert_summary_before_pending_mail(
        new_history,
        summary,
        pending_sequence,
    )
```

如果你只关心单 Agent 的普通对话，这个分支通常不是重点，但它确实存在于官方 pipeline 中。

## **A8. 重新构造新上下文的前缀**

**源码：** `compact.rs:444–451`，以及 [compact.rs:90–121](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact.rs?utm_source=chatgpt.com#L90-L121)

到 A7 为止，仅有压缩后的对话历史。

Codex 还会把新上下文窗口需要的初始指令和上下文更新重新插入。

其中的上下文可能包括项目说明、当前工作信息等；这里我们不展开它们怎样生成，只关注它们最终也可能成为模型输入的一部分。

```
updates, baseline = await session.assemble_initial_context(
    replacement_step_context,
    world_state,
)

prefix, context = split_prefix_updates(updates)

new_history = assemble_compaction_history(
    compacted_history=new_history,
    prefix=prefix,
    context=merge_world_state_updates(context),
)
```

插入位置也有规则，源码 `compact.rs:672–730`：

- 优先放在最后一条真实用户或 Agent 消息之前。
- 如果只有摘要，则放在摘要之前。
- 如果只有 Compaction item，则放在该 item 之前。
- 都不存在则直接追加。

这样压缩后仍然保留了新上下文窗口所需的基础信息，而不是只发送摘要。

## **A9. 安装压缩结果**

**源码：** `compact.rs:456–472`

调用：

```
await session.replace_compacted_history(
    items=new_history,
    reference_context_item=replacement_context,
    metadata=CompactedHistoryMetadata(
        message=summary_text,
        compaction_response_id=compaction_response.response_id,
        ...
    ),
)

await session.recompute_token_usage(replacement_context)
```

这个函数后面会在 JSONL 阶段详细展开。

至此，Local 压缩才完成。

# **第三阶段 B：Remote Compaction V2 Pipeline**

这条路径比 Local 更复杂，建议重点看它。

源码主要涉及三个文件：

- [compact_remote_v2_attempt.rs](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact_remote_v2_attempt.rs?utm_source=chatgpt.com)：准备并发送压缩请求。
- [compact_remote_v2.rs](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact_remote_v2.rs?utm_source=chatgpt.com)：接收压缩结果、筛选消息。
- [compact_remote_v2_installation.rs](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact_remote_v2_installation.rs?utm_source=chatgpt.com)：验证和安装压缩结果。

先看一眼完整顺序：

```
Remote V2
│
├─ B1. 获取历史快照
│
├─ B2. 必要时截短尾部工具输出
│
├─ B3. 标准化历史、检查图片引用
│
├─ B4. 追加 CompactionTrigger
│
├─ B5. 向远端模型服务发送请求
│
├─ B6. 接收 Compaction 类型的输出
│
├─ B7. 删除请求专用的 CompactionTrigger
│
├─ B8. 选择跨窗口保留的原始消息
│       ├─ 用户消息
│       ├─ 特殊保留的 developer 消息
│       ├─ 特殊保留的工具结果
│       └─ 部分 Agent 消息
│
├─ B9. 根据 token / 图片预算裁剪
│
├─ B10. 插入 Compaction 输出
│
└─ B11. 安装 replacement_history
```

## **B1. 获取当前有效历史**

**源码：** `compact_remote_v2_attempt.rs:75–109`

前台压缩时，首先：

```
history = await session.clone_history()

input_goal_ids = extract_goal_message_ids(history)

base_instructions = await session.get_prompt_base_instructions()
```

如果历史中已经包含增量工具声明，还会相应调整基础指令，避免重复。

注意，它读取的是 `ContextManager` 的历史，而不是重新读取完整 JSONL。

## **B2. 压缩请求发出前，先处理超大的工具输出**

**源码：** [compact_remote_history.rs:111–228](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact_remote_history.rs?utm_source=chatgpt.com#L111-L228)

调用：

```
trim_function_call_history_to_fit_context_window(
    history,
    turn_context,
    model.context_window,
    base_instructions,
)
```

作用是处理一种问题：

> 当前历史已经超出模型能接受的上下文窗口，连远端压缩请求本身都可能无法成功发送。

它首先估算整个压缩输入的 token 数。

如果超限：

1. 从最新的历史消息组开始遍历。
2. 遇到可重写的工具输出，就将其内容改为固定的截断说明。
3. 保留工具输出的 `call_id`、成功标记等关键结构。
4. 重算 token。
5. 直到降到限制以内，或者遇到无法继续处理的消息。

这里并不是随便删除整段历史。

例如：

```
{
    "type": "function_call_output",
    "call_id": "call_123",
    "output": "50000 行代码...",
}
```

会在用于压缩的历史副本中变成类似：

```
{
    "type": "function_call_output",
    "call_id": "call_123",
    "output": "Output exceeded the available model context and was truncated",
}
```

重点是保留工具结果身份。

对应伪代码：

```
def trim_function_call_history_to_fit_context_window(
    history, context_window, base_instructions
):
    estimated = estimate_tokens(base_instructions, history)

    if context_window is None or estimated <= context_window:
        return 0, 0

    original = history.annotated_items()
    rewritten = []
    retained_len = len(original)

    # 从新到旧遍历消息组
    for group in newest_history_item_groups(original):
        if estimated <= context_window:
            break

        if should_retain_client_item(group.source):
            break

        output = rewritten_output_for_context_window(group.source)

        if output is None:
            break

        estimated -= estimate_group_tokens(group)
        estimated += estimate_item_tokens(output)

        retained_len = group.source_position
        rewritten.append(output)

    if rewritten:
        history.replace_annotated(
            original[:retained_len] + list(reversed(rewritten))
        )
```

这里的 `rewritten_output_for_context_window()` 仅支持源码指定的工具输出类型，遇到其他类型会停止。

**这一步是 Remote V2 发送压缩请求之前的保护措施，并不是最终的压缩算法。**

## **B3. 准备真正发送服务端的输入**

**源码：** `compact_remote_v2_attempt.rs:133–199`

然后：

```
input_items, input_metadata = unzip(
    await history.for_prompt_annotated(
        model.input_modalities,
        session.image_store,
    )
)

attach_executed_tool_calls(input_items)
```

与 Local 一样，这里会：

- 规范化工具调用与返回关系。
- 移除不支持的媒体内容。
- 检查 File 类型图片引用。
- 对无法解析的媒体使用占位文本。

随后构造 `Prompt`，包含模型当前可见的工具声明等信息。

有一个非常重要的区别：

**Remote V2 的压缩服务端也会接收完整的待压缩历史，包括其中仍然可用的图片。**

它并非先把全部图片转成文本，再提交压缩。

## **B4. 在输入尾部加入特殊** `CompactionTrigger`

**源码：** `compact_remote_v2_attempt.rs:224–243`

这是 Remote V2 与 Local 最大的区别。

```
prompt.input.append({
    "type": "compaction_trigger",
})
```

它是源码内部 `ResponseItem::CompactionTrigger` 的伪表示。

实际请求大致形成：

```
input:
    user_1
    assistant_1
    function_call_1
    function_call_output_1
    user_2
    assistant_2
    CompactionTrigger
```

它告诉支持该能力的模型服务：此次请求需要执行上下文压缩。

注意，不是所有模型服务都支持这种协议。

## **B5. 向服务端请求压缩**

**源码：** [compact_remote_v2.rs:305–436](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact_remote_v2.rs?utm_source=chatgpt.com#L305-L436)

内部复用 Responses 请求基础设施。

```
response_stream = await client.open_model_response(
    prompt=prompt,
    model=model,
    sampling_options=compaction_sampling_options,
)

result = await collect_compaction_output(response_stream)
```

这里包含普通网络重试、请求取消等逻辑。前台 Remote V2 对单次传输的重试预算做了额外限制。

## **B6. 接收服务端的压缩输出**

**源码：** `compact_remote_v2.rs:438–555`

核心是 `collect_compaction_output()`。

它不是简单寻找最后一条 assistant 文本。

而是遍历响应流，寻找：

```
item["type"] == "compaction"
```

源码明确要求**恰好有一个** `Compaction` **输出 item**。

```
async def collect_compaction_output(stream):
    compaction_output = None
    compaction_count = 0
    completed_response_id = None

    async for event in stream:
        if event.type == "output_item_done":
            item = event.item

            if item["type"] == "compaction":
                compaction_count += 1

                if compaction_output is None:
                    compaction_output = item

        elif event.type == "response_completed":
            completed_response_id = event.response_id
            break

    if completed_response_id is None:
        raise CompactionError("Response not completed")

    if compaction_count != 1:
        raise CompactionError("Expected one Compaction item")

    return {
        "compaction_output": compaction_output,
        "response_id": completed_response_id,
    }
```

例如，结果在内部可能表现为：

```
{
    "type": "compaction",
    "encrypted_content": "...",
}
```

这是为了说明结构，具体可用字段取决于服务端返回的 `ResponseItem`。

这里有一个重要区别：

**Remote V2 的** `Compaction` **内容不一定是本地可以理解的纯文本摘要。** Codex 把它当作一种模型上下文 item 保存和重新传递。

## **B7. 从压缩结果历史中去掉请求专用 Trigger**

**源码：** `compact_remote_v2_attempt.rs:270–282`

压缩请求使用了 `CompactionTrigger`，但它不是需要在后续上下文中长期保留的内容。

```
prompt_input = prompt.input
prompt_input.pop()  # 移除最后的 CompactionTrigger
```

之后保留：

```
RemoteCompactV2Attempt(
    prompt_input=prompt_input,
    prompt_input_metadata=input_metadata,
    compaction_output=compaction_output,
    compaction_response_id=response_id,
)
```

下一步构建新历史时使用的就是这些内容。

## **B8. 选择压缩后保留哪些旧消息**

**源码：** [compact_remote_v2.rs:557–679](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact_remote_v2.rs?utm_source=chatgpt.com#L557-L679)

进入：

```
build_v2_compacted_history(
    prompt_input,
    prompt_input_metadata,
    compaction_output,
    features,
    image_budget,
    ...
)
```

内部调用 `select_retained_items()`，设置：

```
RETAINED_MESSAGE_TOKEN_BUDGET = 64_000
```

这里的 64,000 是**保留的原始消息部分**的预算，不是整个上下文窗口的硬上限，也不代表 `Compaction` item 只允许 64,000 token。

### **选择规则**

默认会考虑：

| 消息类型              | 保留条件                          |
| --------------------- | --------------------------------- |
| 普通用户消息          | 能解析为真实用户输入或 HookPrompt |
| 普通 assistant 消息   | 不属于默认选择项                  |
| Agent 消息            | 满足来源、类型和 token 上限要求   |
| 客户端 developer 消息 | 特定 Feature 启用，或显式保留     |
| 工具输出              | 符合客户端保留策略，或显式保留    |
| 旧 Compaction item    | 不是普通用户消息选择项            |

特别看 `should_retain_client_item()`：

```
def should_retain_client_item(item, features):
    if item.metadata.retain_across_windows is not None:
        return item.metadata.retain_across_windows

    if features.retain_client_developer_messages:
        if is_client_authored_developer_message(item):
            return True

    if features.retain_client_tool_outputs:
        if is_retained_tool_output(item):
            return True

    return False
```

对于工具输出，[compact_retained_tool_outputs.rs:6–19](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact_retained_tool_outputs.rs?utm_source=chatgpt.com#L6-L19) 还限制了可被识别为独立保留工具输出的结构。

所以 Remote V2 不是无条件保留所有工具返回。

## **B9. 按 token 预算裁剪保留消息**

**源码：** `compact_remote_v2.rs:725–920`

这一步比较精细，尤其是图片。

算法整体是：

1. 从最新消息组向旧消息组遍历。
2. 对每组判断是否应该保留。
3. 计算这一组的 token 成本。
4. 完整放得下就保留。
5. 超预算时，按消息类型执行截断。
6. 最终按原顺序返回。

与 Local 不同，它还可能把相关的工具调用/输出、图片尺寸提示消息组织成一组，避免破坏必要的关系。

### **B9.1：先按消息组处理，而不是完全独立的 item**

**源码：** [compact_remote_history.rs:22–109](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact_remote_history.rs?utm_source=chatgpt.com#L22-L109)

一组可能是：

```
FunctionCall（有特殊保留标记）
    ↓
FunctionCallOutput（有特殊保留标记）
    ↓
ImageResizeNotice（相关提示）
```

`HistoryItemGroup.keep()` 会把相关的记录按原位置一起放回。

注意：只有满足源码定义的相邻关系和保留条件时，才会按组处理。它并不是把所有历史工具调用都自动配对保留。

### **B9.2：完整放得下就保留**

```
if group_token_count <= remaining:
    remaining -= group_token_count
    selected_group = clone(group)
```

### **B9.3：放不下时再按类型处理**

对于需要完整保留结构的工具输出，如果不够预算，不能随意截断为半个结果。

对于普通文本消息，则可以截断文本。

```
if is_retained_tool_output(group.source):
    # 结构化工具输出不允许任意截半
    break

elif can_truncate_message(group.source):
    selected_group = truncate_message_to_budget(
        group.source,
        remaining,
    )
```

源码这部分还有一些细化条件，下面专门讲媒体处理。

## **B10. 图片在 Remote V2 中如何保留和截断？**

这里是整个多模态 pipeline 的重点。

**源码：** [compact_remote_v2_media.rs:19–107](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact_remote_v2_media.rs?utm_source=chatgpt.com#L19-L107)。

它有两个分支。

### **情况 1：**`CompactionImageBudget` **关闭**

源码 `compact_remote_v2_media.rs:24–32`：

```
def media_part_token_count(part, image_budget_enabled):
    if part["type"] in ("input_text", "output_text"):
        return approx_token_count(part["text"])

    if part["type"] == "input_image":
        if not image_budget_enabled:
            return 0

        return estimate_image_tokens(part)

    return other_media_cost(part)
```

注意这个逻辑：

**ImageBudget 关闭，是保留预算不对图片本身收费，不是删除图片。**

如果包含图片的用户消息被选中，且文本部分在预算以内，那么原图片仍然可能被原样保留。

比如：

```
user_1:
    input_text: "帮我分析图片 A"
    input_image: A
```

计算保留预算时，图片部分可以按 0 计算；如果选中该消息，图片依旧属于新历史。

这也是为什么不能简单理解成“压缩一定把图片全部变成文本”。

### **情况 2：**`CompactionImageBudget` **开启**

此时按图片引用计算成本。

如果预算不足，还需要考虑图片和其标签必须一起保留或一起舍弃。

例如一个本地图片消息可能包含：

```
input_text: [image 1: C:\pictures\abc.png]
input_image: ...
input_text: [end of image]
```

这些标签是示意，实际文本由 Codex 的媒体标签函数生成。

源码中：

- `trailing_media_start()` 定位末尾的媒体及相邻标签。
- `truncate_message_to_token_budget()` 从消息末尾向前保留内容。
- 媒体块只有整体放得下时才保留。
- 如果图片块超出剩余预算，就停止，不能只保留图片的一部分 Base64。

Python 对应算法：

```
def truncate_message_to_token_budget(
    message, max_tokens, image_budget_enabled
):
    content = annotated_content(message)

    remaining = max_tokens
    retained = []

    # Rust 这里从内容列表末尾向前遍历
    while remaining > 0 and content:

        media_start = trailing_media_start(content)

        if media_start is not None:
            media_group = content[media_start:]

            tokens = sum(
                media_part_token_count(
                    part, image_budget_enabled
                )
                for part in media_group
            )

            # 图片及其标签必须整体放得下
            if tokens > remaining:
                break

            remaining -= tokens
            retained.extend(reversed(media_group))
            del content[media_start:]
            continue

        part = content.pop()

        # 这里剩下的是文本
        tokens = approx_token_count(part["text"])

        if tokens > remaining:
            part["text"] = truncate_text(
                part["text"], remaining
            )

        remaining = max(0, remaining - tokens)

        if part["text"]:
            retained.append(part)

    if not retained:
        return None

    retained.reverse()

    message["content"] = retained
    mark_retained_sources_incomplete(message)

    return message
```

所以 Remote V2 的图片处理特点是：

- 不需要先对图片进行 OCR，才能进入压缩。
- 默认保留选择依赖消息来源与类型。
- 图片是否被裁剪，取决于消息选择和媒体预算分支。
- 开启媒体预算时不会把一张图片的字节流截成不完整的图片。

## **B11. 构建最终压缩历史**

**源码：** `compact_remote_v2.rs:557–602`

当选择完保留项之后：

```
retained = select_retained_items(
    prompt_input,
    should_retain=is_retained_for_remote_compaction_v2,
    max_tokens=64_000,
    image_budget=image_budget,
)

checkpoint = compaction_output

new_history = insert_summary(
    retained,
    checkpoint,
    pending_sequence,
)
```

大多数普通场景可以理解为：

```
new_history:
    retained_user_1
    retained_user_2
    retained_other_items
    Compaction
```

如果有尚未正常采样的 Agent 消息，`Compaction` 可能插入在它们之前，而不是严格作为最后一个 item。

### **与 Local 的区别总结**

|                  | Local                            | Remote V2                        |
| ---------------- | -------------------------------- | -------------------------------- |
| 压缩模型输入     | 历史 + 摘要提示词                | 历史 + CompactionTrigger         |
| 结果形式         | 文本摘要                         | Compaction item                  |
| 普通用户消息保留 | 最近用户消息，文本预算 20k       | 按规则选择，预算 64k             |
| 普通图片保留     | 混合媒体消息通常只保留文本       | 选中的消息可以继续携带图片       |
| 工具输出保留     | 默认大量历史工具输出不保留       | 特殊标记的工具输出可保留         |
| 溢出处理         | 压缩请求溢出时删除最旧历史项重试 | 请求前可截短尾部工具输出         |
| 新上下文         | 保留项 + 摘要 + 初始上下文       | 保留项 + Compaction + 初始上下文 |

# **第四阶段：后台 V2 结果如何与新增历史合并？**

这是 Remote V2 独有的重要分支。

假设：

```
T1：历史为 [A, B, C]
    ↓
启动后台压缩，捕获 [A, B, C]
    ↓
T2：Agent 继续执行，追加 [D, E]
    ↓
T3：后台压缩完成
```

问题：后台压缩只覆盖 `[A, B, C]`，却不能把新产生的 `[D, E]` 删掉。

Codex 的思路是**压缩旧前缀，再拼接新的后缀**。

## **第 1 步：后台捕获前缀长度及版本**

**源码：** [compact_remote_v2_attempt.rs:285–333](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact_remote_v2_attempt.rs?utm_source=chatgpt.com#L285-L333)

```
prefix = CompactionPrefix(
    generation=history.history_version(),
    len=len(history.annotated_items()),
    auth_owner=current_auth_owner,
)

snapshot = history.clone()
```

之后后台处理这份快照。

## **第 2 步：后台请求返回候选**

**源码：** `compact_remote_v2_attempt.rs:335–377`

```
candidate = BackgroundCompactionCandidate(
    prefix=prefix,
    model_info=model,
    cutoff_world_state=world_state,
    attempt=remote_compact_result,
)
```

此时只生成候选，不立即替换当前历史。

## **第 3 步：主线程尝试安装候选**

**源码：** [compaction_replacement.rs:35–103](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/session/compaction_replacement.rs?utm_source=chatgpt.com#L35-L103)

验证：

```
if history.history_version() != candidate.prefix.generation:
    return None

if len(history.items) < candidate.prefix.len:
    return None

if current_auth_owner != candidate.prefix.auth_owner:
    return None
```

通过之后分割：

```
current = history.items

replaced = current[:candidate.prefix.len]
suffix = current[candidate.prefix.len:]
```

随后：

```
new_history = assemble_compacted_prefix(candidate)

new_history.extend(suffix)
```

最终：

```
原历史：
    A B C D E

后台压缩：
    Compaction(A B C)

新历史：
    Compaction(A B C) D E
```

真正的实现还会处理重新注入的上下文、需要保留的特殊消息以及原顺序，但核心就是这个前缀替换逻辑。

## **第 4 步：如果候选不可安装怎么办？**

**源码：** [compact_remote_v2_installation.rs:18–77](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact_remote_v2_installation.rs?utm_source=chatgpt.com#L18-L77)

有三种情况：

1. 候选合法，而且安装后 token 预算足够：结束。
2. 候选合法，但追加后缀后仍超预算：再执行一次前台压缩。
3. 候选无效：根据 `CompactionFallback` 决定跳过或执行前台压缩。

```
async def run_compaction(candidate, fallback):
    installed_prefix = False

    if candidate is not None:
        installed = await build_and_install_compaction(candidate)

        if installed:
            installed_prefix = True

            status = await context_window_token_status(...)

            if not status["token_limit_reached"]:
                return True

        elif fallback == "Skip":
            return False

    foreground_installed = await build_and_install_compaction(
        candidate=None
    )

    return installed_prefix or foreground_installed
```

这是 Codex 在后台压缩期间仍能继续正常执行 Agent 的关键机制。

# **第五阶段：TokenBudget 特殊分支**

**源码：** [compact_token_budget.rs:42–75](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/compact_token_budget.rs?utm_source=chatgpt.com#L42-L75) 和 [session/mod.rs:5606–5657](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/session/mod.rs?utm_source=chatgpt.com#L5606-L5657)。

这里没有调用摘要模型。

核心步骤：

1. 运行压缩前置检查。
2. 从旧历史中提取需要跨窗口保留的客户端消息。
3. 创建新上下文窗口。
4. 重新构建初始上下文。
5. 将保留消息与初始上下文组装成新历史。
6. 复用 `replace_compacted_history()` 安装结果。
7. 重算 token。

```
async def start_new_context_window(session, step_context, world_state):
    history = await session.clone_history()

    retained = retained_client_items(
        history.annotated_items(),
        should_retain=lambda item: should_retain_client_item(
            item, session.features
        ),
    )

    window_number, window_ids = session.advance_context_window()

    context_items, baseline = await session.assemble_initial_context(
        step_context,
        world_state,
    )

    new_history = merge_world_state_updates(
        context_items
    ) + list(retained.values())

    await session.replace_compacted_history(
        new_history,
        metadata=CompactedHistoryMetadata(
            message="",
            compaction_response_id=None,
            window_number=window_number,
            window_ids=window_ids,
        ),
    )

    await session.recompute_token_usage(step_context)
```

这个分支本质上是**丢弃普通旧历史并重建窗口**，而不是通过 LLM 对旧历史生成语义摘要。

# **第六阶段：多模态图片和本地文件，在整个 Pipeline 中怎样流转？**

到这里，压缩算法已经讲完了。现在单独沿着一张图片和一个本地代码文件走一遍。

## **1. 本地图片输入**

**源码：** [resolved_user_input.rs:76–103](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/user-input/src/resolved_user_input.rs?utm_source=chatgpt.com#L76-L103)、[179–229](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/user-input/src/resolved_user_input.rs?utm_source=chatgpt.com#L179-L229)。

假设用户提交：

```
帮我分析 C:\images\diagram.png
```

并通过客户端把它作为 `LocalImage` 附件传入。

### **第 1 步：读取本地文件字节**

`ResolvedUserInput::from_client()` 会读取本地图片，捕获其字节或者读取错误。

```
def resolve_local_image(path, detail):
    try:
        content = read_local_image_bytes(path)
        return LocalImage(
            path=path,
            bytes=content,
            detail=detail,
        )
    except Exception as error:
        return LocalImage(
            path=path,
            bytes_error=str(error),
            detail=detail,
        )
```

这说明本地图片不是等到很久以后重新发送模型时才第一次读取。

### **第 2 步：把图片转换为模型消息内容**

`ResolvedUserInput::into_message()` 会将其转换为包含图片的用户消息，必要时附加图片来源标签。

对应源码中：

```
content.extend(
    local_image_content_items_with_label_number(
        path=path,
        image_bytes=content,
        detail=detail,
    )
)
```

如果读取失败，就使用文本错误说明。

### **第 3 步：图片准备与附件上传**

**源码：** [image_preparation.rs:170–265](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/image_preparation.rs?utm_source=chatgpt.com#L170-L265)、[426–495](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/image_preparation.rs?utm_source=chatgpt.com#L426-L495)。

Codex 的内部图片引用主要有：

```
ImageReference.Inline(image_url="data:image/png;base64,...")

ImageReference.File(file_id="...")
```

其中，Inline 图片可以经过解码、调整尺寸、重新编码，然后提交附件存储服务。

```
async def prepare_image(image, detail, attachment_store):
    if image.kind != "Inline":
        return image

    prepared = resize_image(image.image_url, detail)

    result = await attachment_store.upload(
        content=prepared.encoded_bytes
    )

    if result.kind == "Inline":
        return ImageReference.Inline(
            image_url=make_data_url(result.bytes)
        )

    if result.kind == "File":
        return ImageReference.File(
            file_id=result.file_id
        )

    return ImageReference.Inline(
        image_url=prepared.data_url
    )
```

如果预处理失败，还可能将对应图片内容替换为错误占位文本。

对于恢复后的图片，源码也考虑了重新应用图片预处理和保留原始图片数据以供重放的问题。

### **第 4 步：进入历史和 JSONL**

历史消息中保存的是处理后的 `ResponseItem` 图片引用，而不是单纯“一个本地路径”。

因此 JSONL 里的模型消息可能包含：

```
input_image:
    Inline(data URL)
```

或者：

```
input_image:
    File(file_id)
```

这取决于具体的图片准备和附件存储路径。

### **第 5 步：发生压缩**

进入本地或远端压缩分支。

- Local：压缩模型可以看到图片，但新历史中普通混合媒体消息往往只保留文本。
- Remote V2：图片可以随被选中的消息保留，图片预算控制是否需要把它作为媒体成本计入。
- TokenBudget：只有满足跨窗口保留策略的历史消息才会继续保留。

### **第 6 步：重启后恢复图片**

**源码：** [media_availability.rs:21–134](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/context_manager/media_availability.rs?utm_source=chatgpt.com#L21-L134)。

`for_prompt()` 会调用 `replace_unresolvable_media_files()`。

当使用 File 引用时：

1. 收集历史中的图片 `file_id`。
2. 调用附件存储服务检查是否可解析。
3. 对确认不可用的引用，将图片替换为文本占位符。

大致逻辑：

```
async def replace_unresolvable_media_files(items, store):
    file_ids = collect_file_image_ids(items)

    unavailable = set()

    for file_id in file_ids:
        if not await store.is_resolvable(file_id):
            unavailable.add(file_id)

    for item in items:
        for part in image_parts(item):
            if part.file_id in unavailable:
                replace_with_text(
                    part,
                    "[image unavailable]",
                )
```

所以**恢复 JSONL 中的** `file_id`**，不代表服务端文件一定还可用**。

此外，这里不会因为 JSONL 里有一个旧的本地图片路径，就必然重新打开该路径。模型可见的图片引用和用户最初选择的本地路径是两个概念。

## **2. 工具返回的图片**

图片也可能来自工具结果，比如截图工具。

**源码：** `image_preparation.rs:301–410`

Codex 支持处理工具输出中的 `InputImage`，不只是用户输入中的图片。

示意：

```
{
    "type": "function_call_output",
    "call_id": "call_123",
    "output": [
        {"type": "input_text", "text": "截图已生成"},
        {"type": "input_image", "image_url": "..."},
    ],
}
```

在正常模型历史中，这种图片可以作为工具结果的一部分。

在压缩时：

- Local：工具输出通常不会作为普通历史长期保留，但压缩模型可以在原始输入中使用它。
- Remote V2：如果该工具输出符合跨窗口保留条件，可以保留；否则通常依赖服务端 Compaction 结果传递其重要信息。
- 如果 File 引用不可用，则在准备模型输入时会被替换为占位文本。

还要注意，普通工具文本输出会受到 `truncate_function_output_payload()` 的历史截断策略影响。它可能在进入长期历史之前就被截短，不是只有发生 Compaction 时才会缩短。

**源码：** [history.rs:715–763](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/context_manager/history.rs?utm_source=chatgpt.com#L715-L763)。

## **3. 本地普通文件（代码、PDF、Word、二进制）**

这一点需要与图片明确区分。

在当前阅读的 `ResolvedUserInput` 枚举里，直接支持的客户端本地媒体类型主要是 `LocalImage` 和 `LocalAudio`，并没有一个任意文件都直接作为 `LocalFile` 输入的通用分支。

对于代码文件，更常见的实际流程是：

```
用户：分析 a.py
    ↓
LLM 调用文件读取工具
    ↓
工具读取 a.py
    ↓
生成 FunctionCallOutput（文本内容）
    ↓
追加到历史
    ↓
压缩时按工具输出处理
```

比如：

```
{
    "type": "function_call_output",
    "call_id": "call_read_1",
    "output": "def hello():\n    print('hello')",
}
```

PDF 或 Word 则取决于具体的文件工具、解析器以及 API 输入方式：

- 提取文本后，按文本消息/工具输出处理。
- 渲染页面为图片后，按图片处理。
- 如果有支持原生文件上传的独立流程，则应按该流程处理；不能从上面的图片代码推断 Codex 一定会原生上传所有文件格式。

对于无法直接解码的任意二进制文件，**路径和原始字节本身不会自动让模型理解文件内容**。

而且压缩后的新历史通常也不需要继续携带旧代码文件的全部原文，除非它满足明确的保留规则。

# **第七阶段：压缩后的 JSONL 如何持久化？**

这一阶段本地和远端压缩都汇合到同一个函数。

**源码：** [session/mod.rs:4961–5137](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/session/mod.rs?utm_source=chatgpt.com#L4961-L5137)。

函数：`replace_compacted_history()`。

## **第 1 步：得到完整的替换历史**

本地压缩的结果大致是：

```
[prefix, retained_messages, summary]
```

Remote V2 的结果大致是：

```
[prefix, retained_messages, Compaction]
```

如果是后台压缩，还会拼接压缩快照之后的新消息后缀。

## **第 2 步：在锁内验证并安装**

**源码：** `session/mod.rs:4998–5087`

如果是后台前缀替换，首先检查其前缀版本与当前历史是否兼容。

然后处理 ID 和历史元数据，使写入的检查点与当前内存历史一致。

最核心的两行逻辑：

```
replacement_history = clone(items)

session.state.replace_annotated_history(
    items,
    replacement_reason="Compaction",
)
```

也就是说，内存中的 `history` 被直接替换，不是继续积累所有旧消息。

## **第 3 步：构造** `CompactedItem`

**源码：** `session/mod.rs:5088–5105`

```
compacted_item = CompactedItem(
    message=summary_text,                 # Local 有文本摘要
    replacement_history=replacement_history,
    compaction_response_id=response_id,
    window_number=window_number,
    # 还有其他恢复与状态元数据
)
```

其中最重要的就是：

```
replacement_history
```

它已经包含真正安装到有效历史的内容，包括可能保留的图片引用和文本。

`message` 不是恢复时唯一的依据。

这点对于 Remote V2 尤其重要，因为其 `message` 可以为空，而 `replacement_history` 仍然保存压缩输出和其他保留项。

## **第 4 步：追加 JSONL 压缩检查点**

**源码：** `session/mod.rs:5124–5132`

压缩检查点被放入一组 `RolloutItem`，随后调用：

```
await session.persist_rollout_items([
    RolloutItem.Compacted(compacted_item),
    # 其他上下文状态记录
])
```

真正的 JSONL 序列化位于：

[recorder.rs:2108–2134](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/rollout/src/recorder.rs?utm_source=chatgpt.com#L2108-L2134)。

写入逻辑是：

```
async def write_rollout_item(item, ordinal):
    line = {
        "timestamp": now_utc(),
        "ordinal": ordinal,
        "type": item.type,
        "payload": item.payload,
    }

    encoded = json.dumps(line)
    await file.write(encoded + "\n")
    await file.flush()
```

伪代码的字段组织是为了说明语义，不是源码序列化结构的逐字复刻。

## **第 5 步：旧 JSONL 怎么处理？**

**不会因为安装了新的压缩检查点就立即删除旧消息。**

比如日志：

```
1. response_item: user_1
2. response_item: assistant_1
3. response_item: tool_call_1
4. response_item: tool_output_1
5. response_item: user_2
6. response_item: assistant_2

7. compacted:
      replacement_history:
          retained_user_1
          retained_user_2
          Compaction

8. response_item: user_3
9. response_item: assistant_3
```

内存中则是：

```
retained_user_1
retained_user_2
Compaction
user_3
assistant_3
```

**JSONL 用于保留事件记录；**`replacement_history` **才是压缩后的有效历史快照。**

# **第八阶段：重启后如何恢复到压缩后的上下文？**

这一部分也是你最关心的。

主要源码：

- [recorder.rs:1160–1226](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/rollout/src/recorder.rs?utm_source=chatgpt.com#L1160-L1226)
- [rollout_reconstruction.rs:66–93](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/session/rollout_reconstruction.rs?utm_source=chatgpt.com#L66-L93)
- [rollout_reconstruction.rs:530–610](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/session/rollout_reconstruction.rs?utm_source=chatgpt.com#L530-L610)

## **第 1 步：读取 JSONL**

`RolloutRecorder::load_rollout_items()` 会逐行读取并解析存储记录。

```
rollout_items = []

for line in read_rollout_lines(jsonl_path):
    if not line.strip():
        continue

    try:
        value = json.loads(line)
        rollout_item = decode_rollout_line(value)
    except ParseError:
        continue

    rollout_items.append(rollout_item)
```

注意：**源码并没有通过文件倒序索引直接跳到最后一个 checkpoint。**

它仍需要读取并解析记录。

加速的主要是后面的**有效历史重建**，而不是一定能跳过 JSONL 的磁盘读取。

## **第 2 步：找到最新的有效 Compaction 检查点**

函数：`select_input_compaction()`。

**源码：** `rollout_reconstruction.rs:66–93`

它从后向前找到最新的 `Compacted` 项，并验证检查点是否满足恢复条件。

检查包括：

- 必须有 `replacement_history`。
- 必须有窗口编号。
- 在相应历史模式下，需要符合新的恢复元数据契约。

正常情况下：

```
def select_input_compaction(rollout_items, history_mode):
    for index in reversed(range(len(rollout_items))):
        item = rollout_items[index]

        if item.type != "compacted":
            continue

        if not checkpoint_is_usable(item, history_mode):
            return None

        return {
            "checkpoint": item,
            "suffix": rollout_items[index + 1:],
        }

    return None
```

这里的**最新检查点**很重要：一个不完整的新检查点不应该随便由更旧的检查点替代。

后续源码还会处理回滚操作，因此实际选择的是最新的、在当前恢复语义下仍有效的检查点。

## **第 3 步：把检查点的替换历史安装进内存**

**源码：** `rollout_reconstruction.rs:530–543`

```
history.replace_annotated(
    checkpoint.replacement_history
)
```

这一句就恢复了压缩后的有效历史。

不需要重新执行 Compaction。

## **第 4 步：重放检查点之后的消息**

**源码：** `rollout_reconstruction.rs:544–610`

```
for item in rollout_suffix:
    if item.type == "response_item":
        history.replay_annotated_item(
            item.payload,
            truncation_policy,
        )

    elif item.type == "inter_agent_communication":
        history.record_items([
            item.to_model_input_item()
        ])

    elif item.type == "thread_rollback":
        history.drop_last_n_user_turns(
            item.num_turns
        )
```

真实源码还考虑实时输入收尾、保留上下文以及旧版压缩格式。

特别地，旧版 `CompactedItem` 如果没有 `replacement_history`，会走兼容路径：基于已有历史和保存的摘要重新构建压缩历史。这不是当前新格式的标准恢复方式。

因此，对于当前正常格式：

```
history = checkpoint.replacement_history + newer_items
```

这是核心，但不是全部异常恢复逻辑。

## **第 5 步：恢复后构造下一次 LLM 请求**

**源码：** [history.rs:765–797](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/context_manager/history.rs?utm_source=chatgpt.com#L765-L797)。

调用：

```
input_items = await history.for_prompt(
    model.input_modalities,
    image_store,
)
```

其核心顺序：

```
async def for_prompt(history, input_modalities, image_store):
    # 补全可恢复历史中未完成的工具调用关系
    # 去除不合适的孤立工具输出
    # 清理模型不支持的图片
    history.normalize_history(input_modalities)

    # 失效的 File 图片引用改成占位文本
    await replace_unresolvable_media_files(
        history.items,
        image_store,
    )

    # 源码还有专门的音频处理，此处略

    return [
        envelope.item
        for envelope in history.annotated_items()
    ]
```

其中工具调用规范化逻辑在：

[normalize.rs:21–175](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/context_manager/normalize.rs?utm_source=chatgpt.com#L21-L175)。

图片支持判断在：

[normalize.rs:346–390](https://github.com/openai/codex/blob/e787cf7024fa66e4ec26c6c2ba9c122e045dedb9/codex-rs/core/src/context_manager/normalize.rs?utm_source=chatgpt.com#L346-L390)。

注意，这里的 `for_prompt()` 返回的是模型历史输入部分。实际发送的请求还会包含 `base_instructions`、工具声明、模型参数等，不能将其理解为整个 HTTP 请求的全部字段。

# **九、把所有核心分支合在一起**

下面这份代码是我认为最适合你对照 Rust 源码阅读的总伪代码。

它保留 Codex 的核心调用次序和分支，不追求直接运行。

```
async def maybe_compact(session, phase, step_context):
    status = await context_window_token_status(session)

    candidate = await session.async_compaction.take_ready()

    if phase == "PreTurn":
        required = status.token_limit_reached

    elif phase == "MidTurn":
        required = (
            status.token_limit_reached
            or session.new_context_window_requested
        )

    elif phase == "PostTurn":
        required = (
            status.turn_end_compaction_threshold_reached
        )

    if not required and candidate is None:
        return False

    return await run_auto_compact(
        session=session,
        phase=phase,
        step_context=step_context,
        candidate=candidate,
    )


async def run_auto_compact(session, phase, step_context, candidate):
    if not session.inline_compaction_allowed():
        return False

    world_state = await session.build_world_state_for_step(
        step_context, new_window=True
    )

    # 特殊模式：完全不请求模型摘要
    if session.features.token_budget:
        await session.start_new_context_window(
            step_context, world_state
        )
        return True

    if session.provider.remote_compaction == "V2":
        return await remote_compaction_v2(
            session, step_context, world_state, candidate
        )

    return await local_compaction(
        session, step_context, world_state, phase
    )


async def local_compaction(session, context, world_state, phase):
    # 1. 克隆历史
    history = await session.clone_history()

    # 2. 附加压缩提示
    history.record_items([
        make_user_message(
            context.config.compact_prompt or SUMMARIZATION_PROMPT
        )
    ])

    # 3. 向模型发起普通采样；溢出时移除最旧项重试
    while True:
        prompt_input = await history.clone().for_prompt(
            context.model.input_modalities,
            session.image_store,
        )

        try:
            response = await sample_compaction_model(prompt_input)
            break
        except ContextWindowExceeded:
            if len(prompt_input) <= 1:
                raise
            history.remove_first_item()

    # 4. 根据执行阶段取得摘要
    snapshot = await session.clone_history()

    if phase == "PostTurn":
        summary = last_assistant_text(response.output)
    else:
        summary = last_assistant_text(snapshot.raw_items())

    # 5. 构建：特殊保留项 + 用户文本 + 摘要
    new_history = build_compacted_history(
        snapshot,
        SUMMARY_PREFIX + "\n" + (summary or ""),
    )

    # 6. 特殊待处理消息保护和初始上下文重注入
    new_history = retain_pending_mail_if_needed(new_history, history)
    new_history = await assemble_compaction_history(
        new_history, world_state
    )

    # 7. 安装并持久化
    await session.replace_compacted_history(new_history)
    await session.recompute_token_usage()

    return True


async def remote_compaction_v2(
    session, context, world_state, candidate
):
    # 1. 如有后台结果，优先尝试采用
    if candidate is not None:
        installed = await try_install_prefix_compaction(
            session, candidate, world_state
        )

        if installed:
            status = await context_window_token_status(session)
            if not status.token_limit_reached:
                return True

    # 2. 需要时执行前台远端压缩
    history = await session.clone_history()

    # 3. 压缩请求前裁剪尾部过大的工具输出
    trim_function_call_history_to_fit_context_window(
        history,
        context.model.context_window,
        context.base_instructions,
    )

    # 4. 转成实际模型输入，处理图片和工具关系
    prompt_input = await history.for_prompt_annotated(
        context.model.input_modalities,
        session.image_store,
    )

    # 5. 加入特殊压缩触发项
    request_input = extract_items(prompt_input)
    request_input.append(make_compaction_trigger())

    # 6. 调用远端服务
    response = await request_remote_compaction_v2(request_input)

    # 7. 提取唯一 Compaction 输出
    compaction_output = extract_one_compaction_item(response)

    # 8. 只保留原始输入，不保留临时 Trigger
    request_input.pop()

    # 9. 筛选历史，控制文本和图片预算
    new_history = build_v2_compacted_history(
        request_input,
        compaction_output,
        token_budget=64_000,
        image_budget_enabled=session.features.compaction_image_budget,
    )

    # 10. 重建初始上下文
    new_history = await assemble_compaction_history(
        new_history, world_state
    )

    # 11. 安装压缩历史并追加检查点
    await session.replace_compacted_history(new_history)
    await session.recompute_token_usage()

    return True
```

这里为了呈现主干，省略了远端模式的特殊错误重试、模型回退、Hooks、具体元数据对象以及后台候选已被拒绝时的 Skip 策略。那些分别在前面的步骤中说明了。

## **最后归纳：真正会发送给 LLM 的历史是什么？**

以一个有图片、有代码读取工具的会话为例：

压缩前的有效历史

User：分析图片 A + Image A

Assistant：图片分析结果

Tool call：read_file("a.py")

Tool output：代码全文

User：继续分析项目

Assistant：分析结果

Local

初始上下文

保留用户消息文本

保留用户消息文本

Summary 文本

Remote V2

初始上下文

保留用户消息 + 可能的图片 A

其他被选中的历史

Compaction item

示意的是普通消息处理结果，并非保证每次保留的具体 item 完全一致；特殊保留策略和预算可能改变选择。

压缩检查点进入 JSONL 后，如果下一轮又发生：

```
User：继续
Assistant：调用工具
Tool：返回新数据
```

那么实际历史就是：

```
压缩后的 replacement_history
    +
新的 User / Assistant / Tool items
```

每次请求前仍然经过 `for_prompt()`，这也是为什么内存中的历史、JSONL 和 HTTP 请求中的 `input` 不应混为一谈。

**我认为这份源码最值得关注的三个设计点是：**一是 Local 和 Remote V2 分别使用文本摘要与结构化压缩输出；二是通过 `replacement_history` 把模型上下文快照与完整 JSONL 日志解耦；三是后台压缩采用前缀快照加后缀合并，避免覆盖压缩期间新增的对话。它们共同组成了 Codex 的上下文压缩与恢复 pipeline。