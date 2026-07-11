# Telegram Handler 分组与注册机制

> 最后更新：2026-07-10
>
> Written by ZincNya~ ❤

---

## 概述

本文档描述 Telegram handlers 的注册机制、group 分组规则以及优先级关系。理解这套机制可以帮助你：

- 新增 handler 该放在哪个分组才不会打乱原有执行顺序；
- 为什么部分 handler 能直接截断整条消息处理流水线，不让 LLM 再响应；
- 避免 handler 冲突和意外的执行顺序问题

## 目录

- [注册入口](#注册入口)
- [PTB group 行为](#ptb-group-行为)
- [当前 group 总览](#当前-group-总览)
- [Group -1：全局消息收集](#group--1全局消息收集)
- [Group 0：命令、回调与高优先级消息处理](#group-0命令回调与高优先级消息处理)
- [Group 1：AFC 意图检测](#group-1afc-意图检测)
- [Group 2：LLM 自动回复](#group-2llm-自动回复)
- [当前无需重编排的原因](#当前无需重编排的原因)
- [需要注意的边界](#需要注意的边界)
- [建议约定](#建议约定)

---

## 注册入口

Telegram handlers 由 [loader.py](../loader.py) 动态加载，整套加载流程流程可以说是非常清晰：

1. 扫描 [handlers/](../handlers/) 目录。
2. 跳过 `SKIP_MODULES` 中的模块，目前只有 `cli`。
3. 导入每个模块并调用 `register()`。
4. 将返回的 handler 注册到 `Application`。

`register()` 支持两种格式：

> ```python
> def register():
>     return [CommandHandler("nya", sendNya)]
> ```
> 
> 或：
> 
> ```python
> def register():
>     return {
>         "handlers": [CommandHandler("nya", sendNya)],
>         "name": "Nya 语录",
>         "description": "随机发送 ZincNya 语录",
>         "auth": True,
>     }
> ```

`handlers` 中的元素也可以指定 group：

```python
{"handler": MessageHandler(filters.TEXT, callback), "group": 1}
```

不手动填写 `group` 字段时，处理器默认归入第 0 组。

---

## PTB group 行为

`python-telegram-bot` 的 handler group 规则是：

- 同一个 group 内，只有第一个匹配该 update 的 handler 会执行。
- 不同 group 会按 group 编号从小到大依次处理同一个 update。
- handler 抛出 `ApplicationHandlerStop` 后，后续 group 不再处理该 update。

因此本项目里，group 不只是分类，更是控制消息处理优先级、拦截阻断的调度开关。

---

## 当前 group 总览

| Group | 来源 | 职责 |
|------:|------|------|
| `-1` | [utils/core/appLifecycle.py](../utils/core/appLifecycle.py) | 全局消息收集器，先于其他 handlers 收集消息到内部队列 |
| `0` | 大多数 [handlers/](../handlers/) 模块 | 命令、按钮回调、reaction、shutdown mention dispatcher |
| `1` | [handlers/afc.py](../handlers/afc.py) | AFC 意图检测，检测工具调用意图并注入上下文到 bot_data 推送层 |
| `2` | [handlers/llm.py](../handlers/llm.py) | LLM 普通消息自动回复，消费 bot_data 推送层的扩展上下文 |

---

## Group -1：全局消息收集

[utils/core/appLifecycle.py](../utils/core/appLifecycle.py) 在 `loadHandlers(app)` 后额外注册：

```python
app.add_handler(MessageHandler(filters.ALL, messageCollector), group=-1)
```

这一条全匹配消息收集器固定在 -1 分组，把所有收到的 `update.message` 放入 `state.getMessageQueue()`。

这个 handler 仅做数据留存，不会修改消息、也不会阻断后面任何分组继续执行，相当于每条消息路过的第一道登记窗口。

---

## Group 0：命令、回调与高优先级消息处理

### [handlers/start.py](../handlers/start.py)

| Handler | 条件 | 说明 |
|---------|------|------|
| `CommandHandler("start")` | `/start` | 白名单鉴权和欢迎消息 |

`auth=False`，由 handler 内部处理白名单逻辑。

### [handlers/shutdown.py](../handlers/shutdown.py)

| Handler | 条件 | 说明 |
|---------|------|------|
| `CommandHandler("shutdown")` | `/shutdown` | ops 关机 |
| `CommandHandler("restart")` | `/restart` | ops 重启 |
| `CommandHandler("status")` | `/status` | 查看运行状态 |
| `MessageHandler(filters.TEXT & filters.Entity(MessageEntityType.MENTION))` | 文本消息中含 mention entity | 处理 `@bot + 关机/重启/运行状态` 等自然语言控制 |

`_mentionDispatch()` 是当前唯一会抛出 `ApplicationHandlerStop` 的 Telegram handler。

命中 shutdown 关键词后，它会阻断后续 group，避免同一条 `@bot 关机` 再进入 group 1 的 LLM 自动回复。

### [handlers/llmCommand.py](../handlers/llmCommand.py)

| Handler | 条件 | 说明 |
|---------|------|------|
| `CommandHandler("llm")` | `/llm ...` | ops 专用 LLM 控制命令 |

`auth=False`，使用 `Permission.LLM` 做独立鉴权。

### [handlers/llmReview.py](../handlers/llmReview.py)

| Handler | 条件 | 说明 |
|---------|------|------|
| `CallbackQueryHandler(pattern="^llm:review:")` | LLM 回复审核按钮 | Telegram 端回复审核 |
| `CallbackQueryHandler(pattern="^llm:memreview:")` | LLM 记忆审核按钮 | Telegram 端记忆操作审核 |

`auth=False`，内部根据 review 数据和权限处理。

### [handlers/reaction.py](../handlers/reaction.py)

| Handler | 条件 | 说明 |
|---------|------|------|
| `MessageReactionHandler` | reaction update | 记录 reaction 到聊天历史 |

`auth=False`。

### [handlers/book.py](../handlers/book.py)

| Handler | 条件 | 说明 |
|---------|------|------|
| `CommandHandler("book")` | `/book ...` | 搜书命令 |
| `MessageHandler(filters.Regex(r"^(找书|搜书)\s+.+") & ~filters.COMMAND)` | 文本触发词 | 搜书自然语言触发 |
| `CallbackQueryHandler(pattern="^book:")` | 书籍搜索按钮 | 搜书翻页/操作回调 |

使用 loader 默认白名单鉴权。

### [handlers/stickers.py](../handlers/stickers.py)

| Handler | 条件 | 说明 |
|---------|------|------|
| `CommandHandler("findSticker")` | `/findSticker ...` | 查找 Telegram 表情包 |
| `CallbackQueryHandler(pattern="^sticker:")` | sticker 按钮 | 下载/操作表情包 |

使用 loader 默认白名单鉴权。

### [handlers/todos.py](../handlers/todos.py)

| Handler | 条件 | 说明 |
|---------|------|------|
| `CommandHandler("todos")` | `/todos ...` | 待办事项命令 |
| `CallbackQueryHandler(pattern="^todos:")` | todos 按钮 | 待办事项回调 |

使用 loader 默认白名单鉴权。

### [handlers/nya.py](../handlers/nya.py)

| Handler | 条件 | 说明 |
|---------|------|------|
| `CommandHandler("nya")` | `/nya` | 随机发送 ZincNya 语录 |

使用 loader 默认白名单鉴权。

---

## Group 1：AFC 意图检测

### [handlers/afc.py](../handlers/afc.py)

| Handler | 条件 | 说明 |
|---------|------|------|
| `MessageHandler(filters.TEXT & ~filters.COMMAND)` | 非命令文本消息 | 检测用户消息中的 AFC 工具调用意图 |

AFC handler 放在 group 1 的原因：

- **独占 group 1**：确保对每条 TEXT 消息都运行（无同 group 竞争）
- **早于 LLM**：在 group 2 的 LLM 处理前完成工具上下文注入
- **晚于控制命令**：group 0 的 shutdown mention dispatcher 命中后会抛出 `ApplicationHandlerStop`，阻止 AFC 和 LLM 运行

工作流程：

1. 检测用户消息中是否包含工具调用意图（calc / search / weather / datetime 等）
2. 如果命中，将工具 API 文档和使用说明构建成上下文块
3. 推入 `bot_data[f"llm_extra_blocks_{chatID}"]["afc"]`（bot_data 推送层）
4. 后续 group 2 的 LLM handler 会读取并消费这个上下文块

`auth=False`，依赖 group 2 的 LLM handler 做权限检查。

---

## Group 2：LLM 自动回复

### [handlers/llm.py](../handlers/llm.py)

| Handler | 条件 | 说明 |
|---------|------|------|
| `MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND)` | 非命令文本、图片、图片 document | LLM 自动回复入口 |

LLM 放在 group 2 的原因：

- group 0 的 shutdown mention dispatcher 必须先看到 `@bot 关机/重启/运行状态`。
- shutdown dispatcher 命中后会抛出 `ApplicationHandlerStop`，阻止 group 1 的 AFC 和 group 2 的 LLM 运行。
- group 1 的 AFC 意图检测先运行，将工具上下文注入到 bot_data 推送层。
- LLM 在最后运行，消费 bot_data 推送层中的所有扩展上下文（AFC / 未来可能的其他模块）。

LLM handler 自己负责二次 gate：

- LLM 总开关。
- command guard。
- whitelist。
- 私聊始终允许。
- 群聊按 `groupTriggerMode` 判断：
  - `mention`：必须 `@bot`。
  - `keyword`：`@bot` 或命中配置关键词。
- 未触发时尽早返回，不下载图片、不进入 rate limit、不进入 debounce。

---

## 当前无需重编排的原因

当前分组已经表达了真实依赖：

1. group `-1` 是纯收集器，不参与业务决策。
2. group `0` 处理命令、按钮、reaction，以及需要优先于 LLM 的 shutdown mention dispatch。
3. group `1` 处理 AFC 意图检测，为 LLM 准备工具上下文（可扩展为其他上下文注入模块）。
4. group `2` 只放 LLM 自动回复，让它天然晚于控制类消息和上下文注入，作为”兜底处理普通消息”的最后一环。

这比把所有 handler 都放到 group `0` 更安全，因为 LLM 是唯一需要”兜底处理普通消息”的模块，应该位于最低优先级。

---

## 需要注意的边界

### 同一 group 内只有第一个匹配 handler 执行

loader 通过 `pkgutil.iter_modules()` 动态扫描模块，模块加载顺序不应该作为业务依赖。

因此如果两个 group `0` handlers 可能匹配同一类 update，不能依赖“谁先注册”来表达优先级；应该改用不同 group 或更精确的 filter。

### 新增普通消息 handler 时要谨慎

如果未来新增非命令 `MessageHandler`，需要先判断它和 LLM、AFC 的关系：

- 如果它应该优先于 LLM，并且命中后不应让 LLM 继续处理，应放在 group `0` 并在处理成功后抛出 `ApplicationHandlerStop`。
- 如果它是上下文注入型（为 LLM 准备额外信息），应放在 group `1` 并写入 bot_data 推送层的独立槽位（参考 AFC 的实现）。
- 如果它只是旁路记录，不应阻断后续处理，应避免抛 stop。
- 如果它是 LLM 之后的后处理，应放到 group `3` 或更后。

### 新增自然语言控制命令时优先考虑 shutdown dispatcher 模式

类似 `@bot 关机` 这种控制语义，应在 LLM 之前处理并阻断 LLM。

如果未来增加更多 `@bot + 固定关键词` 控制功能，可以考虑抽出一个统一的 group `0` mention command dispatcher，而不是继续让 LLM 判断。

---

## 建议约定

后续新增 handler 时按以下规则选择 group：

| 场景 | 建议 group | 是否 stop |
|------|-----------:|-----------|
| 全局无副作用收集/观测 | `-1` | 否 |
| Telegram 命令 `/xxx` | `0` | 通常否 |
| CallbackQuery 按钮 | `0` | 通常否 |
| 需要优先于 LLM 的自然语言控制 | `0` | 命中后是 |
| 为 LLM 准备额外上下文（意图检测/工具注入等） | `1` | 否 |
| LLM 普通消息自动回复 | `2` | 否 |
| LLM 后置处理/观测 | `3` | 通常否 |

**上下文注入模块（group 1）的设计约束**：

- 使用 bot_data 推送层：`bot_data[f"llm_extra_blocks_{chatID}"][模块id] = (tier, label, content)`
- 每个模块占用独立槽位（以模块 id 为 key），互不覆盖
- 消费端（LLM）在 `buildConversationContext` 中读取并整体 `pop` 清除
- 生产端在写入前先清除自己上一条消息遗留的槽位
- 详见 `docs/bot-data-push-layer.md`

当前项目暂不需要实际重编排；更重要的是保持这些约束，并在新增 handler 时显式说明它和 LLM 的先后关系。