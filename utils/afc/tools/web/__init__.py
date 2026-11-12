"""
utils/afc/tools/web

网页读取工具 - 让 LLM 读取一个公开网页或文本文件的文字内容。

主函数（供 AFC 调用）：
    - readURL: 读取一个 http(s) 地址的文字内容

抓取、地址安全校验与正文提取在 fetcher.py。这里只 import 模块而不 import 其中的
函数名：registry 会把本包内定义的公开函数都登记为工具函数。
"""

from utils.afc.errors import ToolError

from . import fetcher
from .config import WEB_FETCH_MAX_CHARS


_TRUNCATED_NOTE = "（内容过长，只保留了开头部分）"


# ==============================================================================
# 主入口（供 AFC registry 扫描）
# ==============================================================================

async def readURL(url: str) -> str:
    """
    读取一个公开网页或文本文件的文字内容。

    参数：
        url: 要读取的完整地址，必须以 http:// 或 https:// 开头

    返回：
        实际读取的地址、标题与正文文字；内容过长时只保留开头部分并标注
    """
    if not isinstance(url, str) or not url.strip():
        raise ToolError("url 不能为空")

    result = await fetcher.fetchURL(url.strip())
    if not result["ok"]:
        raise ToolError(f"读取失败：{result['error'] or '未知错误'}")

    return _formatResult(result)




def _formatResult(result: dict) -> str:
    """把抓取结果排成给模型看的文字：地址、标题、截断标注、正文。"""
    text = result["text"]
    truncated = result["truncatedBytes"]
    if len(text) > WEB_FETCH_MAX_CHARS:
        text = text[:WEB_FETCH_MAX_CHARS]
        truncated = True

    lines = [f"地址：{result['finalUrl'] or result['requestedUrl']}"]
    if result["title"]:
        lines.append(f"标题：{result['title']}")
    if truncated:
        lines.append(_TRUNCATED_NOTE)
    lines.append("")
    lines.append(text)

    return "\n".join(lines)
