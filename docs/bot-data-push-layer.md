# Bot_data 推送层：扩展模块向 LLM 注入上下文的机制

> 落地时间：2026-07-03（随 AFC 模块一起实装）
> 最后更新：2026-07-07
>
> Written by ZincNya~ ❤

---

## 概述

LLM 生成回复时需要的上下文，不只有用户消息本身——还会有来自各个扩展模块的额外信息。比如 AFC 检测到用户想调用计算器，就要把计算器 API 的用法文档塞进上下文里；未来如果有日程模块检测到用户在问"明天有什么安排"，也要把日程数据注入进去。

这些模块都在 LLM handler 之前运行（放在更早的 group），但它们怎么把自己准备好的上下文"传递"给后续的 LLM 呢？

以前的做法可能是直接改 `contextBuilder.py`，在里面 import 各个模块的函数，再逐个拼接。但这样一来，`contextBuilder` 就会变成一个和所有扩展模块都耦合的巨型拼接器——每加一个模块都要回来改它，改着改着就不知道哪块是谁的了。而 **bot_data 推送层** 就是为了打破这种耦合而设计的。它让扩展模块可以自行决定"我要给 LLM 塞什么"，而 `contextBuilder` 只负责统一读取和渲染，完全不需要知道具体有哪些模块、它们叫什么名字。

本文档记载：

- 推送层的数据结构和三层清理机制
- 扩展模块（生产端）如何注入上下文块
- `contextBuilder.py`（消费端）如何读取并渲染
- 设计约束和常见陷阱

## 目录

- [背景](#背景)
- [推送层的结构](#推送层的结构)
- [生产端：扩展模块如何注入上下文](#生产端扩展模块如何注入上下文)
- [消费端：contextBuilder 如何读取](#消费端contextbuilder-如何读取)
- [三层清理机制](#三层清理机制)
- [设计约束](#设计约束)
- [常见陷阱](#常见陷阱)
- [如何新增上下文注入模块](#如何新增上下文注入模块)

---

## 背景

AFC 是第一个需要"在 LLM 之前运行、为 LLM 准备额外上下文"的模块。它在 group 1 中检测用户消息的工具调用意图，如果命中，就把工具的 API 文档构建成一段上下文，让后续 group 2 的 LLM 生成回复时能看到。

最初的设计草案是在 `contextBuilder.py` 里 import `utils.afc.afcIntent`，然后直接调 `detectTools()` 拿结果。乍一看这是最直接、最简单的方法——但仔细一想就会发现问题：

- AFC 的意图检测逻辑会在 `contextBuilder` 里跑第二遍（handler 已经跑过一次了）
- 每加一个新模块，都要改 `contextBuilder` 的 import 和拼接逻辑
- `contextBuilder` 必须知道所有扩展模块的函数签名，耦合度极高
- 而且不够优雅

更合理的做法是：handler 跑完检测后，把结果"推"到一个 LLM 能读到的地方，然后 LLM 去那里"拉"。python-telegram-bot 的 `bot_data` 正好适合这个用途——它是全局共享的字典，跨 handler 可见，生命周期贯穿整个 bot 运行时。于是就有了"推送层"的设计。

---

## 推送层的结构

推送层是 `bot_data` 里的一个**二层嵌套字典**：

```python
bot_data[f"llm_extra_blocks_{chatID}"] = {
    "afc": (ContextTier.TOOLS, "工具", "<AFC_TOOLS>...</AFC_TOOLS>"),
    "schedule": (ContextTier.LOW_TRUST, "日程", "<SCHEDULE>...</SCHEDULE>"),
    # 未来可能的其他模块...
}
```

### 结构拆解

1. **外层 key**：`f"llm_extra_blocks_{chatID}"`
   - 按 chatID 隔离，防止不同对话的上下文互相泄漏
   - 格式固定为这个字符串模板，`contextBuilder` 靠它定位

2. **内层 dict**：`{模块id: (tier, label, content)}`
   - **模块id**（`"afc"` / `"schedule"` 等）：每个扩展模块占用自己的槽位，互不覆盖
   - **tier**：`ContextTier` 枚举值，决定这个块在最终上下文里的排序位置（见 [llm-context-assembly.md](llm-context-assembly.md) 的 ContextTier 说明）
   - **label**：这个块的简短标签，渲染时会显示为 `[label] content`
   - **content**：实际的上下文内容（通常是带 XML 标签的文本块）

### 为什么是二层结构

一开始考虑过直接用 `bot_data[f"llm_extra_blocks_{chatID}"]` 存一个 `list`，但这样就没法让多个模块各自管理槽位了——如果 AFC 和 schedule 模块同时注入，后者会把前者的 list 整个覆盖掉。

用 dict 的内层 key 做槽位，就能保证：

- AFC 写入 `["afc"]` 槽位，不会动 `["schedule"]` 槽位
- 消费端 `contextBuilder` 整体 `pop` 时，能一次性取走所有槽位
- 各模块可以独立清理自己上一条消息遗留的槽位

——这也是槽位 id 和模块 id 要保持一致的原因：方便追踪"这块上下文是谁塞进来的"。

---

## 生产端：扩展模块如何注入上下文

以 `handlers/afc.py` 为例：

```python
async def afcIntentDetector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.edited_message
    if not message or not message.text:
        return

    text = message.text
    chatID = str(message.chat_id)
    key = f"llm_extra_blocks_{chatID}"
    
    # 清除本模块上一条消息遗留的槽位（只动自己的槽，不影响其他扩展模块）
    slots = context.bot_data.get(key)
    if slots is not None:
        slots.pop("afc", None)
    
    # 检测意图（四级召回）
    toolNames = await detectTools(text)
    if not toolNames:
        return
    
    # 构建工具上下文块
    contextBlock = buildAFCContextBlock(toolNames)
    
    # 推入 bot_data 推送层
    slots = context.bot_data.setdefault(key, {})
    slots["afc"] = (ContextTier.TOOLS, "工具", contextBlock)
```

### 关键步骤

1. **清理自己的槽位**：`slots.pop("afc", None)` 删除上一条消息可能遗留的块，防止泄漏
2. **检测 / 构建**：运行本模块的业务逻辑，生成上下文内容
3. **写入槽位**：用 `setdefault` 确保外层 dict 存在，再写入 `(tier, label, content)` 三元组

### 注意事项

- **只动自己的槽位**：`slots.pop("afc", None)` 里的 `"afc"` 是本模块的 id，其他模块的槽位（如 `"schedule"`）不能碰
- **槽位 id 必须和 modulesRegistry 里的模块 id 一致**：这是约定，方便追踪是哪个模块注入的
- **如果没有检测到需要注入的内容，不要写入空块**：直接 `return`，让槽位保持空（或者被上次清理后的空状态）

---

## 消费端：contextBuilder 如何读取

`utils/llm/contextBuilder.py` 的 `buildConversationContext` 函数在组装上下文时，会统一读取推送层：

```python
# 从 bot_data 推送层读取扩展模块注入的块（读取即清除，防止泄漏到下一条消息）
extraBlocks: list[tuple[ContextTier, str, str]] = []
if telegramContext is not None:
    pushKey = f"llm_extra_blocks_{chatID}"
    moduleBlocks = telegramContext.bot_data.pop(pushKey, {})
    extraBlocks = list(moduleBlocks.values())

# ... 构建 memory / knowledge / history / url 等核心块 ...

# 合并扩展模块注入的块（AFC 工具上下文等）
allBlocks.extend(extraBlocks)

# 稳定排序：数值越小越靠前；同 tier 保持插入顺序
allBlocks.sort(key=lambda b: b[0])
```

### 关键点

1. **整体 `pop`**：`bot_data.pop(pushKey, {})` 一次性取走整个外层 dict，所有模块的槽位都被清空
2. **提取 values**：`list(moduleBlocks.values())` 拿到所有 `(tier, label, content)` 三元组
3. **统一排序**：扩展模块的块和核心块（memory / knowledge / history）一起按 `tier` 排序，不存在"扩展块永远在后面"的隐藏限制

---

## 三层清理机制

为什么需要"三层"清理？因为异常情况下，某一层可能失效：

### 第一层：生产端清理自己的槽位（防御性）

```python
slots = context.bot_data.get(key)
if slots is not None:
    slots.pop("afc", None)
```

**目的**：如果上一条消息的消费端清理失败（比如 LLM handler 抛异常了），至少不会让旧块泄漏到这一条。

**触发时机**：每次 handler 运行开头。

### 第二层：消费端整体 pop（主力清理）

```python
moduleBlocks = telegramContext.bot_data.pop(pushKey, {})
```

**目的**：正常流程下，LLM 读取后立即清空整个外层 dict，所有模块的槽位一起清除。

**触发时机**：`buildConversationContext` 读取推送层时。

### 第三层：写入只动自己槽位（隔离保证）

```python
slots = context.bot_data.setdefault(key, {})
slots["afc"] = (...)
```

**目的**：写入时用 `setdefault` 确保外层 dict 存在，但只覆盖自己的槽位，不影响其他模块已经写入的槽位。

**触发时机**：注入上下文时。

### 为什么三层都需要

假设只有消费端清理（第二层），来推演一遍会出什么岔子：

1. AFC handler 注入了工具上下文
2. LLM handler 因为某个异常退出，没跑到 `buildConversationContext`
3. 下一条消息来了，AFC 检测到不需要注入，于是什么都不做
4. LLM 这次正常运行，但会读到**上一条消息遗留的旧块**

有了生产端清理（第一层），即使消费端失败，下一条消息的 handler 开头也会主动清掉自己的旧槽位。写入隔离（第三层）则保证多个模块并发注入时不会互相覆盖——虽然目前只有 AFC 一个模块在用推送层，但这层保护是为将来准备的。

---

## 设计约束

这套机制能工作，靠的是几条惯例——

1. **扩展模块必须在 LLM handler 之前运行**
   - AFC 在 group 1，LLM 在 group 2
   - 如果顺序反了，LLM 会读到空的推送层

2. **槽位 id 必须和模块 id 一致**
   - 约定俗成的规则，方便追踪和调试
   - 如果 AFC 用了 `"tool"` 而不是 `"afc"`，会让人困惑是哪个模块注入的

3. **消费端必须用 `pop` 而不是 `get`**
   - `get` 只读取不删除，会导致块泄漏到下一条消息
   - `pop` 是"读取即清除"，符合推送层的语义

4. **生产端在写入前必须清理自己的槽位**
   - 即使本次不注入（检测结果为空），也要在开头 `pop` 一次
   - 否则上一条消息的块可能泄漏

5. **推送层只用于 Telegram 触发的 LLM 调用**
   - 控制台 `/send -c` 不会经过 Telegram handlers，`telegramContext` 为 `None`
   - `contextBuilder` 里有 `if telegramContext is not None` 的判断，控制台路径跳过推送层

---

## 常见陷阱

### 陷阱 1：忘记清理槽位，导致块泄漏

**症状**：用户问"1+1"，AFC 注入计算器上下文，LLM 正常回答；下一条消息用户问"天气怎么样"，LLM 的回复里却还在提计算器。

**原因**：第二条消息 AFC 检测结果为空，没有写入槽位，但也没有清理上一条遗留的槽位。

**修复**：在 handler 开头无条件清理自己的槽位：

```python
slots = context.bot_data.get(key)
if slots is not None:
    slots.pop("afc", None)
```

### 陷阱 2：用错 tier，导致块排序不符合预期

**症状**：AFC 工具上下文排在了 memory / history 之后，LLM 看到的顺序混乱。

**原因**：注入时用了 `ContextTier.LOW_TRUST`（值为 500），排到了 knowledge（300）、tools（200）后面。

**修复**：工具声明是开发者维护的高信任内容，应该用 `ContextTier.TOOLS`（值为 200）——这也是 AFC 实际使用的档位。

### 陷阱 3：多个模块用了同一个槽位 id

**症状**：AFC 和 schedule 模块都写 `slots["tools"]`，后者覆盖了前者。

**原因**：槽位 id 冲突。

**修复**：每个模块用自己的 id 做槽位 key（`"afc"` / `"schedule"`）。

### 陷阱 4：在生产端用了 `get` 而不是 `pop`

**症状**：上一条消息的块泄漏到当前条。

**原因**：`get` 只读取不删除。

**修复**：生产端清理用 `pop`：

```python
slots.pop("afc", None)
```

---

## 如何新增上下文注入模块

假设要添加一个"日程模块"，在用户问"明天有什么安排"时注入日程数据。

### 1. 创建 handler（放在 group 1）

`handlers/schedule.py`：

```python
from telegram import Update
from telegram.ext import MessageHandler, filters, ContextTypes
from utils.llm.contextBuilder import ContextTier

async def scheduleIntentDetector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.edited_message
    if not message or not message.text:
        return
    
    text = message.text
    chatID = str(message.chat_id)
    key = f"llm_extra_blocks_{chatID}"
    
    # 清理自己的槽位
    slots = context.bot_data.get(key)
    if slots is not None:
        slots.pop("schedule", None)
    
    # 检测是否在问日程
    if not ("明天" in text or "日程" in text or "安排" in text):
        return
    
    # 构建日程上下文块
    scheduleData = await fetchScheduleData(chatID)
    contextBlock = f"<SCHEDULE>{scheduleData}</SCHEDULE>"
    
    # 推入 bot_data
    slots = context.bot_data.setdefault(key, {})
    slots["schedule"] = (ContextTier.LOW_TRUST, "日程", contextBlock)

def register():
    return {
        "handlers": [MessageHandler(filters.TEXT & ~filters.COMMAND, scheduleIntentDetector)],
        "name": "日程意图检测",
        "description": "检测日程查询意图并注入上下文",
        "group": 1,  # ← 独占 group=1（如果和 AFC 共存，需要重新分配 group）
    }
```

### 2. 注册到 modulesRegistry

在 `modulesRegistry.py` 里加一条：

```python
"schedule": {
    "id": "schedule",
    "name": "日程管理",
    "description": "检测日程查询意图并为 LLM 注入日程数据",
    "version": "1.0.0",
    "author": "ZincPhos",
    "files": ["handlers/schedule.py", "utils/schedule/"],
    "handlers": ["handlers/schedule.py"],
    "initFunctions": [],
    "backgroundTasks": [],
    "dependencies": [],
    "githubRepo": None,
},
```

### 3. 重启 bot

`contextBuilder` 不需要改任何代码，它会自动读取 `bot_data[f"llm_extra_blocks_{chatID}"]["schedule"]` 槽位并渲染。

### 注意事项

- **group 1 目前被 AFC 独占**：如果要加新模块，需要重新分配 group（AFC 留在 1，新模块放 1.5？或者都改成用更精确的 filter 放在同一个 group）
- **槽位 id 必须唯一**：不要和现有模块冲突
- **tier 选择要合理**：日程数据是用户私有的，应该用 `ContextTier.LOW_TRUST`；工具/能力声明用 `ContextTier.TOOLS`（AFC 即如此）；开发者维护的背景知识用 `ContextTier.KNOWLEDGE`

---

就这样，推送层让扩展模块和 `contextBuilder` 完全解耦——前者只管"推"，后者只管"拉"，中间有 `bot_data` 做桥梁。未来再加新模块，也只是多一个槽位的事情，`contextBuilder` 的话，一行代码都不用动的说——