"""
utils/afc/toolManager.py

AFC 工具状态管理器。

负责 AFC 工具的启用状态与清单（manifest）持久化，镜像 utils/moduleManager.py
的原子写与读取范式，并在此之上提供模块级缓存：扫描循环里逐个工具判断
启用状态时，只在首次读一次盘，之后查内存，避免重复的文件 I/O。

状态文件：
- data/afcTools.json        — 每个工具的 enabled 开关（运行时自动生成，初始为空对象 {}）
- data/afcToolsBuiltin.json — 内置工具（weather/calc/datetime）的 manifest（仓库提供，随代码入库，不忽略）
- data/afcToolsCustom.json  — 第三方工具的 manifest（install 时生成）

其中 afcTools.json / afcToolsCustom.json 已加入 .gitignore，属部署实例私有状态，
不进 git；afcToolsBuiltin.json 是仓库内容的一部分，随代码分发。

关键设计：
- 本模块不 import afcIntent / registry，保持单向依赖，避免循环。
- isAfcToolEnabled() 只读 JSON 状态文件，不 import 任何工具代码。
- getAllAfcTools() 也只读三个 JSON + 扫描目录发现 local-draft，不 import 工具代码。
"""

import os
import json

from config import PROJECT_ROOT


# ── 状态文件路径 ──────────────────────────────────

_AFCTOOLS_CONFIG_PATH = os.path.join(PROJECT_ROOT, "data", "afcTools.json")
_AFCTOOLS_BUILTIN_PATH = os.path.join(PROJECT_ROOT, "data", "afcToolsBuiltin.json")
_AFCTOOLS_CUSTOM_PATH = os.path.join(PROJECT_ROOT, "data", "afcToolsCustom.json")

_TOOLS_DIR_REL = os.path.join("utils", "afc", "tools")


# ── 模块缓存，扫描循环内逐工具查询时只读一次盘 ──

_afcToolsConfigCache = None  # data/afcTools.json 的内存缓存




def loadAfcToolsConfig() -> dict:
    """
    加载 data/afcTools.json，返回 {"weather": {"enabled": true}, ...}

    每次调用都读盘。仅供 CLI 写入前读取当前状态用；
    扫描热路径不直接调它，而是走带缓存的 isAfcToolEnabled()。
    文件不存在或损坏时返回空 dict（降级为"全部默认启用"）。
    """
    if not os.path.exists(_AFCTOOLS_CONFIG_PATH):
        return {}
    try:
        with open(_AFCTOOLS_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def saveAfcToolsConfig(config: dict) -> bool:
    """
    保存 data/afcTools.json（原子写：tmpfile + os.replace），写成功后清空缓存

    参数:
        config: 工具启用状态字典

    返回:
        bool: 是否保存成功
    """
    ok = _atomicWriteJson(_AFCTOOLS_CONFIG_PATH, config)
    if ok:
        invalidateAfcToolsCache()
    return ok


def loadBuiltinAfcTools() -> dict:
    """
    加载 data/afcToolsBuiltin.json（内置工具 manifest）

    返回格式：{"weather": {"id":..., "name":..., "files":[...], "testFile":..., "source":"builtin"}, ...}
    文件不存在或损坏时返回空 dict。
    """
    return _readJsonDict(_AFCTOOLS_BUILTIN_PATH)


def saveBuiltinAfcTools(config: dict) -> bool:
    """保存 data/afcToolsBuiltin.json（原子写）。一般仅初始化/迁移时用"""
    return _atomicWriteJson(_AFCTOOLS_BUILTIN_PATH, config)


def loadCustomAfcTools() -> dict:
    """
    加载 data/afcToolsCustom.json（第三方工具 manifest）

    返回格式：{"timezone": {"enabled":..., "manifest":{...}, "source":"github:...",
              "installedAt":..., "updatedAt":...（可选，update 后写入）,
              "remoteSha":...（可选，update 快车道加速器）}, ...}
    文件不存在或损坏时返回空 dict。
    """
    return _readJsonDict(_AFCTOOLS_CUSTOM_PATH)


def saveCustomAfcTools(config: dict) -> bool:
    """保存 data/afcToolsCustom.json（原子写）"""
    return _atomicWriteJson(_AFCTOOLS_CUSTOM_PATH, config)


def invalidateAfcToolsCache() -> None:
    """
    清空模块级缓存，强制下次 isAfcToolEnabled() 重新读盘

    saveAfcToolsConfig() 内部会自动调用；CLI 命令写入后也应显式调用，
    确保同进程内状态一致。
    """
    global _afcToolsConfigCache
    _afcToolsConfigCache = None




def isAfcToolEnabled(toolName: str) -> bool:
    """
    单一权威来源：检查工具是否启用（默认 True）

    首次调用加载一次 data/afcTools.json 进模块级缓存，后续直接查内存，
    避免扫描循环内对每个工具各读一次盘。

    如果工具名不在状态文件中（首次运行 / 新工具 / 误删条目），默认返回 True。
    这样新增工具时无需手动配置即可立即可用。

    注意：此函数只读 JSON 状态文件，不 import 任何 utils.afc 的其他模块，
    避免循环依赖。缓存失效由 invalidateAfcToolsCache() 负责。

    参数:
        toolName: 工具名（如 "weather" / "calc"）

    返回:
        True 表示启用，False 表示禁用
    """
    global _afcToolsConfigCache
    if _afcToolsConfigCache is None:
        _afcToolsConfigCache = loadAfcToolsConfig()
    return _afcToolsConfigCache.get(toolName, {}).get("enabled", True)




def getAllAfcTools() -> dict:
    """
    返回所有工具（builtin + custom + local-draft），合并后用于 CLI 展示。

    返回格式：
    {
      "weather":  {"enabled": True,  "source": "builtin",    "manifest": {...}},
      "timezone": {"enabled": True,  "source": "github:...", "manifest": {...}},
      "draft1":   {"enabled": True,  "source": "local-draft","manifest": None},
    }

    数据来源：只读三个 JSON 文件 + 扫描 utils/afc/tools/ 目录发现 local-draft，
    不 import 任何工具代码——名称/描述都在 manifest，无需加载模块。

    此函数用于 CLI 展示，不被扫描函数（_scanTriggers/_scanTools）调用，
    所以不会形成循环依赖，也不会触发工具 __init__ 的副作用。
    """
    toolsConfig = loadAfcToolsConfig()
    builtin = loadBuiltinAfcTools()
    custom = loadCustomAfcTools()

    result = {}

    # 1. 内置工具（来源 builtin）
    for toolName, manifest in builtin.items():
        result[toolName] = {
            "enabled": toolsConfig.get(toolName, {}).get("enabled", True),
            "source": manifest.get("source", "builtin"),
            "manifest": manifest,
        }

    # 2. 第三方工具（来源 custom，可覆盖同名 builtin）
    # enabled 以 afcTools.json 为准（enable/disable 的唯一落点，与 isAfcToolEnabled 一致）；
    # custom json 里的 enabled 只是 install 时的初始值，用作兜底默认
    for toolName, customData in custom.items():
        manifest = customData.get("manifest", {})
        result[toolName] = {
            "enabled": toolsConfig.get(toolName, {}).get("enabled", customData.get("enabled", True)),
            "source": customData.get("source", "custom"),
            "manifest": manifest,
        }

    # 3. local-draft：目录存在但既非 builtin 也非 custom
    toolsAbsDir = os.path.join(PROJECT_ROOT, _TOOLS_DIR_REL)
    if os.path.isdir(toolsAbsDir):
        for entry in os.listdir(toolsAbsDir):
            fullPath = os.path.join(toolsAbsDir, entry)
            if not os.path.isdir(fullPath):
                continue
            if entry.startswith(("_", ".")):
                continue
            if entry in result:
                continue  # 已是 builtin 或 custom
            result[entry] = {
                "enabled": toolsConfig.get(entry, {}).get("enabled", True),
                "source": "local-draft",
                "manifest": None,
            }

    return result




def _readJsonDict(path: str) -> dict:
    """读取 JSON 文件为 dict；不存在/损坏/非 dict 时返回空 dict。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _atomicWriteJson(path: str, data) -> bool:
    """
    原子写 JSON 文件（tmpfile + os.replace），保证写一半不会留下损坏文件。

    返回:
        bool: 是否写入成功
    """
    dataDir = os.path.dirname(path)
    os.makedirs(dataDir, exist_ok=True)
    tmpPath = path + ".tmp"
    try:
        with open(tmpPath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmpPath, path)
        return True
    except Exception:
        if os.path.exists(tmpPath):
            try:
                os.remove(tmpPath)
            except Exception:
                pass
        return False
