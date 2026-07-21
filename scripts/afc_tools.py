#!/usr/bin/env python3
"""
scripts/afc_tools.py

ZincNya Bot AFC 工具管理 CLI。

用法：
    python scripts/afc_tools.py list [-a|--all] [-e|--enabled] [-d|--disabled] [--full]
    python scripts/afc_tools.py show <tool_name>
    python scripts/afc_tools.py check
    python scripts/afc_tools.py enable <tool_name>
    python scripts/afc_tools.py disable <tool_name>
    python scripts/afc_tools.py new <tool_name>
    python scripts/afc_tools.py install <github_url> [--branch <branch>] [-f|--force]
    python scripts/afc_tools.py update <tool_name> [--check] [-y|--yes]
    python scripts/afc_tools.py update --all (--check | --yes)
    python scripts/afc_tools.py uninstall <tool_name> [-f|--force]

安全约定：
    本脚本处理"尚未被信任"的工具（含第三方下载的），全程只做 AST 静态分析，
    绝不 import 任何工具代码——import 即执行工具顶层代码，等于在用户审阅前就运行了它。
    工具的 import 推迟到 bot 启动时，由 registry / afcIntent 在信任边界内进行。
"""

import os
import re
import sys
import ast
import json
import shutil
import tempfile
import argparse
import asyncio
import urllib.request
from datetime import datetime

# 添加项目根目录到 sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


class Color:
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    DIM = "\033[90m"
    RESET = "\033[0m"


def _enableWinVT():
    """启用 Windows VT 模式（ANSI 彩色支持）"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def supportsColor():
    """检查终端是否支持彩色输出"""
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        _enableWinVT()
    return True


_USE_COLOR = supportsColor()


def printColor(color, text):
    """打印彩色文本"""
    if not _USE_COLOR:
        print(text)
    else:
        print(f"{color}{text}{Color.RESET}")


def _log(event, details="", level=None):
    """同步脚本内的日志辅助：包装 async logSystemEvent。

    install/uninstall/check 这类安全敏感操作必须留痕，便于事后审计。
    """
    from utils.core.logger import logSystemEvent, LogLevel
    if level is None:
        level = LogLevel.INFO
    try:
        asyncio.run(logSystemEvent(event, details, level))
    except Exception:
        pass  # 日志失败不阻断命令


# ── 工具目录与常量 ────────────────────────────────

_TOOLS_DIR_REL = os.path.join("utils", "afc", "tools")
_TESTS_TOOLS_DIR_REL = os.path.join("tests", "utils", "afc", "tools")
_TOOL_ID_RE = re.compile(r"^[a-z0-9_]+$")




# ==============================================================================
# AST 静态分析（不 import 工具代码）
# ==============================================================================

def _parseFileAst(filePath):
    """
    解析 Python 文件为 AST。返回 ast.Module；文件不存在或语法错误返回 None。

    全程不 import——静态分析是检查未信任工具的唯一安全方式。
    """
    fullPath = os.path.join(PROJECT_ROOT, filePath)
    if not os.path.exists(fullPath):
        return None
    try:
        with open(fullPath, "r", encoding="utf-8") as f:
            return ast.parse(f.read(), filename=filePath)
    except (SyntaxError, ValueError, OSError):
        return None


def _extractStringList(tree, varName):
    """
    从 AST 顶层找 `varName = [...]` 的赋值，返回字符串元素列表。

    只接受顶层 Assign，且元素是字符串字面量（ast.Constant(str)）。
    找不到或结构不符返回 None。
    """
    if tree is None:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == varName:
                if not isinstance(node.value, (ast.List, ast.Tuple)):
                    return None
                items = []
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        items.append(elt.value)
                return items
    return None


def _findAsyncFunctions(tree):
    """
    从 AST 顶层找所有 async def 函数（非下划线开头），返回函数名列表。
    """
    if tree is None:
        return []
    names = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_"):
            names.append(node.name)
    return names


def _extractToolTriggers(toolName):
    """
    从工具的 triggers.py 用 AST 提取 KEYWORDS 和 PATTERNS。

    返回 (keywords, patterns, parseable)：
    - keywords / patterns 为字符串列表（提取不到则为 None）
    - parseable 表示 triggers.py 是否能被 AST 解析（False 说明文件缺失或语法错误）
    """
    triggersPath = os.path.join(_TOOLS_DIR_REL, toolName, "triggers.py")
    tree = _parseFileAst(triggersPath)
    if tree is None:
        return None, None, False
    keywords = _extractStringList(tree, "KEYWORDS")
    patterns = _extractStringList(tree, "PATTERNS")
    return keywords, patterns, True


def _hasNestedQuantifier(pattern):
    """
    ReDoS 启发式：检测嵌套量词模式（如 (x+)+ / (x*)* / (x+)*）。

    只报 Warning，不阻断——启发式有假阳性，交给开发者自查。
    """
    return bool(re.search(r"\([^)]*[+*][^)]*\)[+*]", pattern))




# ==============================================================================
# list 命令
# ==============================================================================

def cmdList(args):
    """列出所有工具"""
    from utils.afc.toolManager import getAllAfcTools

    allTools = getAllAfcTools()

    if args.enabled:
        tools = {k: v for k, v in allTools.items() if v.get("enabled", True)}
    elif args.disabled:
        tools = {k: v for k, v in allTools.items() if not v.get("enabled", True)}
    else:
        tools = allTools

    if not tools:
        printColor(Color.DIM, "- 没有找到工具\n")
        return

    enabledTools = {k: v for k, v in tools.items() if v.get("enabled", True)}
    disabledTools = {k: v for k, v in tools.items() if not v.get("enabled", True)}

    def _displayName(name, info):
        manifest = info.get("manifest") or {}
        return manifest.get("name") or name  # local-draft 退化为目录名

    def _version(info):
        manifest = info.get("manifest") or {}
        v = manifest.get("version")
        return f" (v{v})" if v else ""

    def _printTool(name, info, marker, markerColor):
        line = f"  {markerColor}{marker}{Color.RESET} {name:15} {_displayName(name, info)}{_version(info)} [{info.get('source', '?')}]"
        print(line)
        if args.full:
            _printFullDetail(name, info)

    if enabledTools:
        printColor(Color.GREEN, f"\n已启用工具（{len(enabledTools)} 个）：")
        for name, info in enabledTools.items():
            _printTool(name, info, "[+]", Color.GREEN)

    if disabledTools:
        printColor(Color.DIM, f"\n已禁用工具（{len(disabledTools)} 个）：")
        for name, info in disabledTools.items():
            _printTool(name, info, "[-]", Color.DIM)

    if not args.full:
        printColor(Color.DIM, "\n提示：使用 show <tool> 查看详细信息，list --full 查看触发词")
    print()


def _printFullDetail(toolName, info):
    """list --full 的触发词/文件数明细（触发词只显示前 3 个）"""
    keywords, patterns, parseable = _extractToolTriggers(toolName)

    def _preview(items, label):
        if items is None:
            print(f"      {label}：(无法静态解析)")
            return
        shown = ", ".join(items[:3])
        suffix = f" ... (共 {len(items)} 个)" if len(items) > 3 else f" (共 {len(items)} 个)"
        print(f"      {label}：{shown}{suffix}")

    if parseable:
        _preview(keywords or [], "关键词")
        _preview(patterns or [], "正则")

    manifest = info.get("manifest") or {}
    files = manifest.get("files")
    if files:
        print(f"      文件数：{len(files)} 个")




# ==============================================================================
# show 命令
# ==============================================================================

def cmdShow(toolName):
    """显示工具详细信息"""
    from utils.afc.toolManager import getAllAfcTools

    allTools = getAllAfcTools()
    if toolName not in allTools:
        printColor(Color.RED, f"[X] 工具 {toolName} 不存在")
        return

    info = allTools[toolName]
    manifest = info.get("manifest") or {}

    displayName = manifest.get("name") or toolName
    printColor(Color.CYAN, f"\n工具：{displayName} ({toolName})")
    if manifest.get("version"):
        print(f"版本：{manifest['version']}")
    if manifest.get("author"):
        print(f"作者：{manifest['author']}")

    if info.get("enabled", True):
        printColor(Color.GREEN, "状态：已启用")
    else:
        printColor(Color.DIM, "状态：已禁用")
    print(f"来源：{info.get('source', '?')}")

    if manifest.get("description"):
        print(f"\n描述：")
        print(f"  {manifest['description']}")

    files = manifest.get("files")
    if files:
        print(f"\n文件：")
        for f in files:
            print(f"  - {f}")

    testFile = manifest.get("testFile")
    if testFile:
        print(f"\n测试：")
        print(f"  - {testFile}")

    keywords, patterns, parseable = _extractToolTriggers(toolName)
    if parseable:
        if keywords:
            print(f"\n触发词（关键词）：")
            print(f"  - {', '.join(keywords)}")
        if patterns:
            print(f"\n触发词（正则）：")
            print(f"  - {', '.join(patterns)}")

    if info.get("source") == "local-draft":
        printColor(Color.YELLOW, "\n提示：此为 local-draft 工具（未登记到 builtin/custom manifest）")

    print()




# ==============================================================================
# check 命令
# ==============================================================================

def _checkOneTool(toolName, info):
    """
    检查单个工具，返回 (errors, warnings) 两个字符串列表。

    全程 AST 静态分析，不 import 工具代码。
    """
    errors = []
    warnings = []
    source = info.get("source", "?")
    manifest = info.get("manifest") or {}

    # ── 登记状态（builtin/custom 由 getAllAfcTools 从 manifest JSON 构造、必然已登记，
    # 无需检查；local-draft 未登记，仅 Warning）──
    if source == "local-draft":
        warnings.append("未登记到 builtin/custom manifest（local-draft）")

    # ── __init__.py（硬性：AST 可解析 + 顶层至少定义一个 async 函数）
    # 约定：工具的 async 入口函数定义在 __init__.py 顶层（薄壳转调实现），
    # 而非 from .x import 纯 re-export——这样入口对 AST 显式可见。
    initPath = os.path.join(_TOOLS_DIR_REL, toolName, "__init__.py")
    initTree = _parseFileAst(initPath)
    if initTree is None:
        errors.append("__init__.py 缺失或语法错误")
    else:
        asyncFuncs = _findAsyncFunctions(initTree)
        if not asyncFuncs:
            errors.append("__init__.py 未在顶层定义任何 async 入口函数（registry 无法注册工具）")

    # ── triggers.py（硬性：AST 可解析 + KEYWORDS/PATTERNS 至少一个非空 + 正则可编译）
    keywords, patterns, parseable = _extractToolTriggers(toolName)
    if not parseable:
        errors.append("triggers.py 缺失或语法错误")
    else:
        hasKeywords = bool(keywords)
        hasPatterns = bool(patterns)
        if not hasKeywords and not hasPatterns:
            errors.append("triggers.py 缺少 KEYWORDS 和 PATTERNS（无法触发工具）")
        # 正则可编译校验
        for pat in patterns or []:
            try:
                re.compile(pat)
            except re.error as e:
                errors.append(f"triggers.py 正则编译失败: \"{pat}\" → {e}")
            else:
                if _hasNestedQuantifier(pat):
                    warnings.append(f"ReDoS 风险：正则 \"{pat}\" 含嵌套量词")

    # ── 建议文件（Warning）
    toolDir = os.path.join(PROJECT_ROOT, _TOOLS_DIR_REL, toolName)
    if not os.path.exists(os.path.join(toolDir, "README.md")):
        warnings.append("README.md 缺失（建议项）")
    if not os.path.exists(os.path.join(toolDir, "config.py")):
        warnings.append("config.py 缺失（建议项）")

    # ── 测试文件（Warning；builtin 必须存在由 manifest 保证，custom 可选，local-draft 按惯例查）
    testFile = manifest.get("testFile") or os.path.join(_TESTS_TOOLS_DIR_REL, f"test_{toolName}.py")
    if not os.path.exists(os.path.join(PROJECT_ROOT, testFile)):
        warnings.append(f"测试文件缺失（建议项）：{testFile}")

    return errors, warnings


def cmdCheck():
    """检查所有工具的完整性"""
    from utils.afc.toolManager import getAllAfcTools, loadCustomAfcTools

    allTools = getAllAfcTools()
    if not allTools:
        printColor(Color.DIM, "- 没有找到工具\n")
        return

    print("正在检查工具完整性（AST 静态分析，不 import 工具代码）...")

    errorCount = 0
    warnTools = 0
    errorLog = []

    for toolName, info in allTools.items():
        errors, warnings = _checkOneTool(toolName, info)

        if errors:
            errorCount += 1
            printColor(Color.RED, f"\n[X] {toolName}")
            for e in errors:
                print(f"  {Color.RED}✗{Color.RESET} {e}")
            errorLog.append(f"{toolName}: {'; '.join(errors)}")
        elif warnings:
            warnTools += 1
            printColor(Color.YELLOW, f"\n[!] {toolName}")
        else:
            printColor(Color.GREEN, f"\n[OK] {toolName}")

        # 通过项的简要回显
        if not errors:
            keywords, patterns, parseable = _extractToolTriggers(toolName)
            if parseable:
                kw = len(keywords or [])
                pt = len(patterns or [])
                print(f"  {Color.GREEN}✓{Color.RESET} triggers.py 可解析，KEYWORDS({kw}) + PATTERNS({pt})")

        for w in warnings:
            print(f"  {Color.YELLOW}⚠{Color.RESET} {w}")

    # 依赖检测（custom 工具，从 manifest 读 dependencies；复用 install/update 同款比对）
    customTools = loadCustomAfcTools()

    for toolName, customData in customTools.items():
        manifest = customData.get("manifest", {})
        missingDeps, err = _findMissingDeps(manifest.get("dependencies") or [])
        if err:
            warnTools += 1
            printColor(Color.YELLOW, f"[!] {toolName}: {err}")
            continue
        for dep in missingDeps:
            warnTools += 1
            printColor(Color.YELLOW, f"[!] {toolName}: 依赖 {dep} 未在 requirements.txt 中找到")

    # 汇总
    print()
    if errorCount == 0 and warnTools == 0:
        printColor(Color.GREEN, "[OK] 所有工具检查通过\n")
    else:
        if errorCount:
            printColor(Color.RED, f"发现 {errorCount} 个工具有 Error")
            from utils.core.logger import LogLevel
            _log("AFC 工具检查异常", "；".join(errorLog), LogLevel.ERROR)
        if warnTools:
            printColor(Color.YELLOW, f"发现 {warnTools} 处 Warning")
        print()




# ==============================================================================
# enable / disable 命令
# ==============================================================================

def _setToolEnabled(toolName, enabled):
    """设置工具启用状态的公共逻辑。返回是否成功。"""
    from utils.afc.toolManager import getAllAfcTools, loadAfcToolsConfig, saveAfcToolsConfig

    allTools = getAllAfcTools()
    if toolName not in allTools:
        # local-draft 目录已被 getAllAfcTools 收录，走到这里说明三类来源都不含该工具
        printColor(Color.RED, f"[X] 工具 {toolName} 不存在")
        return False

    config = loadAfcToolsConfig()
    if toolName not in config:
        config[toolName] = {}
    config[toolName]["enabled"] = enabled

    if not saveAfcToolsConfig(config):
        printColor(Color.RED, "[X] 保存配置失败")
        return False
    return True


def cmdEnable(toolName):
    """启用工具"""
    if _setToolEnabled(toolName, True):
        printColor(Color.GREEN, f"\n[OK] 工具 {toolName} 已启用\n")
        printColor(Color.YELLOW, "提示：重启 bot 后生效\n")


def cmdDisable(toolName):
    """禁用工具"""
    if _setToolEnabled(toolName, False):
        printColor(Color.GREEN, f"\n[OK] 工具 {toolName} 已禁用\n")
        printColor(Color.YELLOW, "提示：重启 bot 后生效\n")




# ==============================================================================
# new 命令（脚手架）
# ==============================================================================

_INIT_TEMPLATE = '''"""
utils/afc/tools/{tool}/__init__.py

{tool} 工具
"""

from . import core


async def {entryFunc}() -> str:
    """
    函数简介（一句话，会进入 schema 给 LLM 看）。

    返回：
        结果字符串
    """
    return await core.{entryFunc}()


__all__ = ["{entryFunc}"]
'''

_TRIGGERS_TEMPLATE = '''"""
utils/afc/tools/{tool}/triggers.py

{tool} 工具的 AFC 触发配置。

触发词写得宽松一点没关系——它只是决定"要不要把这个工具的说明书递给
LLM"，真正用不用还是 LLM 自己拿主意。宁可多递几次，也别漏掉真实意图。
"""

# L1 关键词召回（倒排索引）：消息里出现任一关键词即命中本工具
KEYWORDS = [
    # 示例："触发词1", "trigger",
]

# L2 正则召回：消息匹配任一正则即命中本工具
PATTERNS = [
    # 示例：r"\\d+\\s*[\\+\\-\\*/]",
]
'''

_CONFIG_TEMPLATE = '''"""
utils/afc/tools/{tool}/config.py

{tool} 工具配置。

如需环境变量（API key 等），在此读取并在本文件 docstring 里说明，不要写进根
config.py——工具的配置揣在自己兜里，卸载时不残留。

# 环境变量配置说明
## MY_API_KEY（必需）
从 https://example.com 获取。示例：MY_API_KEY=your_key_here
"""

import os

# MY_API_KEY = os.getenv("MY_API_KEY", None)
'''

_CORE_TEMPLATE = '''"""
utils/afc/tools/{tool}/core.py

{tool} 工具核心实现。
"""


async def {entryFunc}() -> str:
    """
    函数简介。

    返回：
        结果字符串
    """
    raise NotImplementedError("请实现 {entryFunc}")
'''

_README_TEMPLATE = '''# {tool}

（一句话描述这个工具能做什么）

## 触发方式

- 关键词：（列出 KEYWORDS）
- 正则：（列出 PATTERNS）

## 配置

（如需环境变量，在此说明；否则写"无"）

## 示例

```
用户：（一条会触发本工具的消息）
工具：（预期行为）
```
'''

_TEST_TEMPLATE = '''"""
tests/utils/afc/tools/test_{tool}.py

测试 {tool} 工具
"""

import pytest

from utils.afc.tools.{tool} import {entryFunc}


@pytest.mark.asyncio
async def test_basic():
    """基本调用（待实现后补充真实断言）"""
    with pytest.raises(NotImplementedError):
        await {entryFunc}()
'''

_AFCTOOL_JSON_TEMPLATE = '''{{
  "id": "{tool}",
  "name": "",
  "version": "0.1.0",
  "author": "",
  "description": "",
  "files": [
    "utils/afc/tools/{tool}/__init__.py",
    "utils/afc/tools/{tool}/triggers.py",
    "utils/afc/tools/{tool}/config.py",
    "utils/afc/tools/{tool}/core.py",
    "utils/afc/tools/{tool}/README.md"
  ],
  "testFile": "tests/utils/afc/tools/test_{tool}.py"
}}
'''


def cmdNew(toolName):
    """脚手架生成新工具"""
    if not _TOOL_ID_RE.match(toolName):
        printColor(Color.RED, f"[X] 工具名格式无效（只允许小写字母、数字、下划线）：{toolName}")
        return

    toolDir = os.path.join(PROJECT_ROOT, _TOOLS_DIR_REL, toolName)
    if os.path.exists(toolDir):
        printColor(Color.RED, f"[X] 工具目录已存在：{toolDir}")
        return

    # 入口函数名：默认与工具同名，可被编辑
    entryFunc = toolName

    ctx = {"tool": toolName, "entryFunc": entryFunc}
    files = {
        os.path.join(_TOOLS_DIR_REL, toolName, "__init__.py"): _INIT_TEMPLATE.format(**ctx),
        os.path.join(_TOOLS_DIR_REL, toolName, "triggers.py"): _TRIGGERS_TEMPLATE.format(**ctx),
        os.path.join(_TOOLS_DIR_REL, toolName, "config.py"): _CONFIG_TEMPLATE.format(**ctx),
        os.path.join(_TOOLS_DIR_REL, toolName, "core.py"): _CORE_TEMPLATE.format(**ctx),
        os.path.join(_TOOLS_DIR_REL, toolName, "README.md"): _README_TEMPLATE.format(**ctx),
        os.path.join(_TOOLS_DIR_REL, toolName, "afcTool.json"): _AFCTOOL_JSON_TEMPLATE.format(**ctx),
        os.path.join(_TESTS_TOOLS_DIR_REL, f"test_{toolName}.py"): _TEST_TEMPLATE.format(**ctx),
    }

    print(f"正在创建工具 {toolName}...\n")
    for relPath, content in files.items():
        absPath = os.path.join(PROJECT_ROOT, relPath)
        os.makedirs(os.path.dirname(absPath), exist_ok=True)
        with open(absPath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  [OK] {relPath}")

    printColor(Color.GREEN, f"\n[OK] 工具 {toolName} 创建成功\n")
    print("下一步：")
    print("  1. 编辑 triggers.py 补充触发词和正则")
    print("  2. 编辑 config.py 补充工具配置（如需环境变量）")
    print(f"  3. 实现 core.py 的 {entryFunc}（及拆分的内部逻辑）")
    print("  4. 补充 tests/utils/afc/tools/test_" + toolName + ".py 的真实断言")
    print("  5. 编辑 afcTool.json，补充 name/description/author，更新 files（如有新增文件）")
    print("  6. 选择工具归属：")
    print("     - 作为内置工具：把 manifest 加入 data/afcToolsBuiltin.json")
    print("     - 作为第三方工具：发布到 GitHub，供 install 流程管理")
    print("  7. 运行 python scripts/afc_tools.py check 验证\n")




# ==============================================================================
# install 命令
# ==============================================================================

_GITHUB_URL_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$")
_RAW_BASE_URL = "https://raw.githubusercontent.com"
_MANIFEST_MAX_BYTES = 1 * 1024 * 1024   # afcTool.json 大小上限
_FILE_MAX_BYTES = 2 * 1024 * 1024       # 单个工具文件大小上限
_MAX_FILE_COUNT = 50                    # 单工具文件数上限
_INSTALL_STAGING_REL = os.path.join("data", ".afc_install")  # 安装暂存目录（与目标同盘，os.replace 才原子）


def _assertSafeToolPath(toolId, filePath, baseSubdir, requireSubdir=True):
    """
    校验 manifest 声明的文件路径不逃逸出工具目录，不通过抛 ValueError。

    用 realpath + commonpath 判定，不用 startswith 子串——后者会被绝对路径吞并
    和 .. 穿越绕过。校验通过即返回，无返回值。

    参数:
        toolId: 工具 id（requireSubdir=True 时用于限定到 base/<toolId>/ 子目录）
        filePath: manifest 声明的相对路径
        baseSubdir: 基准子目录（工具代码用 utils/afc/tools，测试用 tests/utils/afc/tools）
        requireSubdir: 是否要求路径落在 base/<toolId>/ 子目录内。
            工具代码文件为 True；testFile 是扁平的 test_<id>.py（不在 <id>/ 子目录），
            故为 False，只约束在 base 内即可。
    """
    norm = filePath.replace("\\", "/")
    if ".." in norm.split("/") or os.path.isabs(norm):
        raise ValueError(f"非法文件路径：{filePath}")
    if requireSubdir:
        baseDir = os.path.realpath(os.path.join(PROJECT_ROOT, baseSubdir, toolId))
    else:
        baseDir = os.path.realpath(os.path.join(PROJECT_ROOT, baseSubdir))
    dest = os.path.realpath(os.path.join(PROJECT_ROOT, filePath))
    if os.path.commonpath([dest, baseDir]) != baseDir:
        raise ValueError(f"文件路径逃逸出工具目录：{filePath}")


def _downloadWithLimit(url, destPath, maxBytes):
    """
    分块下载 url 到 destPath，累计超过 maxBytes 即中止并删除半成品。

    返回 bool：完整下载且未超限为 True；网络/IO 错误或超限时为 False。
    """
    ok = False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ZincNya-afc-tools"})
        with urllib.request.urlopen(req, timeout=15) as response:
            with open(destPath, "wb") as f:
                total = 0
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        ok = True
                        break
                    total += len(chunk)
                    if total > maxBytes:
                        break
                    f.write(chunk)
    except Exception:
        ok = False
    if not ok and os.path.exists(destPath):
        try:
            os.remove(destPath)
        except OSError:
            pass
    return ok


def _failWithLog(event, reason, logDetails):
    """统一失败出口：打印原因并留 ERROR 痕。返回 False 供调用方直接 return。"""
    from utils.core.logger import LogLevel
    printColor(Color.RED, f"[X] {reason}")
    _log(event, logDetails, LogLevel.ERROR)
    return False


def _fetchRemoteManifest(user, repo, branches):
    """按分支顺序尝试抓取 afcTool.json（1MB 上限、JSON 解析、顶层 dict 校验）。

    返回 (manifest, selectedBranch, fatalError)：
    - 成功：(dict, 分支名, None)
    - 所有分支均不可访问：(None, None, None)
    - 致命错误（超限 / 非法 JSON / 顶层非对象）：(None, None, 错误串)

    全程不打印——怎么报告由调用方决定。
    """
    for tryBranch in branches:
        manifestUrl = f"{_RAW_BASE_URL}/{user}/{repo}/{tryBranch}/afcTool.json"
        try:
            req = urllib.request.Request(manifestUrl, headers={"User-Agent": "ZincNya-afc-tools"})
            with urllib.request.urlopen(req, timeout=15) as response:
                raw = response.read(_MANIFEST_MAX_BYTES + 1)
        except Exception:
            continue  # 分支不存在或网络错误，尝试下一分支
        if len(raw) > _MANIFEST_MAX_BYTES:
            return None, None, f"afcTool.json 超过 1MB 上限（分支 {tryBranch}）"
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, None, f"afcTool.json 不是合法 JSON（分支 {tryBranch}）"
        if not isinstance(manifest, dict):
            return None, None, f"afcTool.json 顶层必须是 JSON 对象（分支 {tryBranch}）"
        return manifest, tryBranch, None
    return None, None, None


def _validateManifestFields(manifest):
    """校验 manifest 必需字段、id 格式、files 类型与数量上限。

    返回 (toolId, files, error)；error 非 None 时前两者为 None。
    """
    missing = [k for k in ("id", "name", "version", "files") if not manifest.get(k)]
    if missing:
        return None, None, f"afcTool.json 缺少必需字段：{', '.join(missing)}"
    toolId = manifest["id"]
    if not isinstance(toolId, str) or not _TOOL_ID_RE.match(toolId):
        return None, None, f"工具 id 格式无效（只允许小写字母、数字、下划线）：{toolId}"
    files = manifest["files"]
    if not isinstance(files, list) or not all(isinstance(p, str) for p in files):
        return None, None, "afcTool.json 的 files 必须是字符串列表"
    if len(files) > _MAX_FILE_COUNT:
        return None, None, f"文件数 {len(files)} 超过上限 {_MAX_FILE_COUNT}"
    return toolId, files, None


def _validateManifestPaths(toolId, files, testFile):
    """校验 manifest 声明的文件路径安全性（先校验再下载，杜绝逃逸路径写盘）。

    返回错误串；全部通过返回 None。
    """
    if testFile and not isinstance(testFile, str):
        return "afcTool.json 的 testFile 必须是字符串"
    try:
        for filePath in files:
            _assertSafeToolPath(toolId, filePath, _TOOLS_DIR_REL)
        if testFile:
            if not os.path.basename(testFile.replace("\\", "/")).startswith("test_"):
                return f"测试文件名必须以 test_ 开头：{testFile}"
            # testFile 是扁平的 test_<id>.py，不在 <id>/ 子目录，只约束在 base 内
            _assertSafeToolPath(toolId, testFile, _TESTS_TOOLS_DIR_REL, requireSubdir=False)
    except ValueError as e:
        return str(e)
    return None


def _findMissingDeps(dependencies):
    """比对 dependencies 与项目 requirements.txt（模糊匹配包名，忽略版本号）。

    返回 (missingDeps, error)：dependencies 不是列表时 error 非 None。
    纯计算，不打印不询问——怎么交互由调用方决定。
    """
    if not isinstance(dependencies, list):
        return None, "afcTool.json 的 dependencies 必须是字符串列表"
    if not dependencies:
        return [], None
    reqPath = os.path.join(PROJECT_ROOT, "requirements.txt")
    reqText = ""
    if os.path.exists(reqPath):
        with open(reqPath, "r", encoding="utf-8") as f:
            reqText = f.read().lower()
    missingDeps = []
    for dep in dependencies:
        pkgName = re.split(r"[<>=!~\[]", str(dep))[0].strip().lower()
        if pkgName and pkgName not in reqText:
            missingDeps.append(str(dep))
    return missingDeps, None


def _downloadToolFiles(githubUrl, rawBase, files, testFile, tempDir):
    """下载工具全部文件到暂存目录（testFile 可选，失败仅警告不中断）。

    rawBase 形如 https://raw.githubusercontent.com/<user>/<repo>/<branch>，末段即分支名。
    返回 (testDownloaded, error)；error 非 None 时应中止。
    """
    print(f"正在从 {githubUrl} (分支: {rawBase.rsplit('/', 1)[-1]}) 下载 {len(files)} 个文件...")
    for filePath in files:
        destPath = os.path.join(tempDir, filePath)
        os.makedirs(os.path.dirname(destPath), exist_ok=True)
        if not _downloadWithLimit(f"{rawBase}/{filePath}", destPath, _FILE_MAX_BYTES):
            return False, f"下载失败或超过 2MB 上限：{filePath}"
        print(f"  [OK] {filePath}")

    # 测试文件：可选，下载失败仅警告并继续
    testDownloaded = False
    if testFile:
        testDest = os.path.join(tempDir, testFile)
        os.makedirs(os.path.dirname(testDest), exist_ok=True)
        if _downloadWithLimit(f"{rawBase}/{testFile}", testDest, _FILE_MAX_BYTES):
            testDownloaded = True
            print(f"  [OK] {testFile}")
        else:
            printColor(Color.YELLOW, f"  [!] 测试文件无法下载（跳过）：{testFile}")
    return testDownloaded, None


def _validateStagingTriggers(tempDir, toolId, action="安装"):
    """校验暂存目录里的 triggers.py（AST 静态分析，绝不 import 工具代码）。

    返回错误串；通过返回 None。ReDoS 启发式警告在此打印。
    """
    triggersTempPath = os.path.join(tempDir, _TOOLS_DIR_REL, toolId, "triggers.py")
    tree = _parseFileAst(triggersTempPath)
    if tree is None:
        return f"triggers.py 缺失或语法错误（无法静态解析，拒绝{action}）"
    keywords = _extractStringList(tree, "KEYWORDS")
    patterns = _extractStringList(tree, "PATTERNS")
    if not keywords and not patterns:
        return "triggers.py 缺少 KEYWORDS 和 PATTERNS（无法触发工具）"
    for pat in patterns or []:
        try:
            re.compile(pat)
        except re.error as e:
            return f"triggers.py 正则编译失败: \"{pat}\" → {e}"
        if _hasNestedQuantifier(pat):
            printColor(Color.YELLOW, f"[!] ReDoS 风险：正则 \"{pat}\" 含嵌套量词")
    return None


def _applyStagedFiles(tempDir, fileList, pendingTmps):
    """把暂存文件原子落盘：先移到 <target>.tmp，再 os.replace 到最终路径。

    pendingTmps 由调用方持有——本函数内 append/remove，外层 finally 负责清理残留。
    「安装文件... / 更新文件...」表头由调用方打印。
    """
    for filePath in fileList:
        srcPath = os.path.join(tempDir, filePath)
        destPath = os.path.join(PROJECT_ROOT, filePath)
        tmpPath = destPath + ".tmp"
        os.makedirs(os.path.dirname(destPath), exist_ok=True)
        pendingTmps.append(tmpPath)
        shutil.move(srcPath, tmpPath)
        os.replace(tmpPath, destPath)
        pendingTmps.remove(tmpPath)
        print(f"  [OK] {filePath}")




def cmdInstall(githubUrl, branch, force):
    """
    从 GitHub 仓库安装第三方 AFC 工具。

    流程：解析 URL → 按分支下载 afcTool.json → 校验 manifest 与文件路径 →
    检查依赖 → 下载到同盘临时目录 → AST 校验 triggers.py → 原子落盘 →
    登记 data/afcToolsCustom.json。任何一步失败都回滚清理，不留半成品。
    """
    from utils.afc.toolManager import (
        loadBuiltinAfcTools, loadCustomAfcTools, saveCustomAfcTools,
    )
    from utils.core.logger import LogLevel

    def _fail(reason):
        """统一失败出口：打印原因并留 ERROR 痕"""
        return _failWithLog("AFC 工具安装失败", reason, f"{githubUrl}: {reason}")

    match = _GITHUB_URL_RE.match(githubUrl)
    if not match:
        printColor(Color.RED, "[X] 无效的 GitHub URL（仅支持 https://github.com/<user>/<repo>）")
        return False
    user, repo = match.groups()

    _log("AFC 工具安装开始", f"url={githubUrl} branch={branch or 'auto'} force={force}")

    tempDir = None
    pendingTmps = []

    try:
        # ── 分支尝试（--branch 指定 > main > master）并下载 afcTool.json ──
        branches = [branch] if branch else ["main", "master"]
        manifest, selectedBranch, fatalError = _fetchRemoteManifest(user, repo, branches)
        if fatalError:
            return _fail(fatalError)
        if manifest is None:
            if branch:
                return _fail(f"分支 {branch} 不存在或无法访问")
            return _fail("无法找到 main 或 master 分支（均未提供可访问的 afcTool.json）")

        # ── manifest 必需字段与 id 格式 ──
        toolId, files, err = _validateManifestFields(manifest)
        if err:
            return _fail(err)

        # ── 路径安全校验（先校验再下载，杜绝逃逸路径写盘）──
        testFile = manifest.get("testFile")
        err = _validateManifestPaths(toolId, files, testFile)
        if err:
            return _fail(err)

        # ── 内置保护与覆盖检查 ──
        if toolId in loadBuiltinAfcTools():
            return _fail(f"工具 {toolId} 是内置工具，禁止覆盖安装")
        toolDir = os.path.join(PROJECT_ROOT, _TOOLS_DIR_REL, toolId)
        if os.path.exists(toolDir):
            if not force:
                printColor(Color.RED, f"[X] 工具目录已存在：{toolDir}")
                printColor(Color.YELLOW, "提示：确认要覆盖请加 -f/--force")
                return False
            printColor(Color.YELLOW, f"[!] 工具 {toolId} 已存在，将覆盖安装")
            _log("AFC 工具覆盖安装", f"{toolId} ← {githubUrl}", LogLevel.WARNING)

        # ── 依赖检查（模糊比对 requirements.txt，缺失需人工确认）──
        missingDeps, err = _findMissingDeps(manifest.get("dependencies") or [])
        if err:
            return _fail(err)
        if missingDeps:
            printColor(Color.YELLOW, "[!] 以下依赖未在 requirements.txt 中找到：")
            for dep in missingDeps:
                print(f"  - {dep}")
            _log("AFC 工具依赖缺失", f"{toolId}: {', '.join(missingDeps)}", LogLevel.WARNING)
            answer = input("是否继续安装？(y/n) ").strip().lower()
            if answer != "y":
                printColor(Color.DIM, "已取消安装")
                return False

        # ── 下载到同盘临时目录（与目标同盘，os.replace 才原子）──
        stagingParent = os.path.join(PROJECT_ROOT, _INSTALL_STAGING_REL)
        os.makedirs(stagingParent, exist_ok=True)
        tempDir = tempfile.mkdtemp(dir=stagingParent)

        rawBase = f"{_RAW_BASE_URL}/{user}/{repo}/{selectedBranch}"
        testDownloaded, err = _downloadToolFiles(githubUrl, rawBase, files, testFile, tempDir)
        if err:
            return _fail(err)

        # ── triggers.py 静态校验（AST 解析，绝不 import 工具代码）──
        err = _validateStagingTriggers(tempDir, toolId)
        if err:
            return _fail(err)

        # ── 原子落盘：先移到 <target>.tmp，再 os.replace 到最终路径 ──
        # 落盘前备份本地已有文件；落盘/登记任一步失败则回滚，不留半成品
        print("安装文件...")
        installFiles = list(files)
        if testDownloaded:
            installFiles.append(testFile)
        backupDir = os.path.join(PROJECT_ROOT, _INSTALL_STAGING_REL, f"backup_install_{toolId}")
        newWritten = _backupToolFiles(installFiles, [], backupDir)

        try:
            _applyStagedFiles(tempDir, installFiles, pendingTmps)

            # ── 登记 data/afcToolsCustom.json（最后一步）──
            # remoteSha 是 update 快车道的可选加速字段，抓取失败静默跳过
            remoteSha = _fetchRemoteSha(user, repo, selectedBranch)
            customTools = loadCustomAfcTools()
            customTools[toolId] = {
                "enabled": True,
                "manifest": manifest,
                "source": f"github:{githubUrl}@{selectedBranch}",
                "installedAt": datetime.now().isoformat(),
            }
            if remoteSha:
                customTools[toolId]["remoteSha"] = remoteSha
            if not saveCustomAfcTools(customTools):
                raise RuntimeError("保存 data/afcToolsCustom.json 失败")
        except Exception as applyErr:
            restored, failedPaths = _rollbackToolFiles(backupDir, newWritten)
            # install 回滚 = 工具从未来过：被删空的工具目录一并清掉
            # （overwrite 场景旧文件已还原、目录非空，自然保留）
            toolDir = os.path.join(PROJECT_ROOT, _TOOLS_DIR_REL, toolId)
            _pruneEmptyDirs(toolDir)
            if os.path.isdir(toolDir) and not os.listdir(toolDir):
                os.rmdir(toolDir)
            if failedPaths:
                printColor(Color.RED, f"[X] 安装失败：{applyErr}")
                printColor(Color.RED, f"[X] 回滚不完整，备份已保留在 {backupDir}，请人工恢复：{', '.join(failedPaths)}")
                _log("AFC 工具安装失败", f"{githubUrl}: {applyErr}（回滚不完整，备份在 {backupDir}）", LogLevel.ERROR)
            else:
                shutil.rmtree(backupDir, ignore_errors=True)
                _failWithLog(
                    "AFC 工具安装失败",
                    f"安装失败：{applyErr}，已回滚清理",
                    f"{githubUrl}: {applyErr}（已回滚 {restored} 个文件）",
                )
            return False

        if os.path.exists(backupDir):
            shutil.rmtree(backupDir, ignore_errors=True)

        printColor(Color.GREEN, f"\n[OK] 工具 {toolId} 安装成功\n")
        printColor(Color.YELLOW, "提示：重启 bot 后生效\n")
        _log("AFC 工具安装成功", f"{toolId} ← {githubUrl}@{selectedBranch}")
        return True

    except Exception as e:
        return _fail(f"安装过程出现异常：{e}")
    finally:
        # 回滚清理：临时目录与未完成的 <target>.tmp 在任何异常路径都不残留
        if tempDir and os.path.exists(tempDir):
            shutil.rmtree(tempDir, ignore_errors=True)
        for tmpPath in pendingTmps:
            if os.path.exists(tmpPath):
                try:
                    os.remove(tmpPath)
                except OSError:
                    pass




# ==============================================================================
# update 命令
# ==============================================================================

def _fetchRemoteSha(user, repo, branch):
    """抓远端分支 HEAD 的 commit sha（GitHub commits API），失败/超时/超配额静默返回 None。

    这只是 update 快车道的加速器——拿不到就走完整字节比对，不影响裁决。
    """
    apiUrl = f"https://api.github.com/repos/{user}/{repo}/commits/{branch}"
    try:
        req = urllib.request.Request(apiUrl, headers={
            "User-Agent": "ZincNya-afc-tools",
            "Accept": "application/vnd.github+json",
        })
        # 响应含 files 数组，大 commit 可能超百 KB——放宽到 1MB 防快车道误不命中
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read(1024 * 1024).decode("utf-8"))
        sha = data.get("sha") if isinstance(data, dict) else None
        return sha if isinstance(sha, str) else None
    except Exception:
        return None


def _parseCustomSource(source):
    """反解 custom json 里的 source 串（github:<url>@<branch>）。

    返回 (url, branch, error)；error 非 None 时前两者为 None。
    source 可被本地篡改——url 重新过 _GITHUB_URL_RE 限定 github.com。
    """
    if not isinstance(source, str) or not source.startswith("github:"):
        return None, None, "source 缺少 github: 前缀"
    payload = source[len("github:"):]
    if "@" not in payload:
        return None, None, "source 缺少 @分支 后缀"
    url, branch = payload.rsplit("@", 1)
    if not _GITHUB_URL_RE.match(url):
        return None, None, f"source 中的 URL 非法：{url}"
    if not branch:
        return None, None, "source 中的分支为空"
    return url, branch, None


def _compareVersions(localVer, remoteVer):
    """简易版本号比较，返回 remote 相对 local 的序：1 新 / 0 同 / -1 旧 / None 不可比。

    规则（非严格 semver）：按 . 切段，每段拆（数字前缀, 后缀），先比数字再比后缀
    （空后缀 > 非空后缀，1.2.0 > 1.2.0-beta）；短侧补 (0, "")（1.2 == 1.2.0）。
    缺失 / 空串 / 非 str → None（调用方退化为纯字节比对）。
    """
    def _parse(ver):
        if not isinstance(ver, (str, int, float)):
            return None
        text = str(ver).strip()
        if not text:
            return None
        segs = []
        for seg in text.split("."):
            m = re.match(r"(\d*)(.*)", seg)
            segs.append((int(m.group(1)) if m.group(1) else 0, m.group(2)))
        return segs

    local = _parse(localVer)
    remote = _parse(remoteVer)
    if local is None or remote is None:
        return None

    length = max(len(local), len(remote))
    local += [(0, "")] * (length - len(local))
    remote += [(0, "")] * (length - len(remote))
    for (ln, ls), (rn, rs) in zip(local, remote):
        if ln != rn:
            return 1 if rn > ln else -1
        if ls != rs:
            if not ls:
                return -1  # local 是 release、remote 是 prerelease → remote 旧
            if not rs:
                return 1   # remote 是 release → remote 新
            return 1 if rs > ls else -1
    return 0


def _fileBytesEqual(pathA, pathB):
    """字节比对两个文件：先比尺寸（快路径），再读全文。"""
    if os.path.getsize(pathA) != os.path.getsize(pathB):
        return False
    with open(pathA, "rb") as fa, open(pathB, "rb") as fb:
        return fa.read() == fb.read()


def _diffToolFiles(oldManifest, newManifest, tempDir, testDownloaded):
    """字节比对暂存目录与本地文件，返回四分类 + 删除集。

    返回 dict：
    - added：新 manifest 新增、本地不存在
    - changed：本地存在但字节不同
    - missing：旧 manifest 有、本地被手删（恢复性写入）
    - unchanged：字节相同
    - deleted：旧 files+旧 testFile − 新声明 files+新声明 testFile
    （删除按声明计算，与 testFile 是否下载成功无关——下载失败的新测试文件
    不会被写入，但旧测试文件也不会被误删，因为新 manifest 仍声明它。）
    """
    oldFiles = list(oldManifest.get("files") or [])
    oldTest = oldManifest.get("testFile")
    newFiles = list(newManifest.get("files") or [])
    newTest = newManifest.get("testFile")

    writeSet = list(newFiles)
    if testDownloaded and newTest:
        writeSet.append(newTest)

    added, changed, missing, unchanged = [], [], [], []
    for relPath in writeSet:
        localPath = os.path.join(PROJECT_ROOT, relPath)
        stagedPath = os.path.join(tempDir, relPath)
        if not os.path.exists(localPath):
            if relPath in oldFiles or relPath == oldTest:
                missing.append(relPath)
            else:
                added.append(relPath)
        elif _fileBytesEqual(localPath, stagedPath):
            unchanged.append(relPath)
        else:
            changed.append(relPath)

    newDeclared = set(newFiles)
    if newTest:
        newDeclared.add(newTest)
    oldDeclared = oldFiles + ([oldTest] if oldTest else [])
    deleted = [p for p in oldDeclared if p not in newDeclared]

    return {
        "added": added,
        "changed": changed,
        "missing": missing,
        "unchanged": unchanged,
        "deleted": deleted,
    }


def _pruneEmptyDirs(toolDir):
    """自底向上删除 toolDir 内的空子目录（不删 toolDir 本体——工具还在）。"""
    if not os.path.isdir(toolDir):
        return
    for dirPath, _, _ in os.walk(toolDir, topdown=False):
        if dirPath == toolDir:
            continue
        if not os.listdir(dirPath):
            try:
                os.rmdir(dirPath)
            except OSError:
                pass


def _backupToolFiles(writeSet, deleteSet, backupDir):
    """把「写入集∩本地存在 + 删除集∩本地存在」copy2 复制到备份目录（单槽，先清残留）。

    返回 newWrittenList：写入集中本地原本不存在的文件（回滚时要删掉的）。
    备份创建失败（如磁盘满）抛异常——调用方据此中止更新（此时未动任何文件）。
    """
    newWritten = []
    if os.path.exists(backupDir):
        shutil.rmtree(backupDir, ignore_errors=True)
    for relPath in list(writeSet) + list(deleteSet):
        localPath = os.path.join(PROJECT_ROOT, relPath)
        if not os.path.exists(localPath):
            if relPath in writeSet:
                newWritten.append(relPath)
            continue
        destPath = os.path.join(backupDir, relPath)
        os.makedirs(os.path.dirname(destPath), exist_ok=True)
        shutil.copy2(localPath, destPath)
    return newWritten


def _rollbackToolFiles(backupDir, newWrittenList):
    """回滚：备份文件 os.replace 移回原位（恢复被覆盖/被删的），删掉本次新写入的。

    尽力而为——单个失败不中断其余。返回 (restoredCount, failedPaths)。
    """
    restored = 0
    failed = []
    if os.path.isdir(backupDir):
        for dirPath, _, fileNames in os.walk(backupDir):
            for name in fileNames:
                srcPath = os.path.join(dirPath, name)
                relPath = os.path.relpath(srcPath, backupDir)
                destPath = os.path.join(PROJECT_ROOT, relPath)
                try:
                    os.makedirs(os.path.dirname(destPath), exist_ok=True)
                    os.replace(srcPath, destPath)
                    restored += 1
                except OSError:
                    failed.append(relPath)
    for relPath in newWrittenList:
        localPath = os.path.join(PROJECT_ROOT, relPath)
        if not os.path.exists(localPath):
            continue
        try:
            os.remove(localPath)
        except OSError:
            failed.append(relPath)
    return restored, failed


def _deleteRemovedFiles(toolName, oldManifest, deleteSet):
    """删除新 manifest 不再声明的文件，删前逐个 _safeRemoveToolFile 重校。

    不信任存储的旧 manifest（custom json 可能被篡改塞 ../../x.py）——逃逸路径
    SKIP + WARNING 日志，镜像 uninstall 的防御。
    """
    oldTest = oldManifest.get("testFile")
    for relPath in deleteSet:
        isTest = (relPath == oldTest)
        baseDir = _TESTS_TOOLS_DIR_REL if isTest else _TOOLS_DIR_REL
        if not _safeRemoveToolFile(toolName, relPath, baseDir, requireSubdir=not isTest):
            from utils.core.logger import LogLevel
            printColor(Color.YELLOW, f"  [SKIP] 跳过非法路径：{relPath}")
            _log("AFC 工具更新路径逃逸", f"{toolName}: {relPath}", LogLevel.WARNING)
            continue
        absPath = os.path.join(PROJECT_ROOT, relPath)
        if not os.path.exists(absPath):
            continue
        os.remove(absPath)
        print(f"  [OK] 删除 {relPath}")


def _printUpdateReport(toolName, localVer, remoteVer, cmpResult, diff, manifestUpdated):
    """差异报告：版本行 + 四分类清单。"""
    hasFileChanges = any([diff["added"], diff["changed"], diff["missing"], diff["deleted"]])
    if cmpResult == 1:
        print(f"工具 {toolName}：v{localVer} → v{remoteVer}")
    elif cmpResult == -1:
        print(f"工具 {toolName}：v{localVer} → v{remoteVer}（降级）")
    elif not hasFileChanges and manifestUpdated:
        print(f"工具 {toolName}：仅清单信息更新（v{localVer}）")
    else:
        print(f"工具 {toolName}：版本未变（v{localVer}），但内容已变")

    for label, key in (("新增", "added"), ("变更", "changed"), ("本地缺失（恢复）", "missing"), ("删除", "deleted")):
        items = diff[key]
        if items:
            print(f"  {label}（{len(items)}）：")
            for p in items:
                print(f"    - {p}")
    print(f"  未变（{len(diff['unchanged'])}）")




def cmdUpdate(toolName, checkOnly=False, assumeYes=False):
    """从 GitHub 更新已安装的第三方工具（raw 字节比对检测 + 备份回滚原子落盘）。

    返回 "updated" / "uptodate" / "failed" / "cancelled"（供 --all 汇总）。
    """
    from utils.afc.toolManager import (
        getAllAfcTools, loadBuiltinAfcTools, loadCustomAfcTools, saveCustomAfcTools,
    )
    from utils.core.logger import LogLevel

    def _fail(reason):
        _failWithLog("AFC 工具更新失败", reason, f"{toolName}: {reason}")
        return "failed"

    # ── A. 资格检查（用户指路问题，不记日志）──
    # toolName 先过 id 校验：它要拼备份目录等路径，custom json 键名可能被篡改
    if not _TOOL_ID_RE.match(toolName):
        printColor(Color.RED, f"[X] 工具名格式无效（只允许小写字母、数字、下划线）：{toolName}")
        return "failed"

    customTools = loadCustomAfcTools()
    if toolName not in customTools:
        allTools = getAllAfcTools()
        if toolName not in allTools:
            printColor(Color.RED, f"[X] 工具 {toolName} 不存在")
            return "failed"
        if allTools[toolName].get("source") == "builtin":
            printColor(Color.YELLOW, f"[!] {toolName} 是内置工具，随仓库更新，请用 git pull")
            return "failed"
        printColor(Color.YELLOW, f"[!] {toolName} 是 local-draft 工具，没有登记来源，无法更新")
        return "failed"

    # builtin 影子保护：custom json 被手改成与内置同名时，拒绝覆盖内置文件
    if toolName in loadBuiltinAfcTools():
        return _fail(f"工具 {toolName} 与内置工具同名（custom json 异常），拒绝更新，请先 uninstall")

    entry = customTools[toolName]
    oldManifest = entry.get("manifest") or {}

    # ── B. source 反解 ──
    url, branch, err = _parseCustomSource(entry.get("source"))
    if err:
        return _fail(f"来源信息损坏（{err}），可 uninstall 后重新 install")
    user, repo = _GITHUB_URL_RE.match(url).groups()

    # ── 快车道：remoteSha 相同即已是最新（加速器不是裁决；--check 始终全量比对）──
    # 注意放在「更新开始」日志之前，避免留下有始无终的孤儿日志
    if not checkOnly:
        knownSha = entry.get("remoteSha")
        if knownSha and _fetchRemoteSha(user, repo, branch) == knownSha:
            printColor(Color.GREEN, f"[OK] {toolName} 已是最新（远端提交未变，安装于 {entry.get('installedAt', '?')}）")
            return "uptodate"  # 不记日志

    # ── C. 抓远端 manifest（单分支，用 source 里记录的，不做 main/master 探测）──
    if not checkOnly:
        _log("AFC 工具更新开始", f"{toolName} ← {url}@{branch}")
    remoteManifest, _, fatalError = _fetchRemoteManifest(user, repo, [branch])
    if fatalError:
        return _fail(fatalError)
    if remoteManifest is None:
        return _fail(f"分支 {branch} 不存在或无法访问（分支可能已改名，可 uninstall 后重新 install）")

    # ── D. 远端 manifest 校验 ──
    remoteId, files, err = _validateManifestFields(remoteManifest)
    if err:
        return _fail(err)
    if remoteId != toolName:
        return _fail(f"远端 afcTool.json 的 id（{remoteId}）与本地工具名（{toolName}）不一致，拒绝更新")
    testFile = remoteManifest.get("testFile")
    err = _validateManifestPaths(toolName, files, testFile)
    if err:
        return _fail(err)

    # ── E. 版本比较（只定调，不做裁决——字节比对才是真相）──
    localVer = oldManifest.get("version")
    remoteVer = remoteManifest.get("version")
    cmpResult = _compareVersions(localVer, remoteVer)
    if cmpResult == -1:
        printColor(Color.YELLOW, f"[!] 远端版本 {remoteVer} 低于本地 {localVer}（仓库可能已回滚）")

    # ── F. 依赖变化（只警告，并入最终一次确认）──
    missingDeps, err = _findMissingDeps(remoteManifest.get("dependencies") or [])
    if err:
        return _fail(err)
    if missingDeps:
        printColor(Color.YELLOW, "[!] 以下依赖未在 requirements.txt 中找到：")
        for dep in missingDeps:
            print(f"  - {dep}")
        _log("AFC 工具依赖缺失", f"{toolName}: {', '.join(missingDeps)}", LogLevel.WARNING)

    tempDir = None
    pendingTmps = []
    backupDir = os.path.join(PROJECT_ROOT, _INSTALL_STAGING_REL, f"backup_{toolName}")

    try:
        # ── G. 下载全套到暂存（比对需要全量字节）──
        stagingParent = os.path.join(PROJECT_ROOT, _INSTALL_STAGING_REL)
        os.makedirs(stagingParent, exist_ok=True)
        tempDir = tempfile.mkdtemp(dir=stagingParent)

        rawBase = f"{_RAW_BASE_URL}/{user}/{repo}/{branch}"
        testDownloaded, err = _downloadToolFiles(url, rawBase, files, testFile, tempDir)
        if err:
            return _fail(err)
        err = _validateStagingTriggers(tempDir, toolName, action="更新")
        if err:
            return _fail(err)

        # ── H. 字节比对四分类（+manifest 比较）──
        diff = _diffToolFiles(oldManifest, remoteManifest, tempDir, testDownloaded)
        manifestUpdated = (remoteManifest != oldManifest)
        writeSet = diff["added"] + diff["changed"] + diff["missing"]
        hasFileChanges = bool(writeSet or diff["deleted"])

        if not hasFileChanges and not manifestUpdated:
            printColor(Color.GREEN, f"[OK] {toolName} 已是最新 (v{localVer}，安装于 {entry.get('installedAt', '?')})")
            return "uptodate"  # finally 清暂存；不记日志

        # ── I. 报告 ──
        _printUpdateReport(toolName, localVer, remoteVer, cmpResult, diff, manifestUpdated)
        if checkOnly:
            printColor(Color.DIM, "（--check：未做任何更改）")
            return "updated"

        # ── J. 一次确认（依赖缺失警告已并入报告）──
        if hasFileChanges:
            printColor(Color.YELLOW, "[!] 本地对这些文件的修改将被覆盖")
        if not assumeYes:
            try:
                answer = input(f"确认更新 {toolName}？(y/n) ").strip().lower()
            except EOFError:
                answer = ""
            if answer != "y":
                printColor(Color.DIM, "已取消更新")
                return "cancelled"

        # ── K. 备份-落盘-回滚（工具级原子：要么全新，要么全旧）──
        newWritten = []
        try:
            if hasFileChanges:
                # K0 备份（失败即中止，此时尚未动任何文件）
                newWritten = _backupToolFiles(writeSet, diff["deleted"], backupDir)
                # K1 落盘：先写后删，json 最后
                print("更新文件...")
                _applyStagedFiles(tempDir, writeSet, pendingTmps)
                _deleteRemovedFiles(toolName, oldManifest, diff["deleted"])
                _pruneEmptyDirs(os.path.join(PROJECT_ROOT, _TOOLS_DIR_REL, toolName))

            fresh = loadCustomAfcTools()  # 重读，避免覆盖并发修改
            old = fresh[toolName]
            fresh[toolName] = {
                "enabled": old.get("enabled", True),      # 保留
                "manifest": remoteManifest,                # 换
                "source": old.get("source"),               # 原串保留
                "installedAt": old.get("installedAt"),     # 保留
                "updatedAt": datetime.now().isoformat(),   # 新增
            }
            # 记录远端 HEAD sha（快车道加速器字段；fetch 失败静默、保留旧值，
            # 下次 update 还能拿它继续比对——只是可能比对不上退化为全量，无害）
            sha = _fetchRemoteSha(user, repo, branch)
            if sha:
                fresh[toolName]["remoteSha"] = sha
            elif old.get("remoteSha"):
                fresh[toolName]["remoteSha"] = old["remoteSha"]
            if not saveCustomAfcTools(fresh):
                raise RuntimeError("保存 data/afcToolsCustom.json 失败")
        except Exception as applyErr:
            if not hasFileChanges:
                # 空集豁免：无文件侧状态可回滚，直接 fail
                _failWithLog("AFC 工具更新失败", f"保存配置失败：{applyErr}", f"{toolName}: {applyErr}")
                return "failed"
            # K2 回滚：还原备份 + 删新写入，尽力而为；可能留下空子目录，顺手清
            restored, failedPaths = _rollbackToolFiles(backupDir, newWritten)
            _pruneEmptyDirs(os.path.join(PROJECT_ROOT, _TOOLS_DIR_REL, toolName))
            if failedPaths:
                printColor(Color.RED, f"[X] 更新失败：{applyErr}")
                printColor(Color.RED, f"[X] 回滚不完整，备份已保留在 {backupDir}，请人工恢复：{', '.join(failedPaths)}")
                _log("AFC 工具更新失败", f"{toolName}: {applyErr}（回滚不完整，备份在 {backupDir}）", LogLevel.ERROR)
            else:
                shutil.rmtree(backupDir, ignore_errors=True)
                _failWithLog(
                    "AFC 工具更新失败",
                    f"更新失败：{applyErr}，已回滚到原版本",
                    f"{toolName}: {applyErr}（已回滚 {restored} 个文件）",
                )
            return "failed"

        # K3 全成功：删备份
        if os.path.exists(backupDir):
            shutil.rmtree(backupDir, ignore_errors=True)
        counts = f"新增 {len(diff['added'])} / 变更 {len(diff['changed'])} / 恢复 {len(diff['missing'])} / 删除 {len(diff['deleted'])}"
        printColor(Color.GREEN, f"\n[OK] 工具 {toolName} 已更新（{counts}）\n")
        printColor(Color.YELLOW, "提示：重启 bot 后生效\n")
        _log("AFC 工具更新成功", f"{toolName}: v{localVer}→v{remoteVer}, {counts}")
        return "updated"

    except Exception as e:
        return _fail(f"更新过程出现异常：{e}")
    finally:
        # 暂存目录与未完成的 <target>.tmp 在任何异常路径都不残留；
        # 备份目录不在此清——由 K2（回滚结果）与 K3（成功）决定去留
        if tempDir and os.path.exists(tempDir):
            shutil.rmtree(tempDir, ignore_errors=True)
        for tmpPath in pendingTmps:
            if os.path.exists(tmpPath):
                try:
                    os.remove(tmpPath)
                except OSError:
                    pass


def cmdUpdateAll(checkOnly, assumeYes):
    """批量更新全部第三方工具（--all 需配合 --check 或 --yes，由 main 层保证）。

    返回 outcomes 统计 dict，供 main 决定退出码（有失败则非零）。"""
    from utils.afc.toolManager import loadCustomAfcTools

    customTools = loadCustomAfcTools()
    if not customTools:
        printColor(Color.DIM, "- 没有已安装的第三方工具\n")
        return {"updated": 0, "uptodate": 0, "failed": 0, "cancelled": 0}

    outcomes = {"updated": 0, "uptodate": 0, "failed": 0, "cancelled": 0}
    for toolName in sorted(customTools):
        printColor(Color.CYAN, f"\n=== {toolName} ===")
        outcome = cmdUpdate(toolName, checkOnly=checkOnly, assumeYes=assumeYes)
        if outcome in outcomes:
            outcomes[outcome] += 1
        else:
            outcomes["failed"] += 1

    print(
        f"\n共 {len(customTools)} 个：更新 {outcomes['updated']} / "
        f"已是最新 {outcomes['uptodate']} / 失败 {outcomes['failed']} / 取消 {outcomes['cancelled']}"
    )
    return outcomes




# ==============================================================================
# uninstall 命令
# ==============================================================================

def _safeRemoveToolFile(toolId, filePath, baseSubdir, requireSubdir):
    """
    删除工具文件前的路径校验。返回 True 可删，False 逃逸需跳过。

    不信任存储的 manifest.files 原文——data/afcToolsCustom.json 可能被篡改，
    删除前必须重新 realpath+commonpath 校验，防止误删项目/系统文件。
    """
    try:
        _assertSafeToolPath(toolId, filePath, baseSubdir, requireSubdir=requireSubdir)
        return True
    except ValueError:
        return False


def cmdUninstall(toolName, force):
    """卸载工具"""
    from utils.afc.toolManager import (
        getAllAfcTools, loadCustomAfcTools, saveCustomAfcTools,
    )
    from utils.core.logger import LogLevel

    # toolName 先过 id 校验：它要拼路径，custom json 键名可能被篡改（与 cmdUpdate 同款防御）
    if not _TOOL_ID_RE.match(toolName):
        printColor(Color.RED, f"[X] 工具名格式无效（只允许小写字母、数字、下划线）：{toolName}")
        return

    allTools = getAllAfcTools()
    if toolName not in allTools:
        printColor(Color.RED, f"[X] 工具 {toolName} 不存在")
        return

    info = allTools[toolName]
    source = info.get("source", "?")
    manifest = info.get("manifest") or {}

    # ── builtin：默认引导用 disable，-f 才删 ──
    if source == "builtin" and not force:
        printColor(Color.YELLOW, f"\n[!] {toolName} 是内置工具\n")
        print("  disable       只禁用，不删除文件（推荐）")
        print("  uninstall -f  强制删除文件（不可逆，需重新拉取代码才能恢复）\n")
        return

    # ── local-draft：无 manifest，-f 删整个目录 ──
    if source == "local-draft":
        if not force:
            printColor(Color.YELLOW, f"\n[!] {toolName} 是未登记的 local-draft 工具\n")
            print("  uninstall -f  删除整个工具目录和对应测试文件\n")
            return
        toolDir = os.path.join(PROJECT_ROOT, _TOOLS_DIR_REL, toolName)
        testFile = os.path.join(PROJECT_ROOT, _TESTS_TOOLS_DIR_REL, f"test_{toolName}.py")
        shutil.rmtree(toolDir, ignore_errors=True)
        if os.path.exists(testFile):
            os.remove(testFile)
        printColor(Color.GREEN, f"\n[OK] local-draft 工具 {toolName} 已删除\n")
        _log("AFC 工具卸载成功", f"{toolName} (local-draft)")
        return

    # ── builtin(-f) / custom：按 manifest 删文件，删前逐个校验路径 ──
    files = list(manifest.get("files", []))
    testFile = manifest.get("testFile")
    if testFile:
        files.append(testFile)

    if not files:
        printColor(Color.RED, f"[X] 工具 {toolName} 的 manifest 没有 files 记录")
        return

    print(f"正在卸载工具 {toolName}...")
    deletedCount = 0
    skippedCount = 0

    for filePath in files:
        # testFile 是扁平的 test_<id>.py（不在 <id>/ 子目录），其余文件在 <id>/ 子目录
        isTest = (filePath == testFile)
        baseDir = _TESTS_TOOLS_DIR_REL if isTest else _TOOLS_DIR_REL
        requireSubdir = not isTest

        if not _safeRemoveToolFile(toolName, filePath, baseDir, requireSubdir):
            skippedCount += 1
            printColor(Color.YELLOW, f"  [SKIP] 跳过非法路径：{filePath}")
            _log("AFC 工具卸载路径逃逸", f"{toolName}: {filePath}", LogLevel.WARNING)
            continue

        absPath = os.path.join(PROJECT_ROOT, filePath)
        if not os.path.exists(absPath):
            continue
        try:
            os.remove(absPath)
            deletedCount += 1
            print(f"  [OK] 删除 {filePath}")
        except OSError as e:
            printColor(Color.YELLOW, f"  [!] 删除 {filePath} 失败：{e}")

    # 顺手清掉残留的 afcTool.json：它不在 manifest 的 files 清单里，
    # 但留在目录里会让工具被当成 local-draft「复活」。
    # toolName 过一遍 id 校验再拼路径——custom json 可能被篡改，不信任其 key。
    toolDir = os.path.join(PROJECT_ROOT, _TOOLS_DIR_REL, toolName)
    if _TOOL_ID_RE.match(toolName):
        leftoverManifest = os.path.join(toolDir, "afcTool.json")
        if os.path.exists(leftoverManifest):
            try:
                os.remove(leftoverManifest)
                deletedCount += 1
                print(f"  [OK] 删除 {os.path.join(_TOOLS_DIR_REL, toolName, 'afcTool.json')}")
            except OSError as e:
                printColor(Color.YELLOW, f"  [!] 删除 afcTool.json 失败：{e}")

    # 清理空目录
    if os.path.isdir(toolDir) and not os.listdir(toolDir):
        os.rmdir(toolDir)

    # 从 custom json 移除（builtin 不在 custom，无需处理）
    if source != "builtin":
        customTools = loadCustomAfcTools()
        if toolName in customTools:
            del customTools[toolName]
            saveCustomAfcTools(customTools)

    printColor(Color.GREEN, f"\n[OK] 工具 {toolName} 已卸载")
    print(f"  删除 {deletedCount} 个文件", end="")
    if skippedCount > 0:
        print(f"，跳过 {skippedCount} 个非法路径")
    else:
        print()
    if source == "builtin":
        printColor(Color.YELLOW, "注意：内置工具文件已删除，如需恢复请重新拉取代码\n")
    _log("AFC 工具卸载成功", f"{toolName} ({source}): 删除 {deletedCount} 个文件，跳过 {skippedCount} 个")




# ==============================================================================
# main
# ==============================================================================

def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="ZincNya Bot AFC 工具管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    listParser = subparsers.add_parser("list", help="列出所有工具")
    listParser.add_argument("-a", "--all", action="store_true", help="显示所有工具（默认）")
    listParser.add_argument("-e", "--enabled", action="store_true", help="只显示已启用的工具")
    listParser.add_argument("-d", "--disabled", action="store_true", help="只显示已禁用的工具")
    listParser.add_argument("--full", action="store_true", help="显示触发词和文件数")

    showParser = subparsers.add_parser("show", help="显示工具详细信息")
    showParser.add_argument("name", help="工具名")

    subparsers.add_parser("check", help="检查所有工具的完整性")

    enableParser = subparsers.add_parser("enable", help="启用工具")
    enableParser.add_argument("name", help="工具名")

    disableParser = subparsers.add_parser("disable", help="禁用工具")
    disableParser.add_argument("name", help="工具名")

    newParser = subparsers.add_parser("new", help="脚手架生成新工具")
    newParser.add_argument("name", help="工具名（小写字母、数字、下划线）")

    installParser = subparsers.add_parser("install", help="从 GitHub 安装第三方工具")
    installParser.add_argument("url", help="GitHub 仓库 URL（https://github.com/<user>/<repo>）")
    installParser.add_argument("--branch", help="指定分支（默认依次尝试 main / master）")
    installParser.add_argument("-f", "--force", action="store_true", help="覆盖安装已存在的工具")

    updateParser = subparsers.add_parser("update", help="从 GitHub 更新已安装的第三方工具")
    updateParser.add_argument("name", nargs="?", help="工具名")
    updateParser.add_argument("--all", action="store_true", help="更新全部第三方工具（需配合 --check 或 --yes）")
    updateParser.add_argument("--check", action="store_true", help="只检查更新，不落盘")
    updateParser.add_argument("-y", "--yes", action="store_true", help="跳过确认")

    uninstallParser = subparsers.add_parser("uninstall", help="卸载工具")
    uninstallParser.add_argument("name", help="工具名")
    uninstallParser.add_argument("-f", "--force", action="store_true", help="强制删除（内置/local-draft 必需）")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "list":
        cmdList(args)
    elif args.command == "show":
        cmdShow(args.name)
    elif args.command == "check":
        cmdCheck()
    elif args.command == "enable":
        cmdEnable(args.name)
    elif args.command == "disable":
        cmdDisable(args.name)
    elif args.command == "new":
        cmdNew(args.name)
    elif args.command == "install":
        if not cmdInstall(args.url, args.branch, args.force):
            sys.exit(1)
    elif args.command == "update":
        if args.all and args.name:
            printColor(Color.RED, "[X] update 的工具名与 --all 互斥")
            return
        if args.all and not (args.check or args.yes):
            printColor(Color.RED, "[X] --all 需要配合 --check 或 --yes 使用")
            return
        if args.all:
            outcomes = cmdUpdateAll(args.check, args.yes)
            if outcomes["failed"] > 0:
                sys.exit(1)
        elif not args.name:
            printColor(Color.RED, "[X] 请指定工具名，或用 --all 更新全部（配合 --check 或 --yes）")
            return
        else:
            if cmdUpdate(args.name, checkOnly=args.check, assumeYes=args.yes) == "failed":
                sys.exit(1)
    elif args.command == "uninstall":
        cmdUninstall(args.name, args.force)


if __name__ == "__main__":
    main()
