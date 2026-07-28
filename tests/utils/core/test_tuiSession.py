"""
tests/utils/core/test_tuiSession.py

测试 utils/core/tui/session.py 的 TUISession 基类。

被测契约（runSession 的 finally 钉死顺序）：
    onExit → 恢复 interactiveMode（恢复进入前旧值，非硬置 False）→ cancel + await 后台任务

不碰真实单例：patch utils.core.tui.session.getStateManager 返回 FakeStateManager。
被测源码（utils/core/tui/session.py）绝不修改——失败改测试，不改源码。
"""

import asyncio

import pytest
from unittest.mock import patch

from utils.core.tui.session import TUISession




# ============================================================================
# Fakes
# ============================================================================

class FakeStateManager:
    """可控的状态管理器替身。

    - isInteractive()/setInteractiveMode(v) 操作一个内部 flag。
    - setInteractiveMode 调用序列全量记录（prev 值），用于嵌套恢复与收尾顺序断言。
    - getShutdownEvent()/setConsoleOutputCallback() 是占位（session 不调用，但实例化安全）。
    """

    def __init__(self, initialInteractive: bool = False):
        self._interactiveMode = initialInteractive
        # 记录每次 setInteractiveMode 传入的值（按顺序）
        self.setCalls: list[bool] = []


    def isInteractive(self) -> bool:
        return self._interactiveMode


    def setInteractiveMode(self, value: bool):
        self.setCalls.append(value)
        self._interactiveMode = value


    def getShutdownEvent(self) -> asyncio.Event:
        return asyncio.Event()


    def setConsoleOutputCallback(self, callback):
        pass  # 占位：session 基类不调用，但子类范式可能调，这里给个安全实现




class FakeSession(TUISession):
    """可控的 TUISession 子类。

    通过初始化参数决定 onEnter/mainLoop/onExit 行为；多个可观测点（调用计数、
    录制值、recorder 序列、后台任务登记）供各用例断言。

    不在 __init__ 里接收 stateManager——走真实 super().__init__() 路径，由外部
    patch('utils.core.tui.session.getStateManager', return_value=fakeSm) 注入，
    从而既测到真实 __init__ 又不污染全局单例。
    """

    def __init__(
        self,
        mainLoopResult=None,
        onEnterRaises: Exception | None = None,
        mainLoopRaises: Exception | None = None,
        onExitRaises: Exception | None = None,
        recorder: list[str] | None = None,
        flagSnapshot: list[bool] | None = None,
    ):
        super().__init__()  # 真实路径：内部 getStateManager()（已被 patch 成 fake）

        # 行为开关
        self._mainLoopResult = mainLoopResult
        self._onEnterRaises = onEnterRaises
        self._mainLoopRaises = mainLoopRaises
        self._onExitRaises = onExitRaises

        # 观测点
        self.recorder = recorder if recorder is not None else []
        self.flagSnapshot = flagSnapshot if flagSnapshot is not None else []
        self.onEnterCallCount = 0
        self.mainLoopCallCount = 0
        self.onExitCallCount = 0


    async def onEnter(self):
        self.onEnterCallCount += 1
        self.flagSnapshot.append(self._stateManager.isInteractive())
        if self._onEnterRaises is not None:
            raise self._onEnterRaises


    async def mainLoop(self):
        self.mainLoopCallCount += 1
        self.flagSnapshot.append(self._stateManager.isInteractive())
        if self._mainLoopRaises is not None:
            raise self._mainLoopRaises
        return self._mainLoopResult


    async def onExit(self):
        self.onExitCallCount += 1
        if self.recorder is not None:
            self.recorder.append('onExit')
        if self._onExitRaises is not None:
            raise self._onExitRaises




# ============================================================================
# Fixture + 构造助手
# ============================================================================

@pytest.fixture()
def fakeStateManager():
    return FakeStateManager(initialInteractive=False)


def makeSession(fakeSm: FakeStateManager, **kwargs) -> FakeSession:
    """构造 FakeSession，期间 patch getStateManager 返回 fakeSm（不污染真实单例）。

    __init__ 内 super().__init__() 会调 getStateManager()——此刻 patch 必须生效。
    构造完成后 session 已持有 _stateManager 引用，patch 即可退出。
    """
    with patch('utils.core.tui.session.getStateManager', return_value=fakeSm):
        return FakeSession(**kwargs)




# ============================================================================
# 1. flag 保存/恢复：runSession 期间 flag=True，退出后恢复进入前旧值（False）
# ============================================================================

@pytest.mark.asyncio
async def test_runSession_sets_and_restores_flag(fakeStateManager):
    """进入后 flag=True（mainLoop 内能观测到），退出后恢复进入前旧值 False。"""
    session = makeSession(fakeStateManager, mainLoopResult='ok')

    assert fakeStateManager.isInteractive() is False  # 进入前

    ret = await session.runSession()

    assert ret == 'ok'
    # onEnter + mainLoop 内都观测到 flag=True（进入后、退出前）
    assert session.flagSnapshot == [True, True]
    # 退出后恢复到进入前旧值
    assert fakeStateManager.isInteractive() is False
    # setInteractiveMode 序列：True（进入）→ False（恢复）
    assert fakeStateManager.setCalls == [True, False]




# ============================================================================
# 2. 嵌套恢复：外层 flag 预置 True，内层退出后 flag 仍 True（恢复 prev 而非硬置 False）
# ============================================================================

@pytest.mark.asyncio
async def test_nested_restore_recovers_prev_not_hard_false(fakeStateManager):
    """外层预置 True → 内层 prev=True → 内层退出后 flag 应恢复为 True（prev），而非 False。"""
    fakeStateManager.setInteractiveMode(True)  # 外层预置
    fakeStateManager.setCalls.clear()  # 清掉预置记录，只看内层行为

    session = makeSession(fakeStateManager, mainLoopResult='done')

    assert fakeStateManager.isInteractive() is True  # prev

    await session.runSession()

    # 关键：恢复值 == prev（True），不是硬置 False
    assert fakeStateManager.isInteractive() is True
    assert fakeStateManager.setCalls == [True, True]  # 进入(True) → 恢复(prev=True)

    # 反向断言：硬置 False 会让这里失败——证明是 prev 恢复而非恒 False
    assert fakeStateManager.isInteractive() != False or fakeStateManager.setCalls[-1] is True
    # 等价但更直接：最后一次 setInteractiveMode 传入的就是 True
    assert fakeStateManager.setCalls[-1] is True




# ============================================================================
# 3. 双层嵌套端到端：每层退出后 flag 回到该层进入前的值
# ============================================================================

@pytest.mark.asyncio
async def test_double_nested_end_to_end(fakeStateManager):
    """outer(prev=False) → outer.mainLoop 内启 inner(prev=True) → inner 退出(flag=True)
    → outer 继续 → outer 退出(flag=False)。每层退出都恢复该层进入前的值。

    整个测试体活在同一个 patch 下——outer 与 inner（在 mainLoop 内构造）的 __init__
    都能拿到同一个 fakeSm。
    """
    innerResult = {}

    class OuterSession(FakeSession):
        async def mainLoop(self):
            self.flagSnapshot.append(self._stateManager.isInteractive())  # outer 内 flag=True
            # 启动内层：此刻 outer 已把 flag 置 True，所以 inner 的 prev=True
            inner = FakeSession(mainLoopResult='inner-ok')
            ret = await inner.runSession()
            innerResult['ret'] = ret
            innerResult['flagAfterInnerExit'] = fakeStateManager.isInteractive()
            self.flagSnapshot.append(self._stateManager.isInteractive())  # inner 退出后 outer 仍 True
            return 'outer-ok'


    with patch('utils.core.tui.session.getStateManager', return_value=fakeStateManager):
        outer = OuterSession()
        assert fakeStateManager.isInteractive() is False  # outer prev=False
        ret = await outer.runSession()

    assert ret == 'outer-ok'
    # inner 在 outer mainLoop 内启动，prev=True（outer 已把 flag 置 True）
    assert innerResult['ret'] == 'inner-ok'
    # inner 退出后 flag 恢复到 inner 的 prev=True
    assert innerResult['flagAfterInnerExit'] is True
    # outer 全部退出后 flag 恢复到 outer 的 prev=False
    assert fakeStateManager.isInteractive() is False
    # outer onEnter(1) + mainLoop 内两次快照 = 三次 True
    assert outer.flagSnapshot == [True, True, True]




# ============================================================================
# 4. 后台任务 cancel+await：退出后 task.done() 且被 cancelled
# ============================================================================

@pytest.mark.asyncio
async def test_background_task_cancelled_and_awaited(fakeStateManager):
    """登记一个 await event.wait() 的后台任务，runSession 退出后 task done 且 cancelled。"""
    session = makeSession(fakeStateManager, mainLoopResult='ok')

    started = asyncio.Event()

    async def bgJob():
        started.set()
        try:
            await asyncio.Event().wait()  # 永不自然完成，只能被 cancel
        except asyncio.CancelledError:
            raise  # 让 task 进入 cancelled 状态


    async def runAndRegister():
        task = asyncio.create_task(bgJob())
        session.addBackgroundTask(task)
        await started.wait()  # 确保 bgJob 已进入 await
        return task


    task = await runAndRegister()
    assert not task.done()

    await session.runSession()

    assert task.done()
    assert task.cancelled()




# ============================================================================
# 5. mainLoop 抛异常 → 收尾仍执行（finally 语义）
# ============================================================================

@pytest.mark.asyncio
async def test_mainLoop_exception_still_tears_down(fakeStateManager):
    """mainLoop raise → runSession 传播异常，但 onExit 仍被调、flag 已恢复、后台 task 被 cancel。"""
    session = makeSession(
        fakeStateManager,
        mainLoopRaises=RuntimeError('boom in mainLoop'),
    )

    started = asyncio.Event()

    async def bgJob():
        started.set()
        await asyncio.Event().wait()


    task = asyncio.create_task(bgJob())
    session.addBackgroundTask(task)
    await started.wait()

    with pytest.raises(RuntimeError, match='boom in mainLoop'):
        await session.runSession()

    # finally 收尾仍执行
    assert session.onExitCallCount == 1
    assert fakeStateManager.isInteractive() is False  # 恢复到 prev=False
    assert task.done()
    assert task.cancelled()




# ============================================================================
# 6. onEnter 抛异常 → 收尾仍执行；mainLoop 不被调
# ============================================================================

@pytest.mark.asyncio
async def test_onEnter_exception_skips_mainLoop_and_tears_down(fakeStateManager):
    """onEnter raise → runSession 传播异常，mainLoop 不被调（计数 0），onExit 仍被调，flag 恢复。"""
    session = makeSession(
        fakeStateManager,
        onEnterRaises=RuntimeError('boom in onEnter'),
    )

    with pytest.raises(RuntimeError, match='boom in onEnter'):
        await session.runSession()

    assert session.onEnterCallCount == 1
    assert session.mainLoopCallCount == 0  # mainLoop 没被调
    assert session.onExitCallCount == 1  # finally 仍调 onExit
    assert fakeStateManager.isInteractive() is False  # 恢复到 prev=False




# ============================================================================
# 7. 收尾顺序契约：recorder == ['onExit','flag','cancel','await']
# ============================================================================

class RecordingStateManager(FakeStateManager):
    """在 setInteractiveMode 里 append 'flag' 到 recorder，参与收尾顺序断言。"""

    def __init__(self, recorder: list[str]):
        super().__init__(initialInteractive=False)
        self._orderRecorder = recorder


    def setInteractiveMode(self, value: bool):
        # 先记录再设值——'flag' 标记的是"恢复 flag"这一步发生
        self._orderRecorder.append('flag')
        super().setInteractiveMode(value)




@pytest.mark.asyncio
async def test_teardown_order_onExit_flag_cancel_await():
    """收尾顺序：onExit → 恢复 flag → cancel → await。

    观测手段：
    - onExit append 'onExit'（FakeSession.recorder）。
    - RecordingStateManager.setInteractiveMode append 'flag'。
    - 后台 task 的协程在收到 CancelledError 时 append 'cancel'。
    - 用一个 wrapper task 包裹真正干活的内层 task：wrapper 先 await inner，
      内层被 cancel 抛 CancelledError，wrapper 的 finally append 'await' 再传播。
      session 在 finally 里 cancel wrapper → wrapper 进入 finally（'await'）→
      inner 的 except（'cancel'）已在内层 cancel 时先跑。

    注：session 对 wrapper 调 task.cancel()，wrapper 与 inner 都会收到 CancelledError；
    asyncio 保证先 cancel 外层 task，外层 await inner 时立即抛 CancelledError，
    而 inner 的 CancelledError 在其协程被调度取消时触发。为得到确定的
    cancel→await 顺序，wrapper 在 except CancelledError 里 append 'await'（即
    session 的 `await wrapper` 消费掉 CancelledError 这一刻），这天然晚于 inner
    自己的 'cancel'。
    """
    recorder: list[str] = []
    recordingSm = RecordingStateManager(recorder)

    async def innerJob():
        try:
            await asyncio.Event().wait()  # 永不自然完成
        except asyncio.CancelledError:
            recorder.append('cancel')
            raise


    async def trackedWrapper():
        # 包一层：session cancel 的是 wrapper。wrapper await innerJob；被 cancel 时
        # inner 先收到 CancelledError（append 'cancel'），wrapper 的 except 随后
        # append 'await'——对应 session `await task` 消费 CancelledError 的那一刻。
        try:
            await innerJob()
        except asyncio.CancelledError:
            recorder.append('await')
            raise


    session = makeSession(recordingSm, mainLoopResult='ok', recorder=recorder)

    task = asyncio.create_task(trackedWrapper())
    session.addBackgroundTask(task)
    await asyncio.sleep(0)  # 让 task 进入 await

    await session.runSession()

    # 完整序列：进入 flag → 收尾(onExit → 恢复 flag → cancel → await)
    assert recorder == ['flag', 'onExit', 'flag', 'cancel', 'await']
    # 收尾阶段的钉死顺序（finally）：onExit → flag → cancel → await
    assert recorder[-4:] == ['onExit', 'flag', 'cancel', 'await']




# ============================================================================
# 8. CancelledError 被吞：会抛 CancelledError 的 task，runSession 不传播
# ============================================================================

@pytest.mark.asyncio
async def test_cancelled_error_swallowed(fakeStateManager):
    """登记一个会被 cancel 的 task（其 await 抛 CancelledError），runSession 正常返回不传播。"""
    session = makeSession(fakeStateManager, mainLoopResult='ok')

    async def bgJob():
        await asyncio.Event().wait()  # 被 cancel → CancelledError


    task = asyncio.create_task(bgJob())
    session.addBackgroundTask(task)
    await asyncio.sleep(0)  # 让 task 进入 await

    # 不应抛 CancelledError
    ret = await session.runSession()

    assert ret == 'ok'
    assert task.done()




# ============================================================================
# 9. onExit 抛异常不阻断收尾：runSession 不传播 onExit 的异常
# ============================================================================

@pytest.mark.asyncio
async def test_onExit_exception_does_not_break_teardown(fakeStateManager):
    """onExit raise → session 内部吞掉（logSystemEvent DEBUG），flag 仍恢复、
    后台 task 仍 cancel、runSession 正常返回（不 raise）。
    """
    session = makeSession(
        fakeStateManager,
        mainLoopResult='ok',
        onExitRaises=RuntimeError('boom in onExit'),
    )

    started = asyncio.Event()

    async def bgJob():
        started.set()
        await asyncio.Event().wait()


    task = asyncio.create_task(bgJob())
    session.addBackgroundTask(task)
    await started.wait()

    # runSession 正常返回，不传播 onExit 异常
    ret = await session.runSession()

    assert ret == 'ok'
    assert session.onExitCallCount == 1  # onExit 确实被调且抛了
    assert fakeStateManager.isInteractive() is False  # flag 仍恢复
    assert task.done()  # 后台 task 仍被 cancel+await
    assert task.cancelled()

# ============================================================================
# 控制台回调单槽踩踏洞（已知局限，xfail 文档化）
# ============================================================================

@pytest.mark.xfail(
    reason="已知局限：stateManager 的 consoleOutputCallback 是全局单槽（非 per-key 字典），"
           "嵌套会话的内层 onExit 注销（置 None）会把外层注册的回调一并清掉。"
           "目前无嵌套 chatScreen 场景故不修；真有嵌套路由需求时改成引用计数。"
           "详见 docs/tui.md「已知局限」。",
    strict=True,
)
def test_console_callback_single_slot_nested_clobber():
    """嵌套会话的 console 回调互相踩踏——预期失败，钉住这个洞。

    模拟：外层注册 handlerA → 内层注册 handlerB → 内层 onExit 注销（置 None）
    → 断言外层 handlerA 仍在（当前实现下必然失败，因为是单槽）。
    """
    from utils.core.stateManager import getStateManager

    state = getStateManager()
    original = state.getConsoleOutputCallback()

    try:
        def handlerA(text):
            pass

        def handlerB(text):
            pass

        # 外层会话注册自己的回调
        state.setConsoleOutputCallback(handlerA)
        # 内层会话注册（覆盖了外层——单槽的第一个后果）
        state.setConsoleOutputCallback(handlerB)
        # 内层退出时注销（置 None——单槽的第二个后果：外层的也没了）
        state.setConsoleOutputCallback(None)

        # 期望：外层的 handlerA 应当还在（引用计数实现下成立）
        # 实际：单槽实现下已被清空 → 本断言失败 → xfail
        assert state.getConsoleOutputCallback() is handlerA

    finally:
        state.setConsoleOutputCallback(original)
