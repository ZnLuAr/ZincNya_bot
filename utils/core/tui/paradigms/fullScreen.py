"""
utils/core/tui/paradigms/fullScreen.py

FullScreenTUIApp —— 全屏接管型 TUI 范式。

屏幕模型：prompt_toolkit Application 以 full_screen=True 接管终端（pt 自管备用屏幕，
不手动 smcup/rmcup）。
输入模型：持续输入 + pt Layout 即时渲染（聊天界面、文本编辑器都是这种）。
终端尺寸口径：prompt_toolkit 的 output.get_size()（pt 布局已分配的高度），不是 ANSI 口径。

构造时机契约：pt Application 在 __init__ 构造（对象构造期），不在 onEnter。会话激活（注册
回调 / 起 receiver）才归 onEnter。这样两个 fullScreen 子类（TextEditor / ChatScreen）构造
时机统一，且 ChatScreenApp 覆写 onEnter 做自己的激活即可，不必"不调 super"。

mainLoop 不在本类实现——保持 TUISession 的抽象下放，每个 fullScreen 子类各自定义交互主循环
（TextEditor 一轮即退、ChatScreen 跑完整聊天循环）。故 FullScreenTUIApp 不可直接实例化。

createApplication 非 abstract（具体方法，子类可覆写）。本模块不依赖任何具体子类（不 import
chatScreen 等），防循环 import。
"""

from abc import abstractmethod

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout

from utils.core.tui.session import TUISession




class FullScreenTUIApp(TUISession):
    """全屏接管范式：prompt_toolkit Application 生命周期模板。

    子类实现 buildLayout（布局根容器）+ setupKeyBindings（键绑定）+ mainLoop（交互主循环），
    按需覆写 createApplication（如需自定义 pt Application 装配）/ onEnter（会话激活）。
    """

    def __init__(self):
        super().__init__()  # TUISession：_stateManager + _backgroundTasks
        # app 在构造期建立（构造时机契约）；createApplication 可被子类覆写
        self._ptApp = self.createApplication()


    # ========================================================================
    # 抽象方法（子类必填）
    # ========================================================================

    @abstractmethod
    def buildLayout(self):
        """构建布局根容器（TextArea / HSplit 等），由 createApplication 包成 Layout。"""
        pass


    @abstractmethod
    def setupKeyBindings(self, kb: KeyBindings):
        """注册键绑定到给定的 KeyBindings 实例。"""
        pass


    # ========================================================================
    # 钩子（子类按需覆写）
    # ========================================================================

    def getExtraApplicationKwargs(self) -> dict:
        """额外传给 Application 的构造参数（默认空）。"""
        return {}


    def createApplication(self) -> Application:
        """构造 pt Application（默认模板；子类可覆写以自定义装配，如改 output/patch 目标）。

        非 @abstractmethod——钉死：FullScreenTUIApp 的子类用此默认模板即可实例化 pt app，
        需要自定义的子类（如 chatScreen 为保 patch 目标）在自己的模块覆写。
        """
        kb = KeyBindings()
        self.setupKeyBindings(kb)
        kwargs = {
            "layout": Layout(self.buildLayout()),
            "key_bindings": kb,
            "full_screen": True,
        }
        kwargs.update(self.getExtraApplicationKwargs())
        return Application(**kwargs)


    async def onEnter(self):
        """纯激活钩子（默认空实现；chatScreen 覆写做 callback 注册 + receiver 启动 + 初次刷新）。"""
        pass


    # ========================================================================
    # 单轮入口
    # ========================================================================

    async def runOnce(self):
        """跑一轮 pt 事件循环（chatScreen 每轮被聊天主循环 while 调用；TextEditor 一轮即退）。

        覆写时须保留返回值语义——chatScreen 的 runOnce 覆写额外保留 _exitRequested 守卫
        （exit 请求时返回 None），不能照搬此处的直接 return result。
        """
        result = await self._ptApp.run_async()
        self._ptApp.output.reset_attributes()
        self._ptApp.output.flush()
        return result
