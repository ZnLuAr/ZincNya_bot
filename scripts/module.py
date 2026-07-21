#!/usr/bin/env python3
"""
scripts/module.py

ZincNya Bot 模块管理 CLI 工具。

用法：
    python scripts/module.py list [-a|--all] [-e|--enabled] [-d|--disabled]
    python scripts/module.py install <name_or_url> [--branch <branch>]
    python scripts/module.py uninstall <name> [-f|--force] [-s|--soft]
    python scripts/module.py enable <name>
    python scripts/module.py disable <name>
    python scripts/module.py show <name>
    python scripts/module.py validate
    python scripts/module.py scan
    python scripts/module.py help
"""

import os
import sys
import json
import re
import argparse
import tempfile
import shutil
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
    return True


_USE_COLOR = supportsColor()


def printColor(color, text):
    """打印彩色文本"""
    if not _USE_COLOR:
        print(text)
    else:
        print(f"{color}{text}{Color.RESET}")


def _assertSafeModulePath(filePath):
    """校验模块文件路径安全性（防路径遍历 / 绝对路径吞并 / 逃逸项目根），非法时抛 ValueError"""
    norm = filePath.replace("\\", "/")

    if ".." in norm.split("/") or os.path.isabs(norm):
        raise ValueError(f"非法文件路径：{filePath}")

    if not (norm.startswith("handlers/") or norm.startswith("utils/")):
        raise ValueError(f"文件路径必须以 handlers/ 或 utils/ 开头：{filePath}")

    dest = os.path.realpath(os.path.join(PROJECT_ROOT, filePath))
    root = os.path.realpath(PROJECT_ROOT)

    if os.path.commonpath([dest, root]) != root:
        raise ValueError(f"路径逃逸出项目根：{filePath}")


def cmdList(args):
    """列出所有模块"""
    from utils.moduleManager import getAllModules

    allModules = getAllModules()

    # 根据 --all/--enabled/--disabled 过滤
    if args.enabled:
        modules = {k: v for k, v in allModules.items() if v.get("enabled", True)}
    elif args.disabled:
        modules = {k: v for k, v in allModules.items() if not v.get("enabled", True)}
    else:
        modules = allModules

    if not modules:
        printColor(Color.DIM, "- 没有找到模块\n")
        return

    # 分组显示
    enabledModules = {k: v for k, v in modules.items() if v.get("enabled", True)}
    disabledModules = {k: v for k, v in modules.items() if not v.get("enabled", True)}

    if enabledModules:
        printColor(Color.GREEN, f"\n已启用模块（{len(enabledModules)} 个）：")
        for name, cfg in enabledModules.items():
            meta = cfg["metadata"]
            source = cfg.get("source", "builtin")
            print(f"  {Color.GREEN}[+]{Color.RESET} {name:15} {meta['name']} (v{meta['version']}) [{source}]")

    if disabledModules:
        printColor(Color.DIM, f"\n已禁用模块（{len(disabledModules)} 个）：")
        for name, cfg in disabledModules.items():
            meta = cfg["metadata"]
            source = cfg.get("source", "builtin")
            print(f"  {Color.DIM}[-]{Color.RESET} {name:15} {meta['name']} (v{meta['version']}) [{source}]")

    print()


def cmdEnable(moduleName):
    """启用模块"""
    from utils.moduleManager import (
        getAllModules,
        loadModulesConfig,
        saveModulesConfig,
        loadUninstalledBuiltins,
    )

    allModules = getAllModules()

    if moduleName not in allModules:
        # 区分"已卸载的内置模块"和"完全不存在"
        if moduleName in loadUninstalledBuiltins():
            printColor(Color.RED, f"[X] 内置模块 {moduleName} 已被卸载")
            printColor(Color.YELLOW, "提示：从 data/modulesUninstalled.json 移除该条目并恢复对应代码后即可重新启用")
        else:
            printColor(Color.RED, f"[X] 模块 {moduleName} 不存在")
        return

    config = loadModulesConfig()

    if moduleName not in config:
        config[moduleName] = {}

    config[moduleName]["enabled"] = True

    if saveModulesConfig(config):
        printColor(Color.GREEN, f"\n[OK] 模块 {moduleName} 已启用\n")
        printColor(Color.YELLOW, "提示：重启 bot 后生效\n")
    else:
        printColor(Color.RED, "[X] 保存配置失败")


def cmdDisable(moduleName):
    """禁用模块"""
    from utils.moduleManager import getAllModules, loadModulesConfig, saveModulesConfig

    allModules = getAllModules()

    if moduleName not in allModules:
        printColor(Color.RED, f"[X] 模块 {moduleName} 不存在")
        return

    config = loadModulesConfig()

    if moduleName not in config:
        config[moduleName] = {}

    config[moduleName]["enabled"] = False

    if saveModulesConfig(config):
        printColor(Color.GREEN, f"\n[OK] 模块 {moduleName} 已禁用\n")
        printColor(Color.YELLOW, "提示：重启 bot 后生效\n")
    else:
        printColor(Color.RED, "[X] 保存配置失败")


def cmdShow(moduleName):
    """显示模块详细信息"""
    from utils.moduleManager import getAllModules

    allModules = getAllModules()

    if moduleName not in allModules:
        printColor(Color.RED, f"[X] 模块 {moduleName} 不存在")
        return

    cfg = allModules[moduleName]
    meta = cfg["metadata"]

    printColor(Color.CYAN, f"\n模块：{meta['name']} ({moduleName})")
    print(f"版本：{meta['version']}")
    print(f"作者：{meta.get('author', 'N/A')}")
    if cfg.get("enabled", True):
        printColor(Color.GREEN, "状态：已启用")
    else:
        printColor(Color.DIM, "状态：已禁用")
    print(f"来源：{cfg.get('source', 'builtin')}")

    if cfg.get("installedAt"):
        print(f"安装时间：{cfg['installedAt']}")

    print(f"\n描述：")
    print(f"  {meta.get('description', 'N/A')}")

    print(f"\n文件：")
    for f in meta.get("files", []):
        isHandler = f in meta.get("handlers", [])
        marker = f"{Color.CYAN}[handler]{Color.RESET}" if isHandler else ""
        print(f"  - {f} {marker}")

    if meta.get("initFunctions"):
        print(f"\n初始化函数：")
        for func in meta["initFunctions"]:
            print(f"  - {func}")

    if meta.get("backgroundTasks"):
        print(f"\n后台任务：")
        for task in meta["backgroundTasks"]:
            print(f"  - {task}")

    if meta.get("dependencies"):
        print(f"\n依赖：")
        for dep in meta["dependencies"]:
            print(f"  - {dep}")

    print()


def cmdValidate():
    """验证所有模块的文件是否存在"""
    from utils.moduleManager import getAllModules

    allModules = getAllModules()

    print("正在验证模块文件...")

    hasError = False

    for moduleName, cfg in allModules.items():
        meta = cfg["metadata"]
        allFiles = meta.get("files", [])

        missingFiles = []

        for filePath in allFiles:
            # data/*.json 一律跳过存在性检查：afcTools.json / afcToolsCustom.json 是运行时
            # 生成的本地状态，初始可不存在；afcToolsBuiltin.json 虽随仓库提供（.gitignore
            # 不忽略），缺失时由 git 追踪与 toolManager 空 manifest 兜底，故同样不视为缺失
            normalized = filePath.replace("\\", "/")
            if normalized.startswith("data/") and normalized.endswith(".json"):
                continue

            fullPath = os.path.join(PROJECT_ROOT, filePath)
            if not os.path.exists(fullPath):
                missingFiles.append(filePath)

        if missingFiles:
            hasError = True
            printColor(Color.RED, f"\n[X] 模块 {moduleName} ({meta['name']})：")
            for f in missingFiles:
                print(f"  - 缺失文件：{f}")

    if not hasError:
        printColor(Color.GREEN, "\n[OK] 所有模块文件完整\n")
    else:
        printColor(Color.RED, "\n- 发现缺失文件\n")


def cmdScan():
    """扫描 handlers/ 和 utils/ 目录，查找未被管理的文件"""
    from utils.moduleManager import getAllModules

    allModules = getAllModules()

    # 收集所有被管理的文件
    managedFiles = set()
    for cfg in allModules.values():
        for f in cfg["metadata"].get("files", []):
            managedFiles.add(f)

    # 扫描 handlers/ 目录
    handlersDir = os.path.join(PROJECT_ROOT, "handlers")
    unmanagedHandlers = []

    for filename in os.listdir(handlersDir):
        if filename.endswith(".py") and filename != "__init__.py":
            filePath = f"handlers/{filename}"
            if filePath not in managedFiles and filename != "cli.py":
                unmanagedHandlers.append(filePath)

    # 扫描 utils/ 目录（只扫描顶层 .py 文件，避免递归太深）
    utilsDir = os.path.join(PROJECT_ROOT, "utils")
    unmanagedUtils = []

    for filename in os.listdir(utilsDir):
        if filename.endswith(".py") and filename != "__init__.py":
            filePath = f"utils/{filename}"
            if filePath not in managedFiles:
                unmanagedUtils.append(filePath)

    # 输出结果
    if unmanagedHandlers or unmanagedUtils:
        printColor(Color.YELLOW, "\n发现未被管理的文件：\n")

        if unmanagedHandlers:
            print("handlers/:")
            for f in unmanagedHandlers:
                print(f"  - {f}")

        if unmanagedUtils:
            print("\nutils/:")
            for f in unmanagedUtils:
                print(f"  - {f}")

        printColor(Color.YELLOW, "\n提示：这些文件未在 modulesRegistry.py 中注册\n")
    else:
        printColor(Color.GREEN, "[OK] 所有文件都已被管理\n")


def installFromGitHub(repoUrl, branch=None):
    """从 GitHub 下载并安装模块"""
    from utils.moduleManager import loadCustomModules, saveCustomModules

    # 1. 解析 GitHub URL
    match = re.match(r'https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$', repoUrl)
    if not match:
        printColor(Color.RED, "[X] 无效的 GitHub URL")
        return False

    user, repo = match.groups()

    # 2. 确定分支（优先级：--branch 参数 > 尝试 main > 尝试 master）
    if branch:
        branches = [branch]
    else:
        branches = ["main", "master"]

    metadata = None
    selectedBranch = None

    for tryBranch in branches:
        rawUrl = f"https://raw.githubusercontent.com/{user}/{repo}/{tryBranch}"

        try:
            metadataUrl = f"{rawUrl}/module.json"
            with urllib.request.urlopen(metadataUrl, timeout=15) as response:
                metadata = json.loads(response.read().decode('utf-8'))
                selectedBranch = tryBranch
                break
        except Exception:
            continue

    if not metadata:
        if branch:
            printColor(Color.RED, f"[X] 分支 {branch} 不存在或无法访问")
        else:
            printColor(Color.RED, "[X] 无法找到 main 或 master 分支")
        return False

    print(f"正在从 {repoUrl} (分支: {selectedBranch}) 下载模块...")
    rawUrl = f"https://raw.githubusercontent.com/{user}/{repo}/{selectedBranch}"

    # 3. 校验文件路径（安全检查）
    allFiles = metadata.get("files", [])
    for filePath in allFiles:
        try:
            _assertSafeModulePath(filePath)
        except ValueError as e:
            printColor(Color.RED, f"[X] {e}")
            return False

    # 4. 提取模块名（从 metadata.id 字段）
    moduleName = metadata.get("id")
    if not moduleName:
        printColor(Color.RED, "[X] module.json 中缺少 id 字段")
        return False

    # 校验模块名格式（只允许字母、数字、下划线）
    if not re.match(r'^[a-z0-9_]+$', moduleName):
        printColor(Color.RED, f"[X] 模块 id 格式无效（只允许小写字母、数字、下划线）：{moduleName}")
        return False

    # 5. 下载所有文件到临时目录
    tempDir = tempfile.mkdtemp()

    print(f"下载 {len(allFiles)} 个文件...")

    for filePath in allFiles:
        try:
            fileUrl = f"{rawUrl}/{filePath}"
            destPath = os.path.join(tempDir, filePath)
            os.makedirs(os.path.dirname(destPath), exist_ok=True)

            urllib.request.urlretrieve(fileUrl, destPath)
            print(f"  [OK] {filePath}")
        except Exception as e:
            printColor(Color.RED, f"[X] 下载 {filePath} 失败：{e}")
            shutil.rmtree(tempDir)
            return False

    # 6. 校验（检查 handler 文件是否有 register() 函数）
    for handlerFile in metadata.get("handlers", []):
        handlerPath = os.path.join(tempDir, handlerFile)
        try:
            with open(handlerPath, 'r', encoding='utf-8') as f:
                content = f.read()
                if 'def register(' not in content:
                    printColor(Color.RED, f"[X] {handlerFile} 缺少 register() 函数")
                    shutil.rmtree(tempDir)
                    return False
        except Exception as e:
            printColor(Color.RED, f"[X] 校验 {handlerFile} 失败：{e}")
            shutil.rmtree(tempDir)
            return False

    # 7. 移动到项目目录
    print("安装文件...")
    for filePath in allFiles:
        srcPath = os.path.join(tempDir, filePath)
        destPath = os.path.join(PROJECT_ROOT, filePath)
        os.makedirs(os.path.dirname(destPath), exist_ok=True)
        shutil.move(srcPath, destPath)
        print(f"  [OK] {filePath}")

    shutil.rmtree(tempDir)

    # 8. 更新 modulesCustom.json
    customModules = loadCustomModules()
    customModules[moduleName] = {
        "enabled": True,
        "metadata": metadata,
        "source": f"github:{repoUrl}@{selectedBranch}",
        "installedAt": datetime.now().isoformat()
    }

    if not saveCustomModules(customModules):
        printColor(Color.RED, "[X] 保存配置失败")
        return False

    printColor(Color.GREEN, f"\n[OK] 模块 {moduleName} 安装成功\n")
    printColor(Color.YELLOW, "提示：重启 bot 后生效\n")
    return True


def cmdInstall(nameOrUrl, branch=None):
    """安装模块"""
    if nameOrUrl.startswith("http"):
        # GitHub URL
        installFromGitHub(nameOrUrl, branch)
    else:
        # 本地路径（暂不实现）
        printColor(Color.RED, "[X] 暂不支持从本地路径安装")


def cmdUninstall(moduleName, force=False, soft=False):
    """卸载模块"""
    from utils.moduleManager import (
        getAllModules,
        loadCustomModules,
        saveCustomModules,
        loadUninstalledBuiltins,
        saveUninstalledBuiltins,
    )

    allModules = getAllModules()

    if moduleName not in allModules:
        printColor(Color.RED, f"[X] 模块 {moduleName} 不存在")
        return

    cfg = allModules[moduleName]
    meta = cfg["metadata"]
    isBuiltin = cfg.get("source") == "builtin"

    # 内置模块：必须使用 --force 才能卸载
    if isBuiltin and not force:
        printColor(Color.YELLOW, f"\n[!] {moduleName} 是内置模块\n")
        print("  disable      只禁用，不删除文件（推荐）")
        print("  uninstall -f 强制删除文件（不可逆，需重新拉取代码才能恢复）\n")
        return

    # 检查共享文件冲突
    allFiles = meta.get("files", [])
    sharedFiles = []  # 被其他启用模块使用的文件
    exclusiveFiles = []  # 只被当前模块使用的文件

    for filePath in allFiles:
        isShared = False
        sharedWith = []

        for otherName, otherCfg in allModules.items():
            if otherName == moduleName:
                continue
            if not otherCfg.get("enabled", True):
                continue  # 跳过已禁用的模块
            if filePath in otherCfg["metadata"].get("files", []):
                isShared = True
                sharedWith.append(otherName)

        if isShared:
            sharedFiles.append((filePath, sharedWith))
        else:
            exclusiveFiles.append(filePath)

    # 如果有共享文件且未使用 --force 或 --soft，警告并退出
    if sharedFiles and not force and not soft:
        printColor(Color.RED, "[X] 以下文件正被其他启用模块使用：\n")
        for filePath, modules in sharedFiles:
            moduleList = ", ".join(modules)
            print(f"  {Color.RED}[X]{Color.RESET} {filePath} (被 {moduleList} 使用)")

        printColor(Color.YELLOW, "\n提示：")
        print("  --soft/-s  只删除不被其他模块使用的文件（保留共享文件）")
        print("  --force/-f 强制删除所有文件（可能导致其他模块无法使用）\n")
        return

    # 删除文件
    print(f"正在卸载模块 {moduleName}...")

    deletedCount = 0
    skippedCount = 0

    for filePath in allFiles:
        # 存储的 metadata 可能被篡改，删除前必须重新校验路径，防止误删项目/系统文件
        try:
            _assertSafeModulePath(filePath)
        except ValueError as e:
            printColor(Color.YELLOW, f"  [SKIP] 跳过非法路径 {filePath}：{e}")
            continue

        fullPath = os.path.join(PROJECT_ROOT, filePath)
        if not os.path.exists(fullPath):
            continue

        # 检查是否为共享文件
        isShared = any(f == filePath for f, _ in sharedFiles)

        # soft 模式下跳过共享文件
        if soft and isShared:
            sharedWith = next((modules for f, modules in sharedFiles if f == filePath), [])
            moduleList = ", ".join(sharedWith)
            print(f"  {Color.YELLOW}[SKIP]{Color.RESET} 跳过 {filePath} (被 {moduleList} 使用)")
            skippedCount += 1
            continue

        try:
            os.remove(fullPath)
            if isShared:
                print(f"  {Color.RED}[OK]{Color.RESET} 删除 {filePath} (共享文件)")
            else:
                print(f"  [OK] 删除 {filePath}")
            deletedCount += 1
        except Exception as e:
            print(f"  {Color.YELLOW}!{Color.RESET} 删除 {filePath} 失败：{e}")

    # 从 modulesCustom.json 移除（自定义模块）
    # 或加入 modulesUninstalled.json（内置模块）
    if isBuiltin:
        uninstalledList = loadUninstalledBuiltins()
        if moduleName not in uninstalledList:
            uninstalledList.append(moduleName)
            saveUninstalledBuiltins(uninstalledList)
    else:
        customModules = loadCustomModules()
        if moduleName in customModules:
            del customModules[moduleName]
            saveCustomModules(customModules)

    printColor(Color.GREEN, f"\n[OK] 模块 {moduleName} 已卸载")
    print(f"  删除 {deletedCount} 个文件", end="")
    if skippedCount > 0:
        print(f"，跳过 {skippedCount} 个共享文件")
    else:
        print()
    if isBuiltin:
        printColor(Color.YELLOW, "注意：内置模块已加入卸载名单（data/modulesUninstalled.json），重新拉取代码并删除该条目即可恢复")
    printColor(Color.YELLOW, "注意：数据库文件未删除，如需清理请手动删除")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="ZincNya Bot 模块管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # list 命令
    listParser = subparsers.add_parser("list", help="列出所有模块")
    listParser.add_argument("-a", "--all", action="store_true", help="显示所有模块（默认）")
    listParser.add_argument("-e", "--enabled", action="store_true", help="只显示已启用的模块")
    listParser.add_argument("-d", "--disabled", action="store_true", help="只显示已禁用的模块")

    # install 命令
    installParser = subparsers.add_parser("install", help="安装模块")
    installParser.add_argument("name_or_url", help="模块名或 GitHub URL")
    installParser.add_argument("--branch", help="指定 GitHub 分支")

    # uninstall 命令
    uninstallParser = subparsers.add_parser("uninstall", help="卸载模块")
    uninstallParser.add_argument("name", help="模块名")
    uninstallParser.add_argument("-f", "--force", action="store_true", help="强制删除所有文件（包括共享文件）")
    uninstallParser.add_argument("-s", "--soft", action="store_true", help="只删除独占文件，跳过共享文件")

    # enable 命令
    enableParser = subparsers.add_parser("enable", help="启用模块")
    enableParser.add_argument("name", help="模块名")

    # disable 命令
    disableParser = subparsers.add_parser("disable", help="禁用模块")
    disableParser.add_argument("name", help="模块名")

    # show 命令
    showParser = subparsers.add_parser("show", help="显示模块详细信息")
    showParser.add_argument("name", help="模块名")

    # validate 命令
    subparsers.add_parser("validate", help="验证所有模块的文件是否存在")

    # scan 命令
    subparsers.add_parser("scan", help="扫描未被管理的文件")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # 执行命令
    if args.command == "list":
        cmdList(args)
    elif args.command == "install":
        cmdInstall(args.name_or_url, args.branch)
    elif args.command == "uninstall":
        cmdUninstall(args.name, args.force, args.soft)
    elif args.command == "enable":
        cmdEnable(args.name)
    elif args.command == "disable":
        cmdDisable(args.name)
    elif args.command == "show":
        cmdShow(args.name)
    elif args.command == "validate":
        cmdValidate()
    elif args.command == "scan":
        cmdScan()


if __name__ == "__main__":
    main()