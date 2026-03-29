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


### 4. 配置 FFmpeg（必需喵！）

咱需要 FFmpeg 来处理媒体文件的……

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
├── .env                        # 环境变量（不会被提交到 git）
├── .env.example                # 环境变量模板
├── requirements.txt            # Python 依赖
│
├── data/                       # 数据文件喵
│   ├── whitelist.json          # 白名单（不会被提交）
│   ├── operators.json          # 管理员权限配置（不会被提交）
│   ├── chatHistory.db          # 加密聊天记录（不会被提交）
│   ├── .chatKey                # 聊天记录加密密钥（不会被提交）
│   ├── todos.db                # 待办事项数据库（不会被提交）
│   ├── pushedNews.json         # 已推送新闻记录（不会被提交）
│   ├── llmConfig.json          # LLM 功能配置（不会被提交）
│   ├── llmMemory.db            # LLM structured memory 数据库（不会被提交）
│   ├── prompts.json            # LLM 提示词配置（不会被提交）
│   ├── prompts.example.json    # LLM 提示词模板
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
│   ├── llm.py                  # LLM 自动回复与审核回调
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
│   │   ├── clear.py            # /clear 清屏
│   │   └── shutdown.py         # /shutdown 关闭
│   │
│   ├── core/                   # 核心基础设施
│   │   ├── appLifecycle.py     # 应用生命周期（构建、启动、停止、重启）
│   │   ├── consoleListener.py  # 控制台命令监听器
│   │   ├── database.py         # SQLite 数据库封装（WAL、连接管理）
│   │   ├── errorDecorators.py  # 统一错误处理装饰器
│   │   ├── fileCache.py        # 文件缓存系统（TTL + 修改检测）
│   │   ├── resourceManager.py  # 资源清理管理器（退出时回调）
│   │   ├── stateManager.py     # 全局状态管理器
│   │   └── tuiBase.py          # TUI 控制器基类
│   │
│   ├── llm/                    # LLM 集成模块
│   │   ├── config.py           # 配置管理（开关、模式、提示词，读写 JSON）
│   │   ├── state.py            # 运行时状态（审核队列、速率限制、防抖、one-shot）
│   │   ├── client.py           # Anthropic API 封装
│   │   ├── contextBuilder.py   # 上下文组装（memory + history + 当前消息）
│   │   └── memory/             # Structured memory 子系统
│   │       ├── __init__.py
│   │       └── database.py     # SQLite 存储、CRUD、分层检索
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
│   ├── bookSearchAPI.py        # Open Library API 封装
│   ├── newsAPI.py              # 新闻抓取 API 封装（先咕着喵）
│   ├── downloader.py           # 表情包下载与格式转换
│   ├── operators.py            # Operators 权限管理
│   ├── telegramHelpers.py      # Telegram 消息操作工具
│   ├── fileEditor.py           # TUI 文本编辑器
│   ├── terminalUI.py           # 终端 UI 工具（备用屏幕、ANSI）
│   ├── errorHandler.py         # 错误处理与日志
│   ├── inputHelper.py          # 统一异步输入工具
│   └── logger.py               # 树状日志系统
│
├── scripts/                    # 辅助脚本喵
│   ├── update_from_main.sh     # Linux 下自动与 main 分支同步
│   ├── sync-data.ps1           # 远程服务器同步工具
│   └── setup_ffmpeg.py         # FFmpeg 自动配置脚本
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
| `/llm` | 控制 LLM 功能开关、审核模式和模型 |
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
| `llm` | 接收 LLM 生成内容的 Telegram 审核消息 |

想知道更详细的用法，就输入 `/help <command>` 喵！

---


## 开发说明喵

想给咱添加新功能的话，请看这里——


### 添加新命令

1. 在 `utils/command/` 创建新的 Python 文件
2. 实现 `execute(app, args)` 函数
3. （可选）实现 `getHelp()` 函数返回帮助信息喵

也可以对 Zinc Phos. 提出 Pull Request 哦……

### 数据文件

这些文件包含敏感信息，不会被提交到 git：
- `.env` - Bot token 等环境变量
- `data/whitelist.json` - 用户白名单
- `data/operators.json` - 管理员权限配置
- `data/chatHistory.db` - 加密的聊天记录
- `data/.chatKey` - 聊天记录加密密钥
- `data/todos.db` - 待办事项数据库
- `data/pushedNews.json` - 已推送新闻记录
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
4. 配置 FFmpeg：`python scripts/setup_ffmpeg.py` 
5. 启动：`python bot.py` 

---


## 故障排除

遇到问题了的话，看看这里能不能帮到你：

### Bot 无法启动

**错误：`BOT_TOKEN 环境变量未设置`**
- 确保已经创建了 `.env` 文件并填入有效的 token 喵

**错误：`找不到 ffmpeg`**
- 运行 `python scripts/setup_ffmpeg.py` 自动配置
- 或者参考 `ffmpeg/README.md` 手动配置
- Windows：确认 `ffmpeg/bin/ffmpeg.exe` 存在；Linux：确认 `ffmpeg` 在 `$PATH` 中（`which ffmpeg`）

**错误：`data/prompts.json 不存在`**
- 从模板复制一份：`cp data/prompts.example.json data/prompts.json`
- 根据需要修改 `system` 提示词

### LLM 功能

**LLM 无响应 / API 报错**
- 检查 `.env` 中 `ANTHROPIC_API_KEY` 是否填写正确（不能有多余空格或 BOM）
- 401 错误：API key 无效或已过期，前往 [console.anthropic.com](https://console.anthropic.com) 重新生成
- 超时 / 无响应：可能是 Anthropic 服务侧的临时故障，稍后重试；查看 `log/` 中的错误日志确认

**LLM 触发不了（发消息没有反应）**
- 确认已在控制台执行 `/llm on` 开启功能
- 确认发消息的用户在白名单中（`/whitelist` 查看）
- 群聊中需要 @ bot 才会触发；私聊直接发即可
- 查看控制台日志，确认消息是否被 handler 接收

**审核消息显示 `[已过期]`**
- Bot 重启后 `bot_data` 会清空，重启前未处理的审核消息均会过期，属于正常现象

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
