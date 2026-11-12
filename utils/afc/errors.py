"""
AFC 框架级异常定义。

工具用抛异常表示失败，不把失败写进正常返回文字：
    - ToolError：调用本身有问题（参数取值不对、算不出来等），消息会交给模型
    - ToolDependencyError：外部依赖不可用，消息同样会交给模型
其他异常由执行器兜住并记为失败，异常内容只进日志，不交给模型。
"""


class AFCError(Exception):
    """AFC 框架基础异常类。"""
    pass


class ToolError(AFCError):
    """
    工具明确失败。

    消息文字会作为失败原因交给模型，要写成模型读了能改正调用的说明，
    不要带内部路径、密钥或堆栈。
    """
    pass


class ToolDependencyError(AFCError):
    """
    外部依赖错误（网络 API、数据库、文件系统等）。

    工具遇到外部依赖不可用时应抛此异常，执行器记为失败并把消息交给模型。
    """
    pass
