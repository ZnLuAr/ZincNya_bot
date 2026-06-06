# ZincNya Bot

一个基于 Python-telegram-bot 的 Telegram Bot 项目喵！

由 Zinc Phos 创造的机器人，说是……

---


## 快速开始

### 1. 克隆项目

需要使用以下的指令把代码克隆到本地——

```bash
git clone git@github.com:ZnLuAr/ZincNya_bot.git
cd ZincNya_bot
```


### 2. 安装依赖

然后，给咱装上需要的工具喵——

```bash
pip install -r requirements.txt
```


### 3. 配置环境变量

把 `.env.example` 复制一份改名叫 `.env`，然后填上你的秘密：

```bash
cp .env.example .env
```

编辑 `.env` 文件，把 Bot Token 填进去：

```env
BOT_TOKEN=your_bot_token_here
```

> **如何获取 Bot Token？**
> 1. 在 Telegram 里找到这位 [@BotFather](https://t.me/BotFather) 大佬；
> 2. 发送 `/newbot` ，或者使用 Open 根据指引创建一个新的 bot 
> 3. 把获得的 token 复制下来就好了——


### 4. 配置 FFmpeg（FindSticker 功能需要）

咱的 FindSticker 表情包下载功能，需要 FFmpeg 来转换媒体格式。

**方式一：自动配置**

让脚本帮你搞定一切喵：

```bash
python scripts/setup_ffmpeg.py
```

**方式二：手动配置**

如果你想自己动手的话，可以看看 [ffmpeg/README.md](ffmpeg/README.md) 里的详细步骤哦


### 5. 启动 Bot

一切准备就绪，喵的一声，就启动啦——

```bash
python bot.py
```

---


## 项目结构喵

咱是这样被构成的——

<details>
<summary>展开查看完整结构……</summary>

```
ZincNya_bot/
├── bot.py                      # Bot 主程序
├── config.py                   # 配置与常量
├── loader.py                   # Handler 动态加载器
├── modulesRegistry.py          # 模块注册表（声明式模块元数据）
├── .env                        # 环境变量（不会被提交到 git）
├── .env.example                # 环境变量模板
├── requirements.txt            # Python 依赖
├── requirements-dev.txt        # 开发依赖（pytest、覆盖率工具）
├── pytest.ini                  # pytest 配置
│
├── data/                       # 数据文件喵
│   ├── modules.json            # 模块启用/禁用配置（不会被提交）
│   ├── whitelist.json          # 白名单（不会被提交）
│   ├── operators.json          # 管理员权限配置（不会被提交）
│   ├── chatHistory.db          # 加密聊天记录（不会被提交）
│   ├── .chatKey                # 聊天记录加密密钥（不会被提交）
│   ├── todos.db                # 待办事项数据库（不会被提交）
│   ├── llm/                    # LLM 相关数据（不会被提交）
│   │   ├── llmConfig.json      # LLM 功能配置
│   │   ├── llmMemory.db        # Structured memory 数据库
│   │   ├── prompts.json        # 提示词配置
│   │   ├── prompts.example.json # 提示词模板
│   │   ├── knowledge.db        # 知识库索引数据库
│   │   └── knowledge/          # 知识库 Markdown 文件
│   └── ZincNyaQuotes.json      # 语录数据喵
│
├── ffmpeg/                     # FFmpeg 可执行文件
│   └── README.md               # FFmpeg 配置说明
│
├── handlers/                   # Telegram 消息处理器
│   ├── cli.py                  # 控制台命令路由
│   ├── start.py                # /start 白名单鉴权与欢迎
│   ├── stickers.py             # 表情包搜索与下载
│   ├── nya.py                  # /nya 语录功能
│   ├── book.py                 # /book 书籍搜索
│   ├── todos.py                # /todos 待办事项管理
│   ├── reaction.py             # 消息 reaction 记录到聊天历史
│   ├── llm.py                  # LLM 自动回复（文字 + 图片 + URL 读取）、记忆操作解析与审核分发
│   ├── llmReview.py            # Telegram 端 LLM 审核回调（回复与记忆操作）
│   ├── llmCommand.py           # Telegram 端 /llm 命令（ops 控制）
│   └── shutdown.py             # 远程关机/重启/状态（仅 ops）
│
├── utils/                      # 工具模块
│   ├── command/                # CLI 命令
│   │   ├── help.py             # /help 帮助
│   │   ├── whitelist.py        # /whitelist 白名单管理
│   │   ├── send.py             # /send 发送消息与聊天界面
│   │   ├── history.py          # /history 聊天记录预览与导出
│   │   ├── nya.py              # /nya 语录管理
│   │   ├── log.py              # /log 日志管理
│   │   ├── todos.py            # /todos 待办事项管理（控制台）
│   │   ├── llm.py              # /llm LLM 功能控制与控制台审核
│   │   ├── news.py             # /news 新闻推送管理
│   │   ├── clear.py            # /clear 清屏
│   │   └── shutdown.py         # /shutdown 关闭
│   │
│   ├── moduleManager.py        # 模块配置管理（加载/保存/查询启用状态）
│   │
│   ├── core/                   # 核心基础设施
│   │   ├── appLifecycle.py     # 应用生命周期（构建、启动、停止、重启）
│   │   ├── consoleListener.py  # 控制台命令监听器
│   │   ├── database.py         # SQLite 数据库封装（WAL、连接管理）
│   │   ├── errorDecorators.py  # 统一错误处理装饰器
│   │   ├── errorHandler.py     # 错误日志双写与异常记录
│   │   ├── fileCache.py        # 文件缓存系统（TTL + 修改检测）
│   │   ├── logger.py           # 树状日志系统（INFO/WARNING/ERROR）
│   │   ├── resourceManager.py  # 资源清理管理器（退出时回调）
│   │   ├── stateManager.py     # 全局状态管理器
│   │   ├── terminalUI.py       # 终端 UI 工具（备用屏幕、ANSI）
│   │   ├── tuiBase.py          # TUI 控制器基类
│   │   └── schema/             # 数据库 schema 集中管理
│   │       ├── __init__.py     # loadSchema(name) 统一加载器
│   │       ├── chatHistory.sql # messages 表结构
│   │       ├── llmMemory.sql   # memory_entries 表结构
│   │       ├── llmKnowledge.sql # knowledge_entries 表结构
│   │       └── todos.sql       # todos 表结构
│   │
│   ├── llm/                    # LLM 集成模块
│   │   ├── config.py           # 配置管理（开关、审核模式、模型、视觉模型、群聊触发、记忆、URL 读取、知识库）
│   │   ├── state.py            # 运行时状态（多类型审核队列、速率限制、防抖、one-shot）
│   │   ├── review.py           # 审核共享操作（console / chatScreen 的 send/retry/cancel）
│   │   ├── contextBuilder.py   # 上下文组装（memory + knowledge + history + URL + 当前消息）
│   │   ├── vision.py           # 图片提取（photo/document/reply）与下载编码
│   │   ├── urlIntent.py        # URL 读取意图判断（关键词 / 正则 / 全局抑制 / 近邻否定）
│   │   ├── urlReader.py        # 受控 URL 抓取（SSRF 防护、redirect、byte cap、HTML/text 提取）
│   │   ├── client/             # 多模型 API 客户端（双调用视觉架构）
│   │   │   ├── _base.py        # LLMProvider 抽象基类（支持纯文本与多模态）
│   │   │   ├── _router.py      # 模型前缀路由 + 模糊匹配纠错
│   │   │   ├── _guardrails.py  # 安全防护提示词、视觉描述 prompt 与记忆操作指令
│   │   │   ├── _request.py     # 请求发送与自动重试
│   │   │   ├── _generate.py    # 回复生成编排（system prompt 构建 + 双调用视觉）
│   │   │   ├── anthropic.py    # Anthropic Claude API
│   │   │   ├── gemini.py       # Google Gemini API
│   │   │   └── openaiCompat.py # OpenAI 兼容接口（OpenAI / DeepSeek / 豆包）
│   │   ├── memory/             # Structured memory 子系统
│   │   │   ├── database.py     # SQLite 存储、CRUD、分层检索
│   │   │   ├── action.py       # LLM 自主记忆操作（解析、校验、执行）
│   │   │   └── ui.py           # LLM 记忆管理 TUI
│   │   └── knowledge/          # 知识库（RAG）子系统
│   │       ├── database.py     # SQLite 存储、BM25 检索、token 缓存
│   │       ├── loader.py       # Markdown 解析、增量索引
│   │       └── tokenizer.py    # 中英文混合分词 + BM25 评分
│   │
│   ├── todos/                  # 待办事项子系统
│   │   ├── database.py         # SQLite 数据存储
│   │   ├── utils.py            # 时间解析、优先级解析、格式化
│   │   ├── tgRender.py         # Telegram 侧渲染
│   │   ├── cliRender.py        # 控制台侧渲染
│   │   └── reminder.py         # 后台提醒循环
│   │
│   ├── whitelistManager/       # 白名单管理模块
│   │   ├── data.py             # 数据层（IO + 业务逻辑 + /start 处理）
│   │   └── ui.py               # UI 层（ViewModel + TUI）
│   │
│   ├── nyaQuoteManager/        # 语录管理模块
│   │   ├── data.py             # 数据层（IO + 随机选择）
│   │   └── ui.py               # UI 层（ViewModel + TUI）
│   │
│   ├── chatHistory.py          # 聊天记录加密存储
│   ├── chatUI.py               # prompt_toolkit 全屏聊天界面（固定底部输入）
│   ├── bookSearchAPI.py        # Open Library API 封装
│   ├── newsAPI.py              # 新闻抓取 API 封装（先咕着喵）
│   ├── downloader.py           # 表情包下载与格式转换
│   ├── operators.py            # Operators 权限管理
│   ├── telegramHelpers.py      # Telegram 消息操作工具
│   ├── fileEditor.py           # TUI 文本编辑器
│   └── inputHelper.py          # 统一异步输入工具（含多行输入）
│
├── scripts/                    # 辅助脚本喵
│   ├── module.py               # 模块管理 CLI（list/show/enable/disable/validate/scan）
│   ├── test.py                 # 测试运行器（pytest 封装）
│   ├── update_from_main.sh     # Linux 下自动与 main 分支同步
│   ├── sync-data.ps1           # 远程服务器同步工具（文件级覆盖）
│   ├── merge_data.py           # 离线 data/ 合并（dry-run 预览 + 备份后写入）
│   ├── expand_knowledge_tags.py # 离线 LLM tag 扩展（手动运行）
│   └── setup_ffmpeg.py         # FFmpeg 自动配置脚本
│
├── tests/                      # 单元测试（pytest）
│   ├── conftest.py             # 全局 fixture 与测试配置
│   ├── test_module_system.py   # 模块系统测试
│   ├── handlers/               # Handler 层测试
│   └── utils/                  # 工具模块测试
│       ├── core/               # 核心基础设施测试
│       ├── llm/                # LLM 模块测试
│       ├── todos/              # 待办事项测试
│       ├── nyaQuoteManager/    # 语录管理测试
│       └── whitelistManager/   # 白名单管理测试
│
└── log/                        # 日志文件（不会被提交）
    ├── log_YYYY-MM-DD.log      # 操作日志（每天一个）
    └── error_YYYY-MM-DD.log    # 错误日志（每天一个）
```

</details>

---


## 可用命令喵

启动 Bot 之后，在控制台输入这些命令就能控制咱喵～

| 命令 | 说明 |
|------|------|
| `/help` | 查看帮助信息 |
| `/whitelist` | 管理白名单 |
| `/send` | 发送消息或进入聊天界面 |
| `/history` | 预览或导出聊天历史记录 |
| `/nya` | 语录相关功能 |
| `/todos` | 管理待办事项 |
| `/llm` | 控制 LLM 功能开关、审核模式、模型、群聊触发、记忆与 URL 读取 |
| `/log` | 管理日志文件 |
| `/clear` | 清理控制台 |
| `/shutdown` | 关闭 Bot |

在 Telegram 里，用户可以使用这些命令喵～

| 命令 | 说明 |
|------|------|
| `/start` | 白名单鉴权与欢迎 |
| `/findsticker` | 搜索和下载表情包 |
| `/book <关键词>` | 搜索书籍 |
| `/nya` | 获取一条随机语录 |
| `/todos` | 管理个人待办事项 |

管理员（ops）还可以使用这些命令喵～

| 命令 | 说明 |
|------|------|
| `/shutdown` | 远程关闭 Bot |
| `/restart` | 远程重启 Bot |
| `/status` | 查看 Bot 运行状态 |
| `/llm` | 查看 LLM 开关状态 |
| `@bot 关机` | 通过 @ 提及触发关机 |
| `@bot 重启` | 通过 @ 提及触发重启 |
| `@bot 运行状态` | 通过 @ 提及查看状态 |

Operator 权限位（在 `operators.json` 中配置）：

| 权限 | 说明 |
|------|------|
| `shutdown` | 允许远程关机 |
| `restart` | 允许远程重启 |
| `status` | 允许查看运行状态 |
| `notify` | 接收未授权用户访问通知 |
| `llm` | 接收 LLM 生成内容与记忆操作的 Telegram 审核消息 |

想知道更详细的用法，就输入 `/help <command>` 喵！

---


## 开发说明喵

想给咱添加新功能的话，请看这里——


### 模块系统

ZincNya Bot 使用声明式模块系统管理功能模块。每个模块在 `modulesRegistry.py` 中注册元数据，可通过 `scripts/module.py` 动态启用/禁用。

**查看所有模块**
```bash
python scripts/module.py list
```

**查看模块详情**
```bash
python scripts/module.py show llm
```

**启用/禁用模块**
```bash
python scripts/module.py enable todos
python scripts/module.py disable stickers
```

**验证模块文件完整性**
```bash
python scripts/module.py validate
```

**安装 / 卸载模块**
```bash
# 从名称或 GitHub URL 安装（可用 --branch 指定分支）
python scripts/module.py install <name_or_url>

# 卸载模块（-s 仅删独占文件保留配置，-f 强制删除）
python scripts/module.py uninstall <name>
```

**扫描未注册的模块文件**
```bash
python scripts/module.py scan
```

模块配置保存在 `data/modules.json`，重启 bot 后生效。


### 添加新命令

1. 在 `utils/command/` 创建新的 Python 文件
2. 实现 `execute(app, args)` 函数
3. （可选）实现 `getHelp()` 函数返回帮助信息喵

也可以对 Zinc Phos. 提出 Pull Request 哦……


### 运行测试

```bash
# 安装开发依赖
pip install -r requirements-dev.txt

# 运行所有测试
python scripts/test.py

# 运行特定模块测试
python scripts/test.py -m llm

# 生成覆盖率报告
python scripts/test.py --cov

# 跳过慢速测试
python scripts/test.py --fast
```

测试详情见 [tests/README.md](tests/README.md)。

### 数据文件

这些文件包含敏感信息，不会被提交到 git：
- `.env` - Bot token 等环境变量
- `data/whitelist.json` - 用户白名单
- `data/operators.json` - 管理员权限配置
- `data/chatHistory.db` - 加密的聊天记录
- `data/.chatKey` - 聊天记录加密密钥
- `data/todos.db` - 待办事项数据库
- `data/llm/llmConfig.json` - LLM 功能配置
- `data/llm/llmMemory.db` - LLM structured memory 数据库
- `data/llm/prompts.json` - LLM 提示词配置
- `data/llm/knowledge.db` - LLM 知识库索引数据库
- `data/llm/knowledge/` - LLM 知识库 Markdown 文件
- `data/chatExport/` - 导出的聊天记录
- `data/chatBackup/` - 聊天记录自动归档

第一次部署的时候需要手动创建 `.env` 文件，
其他数据文件会在使用时自动生成。
它们是属于你的秘密，请注意保护好它们喵——

---


## 部署到新环境

要把咱搬到新服务器的话，按照这个步骤来：

1. 克隆代码
2. 安装依赖：`pip install -r requirements.txt` 
3. 配置 `.env` 文件
4. （可选）配置 FFmpeg：`python scripts/setup_ffmpeg.py`（FindSticker 功能需要）
5. 启动：`python bot.py` 

---


## 故障排除

遇到问题了的话，看看这里能不能帮到你：

### Bot 无法启动

**错误：`BOT_TOKEN 环境变量未设置`**
- 确保已经创建了 `.env` 文件并填入有效的 token 喵

**错误：`data/prompts.json 不存在`**
- 从模板复制一份：`cp data/llm/prompts.example.json data/llm/prompts.json`
- 根据需要修改 `system` 提示词

### FindSticker 功能

**错误：`缺失 ffmpeg`**
- 运行 `python scripts/setup_ffmpeg.py` 自动配置
- 或者参考 `ffmpeg/README.md` 手动配置
- Windows：确认 `ffmpeg/bin/ffmpeg.exe` 存在；Linux：确认 `ffmpeg` 在 `$PATH` 中（`which ffmpeg`）

### LLM 功能

**支持的模型**

| 前缀 | 提供商 | API Key 环境变量 |
|------|--------|-----------------|
| `claude-` | Anthropic | `ANTHROPIC_API_KEY` |
| `gemini-` | Google Gemini | `GEMINI_API_KEY` |
| `gpt-`、`o1-`、`o3-` | OpenAI | `OPENAI_API_KEY` |
| `deepseek-` | DeepSeek | `DEEPSEEK_API_KEY` |
| `doubao-` | 豆包（火山引擎） | `DOUBAO_API_KEY` |

模型切换命令：`/llm model switch <模型名>`（如 `claude-sonnet-4-6`、`gemini-2.5-flash`、`deepseek-chat`）

**图片支持（双调用视觉架构）**

LLM 支持读取用户发送的图片，采用双调用架构：先用轻量视觉模型生成客观图片描述，再将描述文本注入主调用的上下文中由角色模型回复。

触发方式：
- 私聊发图片 + caption → caption 触发 LLM，图片参与视觉描述
- 群聊发图片 + caption 中 @bot → 同上
- 私聊 reply 含图消息 + 文字 → 文字触发 LLM，reply 目标的图片参与视觉描述
- 群聊 reply 含图消息 + 文字 @bot → 同上
- 纯图片（无文字）→ 不触发，静默忽略

视觉模型可在 `data/llm/llmConfig.json` 中通过 `visionModel` 字段配置（默认 `claude-sonnet-4-6`），与主对话模型 `model` 独立。

**LLM 无响应 / API 报错**
- 检查 `.env` 中对应 provider 的 API Key 是否填写正确（不能有多余空格或 BOM）
- 401 错误：API key 无效或已过期，前往对应控制台重新生成
- 超时 / 无响应：可能是服务侧的临时故障，稍后重试；查看 `log/` 中的错误日志确认

**LLM 触发不了（发消息没有反应）**
- 确认已在控制台执行 `/llm on` 开启功能
- 确认发消息的用户在白名单中（`/whitelist` 查看）
- 群聊中需要 @ bot 才会触发；私聊直接发即可
- 查看控制台日志，确认消息是否被 handler 接收

**审核消息显示 `[已过期]`**
- Bot 重启后 `bot_data` 会清空，重启前未处理的审核消息均会过期，属于正常现象

**LLM 审核补充反馈**
- Ops 在审核 LLM 回复时，如果发现信息不足或需要额外上下文，可以使用补充反馈功能
- **Telegram 模式**：回复审核消息，内容以 `:fb ` 开头（如 `:fb 假设用户预算 1000 元`）
- **控制台模式**：在审核时选择 `[F]eedback`，然后输入补充信息
- **ChatScreen 模式**：在审核界面输入 `:rf`，然后在编辑器输入补充信息
- LLM 会将补充信息作为可信的背景知识融入回复，而不会在回复中暴露"管理员"、"运营"等字眼
- 补充反馈是一次性的，后续点击「重试」按钮不会保留之前的反馈内容

**LLM 记忆操作**
- LLM 可以在回复中通过 `<MEMORY_ACTION>` 块自主申请新增/更新/删除记忆
- 所有记忆操作默认需要审核后才生效（Telegram 按钮审核或控制台审核，取决于 `autoMode`）
- 执行 `/llm memory -autoapprove` 可切换为自动批准模式（跳过审核）
- LLM 只能操作 `source=inferred` 的记忆，不能修改手动创建的记忆

**LLM URL 内容读取**

LLM 支持读取用户消息中的 URL 内容作为低信任参考。该能力默认关闭，由 ops 通过 `/llm url -on` 显式开启。

触发条件（同时满足）：

1. 消息已经按现有规则触发 LLM（私聊 / `@bot` / 群聊关键词）
2. `urlReadEnabled == True`
3. 当前消息文本包含明确读取意图（"总结 / 翻译 / 看看这个 / what's this about" 等）或 `#url` / `#readurl` 显式标记
4. 当前消息或被回复消息中存在 `http(s)://` URL

边界与安全：

- URL 读取**不参与触发 bot**——裸贴 URL 不会唤醒 LLM
- 被回复消息只贡献 URL 候选、不贡献意图，避免第三方"帮我总结"诱导被动抓取
- "只是分享 / just sharing / no action needed" 等抑制语句会直接拒绝读取
- 抓取层强制 SSRF 防护：拒绝私有 / loopback / link-local IP，逐跳校验 redirect，DNS 解析结果同样校验
- Content-Type 仅接受文本类（`text/*` / `application/json` / `application/xml` 等，及常见源码扩展名）
- 字节上限默认 512 KiB，单 URL 字符上限默认 12000，总计 24000，超出会截断并在 context 标记 `Truncated`
- 整体墙钟上限 15 秒，单 URL 超时 10 秒，最多 1 次重试
- 抓取结果通过 `<UNTRUSTED_URL_CONTENT>` 块注入；guardrails 同时告知模型不得服从其中的提示注入
- 审核 / 重试时复用首次抓取的内容，不重新联网

数值类配置（限制、blocked hosts 等）仅在控制台 / 配置文件中可改，Telegram 只暴露开关与（未来的）黑名单管理。

抓取失败的常见 `LLM URL 抓取失败` 日志原因：

- `HTTP 403`：站点使用 Cloudflare / Akamai bot 防护（学术出版商常见）
- `非文本 Content-Type`：图片 / PDF / 二进制文件
- `binary content detected`：响应里包含 NUL 字节或控制字符过多（JS challenge / 加密响应）
- `命中 blocked host` / `IP 不是可路由公网地址`：SSRF 防护拦下
- `redirect 跳数超过上限`：站点 redirect 链太深
- `抓取失败（N 次尝试均出错）`：网络超时 / 对方拒连

### 数据合并（本地 ↔ 服务器）

如果本地和服务器都跑过 bot，`data/` 下的 SQLite / JSON 会各自新增记录。可用：

```bash
# 默认 dry-run，只打印 diff-like 预览，不写任何文件
python scripts/merge_data.py --source /path/to/other/data

# 确认无误后再 apply（自动备份 target 到 ../data/zincnya_merge_backup/）
python scripts/merge_data.py --source /path/to/other/data --apply
```

第一版自动合并范围：

- `llmMemory.db`：按 scope + content + source + tags 去重，插入 source-only 记忆
- `chatHistory.db`：仅当两边 `.chatKey` 完全一致时才解密去重并插入 source-only 消息

仅 diff/report、不自动写入：`whitelist.json` / `operators.json` / `llmConfig.json` / `prompts.json` / `ZincNyaQuotes.json` / `pushedNews.json` / `todos.db`。这些差异需要人工 review 后手动同步。

> **`knowledge.db` 不参与合并**：知识库索引由 Git 管理的 `data/llm/knowledge/*.md` 文件在启动时自动重建（`reindex`），无需跨环境同步数据库。

### 权限 / 管理员命令

**`/status`、`/shutdown`、`/restart` 在 Telegram 中无响应**
- 检查 `data/operators.json`，确认你的用户 ID 已添加，且 `permissions` 中包含对应权限位（`status`、`shutdown`、`restart`）
- 示例配置：
  ```json
  {
    "123456789": {
      "name": "你的名字",
      "permissions": ["shutdown", "restart", "status", "notify", "llm"]
    }
  }
  ```

### 其他问题

查看 `log/` 文件夹中的日志文件，看看详细的错误信息；

有什么解决不了的问题，可以提 Issue 喵！

---


## 许可证

MIT License 喵

---

**Made with ❤️ by ZincPhos**


###### Written by ZincNya~
