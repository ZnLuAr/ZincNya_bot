# LLM Handler 架构文档

> 最后更新：2026-07-10
> Written by ZincNya~ ❤

---

## 概述

`handlers/llm.py` 是 LLM 功能的 Telegram 入口，整条从接收用户消息到产出回复、分发审核的协调流程都由它串联起来。随着功能在演进过程中，陆续叠上图片解析、消息防抖、群聊触发规则、长期记忆读写、多模式审核、网页抓取一堆能力，这个 handler 早就不再是简单的「收到消息就调用模型」单一线程，现在，不如说 LLM 就是一个分多阶段协同调度的协调器……

这份文档的目的是让未来维护者可以不必逐行翻代码，就能快速理解：

- 一条消息从进入 handler 到最终回复/被审核/被丢弃，经过了哪些阶段；
- 每个 helper 函数为什么存在，以及它解决了什么问题；
- 防抖、触发、审核、URL、图片这些相对独立的机制是如何相互作用的；
- 哪些行为是刻意保持的边界，不能"顺手统一"。

## 目录

- [设计决策](#设计决策)
  - [为什么拆成大量小 helper](#为什么拆成大量小-helper)
  - [为什么 `_dispatchLLMReply` 要做成后台任务](#为什么-_dispatchllmreply-要做成后台任务)
  - [为什么 URL 读取必须满足触发条件且能读取到存在意图才执行](#为什么-url-读取必须满足触发条件且能读取到存在意图才执行)
  - [为什么 reply-to 只贡献 URL 候选，不贡献意图](#为什么-reply-to-只贡献-url-候选不贡献意图)
  - [为什么三种 auto mode 不合并](#为什么三种-auto-mode-不合并)
- [文件结构](#文件结构)
- [处理流水线总览](#处理流水线总览)
- [同步阶段：`handleLLMMessage`](#同步阶段handlellmmessage)
- [后台阶段：`_dispatchLLMReply`](#后台阶段_dispatchllmreply)
- [触发判断](#触发判断)
- [防抖聚合](#防抖聚合)
- [URL 读取集成](#url-读取集成)
- [图片处理](#图片处理)
- [回复分发（`autoMode`）](#回复分发automode)
- [上下文组织](#上下文组织)
- [记忆操作分发（`memoryAutoApprove`）](#记忆操作分发memoryautoapprove)
- [安全边界](#安全边界)
- [Handler 注册](#handler-注册)
- [命令接口（概览）](#命令接口概览)
- [已知局限](#已知局限)
- [维护原则](#维护原则)

---

## 设计决策

当初搭建这套管线时每一处拆分、限制都反复权衡过，记录下来，避免之后修改时忘记取舍缘由。

### 为什么拆成大量小 helper

最早期 `handleLLMMessage` 是两百行以上的巨型函数，权限校验、图片下载、模型调用、审核入队所有逻辑全部堆在一起，读起来一团乱。

那么，就需要对这个函数进行拆分了……拆分的标准十分清晰：单个辅助函数只负责一类判断 / 数据转换，函数名直接体现功能；顶层主逻辑只做流程调度，像指挥流水线的协调者，这样就不用塞满细碎判断。

要是不拆分，后续新增功能会不断膨胀主函数，同一段代码里混杂网络请求、权限、上下文构造、记忆解析，排查问题会格外费劲。哪怕现在多了一堆小工具，可维护性都要好很多。

### 为什么 `_dispatchLLMReply` 要做成后台任务

用户连续刷屏发送多条消息时，不能每条都立刻发起 LLM 请求，需要短暂窗口做消息聚合合并。所以同步 handler 只负责校验消息、存入缓冲、拉起后台任务，真实的模型调用，则全部延后到防抖窗口结束后一起执行。

不过这也带来了一个副作用：后台任务会因此脱离同步装饰器 `@handleTelegramErrors` 的保护，**所有异常都必须在函数内部手动捕获记录**。

### 为什么 URL 读取必须满足触发条件且能读取到存在意图才执行

URL 抓取不能单独作为唤醒客户端的入口，裸发链接不能主动联网，这是出于安全考虑……

必须同时满足三条限制：

1. LLM 已经按现有规则被触发（私聊、`@bot` mention、或群聊关键词）；
2. 当前用户消息表达了明确的读取意图；
3. `urlReadEnabled` 已开启。

这么设计，是为了避免客户端无差别主动访问外网，减少不必要网络开销与 SSRF 风险。~~文明上网从你我做起喵。~~

### 为什么 reply-to 只贡献 URL 候选，不贡献意图

回复一条带 URL 的消息说"帮我看看"是合理场景，所以 reply-to 的文本必须能提供 URL 候选。但不能把回复文本里的读取指令当作有效意图。

这是因为如果放开限制，攻击者就可以提前发送一条包含 “帮我读取链接” 的消息，诱导其他人回复这条消息被动触发抓取了，是很严重的安全隐患。因此做了隔离：

- `urlIntentText` 只取自用户本次发送的原文；
- `urlCandidateText` 才合并当前文本与回复消息，仅用来提取链接。

### 为什么三种 auto mode 不合并

`autoMode` 分为 `on / off / console` 三类，分别适配完全不同的使用场景，是没办法简单合并成二元开关的——

- `on`：直接发送，适合信任环境（私聊调试）；
- `off`：Telegram 审核，ops 在对话里批准/驳回；
- `console`：控制台/chatScreen 审核，适合 bot 管理员离线审查。

这些是不同部署阶段的需求，不应该用单一"审核/直接"二值覆盖。

---

## 文件结构

```
handlers/
├── llm.py              # 本文档主体：LLM 消息处理入口
├── llmReview.py        # Telegram 审核消息与 callback（inline keyboard）
└── llmCommand.py       # /llm ops 命令

utils/
├── markdownToHtml.py   # Markdown → Telegram HTML 转换工具
├── telegramHelpers.py  # Telegram 消息操作公共工具（含 sendLLMReplyWithMarkdown）
├── chatHistory.py      # 聊天历史持久化
└── operators.py        # ops 权限管理

utils/llm/
├── __init__.py         # 统一导出
├── config.py           # 配置：开关、模式、模型、触发、记忆、URL
├── state.py            # 运行时状态：审核队列、速率限制、防抖、one-shot
├── contextBuilder.py   # 上下文组装（memory + history + URL + 当前消息）
├── urlReader.py        # URL 提取、意图判断、安全抓取、内容提取
├── review.py           # console 审核动作（send/retry/cancel/editSubmit）
├── vision.py           # 图片提取与下载
├── client/
│   ├── _generate.py    # generateReply：主 LLM 调用
│   ├── _guardrails.py  # SYSTEM_GUARDRAILS 安全规则
│   └── _request.py     # 重试策略
├── memory/
│   ├── action.py       # 记忆操作解析、校验、执行
│   ├── database.py     # memory_entries 存储与格式化
│   └── ui.py           # Telegram 记忆审核 UI（inline keyboard）
└── knowledge/
    ├── database.py     # knowledge_entries 存储
    ├── loader.py       # Markdown 文件加载与解析
    ├── retriever.py    # BM25 检索逻辑
    └── tokenizer.py    # 中英文分词
```

---

## 处理流水线总览

一条带文字或图片的 Telegram 消息，在进入 `handleLLMMessage` 后，会分成同步预处理、后台生成两大阶段流转：

```
┌─────────────────────────────────────────────────────────┐
│                  handleLLMMessage (同步)                 │
│                                                         │
│  1. LLM 全局开关                                        │
│  2. message 存在                                        │
│  3. 获取 raw 文本                                       │
│  4. 命令排除（/foo）                                    │
│  5. :edit 反向索引拦截                                  │
│  6. raw 文本非空                                        │
│  7. 白名单授权                                          │
│  8. 触发判断（私聊 / @ / 关键词）                       │
│  9. 速率限制                                            │
│ 10. 提取图片 refs                                       │
│ 11. 构造 pureText / urlIntentText / urlCandidateText    │
│ 12. 下载图片 + 补充 prompt 说明                         │
│ 13. appendPendingMessage + 起/替换后台任务              │
│                                                         │
└──────────────────────────┬──────────────────────────────┘
                           │ asyncio.create_task
                           ▼
┌─────────────────────────────────────────────────────────┐
│                _dispatchLLMReply (后台任务)              │
│                                                         │
│  1. sleep LLM_DEBOUNCE_SECONDS                          │
│  2. 聚合 batch（合并多条 pending）                      │
│  3. URL 读取（按意图和候选文本）                        │
│  4. 构造 displayOriginalMsg（+ URL 摘要）               │
│  5. typing action                                       │
│  6. generateReply（传递 urlContexts / images）          │
│  7. addRateLimit（仅成功时）                            │
│  8. 从 reply 中解析并校验 memory actions                │
│  9. 分发 output（文字回复 + 记忆操作）                  │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

---

## 同步阶段：`handleLLMMessage`

位置：`handlers/llm.py`

```python
@handleTelegramErrors(errorReply="……呜、刚才这条消息没能处理好喵……")
async def handleLLMMessage(update, context):
    ...
```

### 每一步的职责

| 序号 | 调用 | 职责 |
|------|------|------|
| 1 | `getLLMEnabled()` | LLM 全局开关；关则直接 return，不经过流程 |
| 2 | `update.message` | 群入群、系统推送等无实质内容消息，直接忽略 |
| 3 | `_getRawMessageText(message)` | 读取文本，没有文本则读取图片 caption |
| 4 | `_isCommandLikeMessage(rawText)` | `/` 开头的指令直接交给命令模块，不走 LLM 流程 |
| 5 | `handleEditReply(message, context)` | 命中编辑索引直接消费，不再往下走 |
| 6 | `rawText` 非空 | 只有图片没有文字说明，则跳过触发对话 |
| 7 | `_getAuthorizedUserID(message)` | 白名单校验，返回 userID 或 None |
| 8 | `_shouldTriggerLLM(message, botUsername, isPrivate)` | 私聊默认触发(`True`)，群聊需要 @或匹配关键词 |
| 9 | `isRateLimited(userID)` | 处于冷却期给用户提示后返回 |
| 10 | `_extractImageRefsForLLM(message)` | 提取当前消息图片，以及被回复消息里的图片 |
| 11 | `_preparePurePromptText(...)` | 返回四元组：pureText, includeContext, urlIntentText, urlCandidateText |
| 12 | `_downloadImagesAndAnnotatePrompt(...)` | 下同步下载图片，失败则在 prompt 中追加说明文字 |
| 13 | `_enqueueLLMDebounce(...)` | 消息入缓冲，取消旧任务并拉起新后台协程 |

### `_preparePurePromptText` 的 4 个返回值

```python
def _preparePurePromptText(message, rawText, botUsername) -> tuple[str, bool, str, str]:
```

| 返回字段 | 含义 | 来源 |
|----------|------|------|
| `pureText` | 	最终送入模型的对话原文，可能前置 reply-to 引用 | 原始文本清理 @标记、`#context` 标识，附加回复消息正文 |
| `includeContext` | 是否启用记忆、历史对话上下文 | `#context` 标记 + `memoryEnabled` 全局开关 + `contextOnce` |
| `urlIntentText` | 仅用来判断网页抓取意图 | 去 mention/`#context` 后的纯 current 文本 |
| `urlCandidateText` | 用来提取全部待抓取链接 | `pureText`（去 mention）+ reply-to text/caption |

四类文本用途完全隔离，**不能混用**，在安全边界一节会再细说

---

## 后台阶段：`_dispatchLLMReply`

```python
async def _dispatchLLMReply(
    debounceKey, userID, username, chatID, triggerMsgID, context
):
```

### 关键注意点

- 跑在 `asyncio.create_task` 中，与同步阶段断开
- **`@handleTelegramErrors` 不生效**（装饰器只覆盖同步阶段），必须自己 `try/except`
- `CancelledError` 必须重新 raise（让替换后的新任务能接管）
- 只有 `generateReply` 成功产出回复，才会给用户新增 `addRateLimit(userID)` 限流冷却，请求失败不计入限制

### 步骤细节

| 序号 | 调用 | 备注 |
|------|------|------|
| 1 | `asyncio.sleep(LLM_DEBOUNCE_SECONDS)` | 用户连续发消息时会被新任务取消 |
| 2 | `_collectDebouncedBatch(debounceKey)` | 返回 `(combinedText, includeContext, images, urlIntentText, urlCandidateText)`；空则 return |
| 3 | `readURLContextsForUserText(intentText, candidateText)` | 前提：`urlReadEnabled` 且命中意图词；否则返回 `[]` |
| 4 | `_formatDisplayOriginalMsg(...)` | 生成 ops 审核用的展示原文；带图/URL 时追加摘要 |
| 5 | `_sendTypingActionSafely(...)` | 发送 typing，失败静默 |
| 6 | `_generateReplyOrNotify(...)` | 调用 `generateReply(..., images, urlContexts)`；失败返回 None 并提示用户 |
| 7 | `addRateLimit(userID)` | 仅成功生成回复时添加冷却 |
| 8 | `_parseAndValidateMemoryActions(reply, includeContext)` | 剥离 `<MEMORY_ACTION>` 块；越界动作直接丢弃 |
| 9 | `_dispatchGeneratedOutput(...)` | 按 autoMode 分发文字回复；按 memoryAutoApprove 执行记忆操作 |

---

## 触发判断

位置：`_shouldTriggerLLM` / `_isBotMentioned` / `_matchesGroupTriggerKeyword`

| 聊天类型 | 触发模式 `mention` | 触发模式 `keyword` |
|----------|---------------------|---------------------|
| 私聊 | 总是触发 | 总是触发 |
| 群聊 | 仅 `@bot` | `@bot` 或 匹配预设关键词 |

**`_isBotMentioned`** 使用 Telegram `MessageEntityType.MENTION` 原生实体精确匹配 `@bot_username`，避免单纯子串误匹配其他同名 bot 带来的误判（例如 `@notmybot`）。

**群聊关键词**由 `getGroupTriggerKeywords()` 配置，大小写不敏感，仅做包含匹配；设计上，**不支持**群内无条件全量触发，防止无关消息持续稀释上下文、过度占用接口资源。

---

## 防抖聚合

位置：`utils/llm/state.py`

### 缓存数据结构

```python
_pendingMessages: dict[str, list[dict]]
# debounceKey -> [
#   {"text", "includeContext", "images", "urlIntentText", "urlCandidateText"},
#   ...
# ]
# 实际上就是一个包含了单条消息数据的字典……
_pendingTasks: dict[str, asyncio.Task]
# 再以 debounceKey 绑定当前运行中的后台协程
```

`debounceKey` 有拼接规则 `f"{chatID}:{userID}"`。同一用户在不同 chat 中分别聚合，也即不同聊天、不同用户缓冲完全隔离。

### 工作流程

1. 收到消息，`appendPendingMessage()` 写入缓冲列表；若超过单用户最大缓冲条数(`LLM_PENDING_MSG_LIMIT`) 时，就返回 False 提示"消息太多"。
2. 若已有旧 `asyncio.Task` 在跑，则执行 `task.cancel()` 取消旧的进程。
3. 创建新的 `_dispatchLLMReply` task，然后注册 `done_callback` 进行清理。
4. 新任务等待防抖时长 (`LLM_DEBOUNCE_SECONDS`) 后，批量合并处理全部缓冲消息。

### 消息合并规则

- 多行文本 `text` 使用换行拼接。
- 任意一条消息开启上下文 (`includeContext` 为 `True`)，整体就启用记忆与历史；`contextOnce` 单次标记仅生效一轮。
- 图片资源列表 `images` 直接串联。
- `urlIntentText` / `urlCandidateText` 用换行拼接。

---

## URL 读取集成

位置：`utils/llm/urlReader.py`

### 同时满足三条才会抓取

1. 网页读取总开关 `urlReadEnabled == True`。
2. `hasURLReadIntent(urlIntentText)` 返回 True，即识别到解析、总结类需求；。
3. `extractURLs(urlCandidateText)` 非空，即成功提取到有效链接。

### 意图判断使用保守策略

- 显式标记 `#url` / `#readurl` 直接触发。
- 否定词优先：`不要 / 别 / do not / don't` 等出现则不触发。
- 匹配到关键词 `读 / 看 / 总结 / 分析 / 翻译 / summarize / explain` 等，判定为有需求。

### 上下文注入位置

由 `buildConversationContext()` 将结果作为 `<URL>` block 注入到 `<RETRIEVED_CONTEXT>` 内，按 tier 排序后，与 Memory/History 同级。

### 审核重试复用逻辑

`urlContexts` 会保存进审核项；retry 时**复用已抓取内容**，不重新联网。避免：

- 点击 retry 产生新的网络请求；
- 网页内容在审核前后变化导致语义漂移。

---

## 图片处理

位置：`utils/llm/vision.py`（ref 提取）+ `_generate.py`（调用策略）

### 图片资源 (ref) 提取

`_extractImageRefsForLLM(message)` 先从当前消息取，若消息没有图则回退到 `reply_to_message`。

### 下载时机

下载发生在同步阶段，**不在防抖后**。这样做是因为，下载失败时可以立即在 prompt 里加说明（"用户发送了图片但下载失败"），不用等到防抖结束才发现资源缺失。

### 调用策略

由 `visionModel` 是否等于 `model` 决定：

- **等于**：单调用，图片直接传主模型；
- **不等于**：双调用，先用视觉模型描述图片，再把文本描述给主模型，节省主模型 token。

---

## 回复分发（`autoMode`）

这块是生成完文本之后，决定消息直接发给用户、还是丢给管理员审核的分支逻辑，放在 `_dispatchTextReply` 里面。三种模式分开设计各有适配场景，混在一起反而会把逻辑搅乱。

| `autoMode` | 行为 |
|------------|------|
| `on` | 调用 `sendLLMReplyWithMarkdown()` 直接发送，内部会自动把 Markdown 转成 Telegram 能识别的 HTML 格式 |
| `off` | 不会直接下发用户，而向每个有 `Permission.LLM` 的管理员发 Telegram 审核消息；同时给触发的那条消息挂上👀 reaction |
| `console` | 入队 `addReviewItem(...)`，不往 Telegram 发审核卡片，转存入控制台的审核队列，适合管理员常驻终端离线复核内容 |

有个兜底的细节：要是系统里没配置任何管理员，off 和 console 模式不会一点反应都没有。客户端会主动在聊天窗口提示管理员权限配置有缺位，免得用户发完的消息彻底石沉大海……

### Markdown → HTML 转换

LLM 输出习惯用 Markdown 排版，但 Telegram 原生只支持一小部分富文本标签，所以单独抽了转换工具做适配，Markdown 格式会自动转换为 Telegram HTML 并渲染。

**实现链路**
- 转换核心工具：`utils/markdownToHtml.py::convertMarkdownToHtml()`
- 统一发送封装：`utils/telegramHelpers.py::sendLLMReplyWithMarkdown()`
- 调用点位：自动发送分支、审核通过重发、网络报错重试三处都会复用这套逻辑

**转换规则**：
| Markdown 语法 | HTML 输出 | 示例 |
|--------------|-----------|------|
| \`\`\`lang\n...\n\`\`\` | `<pre><code>...</code></pre>` | 代码块 |
| \`code\` | `<code>code</code>` | 行内代码 |
| `**text**` | `<b>text</b>` | 粗体 |
| `# title` | `<b>title</b>` | 标题 |

**安全防护设计**：

所有代码块、行代码内部字符都会做 HTML 转义，避免注入特殊标签造成界面错乱；正文里 `< > &` 这类符号也会统一替换成实体字符，杜绝简单 XSS 风险。

同时，工具不会去处理普通文本里的星号，像 `2*3*5`、`file_name.txt`，还有单个 `_`、跨行粗体和斜体——它们对应的 Markdown 格式在日常的聊天场景中实在是太常见了，以至于误判的概率实在是太高……所以这类内容不会被强行识别成排版标记。

而且，如果 Telegram 返回 "can't parse entities" 错误，会自动丢弃富文本格式、改用纯文本重新发送。同时在日志里记录一条 `Warning` 项方便事后排查。

---

## 上下文组织

给 LLM 拼接完整对话上下文的逻辑写在 `contextBuilder.py`。上下文的组装顺序是反复调试后定下的最优结构，目的是解决弱模型在长上下文里模型遗漏用户当前提问的问题，不建议随便调换块顺序

### 块顺序与格式

最终拼出的 user content 采用 **Query Reinforcement + 三层结构**（详见 [docs/llm-context-assembly.md](llm-context-assembly.md)）：

```
[核心任务]
你需要回答 <CURRENT_USER_MESSAGE> 块中的用户消息。
该消息将在下方出现。

<RETRIEVED_CONTEXT>
[来源：知识库]
<TRUSTED_KNOWLEDGE>
[以下是开发者提供的背景知识，仅作参考]
- [interests] 编程语言偏好: 熟悉 Python 和 Rust...
</TRUSTED_KNOWLEDGE>

[来源：长期记忆]
<UNTRUSTED_MEMORY>
[以下是长期记忆，仅作参考]
- (global:global, p=10, id=42, src=manual) 用户偏好简体中文回复
</UNTRUSTED_MEMORY>

[来源：对话历史]
<UNTRUSTED_HISTORY>
[以下是对话历史，仅作参考]
- [14:23:01] <用户> 你好
- [14:23:05] <ZincNya~> 你好喵
</UNTRUSTED_HISTORY>
</RETRIEVED_CONTEXT>

<CURRENT_USER_MESSAGE>
你喜欢什么编程语言？
</CURRENT_USER_MESSAGE>

<TASK_SYNTHESIS>

用户当前消息：
"你喜欢什么编程语言？"

请回答用户的消息。在回答时：
- 优先参考 <RETRIEVED_CONTEXT> 中标注为 [来源：XXX] 的信息
- 如果上下文中有与用户消息直接相关的内容，请在回答中体现
- 保持你的人设和说话风格
- 给出针对性的回复，而不是通用回复

上下文已按相关性排序，越靠前的越相关。
</TASK_SYNTHESIS>
...
</URL>
```

### 每一块的设计考量

1. **块的顺序是固定的三层结构（Query Reinforcement）**：
   - **层 1**：任务说明（告知 LLM 需要回答的是下方的 `<CURRENT_USER_MESSAGE>`）
   - **层 2**：`<RETRIEVED_CONTEXT>` 检索上下文（按 ContextTier 排序：知识库 → 长期记忆 / 对话历史 / URL 内容）
   - **层 3**：`<CURRENT_USER_MESSAGE>` 当前用户消息 + `<TASK_SYNTHESIS>` 合成指令

   这种结构通过前置任务说明强化指令，让模型在阅读上下文前就明确”最终要回答的是用户消息”，同时把用户消息放在上下文之后、紧邻合成指令，形成”提醒 → 上下文 → 任务 → 再次强化”的注意力引导，大幅减少”看了参考资料忘了用户提问”的情况。详见 [docs/llm-context-assembly.md](llm-context-assembly.md)。

2. 前置统一提示强化指令。单独分出「任务说明」「核心指令」两段前置文字，不用每个信息块都重复提醒模型优先级，精简 token 占用。
3. 区分独立标签块。知识库、记忆、历史、网页分开包裹专属标记，搭配注释说明区块作用，模型能清晰区分不同信息来源，不会混为一谈。
4. 记忆携带定位 ID。每条记忆末尾带上 ^数字ID，后面 LLM 发起记忆增删改操作时，能精准定位目标条目，不用模糊匹配文本内容。

### 实现位置

- 块顺序与 Query Reinforcement：`utils/llm/contextBuilder.py::buildConversationContext()`
- 记忆元数据格式：`utils/llm/memory/database.py::buildMemoryContextBlock()`
- 知识库块格式：`utils/llm/knowledge/database.py::buildKnowledgeContextBlock()`

### 维护注意事项

- **块顺序不可随意调整**：不建议调换各个信息块的前后排序，打乱会直接影响模型输出准确度；
- **块标记不可合并**：`<MEMORY>` / `<KNOWLEDGE>` / `<URL>` 语义不同，不能统一成单一 `<CONTEXT>` 标签，模型没法区分信息可信度；
- **记忆 id 字段必须保留**：虽然在行尾（`^id`），但 `<MEMORY_ACTION>` 指令依赖它来修改记忆
- **HTML 注释不可删除**：`<!-- ... -->` 提供块含义说明，删掉会让 LLM 失去上下文类型信息

---

## 记忆操作分发（`memoryAutoApprove`）

LLM 在回复里夹带 `<MEMORY_ACTION>` 标签、想要新增 / 修改 / 删除记忆时，会走到 `_dispatchMemoryActions` 做分发处理，区分自动通过和人工审核两条分支。

| 条件 | 行为 |
|------|------|
| `memoryAutoApprove == True` | `executeAction(...)` 立即执行，无需管理员插手 |
| `memoryAutoApprove == False` 且 `autoMode == "console"` | 推入控制台审核队列，等管理员手动确认 |
| `memoryAutoApprove == False` 且非 console | 给管理员推送 Telegram 记忆审核卡片 |
| 无 ops 且非 auto-approve | 静默丢弃本次记忆操作，仅打印 `Warning` 日志 |

这里和普通回复的兜底逻辑做了区分：用户能直观看见有没有文字回复，消息消失会疑惑；但记忆是后台隐性操作，用户大多感知不到，没必要额外弹窗提示，安静记录日志就足够啦。

---

## 安全边界

下面这些隔离规则都是踩过坑之后特意定下的，千万别为了代码简洁"顺手"合并逻辑，每一条都对应一类风险场景。

1. URL intent vs candidate 分离
> `urlIntentText` 和 `urlCandidateText` 必须来自不同源。意图只取自用户本次发言，链接候选可以带上回复消息内容。一旦合并，攻击者能通过转发带指令的消息，诱导其他人被动触发网页抓取，这是很危险的安全漏洞。

2. reply-to 只贡献 URL 候选，只提取链接，不生成读取意图
> 回复里的文字再怎么写"帮我看看这个"，都不会被判定为读取指令，仅用来捞里面的 URL，意图判定权限只留给用户本人当期消息。

3. 群聊不扩大触发面
> 只保留 @提及、指定关键词两种唤醒方式，不开放带问号或带链接就自动触发的这类宽松规则，避免群内大量无关消息频繁调用 LLM，浪费资源不说，还会稀释上下文。

4. 速率限制只在成功后计入
> 网络超时、参数错误、接口报错全部不计入冷却，用户遇到故障可以立刻重试，不会平白占用限流额度，也不会无限冷却……

5. 后台任务 `_dispatchLLMReply` 必须重新抛出 `CancelledError`
> 多条消息连续刷屏会不断新建、取消旧防抖任务，不把取消异常上抛就会导致新旧任务互相阻塞，消息聚合逻辑彻底乱掉。

6. 审核缓存 key 格式不做统一
> 客户端中有：
> ```
> llm_review_{chatID}_{reviewMsgID}         # 主 reply review
> llm_memreview_{chatID}_{reviewMsgID}      # 主 memory review
> llm_editidx_{reviewMsgID}                 # :edit 反向索引
> ```
> 
> reply 的 `chatID` 在 callback 里转 `int`；memory 的 `chatID` 不转。回复审核、记忆审核、编辑索引三类 key 命名规则分开，是早期迭代遗留的实现，改动要同步修改全链路回调匹配逻辑，收益很低，没必要统一重构。

---

## Handler 注册

整套 handler 加载顺序是提前划分好 group 优先级的。其中，LLM 处理器固定占 group=2，不能随便调换序号。

```python
def register():
    return {
        "handlers": [
            {"handler": MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.IMAGE)
                & ~filters.COMMAND,
                handleLLMMessage,
            ), "group": 2},
        ],
        ...
    }
```

**group=2** 是关键：
- group 0: 命令、shutdown mention dispatcher
- group 1: AFC 意图检测（独占，先于 LLM）
- group 2: LLM 自动回复（本 handler）

要是把 LLM handler 挪去同组或是更早的序号，读取时机就会乱掉，工具数据还没写入就被消费，调用直接失效。group 0 的处理器还能通过 `ApplicationHandlerStop` 直接截断整条消息流程，一旦触发，后面 group 全都不会再执行（详见 `telegram-handlers-group.md`）。

---

## 命令接口（概览）

所有管控开关都归管理员用 Telegram /llm 指令调度，日常使用者是没有权限调整底层配置的——

```
/llm                         查看开关
/llm -on | -off              开启/关闭 LLM
/llm status                  显示完整配置
/llm model [switch <名称>]   主模型
/llm visionmodel [...]       视觉模型
/llm trigger mention|keyword 群聊触发模式
/llm keyword add|del <词>    触发关键词
/llm memory -on | -off       长期记忆
/llm url -on | -off          URL 读取
```

像记忆管理、域名黑名单这类精细化限制，Telegram 指令不对外暴露，只能进到控制台里执行 /llm 查看修改，防止误触，把安全边界改乱。

---

## 已知局限

这里记录当前版本管线没法完美解决、或是设计上遗留的小短板，后续迭代可以对照着优化：

- `memoryEnabled` 全局开关**对 Telegram 自动回复实际无效**：`chatHistory.db` 只在 `/send -c` 聊天界面下写入，LLM handler 本身不写入历史。除非先用 `/send -c` 与目标 chatID 聊过天，否则 `memoryEnabled + 历史` 不起作用。
- `handleTelegramErrors` 不保护 `_dispatchLLMReply`。里面任何未 catch 的异常都会触发 asyncio 的 "Task exception was never retrieved" warning，需要依赖自身的 try/except。
- URL reader 的意图判断是基于关键词的保守策略：不加意图词的"再试一次"类追问不会触发 URL 读取。必要时需要用户显式说"再读一次"或加 `#url` marker。
- 调试 bug 时，由于拆分后调用链较深，需要结合 `_dispatchLLMReply` 的日志（`System` 标签）和 Telegram API 的报错一起看。

---

## 维护原则

修改 `handlers/llm.py` 时建议遵守：

1. **新增能力优先做成 helper**，尽量不让 `handleLLMMessage` 或 `_dispatchLLMReply` 再度膨胀。
2. **尽量不破坏现有触发边界**（mention/keyword/URL intent 三层）。
3. **不合并 `autoMode` 的三种路径**，线上调试、Telegram 审核、控制台离线审查是三套完全独立的运维场景，它们对应真实的不同部署阶段。
4. **不轻易改动防抖数据结构**。改过一次（tuple → dict），新增字段时追加 key 比改结构成本低。
5. **任何新增的外部 I/O（HTTP、DB）**都应该考虑是否要进 `_dispatchLLMReply` 的后台阶段，同时记得注册到`ResourceManager`，程序退出时正常释放连接，不会残留占用进程的句柄。
