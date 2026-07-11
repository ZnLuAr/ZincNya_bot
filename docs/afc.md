# AFC 架构文档

> 最后更新：2026-07-06
> 
> 这份文档记载的是为 LLM 模块设计的 AFC (Autonomous Function Calling) 工具调用系统的细节——包括意图检测怎么做、工具怎么注册、执行流程走哪些阶段，以及为什么要自己实现而不用原生 Function Calling API，和设计时的一些取舍。
> 
> 说是为 LLM 模块设计，其实锌酱也能用到，也算是锌酱的本领……所以可以多多写工具，让一 LM 得道，锌酱升天喵——
>
> Written by ZincNya~ ❤
>

---

## 概述

**AFC (Autonomous Function Calling)** 是 ZincNya_bot 客户端自实现的工具调用系统，让 LLM 能够自动调用外部工具（如天气查询、计算器）来辅助回复。

**这不是 Anthropic/OpenAI 的原生 Function Calling API**，而是在客户端侧自行设计的类 AFC 架构：当用户消息触发工具意图（如"今天天气"、"算一下 2+3"），系统将工具 schema 以自定义格式（`<TOOLS>` 块）注入到 LLM 上下文，LLM 在回复中生成 `<AFC_ACTION>` JSON 调用工具，客户端解析后执行工具函数，将结果包装为 `<FUNCTION_RESULT>` 返回给 LLM 继续生成最终回复。

整个流程对标原生 Function Calling 的能力，但实现层完全自主可控，不依赖特定提供商的 API 特性。

AFC 设计为**可扩展框架**：新增工具只需在 `utils/afc/tools/` 下按约定实现 `__init__.py` + `triggers.py`，框架自动扫描注册，不修改核心代码。

这份文档面向未来维护者和工具开发者，解答：

- 一条消息从进入 handler 到工具被执行，经过了哪些阶段；
- 意图检测的四级召回策略是如何工作的；
- 如何添加新工具、编写 schema、测试；
- 为什么某些设计选择（group=1 handler、倒排索引、自定义格式）是刻意保持的。

## 目录

- [设计决策](#设计决策)
  - [为什么自实现而不用原生 Function Calling](#为什么自实现而不用原生-function-calling)
  - [为什么 AFC 是独立模块而不是 LLM 的子模块](#为什么-afc-是独立模块而不是-llm-的子模块)
  - [为什么 handler 放在 group=1](#为什么-handler-放在-group1)
  - [为什么意图检测要分级召回](#为什么意图检测要分级召回)
  - [为什么工具配置不写入根 config.py](#为什么工具配置不写入根-configpy)
  - [为什么 registry 按 __module__ 过滤导入函数](#为什么-registry-按-__module__-过滤导入函数)
- [架构概览](#架构概览)
- [处理流水线](#处理流水线)
- [意图检测 (afcIntent)](#意图检测-afcintent)
- [工具注册与 schema 生成 (registry)](#工具注册与-schema-生成-registry)
- [上下文注入 (afcApi)](#上下文注入-afcapi)
- [响应解析 (responseHandler)](#响应解析-responsehandler)
- [工具执行 (executor)](#工具执行-executor)
- [添加新工具](#添加新工具)
- [测试指南](#测试指南)
- [安全边界](#安全边界)
- [已知局限](#已知局限)
- [维护原则](#维护原则)
- [参考](#参考)

---

## 设计决策

先在这里把几个「为什么是现在这样」讲清楚——这些决定当时都掂量了挺久，写下来免得以后自己也忘了缘由。

### 为什么自实现而不用原生 Function Calling

Anthropic Claude 和 OpenAI GPT 都有原生 Function Calling / Tool Use API，为什么不直接用？

**理由：**

1. **跨 provider 统一**：客户端支持多家 LLM（Anthropic / OpenAI / Gemini / DeepSeek / 豆包），各家 Function Calling 格式不同，自实现统一接口是最简洁的设计。并且，存在民间 API 或特定的国产/开源大模型，其原生 Function Calling 支持的效果并不算好，统一反而能平均化效果；
2. **意图检测可控**：原生 API 每次调用都注入全部工具 schema，在给 LLM 本就不算小的 payload 之下，这样多少会在不需要 AFC 的情况下分散掉 LLM 的注意力。干脆自实现，就可以实现按照用户消息意图选择性注入；
3. **审核与日志**：客户端需要审核 LLM 回复、记录工具调用日志，自实现便于在调用前后插入钩子；
4. **离线测试友好**：自定义格式可以在单元测试中完全 mock，而不依赖真实 LLM API；
5. **扩展灵活**：后续如果要加入工具权限控制、费用统计、缓存策略等的功能，不会受提供商的 API 限制。

那么，**代价是什么呢** ？LLM 需要理解自定义的 `<TOOLS>` / `<AFC_ACTION>` / `<FUNCTION_RESULT>` 格式，这就产生了对 prompt 工程的依赖了（System Prompt 里有格式说明和示例）。实测 Claude Sonnet 4.6+ / GPT-4+ / Gemini 2.0+ 均能正确遵守格式。

### 为什么 AFC 是独立模块而不是 LLM 的子模块

最初设想时，AFC 被设计成 LLM 模块的一部分——毕竟工具调用是为 LLM 服务的，放在一起好像挺合理。但这样一来，LLM 模块就会变成一个巨型模块：不光要处理对话、记忆、知识库检索，还要操心意图检测、工具注册、执行编排的事情……每加一个工具，改的都是同一个目录，职责越堆越重。

更关键的是，**模块依赖会是单向的**——要是 AFC 嵌在 LLM 里，就变成了"LLM 依赖 AFC"，那么卸载 AFC 也很难卸得干净；反过来，测试 AFC 也得把整个 LLM 模块都加载进来。而实际上这两者应该是**可以独立装卸的**：AFC 可以整个卸载，LLM 照样跑（只是没工具调用能力）；测试 AFC 的工具注册和执行逻辑时，也不需要启动完整的 LLM 流程。

于是就重新设计成了现在这样：AFC 是独立的一级模块，通过推送层向 LLM 注入工具上下文。两者通过 `bot_data` 解耦，各自可以独立装卸、独立测试，互不强依赖对方的存在。模块拆分之后，职责边界也清晰了——LLM 模块负责"怎么生成回复"，AFC 模块负责"给 LLM 准备哪些工具"，推送层负责"怎么把工具信息递过去"。

### 为什么 handler 放在 group=1

`handlers/afc.py` 独占 group=1，早于 LLM handler (group=2)。这样 AFC 意图检测在 LLM 处理前完成，工具上下文块提前推入 `bot_data["llm_extra_blocks_{chatID}"]["afc"]`，LLM handler 消费时，工具 schema 就已经就位，只欠声明调用了。

——若放在 group=2 与 LLM 同组，则两者并发执行，bot_data 写入时序不确定，可能工具上下文块注入晚于 LLM 读取。且在 [Telegram Hanlder 文档](./telegram-handlers-group.md) 中也告示过：

>
> **模块加载顺序不应该作为业务依赖**。
>
> 因此如果两个 group `0` handlers 可能匹配同一类 update，不能依赖“谁先注册”来表达优先级；应该改用不同 group 或更精确的 filter。
>

### 为什么意图检测要分级召回

AFC 目标是**高召回率**、准确执行，宁可误注入工具 schema 也不应该漏掉真实意图。这是因为，相比于注入工具 schema 几百 token 的成本，漏掉意图的代价显然更高——用户问天气，LLM 只能干巴巴地"我无法查询"，这多少是一件令人气馁的事情的说……

既然多给几个选项不痛不痒、漏掉却很扫兴，那么就放宽着来。AFC 意图检测使用四级召回策略：

- **L1 关键词**（倒排索引）：精准匹配，比如"天气" 匹配到 `weather`
- **L2 正则**：模式匹配，如 `\d+\s*[\+\-\*/]` 能匹配到 `calc`
- **L3 通用兜底**：命中"帮我"/"查一下"，但 L1/L2 没能兜住，此时返回所有工具，让 LLM 自己选
- **L4 上下文延续**：短消息（≤10 字）+ 指代词（"那"/"再"/"也"），继承上一轮工具集

L3 是"宁可过注入"的体现；L4 旨在支持多轮对话（"那明天（的天气）呢？" 继承上轮的 `weather`）。

### 为什么工具配置不写入根 config.py

AFC 工具，像 weather，它们的配置项（API key、超时、缓存 TTL 等）不硬编码在根 `config.py` 和 `.env.example`，而是**工具自包含**——

- 每个工具目录下自己的 `config.py` 从环境变量读取（如 `weather/config.py` 读 `WEATHER_API_KEY`）；
- 配置说明写在工具 `config.py` 的 docstring 中，不污染根配置模板。

这其实是对 AFC 模块的定位在设计时的考量：AFC 应该是可扩展框架，能够自由装卸想要的工具。要是每加一个工具就往根配置塞新项的话，不仅会导致配置文件膨胀，且工具卸载后配置项残留，怎么说都不够优雅……让工具把自己的配置揣在自己兜里，模块化管理起来，会清爽得多。

### 为什么 registry 按 __module__ 过滤导入函数

工具 `__init__.py` 里导入的辅助函数（如 `from utils.core.logger import logSystemEvent`）会被 `inspect.getmembers(toolModule, inspect.isfunction)` 扫到，误登记为工具函数。

registry 用 `obj.__module__.startswith(toolPackage)` 过滤：只保留定义在本工具包内的函数（`utils.afc.tools.weather.*`），排除外部导入（`utils.core.logger.*`）。

这样工具就能放心大胆地 import 项目里别的模块，不必因为"小心别裸导入函数"这种脆弱约定而束手束脚了。

---

## 架构概览

一条消息从用户手里发出，被客户端接收到，到最后变成带着 AFC 返回数据的回复，中间是这样一条线走下来的：

```
用户消息
    ↓
handlers/afc.py (group=1)               ← 意图检测
    ↓ detectTools(message, chatID)
utils/afc/afcIntent.py                  ← 四级召回
    ↓ 候选工具名集合 {"weather", "calc"}
utils/afc/registry.py                   ← 扫描 tools/*/，生成 schema
    ↓ getToolsSchema(toolNames)
utils/llm/afcApi/contextBlock.py        ← 构建 <TOOLS> 块
    ↓ 推入 bot_data["llm_extra_blocks_{chatID}"]["afc"]
handlers/llm.py (group=2)               ← 读取推送层
    ↓ LLM 生成 <AFC_ACTION>{...}</AFC_ACTION>
utils/llm/afcApi/responseHandler.py     ← 解析调用
    ↓ extract JSON
utils/afc/executor.py                   ← 执行工具函数
    ↓ 调用 weather.getWeather / calc.calculate
    ↓ 返回 <FUNCTION_RESULT>结果</FUNCTION_RESULT>
    ↓ 拼回 LLM 上下文，继续生成
handlers/llm.py                         ← 最终回复用户
```

**关键模块：**

- `handlers/afc.py` — group=1 意图检测 handler，早于 LLM
- `utils/afc/afcIntent.py` — 四级召回意图检测
- `utils/afc/registry.py` — 工具扫描、schema 生成、callable 缓存
- `utils/afc/executor.py` — 工具调用编排、超时、迭代、日志
- `utils/llm/afcApi/contextBlock.py` — 构建 `<TOOLS>` 上下文块
- `utils/llm/afcApi/responseHandler.py` — 解析 LLM 响应中的 `<AFC_ACTION>`

---

## 处理流水线

把上面的剖开，一站一站看清楚——

### 1. 意图检测阶段 (handlers/afc.py)

用户消息到达，`afcIntentDetector` (group=1) 执行：

```python
text = message.text
chatID = str(message.chat_id)
toolNames = detectTools(text, chatID)  # 四级召回
if not toolNames:
    return  # 无意图，不注入
```

若检测到意图，构建工具上下文块：

```python
block = buildAFCContextBlock(toolNames)  # 调用 registry 生成 schema
context.bot_data.setdefault(key, {})["afc"] = (ContextTier.TOOLS, "工具", block)
```

### 2. LLM 上下文组装 (handlers/llm.py)

LLM handler (group=2) 读取推送层：

```python
extraBlocks = context.bot_data.pop(f"llm_extra_blocks_{chatID}", {})
for tier, label, content in extraBlocks.values():
    # 注入工具 schema 到 messages
```

### 3. LLM 生成与工具调用迭代

LLM 看到 `<TOOLS>` 上下文，依据提示词的提示，生成包含 `<AFC_ACTION>` 的回复：

```xml
<AFC_ACTION>
{"tool": "weather", "function": "getWeather", "parameters": {"city": "北京", "date": "today"}}
</AFC_ACTION>
```

`responseHandler` 解析后调用 `executor.execute(action)`，返回：

```xml
<FUNCTION_RESULT>北京 今天\n天气：晴\n...</FUNCTION_RESULT>
```

有 executor 将结果拼回上下文，LLM 继续生成，这之间最多经历 10 轮迭代。

---

## 意图检测 (afcIntent)

### 四级召回

```python
def detectTools(message: str, chatID: str) -> set[str]:
    # L1: 工具关键词匹配（倒排索引 _keywordIndex）
    # L2: 工具正则匹配（L1 未命中时）
    # L3: 通用兜底（命中 GENERIC_TRIGGERS 但无工具匹配 → 返回所有工具）
    # L4: 上下文延续（短消息 + 指代词 → 继承上一轮）
```

**倒排索引：** 启动时扫描 `tools/*/triggers.py` 的 `KEYWORDS` 和 `PATTERNS`，构建 `{keyword: {tool1, tool2}}`。查询时 O(k) 复杂度（k=关键词数），不遍历所有工具。

**通用兜底词：** `["帮我", "帮忙", "查一下", "算一下", "搜一下", "#afc", "#tool", "#工具"]`

**上下文延续：** `len(message) <= MAX_CONTEXTUAL_MESSAGE_LEN` (默认 10) 且含指代词 `["那", "呢", "它", "这个", "那个", "再", "还", "也"]`，返回 `_lastTriggeredTools[chatID]`。

---

## 工具注册与 schema 生成 (registry)

registry，可以说，是整套框架里最「自动」的一环——它在启动时把 `tools/` 底下的东西全扫一遍，工具的开发者可以几乎不用管注册这回事。

### 扫描机制

启动时 `_scanTools()` 遍历 `utils/afc/tools/`：

```python
for toolDir in toolsPath.iterdir():
    toolModule = importlib.import_module(f"utils.afc.tools.{toolName}")
    for name, obj in inspect.getmembers(toolModule, inspect.isfunction):
        if obj.__module__.startswith(toolPackage):  # 过滤外部导入
            schema = _generateFunctionSchema(obj, toolName)  # 解析 docstring
            toolSchemas.append(schema)
```

### Schema 生成

在扫到工具函数之后，还得让 LLM 知道「这个函数是干什么的、该传什么参数」——这就是 schema 的活。registry 不需要工具开发者手写 schema，而是从函数本身自动扒出来：函数签名给出参数名、类型、哪些必填哪些可选，docstring 会给出这个函数和每个参数的文字说明。

```python
def _generateFunctionSchema(func: callable, toolName: str) -> dict:
    sig = inspect.signature(func)
    doc = inspect.getdoc(func)
    # 解析参数注解（typing）、默认值、docstring 描述
    # 返回类 OpenAI function calling 格式 schema
```

也就是说，工具开发者只管写一个带类型注解和 docstring 的 `async` 函数，schema 会顺着这些信息自动长出来。不过要做到这些，这建立在 **约定** 被遵守的前提下：

- 函数首先必须是 `async def`（executor 用 `await` 调用）
- 参数用类型注解（`city: str`, `date: str = "today"`）
- Docstring 格式：

```python
"""
这里是一行简介

参数：
    city: 城市名（中文或英文）
    date: 日期参数（默认 today）

返回：
    格式化的结果字符串
"""
```

说到底，类型注解和 docstring 就是工具递给 LLM 的说明书——写得清楚，LLM 才知道这个函数存在、又该怎么调它。

~~——写函数带上规范且完整的 docstring，也是好习惯的说……~~

### Callable 缓存

有

```python
_toolsCallable: dict[str, dict[str, callable]] = {}  # {tool: {func: callable}}
```

即执行函数（executor）通过 `getToolCallable(toolName, funcName)`，取得函数对象直接调用。

---

## 上下文注入 (afcApi)

### 构建工具块

```python
def buildAFCContextBlock(toolNames: set[str]) -> str:
    schemas = getToolsSchema(toolNames)  # 从 registry 获取 schema 列表
    return buildToolsContext(schemas)     # 渲染为 <AFC_TOOLS> 文本块
```

`buildToolsContext()` 负责将 schema 列表渲染成文本格式，包含 `<AFC_TOOLS>` 标签和调用说明，注入到 LLM 上下文，供 LLM 了解它所有的能力。

### 推送层设计

工具 schema 备好，得有个地方交到 LLM handler 手上——这就是推送层。

不过麻烦在于，AFC handler 和 LLM handler 本是两个独立的 handler，PTB (Python Telegram bot) 并不会帮我们直接把数据从一个中传到另一个。好在 PTB 给每个 bot 备了一个全局共享的 `bot_data` 字典，两个 handler 都够得着——那就把它当块中转用的告示牌：AFC 在上面写下工具 schema，LLM 过一会儿来把它取走。

**扁平 key + 模块槽位**：`bot_data[f"llm_extra_blocks_{chatID}"]["afc"]`

- 扁平 key：每个 chatID 一个 dict，避免嵌套层级
- 模块槽位：`afc` 是本模块在推送层的槽位名，其它扩展模块用自己的 key（如 `search`），互不冲突

之所以要分出「每个 chatID 一格、每个模块又各占一个槽」，是因为这块告示牌是全体共用的——不同聊天、不同扩展模块的东西全堆在这一个 `bot_data` 里，得靠一套规范良好的 key 结构把它们隔开，不应让它们互相影响。

**三层清理：** 既然是块公共告示牌，写完就得擦干净，不然上一条消息的残留会串味到下一条：

1. 生产端清理：handler 写入前 `slots.pop("afc", None)` 清除上一条消息遗留
2. 消费端清理：LLM handler 读完后 `pop(f"llm_extra_blocks_{chatID}")`
3. 槽位隔离：各模块只动自己的槽，不影响其它扩展

> 推送层的完整设计（数据结构、清理时机、为什么这样分层）单独成文，见 [docs/bot-data-push-layer.md](bot-data-push-layer.md)。

---

## 响应解析 (responseHandler)

responseHandler 站在 LLM 和 executor 中间：LLM 生成的回复是一整段文本，工具调用只是其中夹带的一小截 `<AFC_ACTION>` 标签，得先有人把它从文本里认出来、扒成 JSON，才轮得到 executor 去执行。这个「认出来、扒出来」的活，就是 responseHandler 应该干的活——

### 提取 AFC_ACTION

正则匹配 `<AFC_ACTION>...</AFC_ACTION>`，命中就把里头的内容当 JSON 解析，没命中就返回 `None`，说明这次 LLM 没打算调工具，这时照常回复就好：

```python
def extractAFCAction(response: str) -> dict | None:
    pattern = r'<AFC_ACTION>(.*?)</AFC_ACTION>'
    match = re.search(pattern, response, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return None
```

### 幂等执行

在扒出调用之后、真正执行之前，其实还夹了一层去重——

同一个 `<AFC_ACTION>` JSON（内容相同）在一次对话中只执行一次，结果在缓存等待复用。这样，LLM 重试时不会把反复做同一件事——比如反复去查同一座城市的天气，白白耗一次 API 配额。

---

## 工具执行 (executor)

### 调用编排

扒出来的调用交到 executor 手上，就轮到它「真正动手」了——前面的意图检测、schema 注入、LLM 生成、responseHandler 解析都已走完，现在 executor 拿着这段 `<AFC_ACTION>{"tool": "weather", ...}</AFC_ACTION>` 去调真正的工具函数、拿到结果，再包上 `<FUNCTION_RESULT>` 扔回给 LLM 继续生成。

实现的细节差不多是这样的——

```python
async def execute(actionJSON: str) -> str:
    action = json.loads(actionJSON)
    toolName = action["tool"]
    funcName = action.get("function")  # 多函数工具必需
    params = action.get("parameters", {})

    func = getToolCallable(toolName, funcName)
    result = await asyncio.wait_for(func(**params), timeout=15)
    return f"<FUNCTION_RESULT>{result}</FUNCTION_RESULT>"
```

即解析 JSON → 从 registry 取函数对象 → 传参调用 → 包装结果 这么一条直线。

### 超时与异常

工具的一头连接着的可能会是外部世界，纷繁的信息洪流中，什么岔子都可能出。所以在这一层实行紧盯策略：

- 单次调用超时 15s（`asyncio.wait_for`）
- JSON 解析失败 / 工具不存在 / 参数错误，则返回 `<FUNCTION_ERROR>错误：...</FUNCTION_ERROR>`
- 所有调用日志记录（成功/失败/超时）

> AFC 的错误抛出、传递、捕获、日志规范独立成文，见 [docs/afc-error-handling.md](afc-error-handling.md)。新增工具的错误处理应遵循该规范。

### 迭代限制

LLM 可能连续生成多个 `<AFC_ACTION>`，`responseHandler` 的 `handleAFCInLLMResponse()` 最多迭代 10 轮（`maxIterations` 参数默认值）——万一 LLM 钻进牛角尖反复调用，这道闸也能兜住，不至于无限循环下去。

---

## 添加新工具

想给 LLM 添加新本事的话，跟着下面的几步走就好——

需要特别特别注意的是：**这是最小实现**——下面的例子为了快速上手，把工具逻辑全写在 `__init__.py` 一个文件里。实际开发复杂工具时（比如 weather 有缓存、日期解析、HTTP 客户端），该拆文件就拆(cache.py / dateParser.py / client.py)，`__init__.py` 只留对外暴露的函数入口。作为参考 calc 和 weather 都是这样写的。

……就是说，一个工具小到能在 `__init__.py` 塞下所有，不代表它就应该把所有东西都塞进里面的——！

### 1. 创建工具目录

```bash
mkdir -p utils/afc/tools/mytool
touch utils/afc/tools/mytool/__init__.py
touch utils/afc/tools/mytool/triggers.py
```

每个工具自包含在一个目录下——这样以后要卸载或分享某个工具，整个目录直接拎走，不会有文件散落在项目各处。

### 2. 实现工具函数 (`__init__.py`)

```python
"""
utils/afc/tools/mytool/__init__.py

我的工具描述
"""

async def myFunction(arg1: str, arg2: int = 10) -> str:
    """
    函数简介

    参数：
        arg1: 参数1描述
        arg2: 参数2描述（默认 10）

    返回：
        结果字符串
    """
    # 实现逻辑
    return f"结果：{arg1}, {arg2}"

__all__ = ["myFunction"]
```

**注意：**

- 必须是 `async def` — executor 用 `await` 调用，同步函数会卡住整个 bot
- 参数用类型注解(`str` / `int` / `float` / `bool`) — registry 从这里提取参数类型到 schema
- 返回 `str` — executor 会自动包在 `<FUNCTION_RESULT>` 里交给 LLM
- Docstring 遵循格式 — registry 解析 "参数:" 和 "返回:" 生成 schema 的文字说明
- `__all__` 列出对外函数 — 防止内部辅助函数被 registry 误扫成工具

### 3. 定义触发词 (`triggers.py`)

```python
"""
utils/afc/tools/mytool/triggers.py

工具触发关键词和正则
"""

KEYWORDS = [
    "触发词1",
    "trigger",
    "关键字",
]

PATTERNS = [
    r'正则模式1',
    r'\b(pattern|example)\b',
]
```

触发词写宽松一点没关系——它只是决定"要不要把这个工具的说明书递给 LLM"，多递几次不亏，真正用不用还是 LLM 自己拿主意。宁可让 LLM 看到了不调(浪费几百 token)，也别漏掉真实意图让它干瞪眼。

### 4. 注册到 modulesRegistry.py

在 `afc` 模块的 `files` 列表加：

```python
"utils/afc/tools/mytool/__init__.py",
"utils/afc/tools/mytool/triggers.py",
```

这步是让模块系统知道「这些文件属于 afc 模块」，卸载/禁用/查询时能统一管理。registry 在启动时扫 `utils/afc/tools/` 会自动发现新工具，但 modulesRegistry 不会——它只管模块元数据，不自动扫文件。

### 5. 测试

```python
# tests/utils/afc/tools/test_mytool.py
from utils.afc.tools.mytool import myFunction

async def test_basic():
    result = await myFunction("hello", 5)
    assert "hello" in result
```

跑测试：`python scripts/test.py -m afc`

工具测试至少覆盖：正常调用 + 参数错误 + 边界情况(如空字符串、超长输入、外部 API 失败)。executor / registry 的通用逻辑已有测试，工具开发者只需测工具自身。

### 6. 配置（可选）

若工具需要配置（API key / 超时等），在工具目录下创建 `config.py`：

```python
# utils/afc/tools/mytool/config.py
"""
mytool 工具配置

# 环境变量配置说明

## MY_API_KEY（必需）
从 https://example.com 获取 API 密钥。
示例：
    MY_API_KEY=your_key_here

## MY_TIMEOUT（可选）
请求超时（秒），默认 30。
"""

import os

MY_API_KEY = os.getenv("MY_API_KEY", None)
MY_TIMEOUT = int(os.getenv("MY_TIMEOUT", "30"))
```

配置说明就写在 `config.py` 的 docstring 里，用户打开这个文件就知道要准备哪些环境变量，不用再往根 `config.py` 和 `.env.example` 里塞——那样每加一个工具配置文件就膨胀一截，工具卸载后配置项还残留着。工具自己的配置揣在自己兜里，模块化管理起来清爽得多。

---

**拆文件的时机：** 当 `__init__.py` 超过 100-150 行，或者工具逻辑有明显的几块(HTTP 客户端、缓存、解析、格式化)，这就到了该拆文件的时候了。

参考 weather 的目录结构:

```
weather/
├── __init__.py          # 只保留 getWeather 入口函数
├── triggers.py
├── config.py
├── client.py            # HTTP 请求与 session 管理
├── cache.py             # 内存缓存
├── dateParser.py        # 日期解析
└── formatter.py         # 结果格式化
```

`__init__.py` 只做编排、调各模块，自己不写复杂逻辑。这样测试时能单独 mock 某一层，改一块不影响另一块。

---

## 测试指南

### 单元测试结构

```
tests/utils/afc/
├── test_afcIntent.py       # 意图检测四级召回
├── test_registry.py        # 工具扫描、schema 生成
├── test_executor.py        # 工具调用、超时、异常
├── test_contextBuilder.py  # 迭代限制、上下文组装
└── tools/
    ├── test_calc.py        # calc 工具单测
    └── test_weather.py     # weather 工具单测

tests/handlers/
└── test_afc.py             # afc handler 单测
```

### 测试 Mock 要点

因为 AFC 全程不碰真实 LLM API，单测里几乎所有外部依赖都能干净地 mock 掉：

**工具函数测试：** Mock API 客户端（如 `aiohttp.ClientSession`），验证参数解析、错误处理、格式化。

**executor 测试：** Mock `getToolCallable` 返回假函数，验证超时、JSON 解析、日志。

**registry 测试：** 验证外部导入函数不被登记（`test_external_imports_not_registered`）。

**handler 测试：** Mock `detectTools` 和 `buildAFCContextBlock`，验证 bot_data 槽位读写、清理逻辑。

### 跑测试

```bash
# 跑 AFC 全模块测试
python scripts/test.py -m afc

# 只跑某个文件
python -m pytest tests/utils/afc/test_executor.py --no-cov

# 生成覆盖率
python scripts/test.py -m afc --cov
```

---

## 安全边界

工具能连网、能算数、能被 LLM 自主调起来——而本事越大，越得先把笼子扎牢，把力量关在笼子里。这一节是几道刻意留着的红线。

### 工具执行

- **超时**：单次调用 15s 强制超时
- **迭代**：最多 10 轮 AFC_ACTION，防无限循环
- **隔离**：工具函数无权访问 bot context / update，只能通过参数接收数据
- **日志**：所有调用（成功/失败/超时/异常）记 `logSystemEvent`

### Calc 工具

- **AST 白名单**：只允许 `ast.Add`, `ast.Pow`, `ast.Call` 等安全节点
- **无 eval**：纯 AST 递归求值，不执行任意代码
- **函数白名单**：只允许 `sqrt`, `sin`, `cos`, `log` 等数学函数，拒绝 `exec`, `__import__`
- **幂运算防护**：指数绝对值 > 1000 拒绝（防 `9**9**9` 类炸弹）
- **结果溢出**：绝对值 > 1e100 拒绝

### Weather 工具

- **缓存**：同一城市+日期 10 分钟内复用缓存，避免重复请求
- **API 限流**：免费档 WeatherAPI.com 有请求配额，超限返回 429
- **超时**：HTTP 请求 30s 超时
- **错误分类**：401 / 400 / 429 / 5xx 返回不同错误消息

---

## 已知局限

有些不足是这一版有意先放着的……写下来，也算给以后的自己，或者可能接手的你，留个明白账。

### 1. 工具返回值只能是字符串

LLM 只能读到 `<FUNCTION_RESULT>文本</FUNCTION_RESULT>`，无法接收结构化数据（如 JSON 对象、图片）。若需结构化输出，工具需自己格式化成文本。

### 2. 无跨工具编排

LLM 不能"先调 A 工具取结果，再把结果传给 B 工具"（需 LLM 生成两轮 AFC_ACTION，当前迭代机制支持，但无显式编排语法）。

### 3. 工具无状态

每次调用是独立的，工具无法记住上次执行结果。需状态的场景（如"继续上次搜索"）需 LLM 自己在上下文管理。

### 4. 触发词冲突

两个工具的触发词重叠时，意图检测返回两者，LLM 自己选。无优先级机制。

### 5. schema 不支持复杂类型

当前 schema 生成只支持 `str`, `int`, `float`, `bool`，不支持 `List[str]` / `Dict` 等（需手动在 docstring 说明格式）。

### 6. 依赖 LLM 理解自定义格式

不同于原生 Function Calling API 的结构化输出保证，自实现依赖 LLM 正确理解 `<TOOLS>` 格式并生成合法 `<AFC_ACTION>` JSON。弱模型可能格式错误或幻觉调用不存在的工具——这算是「自己造轮子」要认下的那部分代价。

---

## 维护原则

最后是几条想一直守着的规矩。它们大多指向同一件事：让这套框架保持「加一个工具只碰一个目录」的干净。

### 1. 工具自包含

新工具的所有文件（`__init__.py`, `triggers.py`, `config.py`, 测试）放在 `utils/afc/tools/<toolname>/` 下，不散落在项目其它位置。卸载工具时删这一个目录即可。

### 2. 核心不依赖具体工具

`registry.py` / `executor.py` / `afcIntent.py` 不应硬编码任何工具名。所有工具通过扫描自动发现。

### 3. Schema 优先

工具的能力边界由 schema 定义。LLM 只能看到 schema，不知道工具内部实现。添加参数 / 修改行为要同步更新 docstring。

### 4. 测试覆盖

新工具最好有单元测试（正常调用 + 错误处理 + 边界情况）。registry / executor 的通用逻辑有测试覆盖，工具开发者只需测工具自身逻辑。

### 5. 日志记录

所有工具调用（成功/失败）记 `logSystemEvent`，便于审计和调试。格式：`"AFC 执行成功/失败/超时", f"{toolName}.{funcName}"`。

---

## 参考

- `handlers/afc.py` — group=1 意图检测 handler
- `utils/afc/afcIntent.py` — 四级召回实现
- `utils/afc/registry.py` — 工具扫描与 schema 生成
- `utils/afc/executor.py` — 工具调用编排
- `utils/llm/afcApi/` — LLM 侧上下文注入与响应解析
- `tests/utils/afc/` — 单元测试套件
- [docs/afc-error-handling.md](afc-error-handling.md) — AFC 错误处理规范
- [docs/bot-data-push-layer.md](bot-data-push-layer.md) — Bot Data 及其推送层设计
- OpenAI Function Calling 格式参考：https://platform.openai.com/docs/guides/function-calling

---
