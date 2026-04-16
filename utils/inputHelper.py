"""
utils/inputHelper.py

统一的异步输入工具，基于 prompt_toolkit。
替代 aioconsole.ainput()，与 prompt_toolkit.Application 共享终端管理机制。

使用示例：
    from utils.inputHelper import asyncInput

    user_input = await asyncInput(">> ")
"""




import sys
import shutil
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from utils.terminalUI import countDisplayLines


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
    """
    获取或创建全局线程池

    副作用：首次调用时会向 resourceManager 注册 _shutdownExecutor 清理回调。
    这是有意为之的 lazy 初始化 —— executor 仅在实际需要时创建，
    避免在不使用控制台输入的环境中创建无用线程。
    """
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
            import os
            import fcntl
            import termios

            # Unix: 优先向 TTY 输入队列注入一个回车，可靠唤醒阻塞中的 readline()
            # 注意：使用的 TIOCSTI，在一些较新的 Linux 发行版或安全配置下可能被禁用
            fd = sys.stdin.fileno()
            if os.isatty(fd):
                fcntl.ioctl(fd, termios.TIOCSTI, b'\n')
            else:
                sys.stdin.close()
        except Exception:
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




async def asyncMultilineInput(
    prompt: str = ">> ",
    continuation_prompt: str = ".. ",
    erase_after_submit: bool = True
) -> str:
    """
    异步多行输入函数，支持自动重绘提示符

    使用 prompt_toolkit 的 prompt_async() 实现多行输入和提示符重绘。
    适用于需要输入多行文本的场景（如聊天界面）。

    操作方式：
        - 直接按回车                     换行
        - Alt+Enter 或 Esc+Enter        提交输入
        - Ctrl+S                        提交输入（Windows Terminal 下 Alt+Enter 被截走时的替代）
        - Ctrl+C                        取消输入（返回空字符串）

    参数:
        prompt: 主提示符（第一行）
        continuation_prompt: 续行提示符（第二行及以后）
        erase_after_submit: 提交后是否清除输入回显（默认 True）

    返回:
        用户输入的多行字符串（保留换行符）

    注意：
        - 此函数使用 prompt_async()，在某些 Windows 环境下可能阻塞事件循环
        - 建议仅在独立的交互界面（如备用屏幕缓冲区）中使用
        - 自动启用 patch_stdout，确保日志输出时提示符能正确重绘
        - 不支持并发调用（共享全局 PromptSession）
    """
    session = getPromptSession()

    # Ctrl+S 作为额外提交键（Windows Terminal 会拦截 Alt+Enter 用于切换全屏）
    kb = KeyBindings()
    kb.add('c-s')(lambda event: event.current_buffer.validate_and_handle())

    try:
        with patch_stdout():
            result = await session.prompt_async(
                prompt,
                multiline=True,
                key_bindings=kb,
                prompt_continuation=lambda width, line_number, is_soft_wrap: continuation_prompt
            )

        # 清除输入回显。
        # 不依赖 getCursorPosition()（xterm.js / SSH 等终端的 CPR 行号不可靠），
        # 改为根据输入内容 + 终端宽度自行计算实际占用行数。
        if erase_after_submit:
            try:
                termWidth = shutil.get_terminal_size().columns or 80
            except Exception:
                termWidth = 80

            inputLines = result.split('\n') if result else ['']
            inputDisplayRows = sum(
                countDisplayLines(line, prompt if i == 0 else continuation_prompt, termWidth)
                for i, line in enumerate(inputLines)
            )

            # prompt_toolkit 提交后，光标通常会落到输入块下方的新一行。
            # 因此需要清除：
            #   1. 当前这条落点行
            #   2. 整个输入块实际占用的显示行数
            totalRowsToClear = inputDisplayRows + 1

            # 从当前落点行开始向上逐行清除，最后清除输入块首行并回到行首
            for _ in range(totalRowsToClear - 1):
                sys.stdout.write("\033[2K")
                sys.stdout.write("\033[1A")
            sys.stdout.write("\033[2K\r")
            sys.stdout.flush()

        return result

    except KeyboardInterrupt:
        # Ctrl+C 取消输入
        return ""
    except EOFError:
        # Ctrl+D 或 EOF
        return ""
