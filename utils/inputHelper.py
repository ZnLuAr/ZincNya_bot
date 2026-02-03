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

from concurrent.futures import ThreadPoolExecutor
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout




session: PromptSession = None
executor: ThreadPoolExecutor = None




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
    return executor




def syncInput(prompt: str) -> str:
    """同步输入函数，在线程池中执行"""
    # 暂时使用原生 input() 来排除 prompt_toolkit 的问题
    sys.stdout.write(prompt)
    sys.stdout.flush()
    return sys.stdin.readline().rstrip('\n\r')




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
