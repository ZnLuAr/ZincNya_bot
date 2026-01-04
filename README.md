# ZincNya Bot

一个基于 Python-telegram-bot 的 Telegram Bot 项目。

---

## 快速开始

### 1. 克隆项目

```bash
git clone <your-repo-url>
cd ZincNya_bot
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

复制 `.env.example` 为 `.env` 并填入你的配置：

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```env
BOT_TOKEN=your_bot_token_here
```

> **如何获取 Bot Token？**
> 1. 在 Telegram 中找到 [@BotFather](https://t.me/BotFather)
> 2. 发送 `/newbot` 创建新 bot
> 3. 复制获得的 token

### 4. 配置 FFmpeg（必需）

**方式一：自动配置（推荐）**

```bash
python scripts/setup_ffmpeg.py
```

**方式二：手动配置**

参见 [ffmpeg/README.md](ffmpeg/README.md) 了解详细步骤。

### 5. 启动 Bot

```bash
python bot.py
```

---

## 项目结构

```
ZincNya_bot/
├── bot.py                  # Bot 主程序
├── config.py               # 配置文件
├── loader.py               # 功能加载器
├── .env                    # 环境变量（不会被提交到 git）
├── .env.example            # 环境变量模板
├── requirements.txt        # Python 依赖
│
├── data/                   # 数据文件
│   ├── whitelist.json      # 白名单（不会被提交）
│   ├── chatID.json         # 聊天 ID（不会被提交）
│   └── ZincNyaQuotes.json  # 语录数据
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
├── scripts/                # 辅助脚本
│   └── setup_ffmpeg.py     # FFmpeg 自动配置脚本
│
└── log/                    # 日志文件（不会被提交）
```

---

## 可用命令

启动 Bot 后，在控制台输入以下命令：

| 命令 | 说明 |
|------|------|
| `/help` | 查看帮助信息 |
| `/whitelist` | 管理白名单 |
| `/send` | 发送消息 |
| `/nya` | 语录相关功能 |
| `/clear` | 清理控制台 |
| `/shutdown` | 关闭 Bot |

详细用法请使用 `/help <command>` 查看。

---

## 开发说明

### 添加新命令

1. 在 `utils/command/` 创建新的 Python 文件
2. 实现 `execute(app, args)` 函数
3. （可选）实现 `getHelp()` 函数返回帮助信息

### 数据文件

以下文件包含敏感信息，不会被提交到 git：
- `.env` - Bot token 等环境变量
- `data/whitelist.json` - 用户白名单
- `data/chatID.json` - 聊天 ID 列表

首次部署时需要手动创建这些文件。

---

## 部署到新环境

1. 克隆代码
2. 安装依赖：`pip install -r requirements.txt`
3. 配置 `.env` 文件
4. 配置 FFmpeg：`python scripts/setup_ffmpeg.py`
5. 启动：`python bot.py`

---

## 故障排除

### Bot 无法启动

**错误：`BOT_TOKEN 环境变量未设置喵！`**
- 确保已创建 `.env` 文件并填入有效的 token

**错误：`找不到 ffmpeg`**
- 运行 `python scripts/setup_ffmpeg.py` 自动配置
- 或参考 `ffmpeg/README.md` 手动配置

### 其他问题

查看 `log/` 文件夹中的日志文件了解详细错误信息。

---

## 许可证

[根据你的需要添加许可证信息]

---

**Made with ❤️ by ZincPhos**
