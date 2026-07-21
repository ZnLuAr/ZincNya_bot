# 模块管理系统

> 落地时间：2026-05-25
> 最后更新：2026-07-10
> 
> Written by ZincNya~ ❤

---

## 概述

随着客户端功能的增加，启动时需要准备的东西也越来越多，这对系统的负担越来越大。

以前，所有功能都会通过扫描 handlers/ 自动加载。不管会不会用到，只要放在那里，启动时都会一起 import、一起常驻。于是有必要把这些功能重新整理成一套**显式的模块管理系统**。

模块管理系统让 bot 的功能从隐式的文件扫描变成显式的、可开关的逻辑模块。用户可以按需启用/禁用模块，缩短启动链路和常驻资源，也支持从 GitHub 安装第三方模块。

本文档主要记载：

- 模块的注册表结构和三个 JSON 状态文件
- 加载流程如何从旧的文件扫描改造为模块化加载
- CLI 工具 `scripts/module.py` 的 8 个子命令
- 共享文件的卸载策略和安全边界

## 目录

- [背景](#背景)
- [总体设计](#总体设计)
- [加载流程的改造](#加载流程的改造)
- [CLI: `scripts/module.py`](#cli-scriptsmodulepy)
- [已知局限（第一版不做）](#已知局限第一版不做)
- [与现有架构的关系](#与现有架构的关系)
- [后续可能的改进方向](#后续可能的改进方向)

---

## 背景

LLM、知识库、todos、book、stickers……默认情况下，客户端每次启动的时候，都会把这些一起准备好。但很多部署场景其实只会用到其中一两个模块，把所有东西都一起加载出来，多少还是有点浪费。

所以，这套模块系统主要想解决几件事情：

- 把"功能"从隐式的 handlers/ 文件扫描，变成显式的、可以开关的逻辑模块
- 让部署者按需启用 / 禁用模块，缩短启动链路，也减少常驻资源占用
- 支持从 GitHub 安装第三方模块，把扩展方式从 fork 整个项目，变成一条 install 命令就够了

## 总体设计

在这里，一个"模块"是一个**逻辑分组**，更像是一组一起工作的功能。它们由注册表条目声明：

```python
# modulesRegistry.py
MODULES = {
    "llm": {
        "id": "llm",
        "name": "LLM 对话",
        "description": "...",
        "version": "1.0.0",
        "author": "ZincPhos",
        "files": [...],            # 该模块拥有的所有文件
        "handlers": [...],         # 其中需要调用 register() 的 handler 文件
        "initFunctions": [...],    # 启动时调用的初始化函数 "module.path:funcName"
        "backgroundTasks": [...],  # 后台任务函数（格式同上）
        "dependencies": [],
        "githubRepo": None,
    },
    ...
}
```

字段全部使用 camelCase，和项目里的代码风格保持一致。

### 三个 JSON 状态文件（运行时本地状态，**不进入 git**）

| 文件 | 作用 | 格式 |
| --- | --- | --- |
| `data/modules.json` | 每个模块的 `enabled` 状态（用户的 enable/disable 偏好） | `{"llm": {"enabled": true}, ...}` |
| `data/modulesCustom.json` | 用户从 GitHub 安装的第三方模块的完整元数据 | `{"weather": {"enabled": true, "metadata": {"id": "weather", ...}, "source": "github:<url>@main", "installedAt": "..."}, ...}` |
| `data/modulesUninstalled.json` | 用户用 `uninstall -f` 强制删除的内置模块 id 列表（让 `getAllModules()` 跳过它们） | `["todos", "stickers"]` （数组） |

三个文件都加入了 `.gitignore`。这是因为它们记下来的，其实都只是每个部署实例自己的状态——哪些模块被打开了、后来装了哪些扩展、哪些内置模块被删掉了……这些跟仓库本身是没有关系的，放进 git 只会制造一些无意义的合并冲突的说……

## 加载流程的改造

### loader.py

旧逻辑：`pkgutil.iter_modules()` 扫描 `handlers/`，自动 import 所有有 `register()` 的模块。

于是就改成了：

1. 从 `getAllModules()` 拿到合并后的模块列表（内置 + custom，并排除掉 uninstalled）
2. 跳过 `enabled: false` 的模块
3. 对每个模块，遍历其 `handlers` 字段中的文件，import，并调用 `register()`
4. 注册返回的 handler 到 `Application`

这样，一个模块可以包含多个 handler 文件了。而 cli 这种不算 Telegram handler 的文件，依然通过 `SKIP_MODULES` 跳过。

**SKIP_MODULES 常量**（`loader.py` L38）：明确列出不需要加载的 handler 文件名（不含扩展名）。当前包含 `["cli"]`——`handlers/cli.py` 是控制台命令的入口文件，不实现 `register()` 函数，不属于 Telegram 处理器，因此需要跳过。如果未来有其他非 handler 文件放在 `handlers/` 目录下，也应添加到此列表。

### utils/core/appLifecycle.py

以前这里是硬编码调用 `initChatHistoryDB()`、`initTodosDB()`、`registerBookResources()` 等的。每多一个模块，就得再往这里补一行；想关掉某个模块，也得回来改这里。

现在有：

1. 遍历所有已启用模块，根据 `initFunctions` 列表，动态执行 `importlib.import_module` + `getattr`
2. 用一个 `set` 去重——如果多个模块都依赖同一个初始化函数，例如 `llm`、`reaction`、`send` 都声明了 `utils.chatHistory:initDatabase`，最终也只会调用一次
3. `backgroundTasks` 的处理方式基本一样，不过会先通过 `inspect.signature` 看一下函数有没有参数：需要参数就传 `app`，没有的话，就直接调用啦

像 `todoReminderLoop(app)` 本来就需要拿到 `app`，而 `reindexOnStartup()` 又完全不需要。如果只是为了统一接口，把所有初始化函数都改成一个签名，反而会让 knowledge loader 那边变得更难看；让调度器自己适配，就会自然得多了。

## CLI: `scripts/module.py`

目前有提供 8 个子命令：

```bash
list [-a|-e|-d]      # 列出模块（全部 / 仅启用 / 仅禁用）
show <name>          # 查看模块详情（文件、initFunctions、backgroundTasks、依赖）
enable <name>        # 启用模块
disable <name>       # 禁用模块
install <url>        # 从 GitHub URL 安装模块
uninstall <name>     # 卸载模块（详见下文）
validate             # 检查所有模块的文件是否完整（data/*.json 属运行时本地状态，允许初始不存在，跳过检查）
scan                 # 找出未被任何模块管理的 handler / utils 文件
```

### uninstall 的三种模式

- **默认**：如果待卸文件中有被其他启用模块共享的文件，会直接触发警告并退出，提示选用 `-s` 或 `-f`
- **`-s/--soft`**：只删除独占文件，共享文件会保留下来……算是最安全的方式
- **`-f/--force`**：不管是不是共享文件，全部直接删除

另外，不管用哪种模式，删除前都会对 metadata 里登记的每个路径重新跑一遍和 install 同款的安全校验（`_assertSafeModulePath`）；校验不过的会打印 `[SKIP]` 并跳过不删——就算 `modulesCustom.json` 被人手工篡改过，也不会误删项目或系统文件。

对于内置模块，默认不会直接允许卸载，而是提示优先使用 `disable` 的说——

```bash
> python scripts/module.py uninstall news
[!] news 是内置模块
  disable       # 只禁用，不删除文件（推荐）
  uninstall -f  # 强制删除文件（不可逆，需重新拉取代码才能恢复）
```

如果真的执行 `-f`，除了删除文件之外，还会把模块 id 写进 `modulesUninstalled.json`——这样下次启动的时候，注册表就会主动跳过它，不至于因为文件已经不存在，又在 `validate` 或加载阶段报错。

如果之后想恢复，也很简单：把那个 json 里的记录删掉，再 `git restore` 对应文件就可以了。

### 第三方模块的 `module.json` 规范

第三方仓库需要在根目录放一个 `module.json`。

字段应该和注册表保持一致，其中有——

必需字段：

- `id`
- `name`
- `version`
- `files`

和可选字段：

- `description`
- `author`
- `handlers`
- `initFunctions`
- `backgroundTasks`
- `dependencies`

安装的时候会做一些基本的安全检查：

- `id` 必须匹配 `^[a-z0-9_]+$`
- 所有 `files` 都必须以 `handlers/` 或 `utils/` 开头、不能包含 `..` 路径段、不能是绝对路径，而且 realpath 解析后的真实路径必须仍在项目根目录内（realpath + commonpath 校验，见 `scripts/module.py` 的 `_assertSafeModulePath`），避免路径遍历和逃逸
- `handlers` 中列出的每个文件，都必须真的实现 `def register()`
- 分支会自动尝试 `main` → `master`，当然也可以用 `--branch` 手动指定

## 已知局限（第一版不做）

这一版先没有继续往下做的事情有这些：

- **依赖检查**：注册表虽然已经有 `dependencies` 字段，但 install / enable 时，其实是不会自动检查的，需要自己保证一致性……
- **回滚**：在 `install` 中途失败时，目前只会清理临时目录；已经复制到项目里的文件，还是需要手动处理
- **本地路径 install**：暂时只支持 GitHub，还不能直接安装本地目录的说……
- **memory chatHistory 写入缺口**：其实这个和模块系统是没有直接关系的，不过正好顺手记一下——目前 chatHistory.db 只有 /send -c 控制台聊天界面会写入，LLM handler 自己还不会记录聊天历史，这是之前就存在的问题

## 与现有架构的关系

这套模块系统并没有去动核心基础设施。

像是 `utils/core/` 里的 logger、errorHandler、terminalUI、stateManager、resourceManager 这些仍然始终加载，不参与模块管理。

另外：

- `loader.py` 里的 `requireAuth` 权限包装逻辑保持不变
- `handlers/cli.py` 依然通过 `SKIP_MODULES` 跳过，不属于可以启用 / 禁用的模块喵

## 后续可能的改进方向

还有一些已经记下来的想法，不过暂时还没做到这里：

- `install --local <path>` 支持
- 依赖图检查（禁用 llm 时警告 llmCommand 等下游模块）
- 模块版本检查与更新命令
- 共享文件冲突的 install 时检测（目前只在 uninstall 时检测）