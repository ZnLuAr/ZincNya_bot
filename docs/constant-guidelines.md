# 常量归属指南

> 落地时间：2026-08-06
>
> 这份文档记载的是项目里「常量该放哪」的判据——`config.py`、各文件模块级、函数内三层的归属规则，以及 scripts、基础设施、运行时配置的边界处理。配套本次常量全面整理（业务阈值进 config、消除重复、截断族提常量）立规矩，后续新代码强制遵守。
>
> Written by ZincNya~ ❤

---

## 概述

项目里 `config.py` 越来越臃肿，同时各文件又散落零星常量、函数内 magic number 泛滥——同一个 Telegram 4096 上限能硬编码三处，同一个时间戳格式能在四个 db 模块各写一遍。这不是「谁的代码风格问题」，是缺一套判据：常量该进 config 还是留文件、留文件时该集中定义还是下放函数。

这份文档就是判据。核心一句话：**常量的「变化触发源」决定归属——谁会改它、改它影响什么。**

## 目录

- [config 的三层](#config-的三层)
- [归属判据](#归属判据)
- [三层归属规则](#三层归属规则)
- [边界与陷阱](#边界与陷阱)
- [命名与组织](#命名与组织)
- [判据流程图](#判据流程图)
- [案例](#案例)
- [维护原则](#维护原则)

---

## config 的三层

项目已经有 config 层级的惯例，标准建立其上：

| 层级 | 位置 | 收什么 |
|------|------|--------|
| **根 config** | `config.py` | 代码级常量：环境/部署变量、路径、按功能分区的业务旋钮、跨模块共享的平台约束与格式约定 |
| **模块 config** | `utils/llm/config.py` 等 | 运行时可改配置（`_DEFAULT_CONFIG` json schema + getter）+ 模块专属 enum（如 `ContextTier`） |
| **子工具 config** | `utils/afc/tools/*/config.py` | 工具自有配置 |

**关键边界**：根 config 收「代码常量」（含业务旋钮）；模块 config 收「运行时 json 配置 + 模块 enum」。

举个例子，`utils/llm/config.py` 的 `_DEFAULT_CONFIG` 里那些 `urlReadMaxUrls`、`knowledgeMaxResults` 是 ops 通过 `/llm` 命令实时调的（写进 `llmConfig.json`），它们留模块 config；而硬编码的代码常量（如 `LLM_URL_RETRY_BACKOFF_SECONDS=0.5`）进根 config——两者都关于 URL 读取，但一个是「可调旋钮」、一个是「代码常量」，归属不同。

---

## 归属判据

一个常量，问自己一句：**「什么时候会改它、谁来改？」**，而答案能决定去处：

- **部署/运维改**（环境变量、密钥、外部 URL）→ 根 config 的环境变量区
- **ops/用户调业务行为**（防抖、TTL、速率、分页、模型默认、记忆上限、重试、各类阈值）→ 根 config 的功能区
- **平台/外部硬约束**（Telegram 4096、文件大小上限）→ 根 config，**一处定义多处复用，禁止重复硬编码**
- **跨模块共享约定**（DB 时间戳格式、scope 字面量集合）→ 根 config
- **模块私有实现词汇**（key 前缀、私有正则、私有集合/映射、提示文案模板、单模块路径）→ 该文件模块级
- **本地派生别名**（从 config 或模块内常量派生的便捷名）→ 该文件模块级
- **一次性纯局部值**（切片偏移、临时索引、单次循环边界）→ 函数内

---

## 三层归属规则

### 进根 config.py（按功能分区）

- **环境/部署变量**：`.env` 读的 token、密钥、代理、外部 URL
- **业务可调旋钮**：调它是因为业务需求变（想让 bot 反应更快/审核更久/记忆更多），不是因为代码重构
- **平台与外部硬约束**：Telegram 4096、文件大小上限——一处定义多处复用
- **跨模块共享约定**：DB 时间戳格式、scope 字面量集合

### 留各文件模块级（import 后的「常量区」集中定义）

- **模块私有的实现词汇**：key 前缀、回调 data 格式、私有正则（`_SECRET_RE`）、私有集合（`_VALID_ACTIONS`）、映射表（`LANGUAGE_MAP`）、提示文案模板（`_LOW_TRUST_MEMORY_NOTICE`）、单模块用的路径
- **本地派生别名**：从 config 派生的便捷名（`_REPLY_PREVIEW_LEN = LLM_REVIEW_REPLY_PREVIEW_LEN`），为可读性或避免重复 import
- **模块内截断/阈值常量**：日志预览长度、展示截断、UI 列宽等「该模块的显示参数」

### 留函数内（默认不放）

只有满足「一次性、纯局部、命名无收益」才留：
- 切片偏移 `[-1]`、`parts[2:]`
- 临时索引、单次循环边界
- 空字符串/None 默认值

**提取信号**（出现以下任一，就该提模块级）：
- 同值在 2+ 处出现（重复 → 提取）
- 有语义（截断/超时/阈值）→ 命名表意
- 跨函数/模块可能复用 → 提模块级（同模块）或 config（跨模块）

---

## 边界与陷阱

这几条是根据本次整理出来的规律，刻意保留的边界：

### 运行时配置 vs 代码常量

`_DEFAULT_CONFIG`（json 可改，ops 通过命令调）留模块 config；硬编码旋钮进根 config。两者都可能是「业务参数」，但「能不能在不改代码的情况下调」是分界——能的是运行时配置，不能的是代码常量。

### scripts 自治

`scripts/*.py` 是独立工具脚本，**不进 bot 的 config**——因为 `import config` 会触发 `BOT_TOKEN` 必检（`config.py` 顶部），脚本可能在无 token 环境跑（数据合并、评估等）。脚本私有常量留脚本模块级；脚本可以 `import config` 复用 bot 的共享常量（如 `LLM_MEMORY_DB_PATH`），但前提是脚本运行环境确实有 `.env`。

典型例子：`scripts/merge_data.py` 的 `TIMESTAMP_FORMAT` 保留字面量（与 `config.DB_TIMESTAMP_FORMAT` 同源），不 import config——避免触发 BOT_TOKEN 检查。加注释说明同源即可。

### 基础设施纯净（utils/core/）

`utils/core/` 是最底层基础设施（logger / crypto / database / fileCache），**不 import config**——保持可测试性 + 可被无 token 环境复用。`utils/core/database.py` 的 SQLite `busy_timeout` 用模块级 `_BUSY_TIMEOUT_MS = 5000`，不进 config。

### 消除重复 vs re-export

业务阈值进 config 后，原模块若 `from config import X` 仅为 re-export 给下游，是「间接依赖」。下游应直接 `from config import X`，不让中间模块当转发站。本次整理的 `LLM_MEMORY_MAX_ACTIONS` 就是这么清理的：原 `action.py` import 它仅为 re-export 给 `review.py` / `llm.py`，清理后两者直接从 config import，`action.py` 不再持有。

### 内部实现常量不进 config

正则、私有集合、映射表、提示文案模板、key 前缀——这些是「代码本身」，不是「配置」。放各文件模块级。判断：换了模块它就没意义，是这个模块的「实现词汇」。

---

## 命名与组织

- **config 常量**：`UPPER_SNAKE`，按功能加前缀（`LLM_*` / `BOOK_*` / `AFC_*` / `WHITELIST_*`）
- **模块级常量**：`UPPER_SNAKE`，模块私有加 `_` 前缀（`_VALID_ACTIONS` / `_TG_MAX_LEN`）
- **根 config 分区**：用 `# xxx 相关常量` 单行注释横幅（保持现状风格，不要混用 `# ===`）；模块级常量区可用 `# ===` 横幅按概念组分隔
- **常量区位置**：import 后、第一个定义前，集中定义

---

## 判据流程图

新增一个常量时，按这个顺序判断：

```
新常量出现
  │
  ├─ 从 .env 读 / 部署相关？           → 根 config（环境变量区）
  │
  ├─ 调它是为了改业务行为（旋钮）？    → 根 config（功能分区）
  │
  ├─ 外部硬约束（平台/API 规则）？     → 根 config（一处定义，禁止重复硬编码）
  │
  ├─ 跨模块共享（格式/字面量约定）？   → 根 config
  │
  ├─ 只有这个模块关心的实现词汇？      → 该文件模块级
  │
  ├─ 从别处派生的便捷别名？            → 该文件模块级
  │
  └─ 一次性纯局部值？                  → 函数内
```

---

## 案例

以下是本次整理的典型决策，作为判据的应用样本：

| 常量 | 原位置 | 整理后 | 依据 |
|------|--------|--------|------|
| Telegram 4096 上限 | `llm.py` / `llmReview.py` / `telegramHelpers` 各硬编码 | `config.TG_MESSAGE_MAX_LEN`（3 处复用） | 平台硬约束，禁止重复 |
| `TIMESTAMP_FORMAT` | 4 个 db 模块各写 | `config.DB_TIMESTAMP_FORMAT`（4 复用）+ `merge_data` 保留字面量 | 跨模块共享；merge_data 是 script 自治 |
| `LLM_MEMORY_PRIORITY_CAP` 等 | `memory/action.py` 模块级 | `config.LLM_MEMORY_*` 区 | 业务可调旋钮 |
| `_VISION_MAX_TOKENS` | `client/_generate.py` 模块级 | `config.LLM_VISION_MAX_TOKENS` | 业务可调（视觉生成参数） |
| `_TOOL_TIMEOUT_SECONDS` | `afc/executor.py` 模块级 | `config.AFC_TOOL_TIMEOUT_SECONDS` | 业务可调（工具超时） |
| SQLite `busy_timeout=5000` | `core/database.py` / `merge_data.py` 各硬编码 | 各自模块级 `_BUSY_TIMEOUT_MS` | 基础设施纯净 + script 自治，不 import config |
| 截断 `[:100]` / `[:200]` / `[:300]` | 函数内 magic number（30+ 处） | 各模块级 `_LOG_CONTENT_LEN` / `_DISPLAY_LIMIT` 等 | 有语义的阈值，按用途命名 |
| `_VALID_ACTIONS` / `MEMORY_ACTION_PATTERN` | `memory/action.py` 模块级 | 留原处 | 内部实现词汇（正则/集合），不进 config |
| `LLM_MEMORY_MAX_ACTIONS` re-export 链 | action→review/llm 间接 | review/llm 直接 `from config` | 消除 re-export 间接依赖 |

---

## 维护原则

1. **新代码强制遵守**：新增常量时按判据流程图判断归属，不要随手 hardcode。
2. **重复是信号**：同值出现 2+ 处，立即提共享常量（config 或模块级）。
3. **magic number 命名**：函数内出现有语义的数值（截断/超时/阈值），提模块级命名常量。
4. **config 膨胀可控**：按功能分区组织，新功能区加 `# xxx 相关常量` 横幅；不要把内部实现常量塞 config。
5. **文档跟随**：新增 config 功能区或调整归属规则时，更新本指南的「案例」与「判据」。

---

## 参考

- [config.py](../config.py)——根 config，按功能分区的代码常量
- [utils/llm/config.py](../utils/llm/config.py)——LLM 模块运行时配置（`_DEFAULT_CONFIG`）与 `ContextTier`
