"""
utils/moduleManager.py

模块配置管理工具。

提供模块配置的加载、保存、查询功能。
"""

import os
import json

from config import PROJECT_ROOT


# 配置文件路径
_MODULES_CONFIG_PATH = os.path.join(PROJECT_ROOT, "data", "modules.json")
_CUSTOM_MODULES_PATH = os.path.join(PROJECT_ROOT, "data", "modulesCustom.json")
_UNINSTALLED_BUILTINS_PATH = os.path.join(PROJECT_ROOT, "data", "modulesUninstalled.json")


def loadModulesConfig():
    """
    加载模块启用/禁用配置

    返回格式：
    {
        "llm": {"enabled": true},
        "todos": {"enabled": false}
    }

    如果文件不存在，返回空字典（所有模块默认启用）
    """
    if not os.path.exists(_MODULES_CONFIG_PATH):
        return {}

    try:
        with open(_MODULES_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def saveModulesConfig(config):
    """
    保存模块配置（原子写，使用 tmpfile + os.replace）

    参数：
        config: 模块配置字典

    返回：
        bool: 是否保存成功
    """
    dataDir = os.path.dirname(_MODULES_CONFIG_PATH)
    os.makedirs(dataDir, exist_ok=True)

    tmpPath = _MODULES_CONFIG_PATH + ".tmp"

    try:
        with open(tmpPath, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        os.replace(tmpPath, _MODULES_CONFIG_PATH)
        return True
    except Exception:
        if os.path.exists(tmpPath):
            try:
                os.remove(tmpPath)
            except Exception:
                pass
        return False


def loadCustomModules():
    """
    读取用户安装的自定义模块

    返回格式：
    {
        "weather": {
            "enabled": true,
            "metadata": {...},
            "source": "github:https://github.com/user/weather-module@main",
            "installedAt": "2026-05-24T12:00:00Z"
        }
    }
    """
    if not os.path.exists(_CUSTOM_MODULES_PATH):
        return {}

    try:
        with open(_CUSTOM_MODULES_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def saveCustomModules(config):
    """
    保存自定义模块配置（原子写）

    参数：
        config: 自定义模块配置字典

    返回：
        bool: 是否保存成功
    """
    dataDir = os.path.dirname(_CUSTOM_MODULES_PATH)
    os.makedirs(dataDir, exist_ok=True)

    tmpPath = _CUSTOM_MODULES_PATH + ".tmp"

    try:
        with open(tmpPath, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        os.replace(tmpPath, _CUSTOM_MODULES_PATH)
        return True
    except Exception:
        if os.path.exists(tmpPath):
            try:
                os.remove(tmpPath)
            except Exception:
                pass
        return False


def loadUninstalledBuiltins():
    """
    读取被强制卸载的内置模块 id 列表

    返回：
        list[str]: 已卸载的内置模块 id 列表
    """
    if not os.path.exists(_UNINSTALLED_BUILTINS_PATH):
        return []

    try:
        with open(_UNINSTALLED_BUILTINS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except Exception:
        return []


def saveUninstalledBuiltins(uninstalledList):
    """
    保存被强制卸载的内置模块 id 列表（原子写）

    参数：
        uninstalledList: 已卸载的内置模块 id 列表

    返回：
        bool: 是否保存成功
    """
    dataDir = os.path.dirname(_UNINSTALLED_BUILTINS_PATH)
    os.makedirs(dataDir, exist_ok=True)

    tmpPath = _UNINSTALLED_BUILTINS_PATH + ".tmp"

    try:
        with open(tmpPath, 'w', encoding='utf-8') as f:
            json.dump(uninstalledList, f, ensure_ascii=False, indent=2)

        os.replace(tmpPath, _UNINSTALLED_BUILTINS_PATH)
        return True
    except Exception:
        if os.path.exists(tmpPath):
            try:
                os.remove(tmpPath)
            except Exception:
                pass
        return False


def isModuleEnabled(moduleName):
    """
    检查模块是否启用

    参数：
        moduleName: 模块名称

    返回：
        bool: 是否启用（默认为 True）
    """
    allModules = getAllModules()
    if moduleName not in allModules:
        return False
    return allModules[moduleName].get("enabled", True)


def getAllModules():
    """
    获取所有模块（内置 + 自定义），合并后返回

    返回格式：
    {
        "llm": {
            "enabled": true,
            "metadata": {...},
            "source": "builtin",
            "installedAt": None
        }
    }

    注意：自动去重 initFunctions 和 backgroundTasks，避免多次初始化同一函数
    """
    from modulesRegistry import MODULES

    # 加载配置
    modulesConfig = loadModulesConfig()
    customModules = loadCustomModules()
    uninstalledBuiltins = set(loadUninstalledBuiltins())

    result = {}

    # 1. 加载内置模块（跳过已被强制卸载的）
    for moduleId, metadata in MODULES.items():
        if moduleId in uninstalledBuiltins:
            continue
        enabled = modulesConfig.get(moduleId, {}).get("enabled", True)
        result[moduleId] = {
            "enabled": enabled,
            "metadata": metadata,
            "source": "builtin",
            "installedAt": None
        }

    # 2. 加载自定义模块（可以覆盖内置模块）
    for moduleId, customData in customModules.items():
        result[moduleId] = customData

    # 3. 去重 initFunctions 和 backgroundTasks
    # 收集所有已启用模块的函数
    allInitFunctions = set()
    allBackgroundTasks = set()

    for moduleId, config in result.items():
        if not config.get("enabled", True):
            continue

        metadata = config["metadata"]
        for func in metadata.get("initFunctions", []):
            allInitFunctions.add(func)
        for task in metadata.get("backgroundTasks", []):
            allBackgroundTasks.add(task)

    # 注意：这里只是收集，实际去重在 appLifecycle.py 中进行
    # 因为需要在运行时动态判断哪些函数已经被调用过

    return result
