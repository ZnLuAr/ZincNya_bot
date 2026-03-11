"""
operators.py

Operators 权限管理工具

提供 operators.json 的读取和权限检查功能，用于 bot 管理命令的权限控制。
"""

import json
import os
from config import OPERATORS_DIR , Permission


def loadOperators():
    """加载 operators.json 文件"""
    if not os.path.exists(OPERATORS_DIR):
        return {}

    try:
        with open(OPERATORS_DIR , "r" , encoding="utf-8") as f:
            data = json.load(f)
            return data.get("operators" , {})
    except Exception as e:
        print(f"加载 operators.json 失败：{e}")
        return {}


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
