"""
utils/command/log.py

日志管理命令，用于查看、清理和删除日志文件。

用法：
    /log                    显示日志列表（带序号）
    /log -t <序号>          查看某个日志内容
    /log -c                 清理空日志（只有启动信息的）
    /log -c --all           清空所有日志（需要确认）
    /log -d <序号>          删除指定日志

"""




import os
from typing import List, Tuple

from config import LOG_DIR
from handlers.cli import parseArgsTokens




def _getLogFiles() -> List[Tuple[str, int, str]]:
    """
    获取所有日志文件信息

    返回：
        列表，每项为 (文件名, 行数, 类型)
        类型为 "log" 或 "error"
    """
    if not os.path.exists(LOG_DIR):
        return []

    files = []
    for f in sorted(os.listdir(LOG_DIR)):
        if not f.endswith(".log"):
            continue

        path = os.path.join(LOG_DIR, f)
        try:
            with open(path, "r", encoding="utf-8") as file:
                lines = len([l for l in file.readlines() if l.strip()])
        except Exception:
            lines = 0

        logType = "error" if f.startswith("error_") else "log"
        files.append((f, lines, logType))

    return files


def _isEmptyLog(filename: str) -> bool:
    """检查日志是否为空（只有启动信息）"""
    path = os.path.join(LOG_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        return len(lines) <= 2
    except Exception:
        return False


def _listLogs():
    """显示日志列表"""
    files = _getLogFiles()

    if not files:
        print("…… …… ないですニャー\n")
        return

    print("\n日志文件列表：")
    print("─" * 50)

    for i, (name, lines, logType) in enumerate(files, 1):
        if logType == "error":
            typeStr = "错误日志"
        else:
            typeStr = "操作日志"

        # 标记空日志
        emptyMark = " (空)" if _isEmptyLog(name) else ""
        print(f"  {i:3}. {name:<30} {lines:>4} 行  [{typeStr}]{emptyMark}\n")

    print("─" * 50)
    print(f"共 {len(files)} 个日志文件\n")


def _viewLog(index: int):
    """查看指定日志内容"""
    files = _getLogFiles()

    if not files:
        print("没有找到任何日志文件喵……\n")
        return

    if index < 1 or index > len(files):
        print(f"序号无效喵！请输入 1 到 {len(files)} 之间的数字\n")
        return

    filename = files[index - 1][0]
    path = os.path.join(LOG_DIR, filename)

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        print(f"\n═══ {filename} ═══\n")
        print(content)
        print(f"\n═══ EOF ═══\n")
    except Exception as e:
        print(f"读取日志失败喵：{e}\n")


def _cleanEmptyLogs():
    """清理空日志"""
    files = _getLogFiles()
    deleted = 0

    for name, _, _ in files:
        if _isEmptyLog(name):
            path = os.path.join(LOG_DIR, name)
            try:
                os.remove(path)
                deleted += 1
            except Exception:
                pass

    if deleted > 0:
        print(f"已清理 {deleted} 个空日志文件喵——\n")
    else:
        print("没有找到空日志文件喵……\n")


def _clearAllLogs(confirmed: bool = False):
    """清空所有日志"""
    if not confirmed:
        print("⚠️ 这将删除所有日志文件！")
        print("如果确定要清空，请输入：/log -c --all --confirm\n")
        return

    files = _getLogFiles()
    deleted = 0

    for name, _, _ in files:
        path = os.path.join(LOG_DIR, name)
        try:
            os.remove(path)
            deleted += 1
        except Exception:
            pass

    print(f"清空 {deleted} 个日志文件喵……\n")


def _deleteLog(index: int):
    """删除指定日志"""
    files = _getLogFiles()

    if not files:
        print("あっ …… ないですニャー……\n")
        return

    if index < 1 or index > len(files):
        print(f"序号无效喵——\n要输入 1 到 {len(files)} 之间的数字——\n")
        return

    filename = files[index - 1][0]
    path = os.path.join(LOG_DIR, filename)

    try:
        os.remove(path)
        print(f"已删除 {filename} 喵——\n")
    except Exception as e:
        print(f"删除失败喵：{e}\n")




async def execute(app, args):
    """命令入口"""

    parsed = {
        "type": None,       # -t <序号> 查看日志
        "clean": False,     # -c 清理空日志
        "all": False,       # --all 配合 -c 使用
        "confirm": False,   # --confirm 确认清空所有
        "del": None,        # -d <序号> 删除日志
    }

    argAlias = {
        "t": "type",
        "c": "clean",
        "d": "del",
    }

    parsed = parseArgsTokens(parsed, args, argAlias)

    # 查看日志内容
    if parsed["type"] is not None:
        try:
            index = int(parsed["type"])
            _viewLog(index)
        except ValueError:
            print("请提供有效的日志序号喵……就像 /log -t 1 这样的啦……\n")
        return

    # 删除指定日志
    if parsed["del"] is not None:
        try:
            index = int(parsed["del"])
            _deleteLog(index)
        except ValueError:
            print("请提供有效的日志序号喵……就像 /log -d 1 这样的啦……\n")
        return

    # 清理日志
    if parsed["clean"]:
        if parsed["all"]:
            _clearAllLogs(confirmed=parsed["confirm"])
        else:
            _cleanEmptyLogs()
        return

    # 默认显示列表
    _listLogs()




def getHelp():
    return {
        "name": "/log",

        "description": "管理日志文件",

        "usage": (
            "/log                    显示日志列表\n"
            "/log -t <序号>          查看某个日志内容\n"
            "/log -c                 清理空日志（只有启动信息的）\n"
            "/log -c --all --confirm 清空所有日志\n"
            "/log -d <序号>          删除指定日志"
        ),

        "example": (
            "查看日志列表：/log\n"
            "查看第 3 个日志：/log -t 3\n"
            "清理空日志：/log -c\n"
            "删除第 5 个日志：/log -d 5"
        ),
    }
