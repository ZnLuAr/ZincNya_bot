"""
utils/afc/toolManager.py

AFC 工具状态管理器。

负责 AFC 工具的启用状态与清单（manifest）持久化，镜像 utils/moduleManager.py
的原子写与读取范式，并在此之上提供模块级缓存：取工具说明和执行工具时逐个
判断启用状态，只在首次读一次盘，之后查内存，避免重复的文件 I/O。

状态文件：
- data/afcTools.json        — 每个工具的 enabled 开关（运行时自动生成，初始为空对象 {}）
- data/afcToolsBuiltin.json — 内置工具（weather/calc/datetime/web）的 manifest（仓库提供，随代码入库，不忽略）
- data/afcToolsCustom.json  — 第三方工具的 manifest（install 时生成）

其中 afcTools.json / afcToolsCustom.json 已加入 .gitignore，属部署实例私有状态，
不进 git；afcToolsBuiltin.json 是仓库内容的一部分，随代码分发。

关键设计：
- 本模块不 import registry / executor，保持单向依赖，避免循环。
- 没有写过开关的工具默认启用；内置工具可以在 manifest 里写 "defaultEnabled": false
  改成默认停用（会主动访问外部网络的 web 就是这样），由 ops 显式启用。
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


# ── 模块缓存，逐工具查询启用状态时只读一次盘 ──

_afcToolsConfigCache = None  # data/afcTools.json 的内存缓存
_builtinDefaultsCache = None  # {内置工具名: 没写过开关时是否启用}，取自 afcToolsBuiltin.json




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
    """
    保存 data/afcToolsBuiltin.json（原子写），写成功后清空缓存。一般仅初始化/迁移时用

    isAfcToolEnabled 缓存了 manifest 里的 defaultEnabled，不清的话同进程内读到的还是旧值。
    """
    ok = _atomicWriteJson(_AFCTOOLS_BUILTIN_PATH, config)
    if ok:
        invalidateAfcToolsCache()
    return ok


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
    global _afcToolsConfigCache, _builtinDefaultsCache
    _afcToolsConfigCache = None
    _builtinDefaultsCache = None




def isAfcToolEnabled(toolName: str) -> bool:
    """
    单一权威来源：检查工具是否启用

    首次调用加载一次 data/afcTools.json 进模块级缓存，后续直接查内存，
    避免对每个工具各读一次盘。

    如果工具名不在状态文件中（首次运行 / 新工具 / 误删条目），取默认值：
    内置工具看 manifest 的 defaultEnabled，其余一律 True。这样新增工具时无需
    手动配置即可立即可用，而声明了默认停用的内置工具要等 ops 显式启用。

    注意：此函数只读 JSON 状态文件，不 import 任何 utils.afc 的其他模块，
    避免循环依赖。缓存失效由 invalidateAfcToolsCache() 负责。

    参数:
        toolName: 工具名（如 "weather" / "calc"）

    返回:
        True 表示启用，False 表示禁用
    """
    global _afcToolsConfigCache, _builtinDefaultsCache
    if _afcToolsConfigCache is None:
        _afcToolsConfigCache = loadAfcToolsConfig()
    if _builtinDefaultsCache is None:
        _builtinDefaultsCache = {
            name: _manifestDefaultEnabled(manifest)
            for name, manifest in loadBuiltinAfcTools().items()
        }

    default = _builtinDefaultsCache.get(toolName, True)
    return _afcToolsConfigCache.get(toolName, {}).get("enabled", default)


def _manifestDefaultEnabled(manifest) -> bool:
    """内置工具 manifest 声明的默认启用状态；没写或 manifest 格式不对时为 True。"""
    if not isinstance(manifest, dict):
        return True
    return manifest.get("defaultEnabled", True) is not False




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

    此函数用于 CLI 展示，不被扫描函数（registry._scanTools）调用，
    所以不会形成循环依赖，也不会触发工具 __init__ 的副作用。
    """
    toolsConfig = loadAfcToolsConfig()
    builtin = loadBuiltinAfcTools()
    custom = loadCustomAfcTools()

    result = {}

    # 1. 内置工具（来源 builtin）
    for toolName, manifest in builtin.items():
        result[toolName] = {
            "enabled": toolsConfig.get(toolName, {}).get("enabled", _manifestDefaultEnabled(manifest)),
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
