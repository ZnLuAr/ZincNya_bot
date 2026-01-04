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

把 `.env.example` 复制一份改名叫 `.env`，然后填上你的秘密喵：

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

```
ZincNya_bot/
├── bot.py                  # Bot 主程序
├── config.py               # 配置文件
├── loader.py               # 功能加载器
├── .env                    # 环境变量（不会被提交到 git ）
├── .env.example            # 环境变量模板
├── requirements.txt        # Python 依赖
│
├── data/                   # 数据文件喵
│   ├── whitelist.json      # 白名单（不会被提交）
│   ├── chatID.json         # 聊天 ID（不会被提交）
│   └── ZincNyaQuotes.json  # 语录数据喵
│
├── ffmpeg/                 # FFmpeg 可执行文件
│   └── README.md           # FFmpeg 配置说明
│
├── handlers/               # Telegram 消息处理器
│
├── utils/                  # 工具模块
│   ├── command/            # CLI 命令
│   ├── whitelistManager.py # 白名单管理
│   ├── nyaQuoteManager.py  # 语录管理
│   └── logger.py           # 日志工具
│
├── scripts/                # 辅助脚本喵
│   └── setup_ffmpeg.py     # FFmpeg 自动配置脚本
│
└── log/                    # 日志文件（不会被提交）
```

---


## 可用命令喵

启动 Bot 之后，在控制台输入这些命令就能控制咱喵～

| 命令 | 说明 |
|------|------|
| `/help` | 查看帮助信息 |
| `/whitelist` | 管理白名单 |
| `/send` | 发送消息 |
| `/nya` | 语录相关功能 |
| `/clear` | 清理控制台 |
| `/shutdown` | 关闭 Bot |

想知道更详细的用法，就输入 `/help <command>` 喵！

---


## 开发说明喵

想给咱添加新功能的话，请看这里——


### 添加新命令

1. 在 `utils/command/` 创建新的 Python 文件
2. 实现 `execute(app, args)` 函数
3. （可选）实现 `getHelp()` 函数返回帮助信息喵

### 数据文件

这些文件包含敏感信息，不会被提交到 git：
- `.env` - Bot token 等环境变量喵
- `data/whitelist.json` - 用户白名单喵
- `data/chatID.json` - 聊天 ID 列表喵

第一次部署的时候需要手动创建这些文件，
它们是属于你的秘密，请注意保护好它们喵——

---


## 部署到新环境

要把咱搬到新服务器的话，按照这个步骤来喵：

1. 克隆代码
2. 安装依赖：`pip install -r requirements.txt` 
3. 配置 `.env` 文件
4. 配置 FFmpeg：`python scripts/setup_ffmpeg.py` 
5. 启动：`python bot.py` 

---


## 故障排除

遇到问题了的话，看看这里能不能帮到你喵：

### Bot 无法启动

**错误：`BOT_TOKEN 环境变量未设置！`**
- 确保已经创建了 `.env` 文件并填入有效的 token 喵

**错误：`找不到 ffmpeg`**
- 运行 `python scripts/setup_ffmpeg.py` 自动配置
- 或者参考 `ffmpeg/README.md` 手动配置

### 其他问题

查看 `log/` 文件夹中的日志文件，看看详细的错误信息；

有什么解决不了的问题，可以提 Issue 喵！

---


## 许可证

MIT License 喵

---

**Made with ❤️ by ZincPhos 喵～**
