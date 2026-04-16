"""
operators.py

Operators 权限管理工具

提供 operators.json 的读取、权限检查和按权限查询功能，
用于 bot 管理命令的权限控制和事件通知。
通过 fileCache 缓存，避免每次权限检查都读磁盘。
"""




from config import Permission

from utils.core.fileCache import getOperatorsCache




def loadOperators():
    """加载 operators 数据（通过 fileCache 缓存）"""
    data = getOperatorsCache().get()
    return data.get("operators" , {})




def isOperator(userID: int) -> bool:
    """检查用户是否是 operator """
    operators = loadOperators()
    return str(userID) in operators




def hasPermission(userID: int , permission: Permission) -> bool:
    """检查用户是否有对应权限"""
    operators = loadOperators()
    userIDStr = str(userID)

    if userIDStr not in operators:
        return False

    permissions = operators[userIDStr].get("permissions" , [])
    return str(permission) in permissions




def getOperatorsWithPermission(permission: Permission) -> list[str]:
    """获取拥有指定权限的所有 operator 的 userID 列表"""
    operators = loadOperators()
    permStr = str(permission)
    return [
        uid for uid, info in operators.items()
        if permStr in info.get("permissions", [])
    ]
