# chatScreen 聊天界面技术文档

> 最后更新：2026-07-09
>
> Written by ZincNya~ ❤

---

## 概述

**chatScreen** 是控制台中与 Telegram 用户/群组的全屏交互式聊天界面，让拥有操作者可以在控制台中以 Telegram bot 的身份与 Telegram 用户进行**实时对话**，而不需要打开 Telegram 客户端。

这份文档记载聊天界面的实现细节，比如架构分层、消息流转、按键绑定、LLM 审核集成，以及如何做到不退出界面就能切换聊天对象——面向正在读这篇文档的你，我们在这里把这些搞清楚。

## 目录

- [架构](#架构)
  - [业务编排层](#1-业务编排层)
  - [UI 控制层](#2-ui-控制层)
  - [状态管理](#状态管理)
- [键绑定完整列表](#键绑定完整列表)
- [LLM 审核集成](#llm-审核集成)
- [消息流转](#消息流转)
- [receiverLoop 后台协程](#receiverloop-后台协程)
- [切换功能](#切换功能)
- [扩展指南](#扩展指南)
- [故障排查](#故障排查)
- [相关文件](#相关文件)
- [设计决策记录](#设计决策记录)
- [性能考量](#性能考量)
- [未来改进方向](#未来改进方向)
- [参考资源](#参考资源)

在控制台输入 `/send -c <chatID>` 或 `/send -c`（弹出白名单列表选择），就可以进入全屏 TUI 的聊天界面了——

---

## 架构

chatScreen 功能由两层协作实现——业务编排层管生命周期和输入处理，UI 控制层管渲染和键盘事件，两者通过 `await ui.run()` 解耦。

为什么要分成两层？因为聊天界面要同时做两件事：一边要响应用户的按键、滚动、发送消息（这是 UI 的活），一边要处理 LLM 审核命令、管理后台协程、清理资源（这是业务逻辑的活）。把它们拆开来，UI 层就只管"界面长什么样、键盘按下去发生什么"，业务层管"进来了要做什么、退出时要清理什么"——

两边各干各的，通过 `await ui.run()` 这个阻塞调用连起来。举个例子：UI 正常渲染，若用户按了发送键，`run()` 就返回输入框内容，内容传到业务层，业务逻辑后调用 Telegram API 发出去；如果按下的是 Esc，`run()` 返回 `None`，这时候，业务层知道该退出了。

### 1. 业务编排层 

这一层，可以算是整个聊天界面的总调度官。它负责把各个组件组织起来、让它们按正确的顺序启动和清理，并且在整个过程中维护全局状态 `interactiveMode`，来确保外层的 CLI 不与聊天界面的输入框冲突。

入口在 `utils/chatScreen/session.py` 的 `chatscreen()`，业务编排层**主要负责**：

- 管理 `interactiveMode` 状态，这个状态用于控制是否暂停聊天界面外层 CLI 的输入循环
- 选择聊天对象。如果用户未指定，则调用 `chatIDList()` 弹出白名单选择器
- 启动和清理后台 `receiverLoop()` 协程
- 处理用户输入:发送消息、LLM 审核命令、编辑模式
- 退出时恢复状态、取消后台任务、清理消息队列

这个过程中，有以下**关键变量**:
- `targetChatID`:当前聊天对象的 Telegram chat ID
- `receiverTask`:后台协程任务,监听消息队列
- `_reviewEditItem`:LLM 审核编辑模式的状态(局部变量)

……从用户输入 `/send -c` 并按下回车开始，业务层的**生命周期**如下表所示——

```
进入 chatScreen
    ↓
设置 interactiveMode = True
    ↓
选择 targetChatID (如未指定)
    ↓
加载历史记录                # 初始化 ChatScreenApp
    ↓
启动 receiverLoop() 后台任务
    ↓
主循环: await ui.run()      # 处理用户输入
    ↓
用户按 Esc 退出
    ↓
finally 块:
  - 取消 receiverTask
  - 恢复 interactiveMode = False
  - 清理 consoleOutputCallback
  - 非目标消息放回队列
```

<details>
    <summary>被折叠起来的启动流程</summary>

**以下就是启动流程了**：

**1. 接管控制台**

```python
state = getStateManager()
state.setInteractiveMode(True)
queue: asyncio.Queue = state.getMessageQueue()
shutdownEvent = state.getShutdownEvent()
```

> `setInteractiveMode(True)` 调整客户端 `InteractiveMode` 为 `True`。这个操作会让外层 CLI 的 `asyncInput()` 跳过读取，以此来防止 CLI 和 `prompt_toolkit` 同时抢键盘输入。并且在这个过程中拿到全局消息队列和关机事件的引用，准备传给后面的 receiver。

**2. 初始化 UI**

```python
initialLines = await buildHistoryLines(targetChatID)
initialLines.extend([
    "",
    f"已进入聊天界面喵",
    f"与 {targetChatID} 的实时聊天已连接",
    "=" * 64,
])
ui = ChatScreenApp(targetChatID, initialLines=initialLines)
```

`buildHistoryLines()` 从加密数据库 `chatHistory.db` 加载历史记录，格式化成带时间戳的字符串列表，然后追加欢迎信息，一起传给 `ChatScreenApp` 构造器。UI 初始化时会把这些行存入 `_allLines` 并刷新显示。

**3. 注册控制台输出回调**

```python
def _consoleOutputHandler(text: str):
    lines = text.rstrip('\n').split('\n')
    ui.appendLines(lines)

state.setConsoleOutputCallback(_consoleOutputHandler)
```

设置后，`utils/core/logger.py` 的输出会路由到这个回调，最终显示在 UI 的聊天记录区——这样后台日志（审核队列处理、错误提示等）就能在界面里看到。
——至于为什么要专门做个路由呢？这是因为如果按照平常的方式直接打印日志输出，下方的聊天框和状态栏就会被冲掉，这样整个 TUI 布局就乱了。于是我们在 logger 使用了自实现的专用函数 `safePrint()` (`utils/core/stateManager.py`)，专门把后台日志输出回调路由到 UI。

**4. 启动 receiverLoop**

```python
receiverTask = await startReceiver(state, targetChatID, ui, queue, shutdownEvent)
await asyncio.sleep(0.1)
```

由 `startReceiver()` 返回 `asyncio.create_task(receiverLoop())`（`receiver.py`），这个后台协程会持续从 `queue` 里读消息：

- `await asyncio.wait_for(queue.get(), timeout=0.5)`（receiver.py）每 0.5 秒超时一次
- 超时时检查 `shutdownEvent.is_set()`，如果关机信号触发就调 `ui.requestExit()` 强制退出界面
- 超时时还会检查审核队列，有待审核项就更新状态栏提示
- 收到消息后过滤：`if str(msg.chat.id) != str(targetChatID)`不匹配的暂存到 `nonTargetMessages`
- 匹配的消息，在进行解析后，客户端会调用 `await saveMessage(...)` 将消息存入加密数据库，然后调用 `ui.appendIncomingMessage(...)`，将新接收的消息，最终显示在 UI 的聊天记录区

`receiverTask` 保存这个 task 引用，退出时会顺带着把它 `cancel()` 掉。

**5. 进入主循环**

```python
reviewEditItem = await runMainLoop(bot, targetChatID, ui, shutdownEvent)
```

`runMainLoop()` 在 `mainLoop.py` 中，其核心是 `while not shutdownEvent.is_set()` 循环：

```python
userInput = await ui.run()  # 阻塞，等用户按键

if userInput is None:  # Esc / Ctrl+C
    if _reviewEditItem is not None:
        # 编辑模式中 Esc 取消编辑，放回队列，不退出
        getReviewQueue().put_nowait(_reviewEditItem)
        _reviewEditItem = None
        ui.resetExitFlag()
        ui.clearComposer()
        continue
    
    # 检测切换信号
    if ui._switchDirection:
        return {"action": "switch", "direction": ui._switchDirection}
    
    break  # 正常退出
```

在拿到 `userInput` 后，会进行以下匹配，执行相应动作——
- 接收到审核命令（`:ra` / `:re` 等），调用 `handleChatScreenReviewCommand()`
- 在编辑模式（`:re` 模式下）中提交时，调用 `handleChatScreenEditSubmit()`
- 发送普通消息时，调用 `bot.send_message()` 发送，然后将消息存库

`_reviewEditItem` 是局部变量，记住当前正在编辑的审核项。在 `:re` 时赋值，提交或取消后清空。

**6. 清理**

```python
finally:
    # 编辑模式中退出，则将待审核信息放回队列
    if reviewEditItem is not None:
        if not (isinstance(reviewEditItem, dict) and reviewEditItem.get("action") == "switch"):
            getReviewQueue().put_nowait(reviewEditItem)
    
    state.setConsoleOutputCallback(None)  # 注销回调
    state.setInteractiveMode(False)       # 交还控制台
    receiverTask.cancel()                 # 取消后台协程
    try:
        await receiverTask
    except asyncio.CancelledError:
        pass
```

`finally` 块确保无论正常退出还是异常，都清理干净。`receiverLoop` 的 `finally` 块会把暂存的 `nonTargetMessages` 放回队列，供外层 handler 处理。

**7. 透传切换信号**

```python
if isinstance(reviewEditItem, dict) and reviewEditItem.get("action") == "switch":
    return reviewEditItem
```

如果用户使用 `Alt + ←/→`，`runMainLoop` 就会返回切换信号 `action="switch"`。之后，`chatScreen` 会把它原样返回给调用方 `send.py`，后者会循环调用 `chatScreen`，实现聊天对象在聊天屏幕内的热切换。

此间使用到关键变量：
- `targetChatID` — 当前聊天对象的 Telegram chat ID（函数参数，贯穿整个流程）
- `receiverTask` — 后台协程任务引用，退出时用来 `cancel()`
- `reviewEditItem` — 主循环返回值，编辑模式中退出时放回队列
- `_reviewEditItem` — mainLoop.py 的局部变量，记住当前正在编辑的审核项

</details>

---

### 2. UI 控制层 

而 UI 控制层，其入口位于 `utils/chatScreen/ui.py` 的 `ChatScreenApp`。它主司：

- 管理 `prompt_toolkit` 全屏 `Application` 的布局和事件循环
- 维护聊天记录的缓冲区（`_allLines`）和滚动状态（`_scrollOffset`）
- 处理用户的键盘快捷键，如发送、退出、滚动、清空等
- 更新状态栏提示（审核队列、滚动位置、聊天对象）

又有以下的 **关键组件**:
- `_transcriptArea`：作为只读 `TextArea`，显示聊天记录
- `_composerArea`：可编辑 `TextArea`，多行输入框
- `_statusBar`：底部状态栏，显示快捷键提示和状态信息
- `_allLines`：存在内存中的完整聊天记录行列表
- `_scrollOffset`：滚动偏移量。当为 0 时，则跟随最新消息；>0 则表示向上偏移了 N 行

UI 控制层在主循环中，由**事件驱动**：
```
await ui.run()
    ↓
prompt_toolkit 事件循环阻塞
    ↓
用户按键 → KeyBindings 回调
    ↓
  - Ctrl+S / Alt+Enter → event.app.exit(result=输入框内容)
  - Esc → self._exitRequested=True; event.app.exit(result=None)
  - Alt+↑↓ / PgUp/PgDn → _scrollUp() / _scrollDown()
    ↓
返回到 send.py 主循环,根据 result 处理
```

<details>
    <summary>被折叠起来的初始化二三事</summary>

> 
> 
> 在往下看之前，应该先认识到，整个 `ui.py`，其实是一个大 `ChatScreenApp` 类。
> 
> 有必要先把 `ChatScreenApp` 这个 class 讲清楚——不然流程看了，也形成了自己的认识，切到 `ui.py` 看到几百行的大类，很容易就发懵了——这是常会出现的情况……
> 
> 整个 class 大致分成三块：
> 
> - **`__init__`**：一次性把界面搭起来。创建三块区域（聊天记录区 / 输入区 / 状态栏）、绑定快捷键、拼装 `prompt_toolkit` 的 `Application`、初始化滚动状态并加载初始历史。下面「初始化二三事」讲的就是这一段。
> - **对外 API**：业务层能调的方法，都在这一块。`appendLines` / `appendIncomingMessage` / `appendSelfMessage` 往界面追加消息，`showStatus` 更新状态栏，`clearComposer` 清空输入框，`requestExit` / `resetExitFlag` 管退出标记，`run` 跑事件循环——业务层只碰这些，不碰下面的内部方法。
> - **内部方法**：带前导下划线的私有方法，外部不该直接调。`_refreshTranscript` 按 `_scrollOffset` 切窗口刷新显示，`_scrollUp` / `_scrollDown` / `_clampOffset` 管滚动，`_updateStatus` / `_defaultStatus` 管状态栏文本，`_getWindowHeight` / `_getTermWidth` 拿终端尺寸。
> 
> 了解清楚这三块的分工，下面的代码片段就知道各自落在哪一格了。

**1. 创建三块区域**

```python
# 聊天记录区（只读 TextArea）
self._transcriptArea = TextArea(
    text="",
    multiline=True,
    scrollbar=True,
    wrap_lines=True,
    read_only=True,  # 关键：只读，通过 buffer.set_document() 更新
)

# 输入区（可编辑 TextArea）
self._composerArea = TextArea(
    text="",
    multiline=True,
    wrap_lines=True,
)

# 状态栏（Window + FormattedTextControl）
self._statusBar = Window(
    content=FormattedTextControl(
        text=lambda: [("reverse", self._statusText.ljust(self._getTermWidth()))],
    ),
    height=1,
)
```

三块区域通过 `prompt_toolkit` 的 `HSplit` 从上到下竖着排列，且在消息区域和输入框中间有一条分隔线（`Window(height=1, char="─")`）。

**2. 绑定快捷键**

```python
kb = KeyBindings()

@kb.add("c-s")
@kb.add("escape", "enter")  # Alt+Enter
def _submit(event):
    event.app.exit(result=self._composerArea.text)

@kb.add("c-c")
@kb.add("escape")
def _cancel(event):
    self._exitRequested = True
    event.app.exit(result=None)

@kb.add("escape", "left")  # Alt+←
def _switchPrev(event):
    self._switchDirection = "prev"
    self._exitRequested = True
    event.app.exit(result=None)

...

```

上面的每个按键回调只做 UI 状态更新或 `event.app.exit()`，不涉及业务逻辑——`exit()` 会让 `await ui.run()` 返回，业务层拿到返回值再决定怎么处理。

**3. 初始化滚动状态**

```python
self._allLines: list[str] = []
self._scrollOffset: int = 0  # 0 = 底部（最新），>0 = 向上偏移行数

if initialLines:
    for line in initialLines:
        self._allLines.extend(line.split('\n'))  # 每个元素可能包含多行
    self._refreshTranscript()
```

`_allLines` 是存在内存中的完整聊天记录，每一行都是一个元素。对于 `_allLines`，有 `_scrollOffset` 决定显示哪个窗口：当该值为 0 时，屏幕跟随最新消息（显示最后 N 行），>0 就是向上偏移（浏览历史）。

接下来，是该层的一些**核心方法**，它们在 `ChatScreenApp` 中都起到关键作用——

> **追加消息**
> 
> ```python
> def appendLines(self, lines: list[str]):
>     for line in lines:
>         self._allLines.extend(line.split('\n'))
>     
>     if self._scrollOffset == 0:
>         self._pendingNewMessages = 0  # 跟随最新，重置计数
>     else:
>         self._pendingNewMessages += 1  # 浏览历史，累计新消息
>     
>     self._updateStatus()
>     self._refreshTranscript()
> ```
> 
> 业务层调用 `ui.appendIncomingMessage(timestamp, sender, text)` 的同时，内部也可以调用 `appendLines()`，往 `_allLines` 追加格式化好的行。如果用户正在浏览历史（`_scrollOffset > 0`），新消息不会自动滚到底，而是会在状态栏会显示"有 N 条新消息"的提示，等用户滚回底部再恢复跟随。
> 
> 
> **刷新显示**
> 
> ```python
> def _refreshTranscript(self):
>     windowHeight = self._getWindowHeight()  # 实际渲染高度（行数）
>     total = len(self._allLines)
>     
>     if self._scrollOffset == 0:
>         # 跟随最新：显示最后 windowHeight 行
>         start = max(0, total - windowHeight)
>         visibleLines = self._allLines[start:]
>     else:
>         # 历史浏览：从底部往上偏移 _scrollOffset
>         end = max(0, total - self._scrollOffset)
>         start = max(0, end - windowHeight)
>         visibleLines = self._allLines[start:end]
>     
>     # 确保显示固定行数，顶部不足补空行
>     while len(visibleLines) < windowHeight:
>         visibleLines.insert(0, "")
>     
>     text = "\n".join(visibleLines)
>     
>     # 更新 Buffer（read_only=True 时需要 bypass_readonly）
>     buf = self._transcriptArea.buffer
>     buf.set_document(Document(text=text), bypass_readonly=True)
>     buf.cursor_position = len(text)  # 光标到底部，让视图显示最下方
> ```
> 
> 每次追加消息、滚动、切换状态都会使用这个方法——它根据 `_scrollOffset`，从 `_allLines` 切出可见窗口，刷新 `_transcriptArea` 的显示。
> 
> 
> **滚动**（ui.py:267-279）
> 
> ```python
> def _scrollUp(self, lines: int):
>     """向上滚动（查看历史）"""
>     self._scrollOffset += lines
>     self._clampOffset()  # 确保不超过可滚动范围
>     self._updateStatus()
>     self._refreshTranscript()
> 
> def _scrollDown(self, lines: int):
>     """向下滚动（回到最新）"""
>     self._scrollOffset = max(0, self._scrollOffset - lines)
>     self._updateStatus()
>     self._refreshTranscript()
> ```
> 
> 可以使用 `PgUp` / `PgDn` 按可见窗口高度滚动，也可以只用 `Alt+↑/↓` 按 1 行滚动。滚动时状态栏切到"历史浏览"模式，即此时的 `_scrollOffset > 0`。
> 
> 
> **事件循环**
> 
> ```python
> async def run(self) -> str | None:
>     result = await self._app.run_async()
>     # 退出全屏后重置终端状态，清除残留的状态栏
>     self._app.output.reset_attributes()
>     self._app.output.flush()
>     return None if self._exitRequested else result
> ```
> 
> `await self._app.run_async()` 阻塞在 `prompt_toolkit` 的事件循环，等待用户按键，触发 `event.app.exit(result=...)` 使继续流动。退出后，根据 `_exitRequested` 标志决定返回值：
> - `Esc` / `Ctrl+C` 两组按键，会设置 `_exitRequested = True`，返回 `None`，此时退出聊天窗口
> - `Ctrl+S` / `Alt+Enter` 两组，不会设置标志，返回输入框内容，用于发送消息
> - 在编辑模式中 `Esc` 取消编辑时，业务层会调用 `ui.resetExitFlag()` 来重置标志，避免力大砖飞误退出

</details>

---

### 状态管理

chatScreen 依赖 `utils/core/stateManager.py` 的全局状态：

| 状态 | 类型 | 用途 |
|------|------|------|
| `interactiveMode` | `bool` | 标记当前是否在交互模式，暂停外层 CLI 的 `asyncInput()` |
| `messageQueue` | `asyncio.Queue` | 全局消息队列，bot 收到的所有 Telegram 消息都进入此队列 |
| `consoleOutputCallback` | `Callable` | 控制台输出回调，chatScreen 设置后，logger 的输出会路由到 UI transcript |
| `shutdownEvent` | `asyncio.Event` | 关机信号，receiverLoop 检测到后会强制退出 UI |

`interactiveMode` 是关键——它为 `True` 时，外层 CLI 的 `asyncInput()` 会跳过读取，避免和 `prompt_toolkit` 冲突；退出后必须恢复为 `False`，不然外层 CLI 就卡死了。

---

## 键绑定完整列表

| 快捷键 | 功能 | 说明 |
|--------|------|------|
| `Enter` | 换行 | 在输入框内插入换行符 |
| `Ctrl+S` | 发送消息 | 将输入框内容发送到 Telegram，清空输入框 |
| `Alt+Enter` | 发送消息 | 同 `Ctrl+S` |
| `Esc` | 退出聊天界面 | 如在编辑模式，则取消编辑（不退出） |
| `Ctrl+C` | 退出聊天界面 | 同 `Esc` |
| `Ctrl+X` | 清空输入框 | 清空当前输入框的所有内容 |
| `Alt+↑` | 向上滚动 1 行 | 查看历史消息 |
| `Alt+↓` | 向下滚动 1 行 | 回到最新消息 |
| `PgUp` | 向上滚动 1 页 | 页大小 = 可见窗口高度 |
| `PgDn` | 向下滚动 1 页 | 回到最新消息 |
| `Alt+←` | 切换到上一个聊天对象 | 按 whitelist 列表顺序循环切换 |
| `Alt+→` | 切换到下一个聊天对象 | 按 whitelist 列表顺序循环切换 |

**滚动行为**：
- 滚动时，状态栏切换为 `[历史浏览]` 模式，显示当前偏移量
- 如有新消息到达但用户正在浏览历史，状态栏会显示 `▼ 有 N 条新消息喵`
- 滚动到底部（offset = 0）时，自动切回跟随最新模式

---

## LLM 审核集成

chatScreen 内置了 LLM 审核队列的处理功能——可以在聊天界面里直接审核待发送的 LLM 回复。

### 审核命令

在输入框输入以下命令（不加任何其他文字），按 `Ctrl+S` 提交：

| 命令 | 功能 | 说明 |
|------|------|------|
| `:ra` | 通过并发送 | Review Approve — 审核通过,立即发送到 Telegram |
| `:re` | 进入编辑模式 | Review Edit — 编辑回复内容,输入框变成编辑器 |
| `:rr` | 重试生成 | Review Retry — 不带反馈重试 LLM 生成 |
| `:rf` | 反馈重试 | Review Feedback — 带反馈重试(输入框输入反馈) |
| `:rc` | 取消审核 | Review Cancel — 取消当前审核项,不发送 |
| `:rq` | 查看队列 | Review Queue — 显示审核队列状态 |

### 编辑模式

执行 `:re` 后：
1. 输入框内容被替换为待审核的 LLM 回复预览
2. 状态栏显示 `[编辑模式] 修改后按 Ctrl+S 提交 | Esc 取消编辑`
3. 用户修改内容后按 `Ctrl+S`，修改后的内容会放回审核队列
4. 按 `Esc` 取消编辑，原内容放回队列，**就不用退出聊天界面了**

### 状态栏提示

当审核队列有待处理项时，状态栏自动显示：

```
待审核: (3 条) | :ra 通过 :re 编辑 :rr 重试 :rc 取消 | [预览: "你好，好久不见..."]
```

- `(3 条)`：队列中待审核的数量
- `[预览: ...]`：当前队首项的前 30 字符预览

---

## 消息流转

一条消息从 Telegram 那边发过来，到最后显示在聊天界面上，中间是这样一条线走下来的——

### 接收消息流程

```
Telegram 用户发送消息
        ↓
Bot 的全局 MessageHandler (group=-1, messageCollector)
        ↓
消息进入 stateManager.messageQueue (asyncio.Queue)
        ↓
receiverLoop() 后台协程持续监听队列
        ↓
过滤：只处理 msg.chat.id == targetChatID 的消息
        ↓
解析 sender 和 content
        ↓
保存到加密数据库: chatHistory.saveMessage(targetChatID, "incoming", sender, content)
        ↓
ui.appendIncomingMessage(timestamp, sender, content)
        ↓
UI 更新显示，自动滚动到底部（如未在浏览历史）
```

对于**非目标消息的处理**，receiverLoop 在收到其他聊天的消息时，会暂存到 `nonTargetMessages` 列表；在退出时由 finally 块把这些消息放回 `messageQueue`，供外层处理

---

### 发送消息流程

```
用户在输入框输入文字，按 Ctrl+S
        ↓
ui.run() 返回输入框内容
        ↓
chatScreen 主循环调用 bot.send_message(chat_id=targetChatID, text=...)
        ↓
保存到加密数据库: chatHistory.saveMessage(targetChatID, "outgoing", "ZincNya~", content)
        ↓
ui.appendSelfMessage(timestamp, "ZincNya~", content)
        ↓
UI 更新显示，输入框清空
```

这个过程中，可能出现的以下异常——
- `Forbidden`：对方还未与 bot 开始对话，状态栏显示提示
- 其他异常：状态栏显示 `发送失败: <错误信息>`

---

## receiverLoop 后台协程

`receiverLoop()` 是 chatScreen 的核心后台任务，负责持续监听消息队列并更新 UI——它在后台跑着，一边盯着队列有没有新消息进来，一边定期检查审核队列和关机信号。

### 实现细节

```python
async def receiverLoop():
    nonTargetMessages = []
    
    try:
        while state.isInteractive():
            try:
                # 0.5 秒超时读取队列
                msg = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                # 检测关机信号
                if shutdownEvent.is_set():
                    ui.requestExit()
                    break
                # 定期检查审核队列,更新状态栏
                rq = getReviewQueue()
                if rq.qsize() > 0:
                    ui.showStatus(...)
                continue
            
            # 过滤非目标消息
            if str(msg.chat.id) != str(targetChatID):
                nonTargetMessages.append(msg)
                continue
            
            # 解析并显示
            sender = _getSenderName(msg)
            content = _extractDisplayText(msg)
            await saveMessage(targetChatID, "incoming", sender, content)
            ui.appendIncomingMessage(timestamp, sender, content)
            
    finally:
        # 放回非目标消息
        for msg in nonTargetMessages:
            await queue.put(msg)
```

上面的实现里藏着几个关键设计——**超时机制**：`wait_for(timeout=0.5)` 避免无限阻塞，每 0.5 秒检查一次状态；**消息过滤**：只处理 `msg.chat.id == targetChatID` 的消息，其他暂存；**审核队列轮询**：超时时顺便检查审核队列，更新状态栏提示；**关机检测**：`shutdownEvent.is_set()` 时强制退出 UI，避免卡死；**清理保证**：finally 块确保非目标消息不丢失。

---

## 切换功能

用户按 **Alt+← / Alt+→** 可以在聊天界面内切换到白名单中的上/下一个聊天对象——不用退出界面，屏幕闪一下就换到下一个对象了。

### 信号传递链

信号传递流程是这样的：

```
ui.py (键绑定)
  ↓ 设置 self._switchDirection = "prev"/"next"
  ↓ 调用 self._app.exit(result=None)
  ↓
mainLoop.py
  ↓ 检测 ui._switchDirection
  ↓ 返回 {"action": "switch", "direction": "prev"/"next"}
  ↓
session.py
  ↓ finally 块检测切换信号，不放回审核队列
  ↓ 透传信号给调用方
  ↓
send.py (while 循环)
  ↓ 调用 helpers.getNextChatID(currentChatID, direction)
  ↓ 获取下一个/上一个 chatID
  ↓ currentChatID = nextChatID, continue
  ↓
重新调用 chatScreen(app, bot, nextChatID)
  ↓ 屏幕闪一下，进入新的聊天界面
```

**返回值约定** (`mainLoop.runMainLoop` → `session.chatScreen`)：
- `None` — 正常退出 (Esc / Ctrl+C)
- `{"action": "switch", "direction": "next"/"prev"}` — 请求切换聊天对象
- `dict` (审核项) — 编辑模式中退出，需放回审核队列

**边界情况**：
- 在只有 0 或 1 个用户的情况下使用该快捷键，`getNextChatID` 返回 `None`，给出提示"只有一个聊天对象"
- 当前 chatID 不在 whitelist 中，则回退到列表第一个用户
- 到达列表末尾/开头，则取模循环

---

## 扩展指南

想给聊天界面加新功能的话，下面是几个常见的改动场景——

### 如何添加新的键绑定

假设要添加 `Ctrl+L` 清屏功能——

#### 1. 在 `utils/chatScreen/ui.py` 的 `__init__` 中添加键绑定

```python
# 在 kb = KeyBindings() 之后添加
@kb.add("c-l")
def _clearScreen(event):
    self._allLines.clear()
    self._scrollOffset = 0
    self._refreshTranscript()
```

#### 2. 更新状态栏默认提示

```python
def _defaultStatus(self) -> str:
    return (
        "Enter 换行 | Ctrl+S / Alt+Enter 发送 | Esc 退出 | Ctrl+X 清空"
        " | Alt+↑↓ / PgUp PgDn 滚动历史 | Ctrl+L 清屏"  # 新增
        f" | 聊天对象: {self._targetChatID}"
    )
```

#### 3. 更新本文档的键绑定表

在"键绑定完整列表"章节补充新增的快捷键。

---

### 如何自定义状态栏显示

状态栏通过 `ui.showStatus(text)` 更新。有两种模式：

#### 1. 临时提示（如发送失败）

```python
try:
    await bot.send_message(...)
except Exception as e:
    ui.showStatus(f"发送失败: {e}")
    await asyncio.sleep(2)  # 停留 2 秒
    _restoreDefaultStatus()  # 恢复默认
```

#### 2. 持久状态（如审核队列提示）

在 `receiverLoop()` 的超时分支中定期更新：

```python
except asyncio.TimeoutError:
    rq = getReviewQueue()
    if rq.qsize() > 0:
        hint = peekReviewHint() or ""
        ui.showStatus(f" 待审核: ({rq.qsize()} 条) | ... | {hint} ")
    continue
```

---

### 如何添加新的命令前缀（类似 `:ra`）

假设要添加 `:help` 命令显示帮助——

#### 1. 在 chatScreen 主循环中检测

```python
# 在 "if stripped in (":ra", ":re", ...)" 之前添加
if stripped == ":help":
    ui.appendLines([
        "",
        "=== 可用命令 ===",
        ":ra  - 通过审核",
        ":re  - 编辑回复",
        "...",
    ])
    continue
```

#### 2. 更新文档

在"LLM 审核集成 > 审核命令"章节补充新命令。

---

## 故障排查

下面是几个常见问题和排查思路——

### 问题 1：发送消息时提示 "Forbidden"

**原因**：对方用户还未与 bot 开始对话，bot 无法主动发消息。

**解决**：
1. 让对方先给 bot 发一条消息（如 `/start`）
- 如果这个时候还不行的话，就要检查对方是否在白名单上了
2. 或者在群组中，bot 已加入且有发言权限

---

### 问题 2：消息丢失，UI 没有显示

**排查步骤**：
1. 检查 receiverLoop 是否正常运行：在 `receiverLoop()` 中添加日志
2. 确认 `targetChatID` 正确：打印 `msg.chat.id` 和 `targetChatID` 对比
3. 检查消息队列：在外层 CLI 输入 `/tasks` 查看后台任务状态

**常见原因**：
- `targetChatID` 格式不匹配（数字 vs 字符串）
- receiverTask 被意外取消
- messageQueue 被其他 handler 消费

---

### 问题 3：UI 卡死，按键无响应

这通常是 `prompt_toolkit` 事件循环被阻塞导致的。

**排查**：
1. 检查是否在 UI 回调中执行了同步阻塞操作（如 `time.sleep()`）
2. 检查 `shutdownEvent` 是否被误设置
3. 尝试 `Ctrl+C` 强制退出，查看堆栈信息

**预防**：
- UI 回调中只做状态更新，不做耗时操作
- 耗时操作放到 `asyncio.create_task()` 中

---

### 问题 4：退出后外层 CLI 卡死

这一般是因为 `interactiveMode` 没有正确恢复为 `False`。

**排查**：
1. 检查 chatScreen 的 finally 块是否执行
2. 确认没有在 finally 之前 `return` 或抛出未捕获异常

**修复**：
```python
try:
    # chatScreen 主循环
    ...
finally:
    state.setInteractiveMode(False)  # 必须在 finally 中
```

---

## 相关文件

| 文件 | 职责 |
|------|------|
| `utils/chatScreen/session.py` | chatScreen 主函数与生命周期管理 |
| `utils/chatScreen/receiver.py` | 消息接收后台协程 |
| `utils/chatScreen/mainLoop.py` | 主循环输入处理，切换信号检测 |
| `utils/chatScreen/formatter.py` | 消息格式化工具 |
| `utils/chatScreen/history.py` | 历史加载与显示 |
| `utils/chatScreen/statusBar.py` | 状态栏文本常量 |
| `utils/chatScreen/helpers.py` | 辅助函数 (getNextChatID 导航逻辑) |
| `utils/chatScreen/ui.py` | ChatScreenApp UI 控制层、键绑定、滚动逻辑 |
| `utils/command/send.py` | 命令入口、普通消息发送、目标选择、切换循环 |
| `utils/whitelistManager/data.py` | getAllowedUserIDs 提供切换列表 |
| `utils/chatHistory.py` | 聊天记录加密存储与读取 |
| `utils/core/stateManager.py` | 全局状态管理(interactiveMode / messageQueue) |
| `utils/command/llm.py` | LLM 审核命令处理(`handleChatScreenReviewCommand` 等) |
| `utils/llm/state.py` | 审核队列管理(`getReviewQueue` / `peekReviewHint`) |

---

## 设计决策记录

这里记载几个「为什么是现在这样」的设计决定——

### 为什么 receiverLoop 是后台协程而非同步轮询？

**问题**：chatScreen 需要同时处理用户输入和接收 Telegram 消息。

**方案对比**：
- 方案 A：主循环中 `queue.get_nowait()` 轮询，会阻塞 UI 响应
- 方案 B：receiverLoop 后台协程 + `wait_for(timeout=0.5)`，UI 和接收解耦

权衡后，我们选择 B，这是因为：
- UI 事件循环（`await ui.run()`）可以立即响应用户输入
- receiverLoop 独立运行，不干扰 UI
- 超时机制确保定期检查状态（审核队列/关机信号）

---

### 为什么审核编辑模式下 `Esc` 不退出聊天界面？

在审核编辑模式下按 `Esc`，可能的场景是用户按 `:re` 进入编辑模式，修改到一半想取消。

这个时候，期望的行为是：第一次按 `Esc` 取消编辑，恢复输入框，放回审核队列；而第二次 `Esc` 退出聊天界面

**实现**：
```python
if userInput is None:
    if _reviewEditItem is not None:
        # 编辑模式中 Esc → 取消编辑，不退出
        getReviewQueue().put_nowait(_reviewEditItem)
        _reviewEditItem = None
        ui.resetExitFlag()  # 重置退出标记
        ui.clearComposer()
        continue
    break  # 非编辑模式 → 退出
```

---

## 性能考量

### 历史记录加载

- 启动时调用 `loadHistory(targetChatID)` 一次性加载全部历史
- 历史存储在 `_allLines: list[str]` 中（内存）
- 长聊天记录可能占用较多内存，但通常不超过几 MB

**优化方向**（未实现）：
- 分页加载：只加载最近 N 条，向上滚动时动态加载更多
- 虚拟滚动：只渲染可见区域的行

---

### 消息队列轮询

- `wait_for(timeout=0.5)` 每 0.5 秒超时一次
- 超时时检查审核队列（O(1)）和关机信号（O(1)）
- CPU 占用极低（大部分时间在 await）

---

## 未来改进方向

现在的聊天界面已经够用了，但还有一些想做、暂时没做的功能——

1. **热切换聊天**：不退出 chatScreen，动态更新 targetChatID 和历史
2. **草稿持久化**：切换聊天时保存输入框内容到临时文件
3. **消息搜索**：按 `/` 进入搜索模式，高亮匹配的历史消息
4. **快速跳转**：按 `Ctrl+G` 输入行号跳转到历史某处
5. **多媒体支持**：显示图片/文件的缩略图或下载链接
6. **分屏模式**：左侧聊天列表，右侧当前聊天（类似 Telegram 桌面版）

---

## 参考资源

- [prompt_toolkit 官方文档](https://python-prompt-toolkit.readthedocs.io/)
- [python-telegram-bot 官方文档](https://docs.python-telegram-bot.org/)
- 项目文档：
  - [LLM Handler 架构](llm-handler.md)
  - [LLM 记忆系统](llm-memory.md)