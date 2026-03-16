"""
utils/inputHelper.py

统一的异步输入工具，基于 prompt_toolkit。
替代 aioconsole.ainput()，与 prompt_toolkit.Application 共享终端管理机制。

使用示例：
    from utils.inputHelper import asyncInput

    user_input = await asyncInput("请输入: ")
"""

import sys
import asyncio
import threading

from concurrent.futures import ThreadPoolExecutor
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout




session: PromptSession = None
executor: ThreadPoolExecutor = None

# 关机标志：置为 True 后 syncInput 会立即返回空字符串
_shuttingDown: bool = False
_shuttingDownLock = threading.Lock()




def getPromptSession() -> PromptSession:
    """获取或创建全局 PromptSession 实例"""
    global session
    if session is None:
        session = PromptSession()
    return session




def getExecutor() -> ThreadPoolExecutor:
    """获取或创建全局线程池"""
    global executor
    if executor is None:
        executor = ThreadPoolExecutor(max_workers=1)
        # 注册清理回调
        from utils.core.resourceManager import getResourceManager
        getResourceManager().register(
            "ThreadPoolExecutor (inputHelper)",
            _shutdownExecutor,
            priority=5
        )
    return executor


async def _shutdownExecutor():
    """关闭线程池（由 resourceManager 调用）"""
    global executor
    if executor is not None:
        executor.shutdown(wait=True)




def interruptInput():
    """
    通知 syncInput 停止等待输入。

    在 Windows 控制台下，readline() 无法被 close() 或 cancel() 打断。
    此函数设置关机标志，并向控制台输入缓冲区写入一个回车，
    使 readline() 立即返回，让线程池 Future 得以完成。
    """
    global _shuttingDown
    with _shuttingDownLock:
        _shuttingDown = True

    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes

            class KEY_EVENT_RECORD(ctypes.Structure):
                _fields_ = [
                    ("bKeyDown", ctypes.wintypes.BOOL),
                    ("wRepeatCount", ctypes.wintypes.WORD),
                    ("wVirtualKeyCode", ctypes.wintypes.WORD),
                    ("wVirtualScanCode", ctypes.wintypes.WORD),
                    ("uChar", ctypes.wintypes.WCHAR),
                    ("dwControlKeyState", ctypes.wintypes.DWORD),
                ]

            class _Event(ctypes.Union):
                _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]

            class INPUT_RECORD(ctypes.Structure):
                _fields_ = [
                    ("EventType", ctypes.wintypes.WORD),
                    ("Event", _Event),
                ]

            record = INPUT_RECORD()
            record.EventType = 0x0001  # KEY_EVENT
            record.Event.KeyEvent.bKeyDown = True
            record.Event.KeyEvent.wRepeatCount = 1
            record.Event.KeyEvent.wVirtualKeyCode = 0x0D  # VK_RETURN
            record.Event.KeyEvent.uChar = '\r'

            kernel32 = ctypes.windll.kernel32
            hStdin = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            written = ctypes.wintypes.DWORD(0)
            kernel32.WriteConsoleInputW(
                hStdin, ctypes.byref(record), 1, ctypes.byref(written)
            )
        except Exception:
            pass
    else:
        try:
            sys.stdin.close()
        except Exception:
            pass




def syncInput(prompt: str) -> str:
    """同步输入函数，在线程池中执行"""
    with _shuttingDownLock:
        if _shuttingDown:
            return ""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline().rstrip('\n\r')
    # 关机期间注入的回车，忽略掉
    with _shuttingDownLock:
        if _shuttingDown:
            return ""
    return line




async def asyncInput(prompt: str = "") -> str:
    """
    异步输入函数，替代 aioconsole.ainput()

    在 Windows 上，prompt_toolkit 的 prompt_async() 会阻塞事件循环。
    因此使用 run_in_executor 把同步输入放到线程池中执行，
    让事件循环可以继续调度其他协程。

    参数:
        prompt: 输入提示符

    返回:
        用户输入的字符串
    """
    loop = asyncio.get_event_loop()
    executor = getExecutor()
    return await loop.run_in_executor(executor, syncInput, prompt)
