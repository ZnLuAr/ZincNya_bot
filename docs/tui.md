# TUI 框架技术文档

> 最后更新：2026-07-26
>
> 这份文档是项目里所有交互式终端界面（TUI）的总纲——讲清楚框架长什么样、三种交互范式各自怎么管屏幕和生命周期、横切关注点归谁管，以及想加一种全新交互类型时要动哪里。聊天界面 chatScreen 是其中一种范式的复杂实例，实现细节写在 [docs/chatScreen.md](chatScreen.md)，本文不重复。
>
> Written by ZincNya~ ❤

---

## 概述

项目里有好几处交互式终端界面：白名单管理、语录管理、Memory 管理、聊天界面、文件编辑器。它们的交互方式其实分属几种本质不同的类型，早先散落各处、各管各的——备用屏幕的进出、交互模式旗标、后台任务收尾这些东西没有统一的所有者，抄来抄去还抄出了不一致。

统一的 TUI 框架就是为了把这些理清而存在的。核心是两样东西：

- **一个会话契约 `TUISession`**——所有交互范式共同的基类，只管"横切关注点"（交互模式旗标、后台任务收尾、屏幕生命周期钩子），对"交互方式"零假设。
- **一个开放范式库 `paradigms/`**——每种交互范式一个文件，平级、可叠加。加新范式不动会话契约、不影响已有范式。

目标是三个词：**理清、集中、可控**——项目里有哪些 TUI、它们各自怎么管屏幕和生命周期，有一套讲得清的总纲；加新范式是一处局部改动。

## 目录

- [设计决策](#设计决策)
- [三种范式](#三种范式)
- [TUISession 会话契约](#tuisession-会话契约)
- [加新范式](#加新范式)
- [chatScreen：fullScreen 范式的复杂实例](#chatscreenfullscreen-范式的复杂实例)
- [已知局限](#已知局限)
- [相关文件](#相关文件)

---

## 设计决策

这里讲几个"为什么是现在这样"——

### 为什么 TUISession 对交互方式零假设

早先的 `BaseTUIController` 是个半途重构的产物：从白名单/语录控制器里抽出了列表交互的骨架，但渲染逻辑没收进去，后写的 Memory 才算完成态。结果基类的语义把"列表渲染"和"会话管理"两件事揉在了一起——列表菜单能用，全屏接管型的聊天界面和编辑器根本套不进来。

`TUISession` 是对这个的直接纠正。它只定义会话契约——进会话时干什么（`onEnter`）、主循环是什么（`mainLoop`）、出会话时干什么（`onExit`）——不预设渲染模型，也不预设输入模型。任何范式，含未来还没想到的类型，都继承它。

### 为什么范式是 `paradigms/` 下的平级模块

把"列表型"和"全屏型"硬塞进一个继承树是行不通的——它们屏幕模型完全不同（整屏 redraw vs pt Layout 即时渲染），强行抽象出共同子类只会得到一个谁都不好用的胶水层。

所以范式是**平级模块，可叠加但不乱继承**：

- `TextEditorApp` 继承 `FullScreenTUIApp` 成立——它本来就是全屏接管的特化，共用 pt Layout 生命周期。
- `ListMenuController` 与 `FullScreenTUIApp` 平级互不继承——它们的屏幕模型没有包含关系。

判断标准很简单：新范式是不是某个现有范式的特化？是就继承（像编辑器之于全屏），不是就平级。

### 为什么终端尺寸归各范式自己管

终端尺寸有两套口径：ANSI 口径（`shutil` / `terminalUI.getTerminalSize()`）和 prompt_toolkit 口径（`output.get_size()`）。这俩不能互相委托——列表菜单没用 pt 全屏布局，拿 pt 的尺寸会得到布局裁剪后的高度；全屏 pt 应用拿 ANSI 口径会忽略 pt 已分配的高度。

所以"我活在哪个世界、尺寸从哪取"属于交互方式，归范式自己管——每个范式在自己模块里取一次、取对口径。`ListMenuController.getTerminalHeight()` 委托 `terminalUI.getTerminalSize()[1]`，`ChatScreenApp` 用 `self._app.output.get_size()`。从根上干掉了抄来抄去还抄错的问题。

### 为什么横切关注点永远只在 `session.py` 一处

交互模式旗标、后台任务清单、收尾顺序这些东西，散落在各范式里就是灾难——早先列表菜单手工管旗标、chatScreen 的编排层手工管旗标+回调+任务、编辑器完全不管（靠父菜单持有），三套做法并存。

`TUISession.runSession` 把这些收到一处：进会话存旧旗标→置 True，出会话按固定顺序收尾（`onExit`→恢复旗标→cancel+await 后台任务）。范式只管覆写 `onEnter`/`mainLoop`/`onExit` 表达自己的交互，横切的脏活留给基类。

---

## 三种范式

`utils/core/tui/paradigms/` 下目前有三种范式，本质差异如下——

| 范式 | 屏幕/输入模型 | 终端尺寸口径 | 屏幕进出 | 现有实例 |
|------|--------------|-------------|---------|---------|
| **listMenu** | ANSI 备用屏幕 + 离散按键 → 整屏 redraw | `terminalUI.getTerminalSize()[1]` | 手动 `smcup`/`rmcup`（onEnter/onExit） | whitelist / quote / memory 三家列表 |
| **fullScreen** | pt `Application(full_screen=True)` 持续输入 + Layout 即时渲染 | `output.get_size()` | pt 自管 | chatScreen 聊天界面 |
| **textEditor** | 单个 `TextArea` 全屏接管，单发（保存/放弃即退） | 继承 fullScreen | pt 自管 | `editFile`（quote/memory 编辑路径） |

三种范式各自的模块：

- `listMenu.py` — `ListMenuController(TUISession)`。子类实现 `collectViewModel`（收集条目）+ `buildTable`（构建表格本体）；渲染管线（窗口计算、↑↓ 指示、cls/print/flush）由基类统一负责，`getHelpLine` 是可选钩子。
- `fullScreen.py` — `FullScreenTUIApp(TUISession)`。子类实现 `buildLayout`（布局根容器）+ `setupKeyBindings`（键绑定）+ `mainLoop`（交互主循环）；pt Application 在 `__init__` 构造（`createApplication` 可覆写），单轮入口 `runOnce`。
- `textEditor.py` — `TextEditorApp(FullScreenTUIApp)` + `editFile(filePath)` 薄壳函数。

`textEditor` 继承 `fullScreen` 是因为它就是全屏接管的特化——共用 pt Layout 生命周期，只是布局换成单个 TextArea、交互换成"保存/放弃即退"。

三种范式各自的实现骨架——

<details>
<summary>listMenu 详细实现（<code>ListMenuController</code>）</summary>

`ListMenuController(TUISession)` 定义在 `utils/core/tui/paradigms/listMenu.py`。它是 ABC——子类必须实现两个抽象方法，基类补齐渲染管线和生命周期：

- **子类必填**：`collectViewModel(selectedIndex)`（异步，收集条目）、`buildTable(visibleEntries, selectedIndex, windowStart)`（返回 rich `Table`，只管表格本体：标题/列/数据行/选中标记 `>`/`(+)` 行）。
- **可选钩子**：`getHelpLine()`（返回帮助行字符串或 `None`，默认 `None`）；`handlePendingAction`、`setupExtraKeyBindings`、`getEmptyMessage`、`getExitMessage`（管理模式用）。

渲染管线由基类 `renderUI` 统一负责，子类不碰——

```
getTerminalHeight → calculateVisibleWindow → buildTable → _captureLines(表格)
  → renderMoreIndicators(↑↓) → getHelpLine(若有) → _captureLines(帮助行)
  → drawLines(cls + 逐行 print + flush，返回行数)
```

终端高度走 ANSI 口径：`getTerminalHeight` 是 `@staticmethod`，委托 `terminalUI.getTerminalSize()[1]`。`calculateVisibleWindow` / `renderMoreIndicators` / `_captureLines` / `drawLines` 也都是静态方法。

生命周期映射到 TUISession：

| 钩子 | 实现 |
|------|------|
| `onEnter` | `print()` 占位 + `smcup()` 进备用屏幕 + `refreshEntries()` 拉数据 |
| `mainLoop` | 空列表短路（选择模式）→ 初次 redraw → 键绑定（上下/Esc/数字跳转/select 的 Enter + 子类 `setupExtraKeyBindings`）→ `while` 循环跑 `full_screen=False` 的 pt app；选择模式按 Enter 返回选中项，管理模式走 `handlePendingAction` |
| `onExit` | `rmcup()` 切回主屏 + 留一个空行分隔控制台提示 |

现有子类：`WhitelistTUIController` / `QuoteTUIController` / `MemoryTUIController`（分别在 `utils/whitelistManager/ui.py`、`utils/nyaQuoteManager/ui.py`、`utils/llm/memory/ui.py`）。

</details>

<details>
<summary>fullScreen 详细实现（<code>FullScreenTUIApp</code>）</summary>

`FullScreenTUIApp(TUISession)` 定义在 `utils/core/tui/paradigms/fullScreen.py`。它也是 ABC（`mainLoop` 抽象来自 `TUISession`，本类另加两个抽象方法）：

- **子类必填**：`buildLayout()`（返回布局根容器：`TextArea` / `HSplit` 等）、`setupKeyBindings(kb)`（注册键）、`mainLoop()`（交互主循环）。
- **可覆写的具体方法**：`createApplication()`（构造 pt `Application`，**非 abstract**）、`getExtraApplicationKwargs()`（额外传给 Application 的 kwargs，默认空）、`onEnter()`（默认空实现）。

构造时机契约——pt `Application` 在 `__init__` 构造，不在 `onEnter`：

```python
def __init__(self):
    super().__init__()                      # TUISession：_stateManager + _backgroundTasks
    self._ptApp = self.createApplication()   # 子类可覆写 createApplication

def createApplication(self) -> Application:
    kb = KeyBindings()
    self.setupKeyBindings(kb)
    kwargs = {"layout": Layout(self.buildLayout()), "key_bindings": kb, "full_screen": True}
    kwargs.update(self.getExtraApplicationKwargs())
    return Application(**kwargs)
```

`createApplication` 故意不是 `@abstractmethod`——子类用默认模板就能实例化 pt app；需要自定义的（如 chatScreen 为保测试 patch 目标 `utils.chatScreen.ui.Application`）在自己的模块覆写。本模块**不得 import `utils.chatScreen.*`**（基类不依赖子类实例，防循环 import），这条由 `test_tuiFullScreen.py` 钉着。

单轮入口 `runOnce`（基类默认模板）——

```python
async def runOnce(self):
    result = await self._ptApp.run_async()
    self._ptApp.output.reset_attributes()
    self._ptApp.output.flush()
    return result
```

覆写 `runOnce` 要小心：chatScreen 的覆写额外保留了 `_exitRequested` 守卫（exit 请求时返回 `None`），不能照搬基类的直接 `return result`——否则 exit 信号丢失，主循环退不出。

现有子类：`TextEditorApp`（本库）、`ChatScreenApp`（`utils/chatScreen/ui.py`）。

</details>

<details>
<summary>textEditor 详细实现（<code>TextEditorApp</code>）</summary>

`TextEditorApp(FullScreenTUIApp)` 定义在 `utils/core/tui/paradigms/textEditor.py`，是 fullScreen 的特化——布局换成单个 `TextArea`、交互换成"保存/放弃即退"的单发编辑器。

```python
class TextEditorApp(FullScreenTUIApp):
    def __init__(self, filePath: str):
        self._filePath = filePath
        # 读文件 + 建 TextArea（buildLayout 要用），再 super().__init__()
        # super 链中 createApplication → buildLayout/setupKeyBindings，此时 _editor 已就位
        self._editor = TextArea(text=initialText, multiline=True, scrollbar=True, wrap_lines=True, ...)
        super().__init__()

    def buildLayout(self):
        return self._editor

    def setupKeyBindings(self, kb):
        @kb.add("c-s")
        @kb.add("escape", "enter")      # 保存退出
        def _save(event): ...写回文件...; event.app.exit(result=True)
        @kb.add("c-c")                   # 放弃
        def _cancel(event): event.app.exit(result=False)
        @kb.add("escape")                # 单独 Esc 吞掉（不与 esc+enter 冲突）
        def _swallow(event): pass

    async def mainLoop(self):
        return await self.runOnce()      # 单发：一轮即退
```

`editFile(filePath)` 是保名薄壳——quote/memory 的测试 patch 点（`utils.nyaQuoteManager.ui.editFile` / `utils.llm.memory.ui.editFile`）依赖这个名字稳定：

```python
async def editFile(filePath: str) -> bool:
    return await TextEditorApp(filePath).runSession()
```

走 `runSession`（不是 `run`）——`TextEditorApp` 没有 `run` 方法，调 `.run()` 会 `AttributeError`；`runSession` 走完整单发生命周期（`onEnter` → `mainLoop`(=`runOnce`) → 收尾）。

</details>

---

## TUISession 会话契约

`TUISession` 是唯一基类，定义在 `utils/core/tui/session.py`。生命周期入口叫 `runSession`——

```python
async def runSession(self):
    prevInteractive = self._stateManager.isInteractive()
    self._stateManager.setInteractiveMode(True)
    try:
        await self.onEnter()
        return await self.mainLoop()
    finally:
        # 收尾顺序（钉死）：
        # 0. onExit（范式专属收尾，session 自身 try/except 包住）
        # 1. 恢复 interactiveMode（恢复进入前旧值，非硬置 False）
        # 2. cancel + await 后台任务（只吞 CancelledError）
```

几个要点——

### 入口为什么叫 `runSession` 不叫 `run`

这是为防静默递归有意而为的小小提示。chatScreen 的单轮入口叫 `runOnce`，被聊天主循环 `while` 反复调用；如果全生命周期入口也叫 `run`，哪天漏改了某个 `ui.run()`，就会静默走 `runSession → mainLoop → ... → ui.run()` 无限递归——单测 mock 掉主循环根本抓不到，只在真实 smoke 里炸。改名 `runSession` 后，单轮范式的 `ui.run()` 调用一旦漏改，直接 `AttributeError` 炸出来，loud fail。这条由 `tests/utils/core/test_tuiSession.py` 和 `test_chatUI.py` 的结构断言钉着。

### 嵌套旗标恢复的是旧值，不是硬置 False

`runSession` 进入时存 `prevInteractive`，退出时恢复成它。硬置 False 在早先能跑，是因为聊天界面是顶层入口、进去之前旗标本就是 False；但编辑器是从列表菜单（旗标 True）里唤起的，硬置 False 会踩掉父菜单的状态。恢复旧值后，任何范式被嵌套唤起都不踩父旗标，对顶层行为又等价。双层嵌套的端到端测试钉着这条。

### 后台任务异常自洽在任务内

`addBackgroundTask` 登记的任务，退出时统一 cancel + await，但 await 只吞 `CancelledError`。任务**内部**的非取消异常（比如消息接收协程里既有的 `except Exception: pass`）由任务自己负责吞——不冒泡到 `runSession` 的 finally，否则单个后台任务的非致命异常会打断其它任务的收尾。

收尾顺序（`onExit` → 恢复旗标 → cancel → await）由 `test_tuiSession.py` 用 list recorder 钉成 `['onExit','flag','cancel','await']`。

---

## 加新范式

未来要加一种全新的交互类型（比如带搜索的列表、分屏视图），框架已经留好了扩展点——

1. 在 `utils/core/tui/paradigms/` 下新建一个 `.py`（比如 `searchList.py`），定义 `SearchListApp(TUISession)`。实现 `mainLoop`（必填），按需覆写 `onEnter`/`onExit`。
2. 在 `utils/core/tui/__init__.py` 导出 `SearchListApp`。
3. 把新文件登记进 `modulesRegistry.py` 的 `core` 模块 `files`（必须，否则脱离模块管理；`module.py validate` 只查已登记文件存在，漏登记抓不到）。
4. 补测试到 `tests/utils/core/`，同样登记进 registry。

整个过程不碰 `session.py`、不碰其它范式——这是"开放范式库"的意思。范式之间要复用机制，按"是不是它的特化"决定继承还是组合，别硬抽公共父类。

---

## chatScreen：fullScreen 范式的复杂实例

聊天界面（`utils/chatScreen/ui.py` 的 `ChatScreenApp`）是 `FullScreenTUIApp` 目前来说最为复杂的子类——固定底部输入的布局、消息接收后台协程、LLM 审核集成、Alt+←→ 切换聊天对象，全都在它身上。

它的范式归属是 fullScreen：pt `Application(full_screen=True)` 接管终端，单轮入口 `runOnce` 被聊天主循环 `while` 反复调用，`mainLoop` 委托给业务层主循环。chatScreen 专属的东西（控制台输出路由、receiver 协程启动）放在它自己的 `onEnter`/`onExit` 里，不进 `TUISession` 基类——因为只有它把 logger 输出路由到 UI。

实现细节（布局组件、键绑定、滚动、审核命令、消息流转）写在 [docs/chatScreen.md](chatScreen.md)。

---

## 已知局限

1. **列表菜单唤起编辑器后的退出屏幕恢复**。`textEditor` 范式的全屏编辑器从列表菜单（手动 `smcup` 备用屏幕）里唤起时，pt 自管的 alt-screen 和手动的 `rmcup` 状态在最终退出时恢复得不够彻底，偶有残留。这是 ANSI 备用屏幕与 pt `full_screen` 嵌套的已知交互，列表浏览本身不受影响。统一修法是在 `TUISession` 加 `prepareChildSession`/`restoreChildSession` 钩子包裹嵌套唤起——暂未做，等真实使用中确认值得修再说。
2. **控制台输出回调是单槽**。`stateManager` 的 `consoleOutputCallback` 是全局单值，不是 per-key 字典。chatScreen 的 `onEnter` 注册自己的回调、`onExit` 注销（置 None）。如果两个 chatScreen 实例嵌套（目前不会发生），内层注销会把外层的回调也清掉。这条以 `test_tuiSession.py` 的 xfail 文档化钉着；真有嵌套路由需求时，修法是改成引用计数。
3. **chatScreen 在 Windows 10 及以下的传统 PowerShell / conhost 里渲染不正常**。表现是滚动历史时整屏上移或下移一行、原画面不重绘。原因在 `utils/chatScreen/ui.py` 的 `createApplication` 硬传了 `output=Vt100_Output.from_pty(sys.stdout)`——这绕过了 prompt_toolkit 自己的平台探测（`create_output` 在 win32 上本会按 VT 是否开启依次选 `Windows10_Output` / `ConEmuOutput` / `Win32Output`，最后那个用 Win32 API 直接操作屏幕缓冲、根本不发 ANSI）。老 conhost 读不懂这些转义序列，于是画面错位。
   **只影响 chatScreen 一家**：`FullScreenTUIApp` 的默认 `createApplication` 与 `TextEditorApp` 都不传 `output=`，走 pt 自动探测，所以列表菜单和编辑器在同样的终端里是正常的。
   不修——用 Windows Terminal（或任何开了 VT 的现代终端）即可正常使用，为一个已被微软自己取代的终端改动生产路径上的渲染装配不划算。真要修，最小改法是 `createApplication` 里去掉 `output=` 参数交还给 pt 探测。

---

## 相关文件

| 文件 | 职责 |
|------|------|
| `utils/core/tui/session.py` | `TUISession` 会话契约（横切关注点唯一所有者） |
| `utils/core/tui/paradigms/listMenu.py` | `ListMenuController`——列表+方向键范式 |
| `utils/core/tui/paradigms/fullScreen.py` | `FullScreenTUIApp`——全屏 pt 接管范式 |
| `utils/core/tui/paradigms/textEditor.py` | `TextEditorApp` + `editFile` 薄壳——全屏文本编辑器 |
| `utils/core/tui/__init__.py` | 统一出口（`TUISession` + 各范式 + `editFile`） |
| `utils/core/terminalUI.py` | ANSI 终端工具（`getTerminalSize` / `cls` / `smcup` / `rmcup`） |
| `utils/core/stateManager.py` | 全局状态（`interactiveMode` / 消息队列 / 关机事件 / 控制台回调） |
| `utils/chatScreen/` | chatScreen——fullScreen 范式的聊天界面实例（详见 [chatScreen.md](chatScreen.md)） |
| `utils/whitelistManager/ui.py`、`utils/nyaQuoteManager/ui.py`、`utils/llm/memory/ui.py` | listMenu 范式的三家列表控制器 |

---

## 参考

- [chatScreen 聊天界面](chatScreen.md)——fullScreen 范式的完整实现细节
- [prompt_toolkit 官方文档](https://python-prompt-toolkit.readthedocs.io/)
