# Bot data 扩展上下文推送层

> 最后更新：2026-10-26
>
> **文档定位**：说明 Telegram handler 如何传递消息级的扩展上下文，以及这份数据如何进入 LLM 请求。
>
> Written by ZincNya~ ❤

---

## 概述

一个 LLM 请求需要的上下文，可能由多个相互独立的模块准备。上下文组装器应负责组织内容、排序和渲染，却不该随着每个新来源的加入而导入相应模块、调用它们的业务逻辑，并增加来源专属分支。

Bot data 推送层为这些模块提供一条共同的消息级通道：较早运行的 Telegram handler 把本条消息需要的扩展数据写入 `context.bot_data`；后续的 LLM handler 按同一条消息读取并清除它，再把内容放进普通请求数据。提供者负责判断何时注入、准备什么内容；消费者只处理统一的数据契约。

这套设计是为了给扩展提供独立的数据交接位置。AFC 是触发推送层落地的契机，也是目前实际使用该通道的模块；它不是这层设计的唯一目标。日程、检索结果或其他扩展信息可以沿用同一交接方式，而上下文组装不需要逐个认识这些模块。

本文介绍设计动机、数据格式、消息生命周期、并发隔离和接入约定。需要区分的是：这里描述的是 Telegram handler 之间的短期上下文交接；它不负责保存长期状态，也不涵盖 LLM 对回复内容的后处理。

## 目录

- [设计动机与决策](#设计动机与决策)
- [数据契约](#数据契约)
- [生命周期](#生命周期)
- [当前提供者：AFC](#当前提供者afc)
- [消费端：LLM handler](#消费端llm-handler)
- [并发与清理](#并发与清理)
- [接入新的扩展模块](#接入新的扩展模块)
- [边界与限制](#边界与限制)

---

## 设计动机与决策

扩展模块的业务判断和数据准备，应留在扩展自己的一侧。上下文组装器只应拿到已经准备好的内容，而无需知道它由谁产生、在哪个模块生成，或使用什么服务。

如果上下文组装器直接导入每个扩展，就会逐渐承担各模块的检测和拼接职责。每增加一个来源，都要修改共享的上下文代码；共享代码因此和可选功能逐一耦合。推送层把依赖方向倒过来：扩展向共同通道提供数据，LLM 消费端通过契约读取数据，不反向依赖提供者。

AFC 是第一个需要在 LLM 回复之前判断消息并准备附加上下文的扩展。它让这项交接需求变得具体，但推送层表达的是更一般的模块边界：扩展可单独准备消息相关数据，后续处理者按契约消费。

### 为什么按消息隔离

LLM 防抖缓冲按 `(chatID, userID)` 聚合消息。同一聊天可能有多条消息同时经过 handler；只用 `chatID` 作为存储位置，会使不同消息的扩展数据互相覆盖或被错误读取。因此每份数据都以聊天 ID 和 Telegram message ID 共同定位。

### 为什么 handler 之后转成请求数据

`bot_data` 用于 Telegram handler 链路中的短暂交接。LLM handler 读取后，扩展块就随消息进入 `MessageInput`，并在防抖批次中进入 `GenerationRequest`。后台生成和审核重试复用请求对象，不需要再访问 handler 的共享状态，也不会因重试重复读取或遗失扩展内容。

---

## 数据契约

外层 key 标识一条 Telegram 消息，内层 key 是提供模块自己的槽位：

```python
context.bot_data[f"llm_extra_blocks_{chatID}_{messageID}"][moduleID] = (
    tier,
    label,
    content,
)
```

当前契约中的三项分别表示：

| 字段 | 含义 |
| --- | --- |
| `tier` | 上下文层级，用于和其他上下文块一起排序 |
| `label` | 展示该块时使用的简短标签 |
| `content` | 扩展提供的正文内容 |

槽位名只用于生产端区分和管理数据。消费者读取内层字典的全部值，不根据模块名分支。

每个模块只操作自己的槽位。比如 `"afc"` 是 AFC 当前使用的槽位；之后新增模块应使用自己的稳定标识，避免覆盖同一条消息中其他提供者的数据。

---

## 生命周期

消息沿 Telegram handler 链路交接时，数据按以下顺序流动：

1. 较早运行的扩展 handler 针对当前消息准备数据，并写入对应的消息级槽位。
2. 后续的 LLM handler 读取相同聊天 ID 和 message ID 的槽位，并从 `bot_data` 删除这条消息对应的外层 key。
3. handler 把读取到的块保存到 `MessageInput.extraBlocks`，再将消息交给防抖缓冲。
4. 后台流程创建 `GenerationRequest`；批次请求通过 `extraBlocks` 汇总其中各条消息携带的块。
5. `utils/llm/generation.py::generateResponse` 将这些普通数据传入生成客户端；上下文组装器与其他上下文一起排序和渲染。
6. 审核重试复用保存的 `GenerationRequest`，无需回到 `bot_data`。

消息 handler 的交接在 Telegram update 处理期间完成。防抖等待时间不会延长共享槽位的生命周期；后续后台任务持有的是请求对象中的数据副本。

---

## 当前提供者：AFC

AFC 是当前注册在 group 1 的消息级上下文生产者。它早于 group 2 的 LLM handler 运行，检测当前正文或图片说明中的工具调用意图，并在命中时构建工具上下文块。

其槽位使用消息 ID 隔离，模块标识为 `"afc"`。写入前会清除本模块对当前消息留下的旧槽位；如果没有命中意图或没有生成内容，则不写入。检测或构建过程异常时，AFC 静默降级，让后续 LLM handler 继续按普通消息处理。

示意代码：

```python
key = f"llm_extra_blocks_{chatID}_{messageID}"

slots = context.bot_data.get(key)
if slots is not None:
    slots.pop("afc", None)

block = buildAFCContextBlock(toolNames)
if block:
    context.bot_data.setdefault(key, {})["afc"] = (
        ContextTier.TOOLS,
        "工具",
        block,
    )
```

这里的检测规则、工具清单和工具上下文格式属于 AFC 的职责。推送层只规定如何暂存和交接，不规定扩展如何得出内容。

---

## 消费端：LLM handler

`handlers/llm.py::_takeExtraBlocks` 根据当前消息的聊天 ID 和 message ID 取出所有槽位，然后删除该消息的外层 key：

```python
def _takeExtraBlocks(context, message) -> tuple[tuple, ...]:
    key = f"llm_extra_blocks_{message.chat_id}_{message.message_id}"
    return tuple((context.bot_data or {}).pop(key, {}).values())
```

这一步发生在 LLM 开关、命令识别、授权和触发判断之前。因此，即使当前消息之后不会进入 LLM 生成，已经为它准备的槽位也会被消费，避免留给之后的处理。

消费完成后，LLM 链路只持有普通扩展块，不再依赖提供者的槽位名称。当前 `contextBuilder` 根据块的层级与其他上下文统一排序和渲染。

---

## 并发与清理

- **消息隔离**：外层 key 同时包含聊天 ID 和 message ID，不同消息不会通过同一槽位交接。
- **提供者隔离**：内层 key 是模块标识；每个生产者只清除或写入自己的槽位。
- **读取即清除**：消费者使用 `pop` 取出该消息的所有槽位，防止正常处理后残留。
- **重复处理保护**：生产者在准备当前消息时清除自己的旧槽位，避免 update 重入时沿用过期结果。
- **请求接管生命周期**：handler 消费后，数据随 `MessageInput` 和 `GenerationRequest` 继续流转，不在共享字典中等待后台任务。

消息级隔离解决的是不同消息之间的数据串用。它并不提供持久化、跨进程同步或所有权租约；扩展仍应遵循 Telegram handler 的执行顺序，并确保同一 update 的提供者在消费者之前运行。

---

## 接入新的扩展模块

新的上下文提供者应先判断自己准备的数据是否属于当前 Telegram 消息，并确认它要进入 LLM 输入链路。符合条件时：

1. 在 LLM handler 之前处理该消息，并使用同一组聊天 ID、message ID 构造外层 key。
2. 仅写入和清理自己的模块槽位，值遵循 `(tier, label, content)` 契约。
3. 未产生有效内容时不写空块；每次处理当前消息时清理自己的旧槽位。
4. 在 `modulesRegistry.py` 登记运行文件和测试文件，并为 handler 声明合适的处理顺序。
5. 通过 LLM handler 的通用消费路径传递数据，不增加生产者名称判断或来源专属字段。
6. 测试命中、未命中、处理异常、LLM 不消费该消息以及同一聊天并发消息等情况。

`tier` 决定排序，不自动证明内容可信。用户数据、第三方服务结果和其他不可信文本应按其真实来源选择合适层级，并由上下文渲染逻辑保留对应的信任边界。

如果数据需要跨消息复用或跨进程保留，`bot_data` 推送层不适用；应放入拥有明确生命周期和读写契约的状态存储。

---

## 边界与限制

1. 当前实际生产者是 AFC；日程等其他来源是可沿用同一契约的扩展方向，不代表代码中已经存在对应模块。
2. 当前通道服务 Telegram handler 输入侧的数据交接；控制台等其他生成入口不会自动经过 `bot_data`。
3. 本文描述普通扩展上下文如何到达 LLM 输入，不代表 LLM 的生成、工具执行或回复后处理都已与 AFC 解耦。当前生成客户端仍有 AFC 专用的输出处理调用。
4. `bot_data` 是进程内共享字典。消费者未运行时，相应槽位没有独立的定时过期清理；这层不适合持久化、跨进程传递或长期状态。

相关文档与代码：

- [LLM handler](llm-handler.md)
- [上下文组装](llm-context-assembly.md)
- [AFC](afc.md)
- [Telegram handler 顺序](telegram-handlers-group.md)
- [AFC handler](../handlers/afc.py)
- [LLM handler](../handlers/llm.py)
