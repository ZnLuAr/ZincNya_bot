"""
AFC 框架级异常定义。

所有 AFC 工具应使用这些基类或标准库异常，不应直接抛 Exception。
"""


class AFCError(Exception):
    """AFC 框架基础异常类。"""
    pass


class ToolDependencyError(AFCError):
    """
    外部依赖错误（网络 API、数据库、文件系统等）。

    工具遇到外部依赖不可用时应抛此异常，executor 会捕获并返回降级响应。
    """
    pass
