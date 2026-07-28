"""
utils/core/tui/session.py

TUISession —— 交互式 TUI 会话契约基类：横切关注点（交互模式 flag、后台任务收尾、
屏幕生命周期钩子）的唯一所有者。

对"交互方式"零假设——不预设渲染模型、不预设输入模型。任何范式（列表/全屏/编辑器/
未来未知类型）继承它，只实现 mainLoop（交互主循环）+ 按需覆写 onEnter/onExit 钩子。
终端尺寸这类"两套世界"（ANSI 口径 vs prompt_toolkit 口径）由各范式在自己模块里声明
取数方式，不进基类——它属于交互方式，归范式管。

收尾顺序（runSession 的 finally，钉死）：
    onExit（范式专属收尾） → 恢复 interactiveMode（恢复进入前旧值） → cancel + await 后台任务

后台任务异常自洽在任务内（任务自身的 except 负责吞非致命异常），session 只管 cancel/await
时吞 CancelledError——否则单个后台任务的非致命异常会打断其它任务的收尾。
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Any, List

from utils.core.logger import LogLevel, logSystemEvent
from utils.core.stateManager import getStateManager




class TUISession(ABC):
    """交互式 TUI 会话基类：横切关注点的唯一所有者

    只管会话生命周期——不碰渲染、不碰输入模型。所有的交互范式都继承它，
    实现 mainLoop（必填）+ 按需覆写 onEnter/onExit 钩子。
    """

    def __init__(self):
        self._stateManager = getStateManager()
        self._backgroundTasks: List[asyncio.Task] = []


    # ========================================================================
    # 钩子（范式按需覆写）
    # ========================================================================

    async def onEnter(self):
        """屏幕接管 / 数据初始化 / 范式专属资源注册（默认空实现）"""
        pass


    @abstractmethod
    async def mainLoop(self) -> Any:
        """主循环（范式各自定义交互）。runSession 的核心，是必填的"""
        pass


    async def onExit(self):
        """屏幕恢复 / 范式专属资源注销（默认空实现）"""
        pass


    def addBackgroundTask(self, task: asyncio.Task):
        """登记后台任务；runSession 退出时统一 cancel + await 收尾"""
        self._backgroundTasks.append(task)


    # ========================================================================
    # 生命周期入口
    # ========================================================================

    async def runSession(self) -> Any:
        """全生命周期入口：onEnter → mainLoop → 收尾

        入口命名 runSession（而非 run）是结构性防静默递归约定——单轮范式（如 chatScreen）
        的每轮入口叫 runOnce，全生命周期入口若也叫 run，漏改某处 ui.run() 会静默走
        runSession → mainLoop → ... → ui.run() 无限递归；改名后漏改直接 AttributeError。
        """
        prevInteractive = self._stateManager.isInteractive()
        self._stateManager.setInteractiveMode(True)

        try:
            await self.onEnter()
            return await self.mainLoop()

        finally:
            # 0. onExit（范式专属收尾）。session 自身 try/except 包住：onExit 抛异常不阻断
            #    后续横切收尾（清理保证由 session 承担，不压给子类作者）
            try:
                await self.onExit()
            except Exception as exc:
                await logSystemEvent(
                    "TUI onExit 异常",
                    f"{type(self).__name__}: {exc}",
                    LogLevel.DEBUG,
                )

            # 1. 恢复 interactiveMode（恢复进入前存的旧值，非硬置 False——嵌套安全）
            #    receiver 式 while-isInteractive 在 flag 落地后自然退出循环
            self._stateManager.setInteractiveMode(prevInteractive)

            # 2. cancel + await 后台任务。cancel 兜底打断 wait_for；await 只吞 CancelledError，
            #    任务内部的非取消异常由任务自身负责（见模块 docstring）
            for task in self._backgroundTasks:
                task.cancel()
            for task in self._backgroundTasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
