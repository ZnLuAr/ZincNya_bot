"""
utils/inputHelper.py

统一的异步输入工具，基于 prompt_toolkit。
替代 aioconsole.ainput()，与 prompt_toolkit.Application 共享终端管理机制。

使用示例：
    from utils.inputHelper import asyncInput

    user_input = await asyncInput("请输入: ")
"""

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout


session: PromptSession = None




def getPromptSession() -> PromptSession:
    """获取或创建全局 PromptSession 实例"""
    global session
    if session is None:
        session = PromptSession()
    return session




async def asyncInput(prompt: str = "") -> str:
    """
    异步输入函数，替代 aioconsole.ainput()

    使用 prompt_toolkit 的 PromptSession，与 Application 共享终端管理。
    在 asyncio 事件循环中不会阻塞。

    参数:
        prompt: 输入提示符

    返回:
        用户输入的字符串
    """
    session = getPromptSession()
    with patch_stdout():
        return await session.prompt_async(prompt)
