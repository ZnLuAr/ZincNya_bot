"""
utils/terminalUI.py

通用终端 UI 工具模块，提供跨平台的终端控制功能。

主要功能：
    - 备用屏幕缓冲区管理
    - ANSI 转义序列封装
    - 跨平台兼容性处理
    - 终端尺寸获取


================================================================================
使用示例
================================================================================

# 1. 使用备用屏幕（推荐用于全屏交互界面）
with AlternateScreen():
    print("这里的内容在备用屏幕上")
    print("退出后会自动清理，不污染主屏幕")
# 退出后自动恢复到主屏幕

# 2. 手动控制光标
cursorUp(5)        # 光标上移 5 行
clearLine()        # 清除当前行
cursorToHome()    # 光标移到左上角

# 3. 获取终端尺寸
width, height = getTerminalSize()


================================================================================
跨平台兼容性
================================================================================

本模块在导入时会自动检测平台并启用必要的功能：
    - Linux/macOS: 原生支持所有 ANSI 转义序列
    - Windows 10+: 自动启用 ANSI 支持
    - Windows 7-9: 尽力支持，部分功能可能不可用

"""

import os
import shutil
import sys
from typing import Tuple


# ============================================================================
# 平台兼容性初始化
# ============================================================================

def enableAnsiOnWindows():
    """
    在 Windows 上启用 ANSI 转义序列支持。

    Windows 10+ 默认支持 ANSI，但需要显式启用。
    对于旧版 Windows，此函数会尝试启用但可能失败。
    """
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # STD_OUTPUT_HANDLE = -11
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            # ENABLE_PROCESSED_OUTPUT = 0x0001
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            mode.value |= 0x0004 | 0x0001
            kernel32.SetConsoleMode(handle, mode)
        except Exception:
            # 静默失败，在不支持的环境中继续运行
            pass


# 模块导入时自动启用 Windows ANSI 支持
enableAnsiOnWindows()


# ============================================================================
# ANSI 转义序列封装
# ============================================================================

def cursorUp(n: int = 1):
    """光标上移 n 行"""
    sys.stdout.write(f"\x1b[{n}A")
    sys.stdout.flush()


def cursorDown(n: int = 1):
    """光标下移 n 行"""
    sys.stdout.write(f"\x1b[{n}B")
    sys.stdout.flush()


def cursorForward(n: int = 1):
    """光标右移 n 列"""
    sys.stdout.write(f"\x1b[{n}C")
    sys.stdout.flush()


def cursorBack(n: int = 1):
    """光标左移 n 列"""
    sys.stdout.write(f"\x1b[{n}D")
    sys.stdout.flush()


def cursorToColumn(col: int = 0):
    """光标移到指定列（0-based）"""
    sys.stdout.write(f"\x1b[{col + 1}G")
    sys.stdout.flush()


def cursorToHome():
    """光标移到左上角 (0, 0)"""
    sys.stdout.write("\x1b[H")
    sys.stdout.flush()


def cursorToPosition(row: int, col: int):
    """光标移到指定位置 (row, col)，1-based"""
    sys.stdout.write(f"\x1b[{row};{col}H")
    sys.stdout.flush()


def saveCursorPosition():
    """保存当前光标位置"""
    sys.stdout.write("\x1b[s")
    sys.stdout.flush()


def restoreCursorPosition():
    """恢复之前保存的光标位置"""
    sys.stdout.write("\x1b[u")
    sys.stdout.flush()


def clearLine():
    """清除当前行"""
    sys.stdout.write("\x1b[2K")
    sys.stdout.flush()


def clearLineFromCursor():
    """清除从光标到行尾"""
    sys.stdout.write("\x1b[0K")
    sys.stdout.flush()


def clearLineToCursor():
    """清除从行首到光标"""
    sys.stdout.write("\x1b[1K")
    sys.stdout.flush()


def clearScreen():
    """清除整个屏幕并将光标移到左上角"""
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def clearScreenFromCursor():
    """清除从光标到屏幕末尾"""
    sys.stdout.write("\x1b[0J")
    sys.stdout.flush()


def clearScreenToCursor():
    """清除从屏幕开头到光标"""
    sys.stdout.write("\x1b[1J")
    sys.stdout.flush()


def hideCursor():
    """隐藏光标"""
    sys.stdout.write("\x1b[?25l")
    sys.stdout.flush()


def showCursor():
    """显示光标"""
    sys.stdout.write("\x1b[?25h")
    sys.stdout.flush()


# ============================================================================
# 备用屏幕缓冲区
# ============================================================================

def enterAlternateScreen():
    """切换到备用屏幕缓冲区"""
    sys.stdout.write("\x1b[?1049h")
    sys.stdout.flush()


def exitAlternateScreen():
    """切回主屏幕缓冲区"""
    sys.stdout.write("\x1b[?1049l")
    sys.stdout.flush()


class AlternateScreen:
    """
    备用屏幕缓冲区上下文管理器。

    使用 with 语句自动管理备用屏幕的进入和退出：

    with AlternateScreen():
        # 在备用屏幕上的操作
        print("这些内容在备用屏幕上")
    # 退出后自动恢复到主屏幕

    特性：
        - 自动清屏
        - 退出时自动恢复
        - 异常安全（即使出错也会恢复）
    """

    def __init__(self, clear: bool = True):
        """
        初始化备用屏幕上下文。

        参数:
            clear: 进入备用屏幕后是否清屏（默认 True）
        """
        self.clear = clear

    def __enter__(self):
        enterAlternateScreen()
        if self.clear:
            clearScreen()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        exitAlternateScreen()
        return False  # 不抑制异常


# ============================================================================
# 终端信息获取
# ============================================================================

def getCursorPosition() -> tuple[int, int] | None:
    """
    查询当前光标位置（同步，跨平台）

    返回:
        (row, col) 元组（1-based），失败返回 None

    注意：
        - 不能在 prompt_toolkit 的 prompt_async() 期间调用
        - Windows 使用 Win32 API，Unix 使用 ANSI 转义序列
    """
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes

            class COORD(ctypes.Structure):
                _fields_ = [("X", ctypes.wintypes.SHORT), ("Y", ctypes.wintypes.SHORT)]

            class SMALL_RECT(ctypes.Structure):
                _fields_ = [
                    ("Left", ctypes.wintypes.SHORT),
                    ("Top", ctypes.wintypes.SHORT),
                    ("Right", ctypes.wintypes.SHORT),
                    ("Bottom", ctypes.wintypes.SHORT),
                ]

            class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
                _fields_ = [
                    ("dwSize", COORD),
                    ("dwCursorPosition", COORD),
                    ("wAttributes", ctypes.wintypes.WORD),
                    ("srWindow", SMALL_RECT),
                    ("dwMaximumWindowSize", COORD),
                ]

            kernel32 = ctypes.windll.kernel32
            hConsole = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            csbi = CONSOLE_SCREEN_BUFFER_INFO()

            if kernel32.GetConsoleScreenBufferInfo(hConsole, ctypes.byref(csbi)):
                return csbi.dwCursorPosition.Y + 1, csbi.dwCursorPosition.X + 1
        except Exception:
            return None
    else:
        try:
            import termios
            import tty
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                sys.stdout.write("\033[6n")
                sys.stdout.flush()
                response = ""
                while True:
                    ch = sys.stdin.read(1)
                    response += ch
                    if ch == 'R':
                        break
                if response.startswith("\033[") and response.endswith("R"):
                    coords = response[2:-1].split(';')
                    if len(coords) == 2:
                        return int(coords[0]), int(coords[1])
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except Exception:
            return None

    return None


def getTerminalSize() -> Tuple[int, int]:
    """
    获取终端尺寸。

    返回:
        (宽度, 高度) 元组，单位为字符

    如果无法获取，返回默认值 (80, 24)
    """
    try:
        size = shutil.get_terminal_size()
        return size.columns, size.lines
    except Exception:
        return 80, 24


# ============================================================================
# 便捷函数
# ============================================================================

def carriageReturn():
    """回车（光标移到行首）"""
    sys.stdout.write("\r")
    sys.stdout.flush()


def newline():
    """换行"""
    sys.stdout.write("\n")
    sys.stdout.flush()


def bell():
    """发出蜂鸣声（如果终端支持）"""
    sys.stdout.write("\x07")
    sys.stdout.flush()


# ============================================================================
# 兼容性别名（与 whitelistManager.py 中的函数名保持一致）
# ============================================================================

cuu = cursorUp
cud = cursorDown
clr = clearLine
crt = carriageReturn
bol = cursorToColumn
scp = saveCursorPosition
rcp = restoreCursorPosition
cls = clearScreen
smcup = enterAlternateScreen
rmcup = exitAlternateScreen


# ============================================================================
# 字符宽度与显示行数计算
# ============================================================================

import unicodedata as _unicodedata


def charDisplayWidth(ch: str) -> int:
    """返回单个字符的终端显示宽度（全角/宽字符占 2 列，其余占 1 列）"""
    eaw = _unicodedata.east_asian_width(ch)
    return 2 if eaw in ('W', 'F') else 1


def strDisplayWidth(s: str) -> int:
    """返回字符串在终端中的显示宽度（考虑全角字符）"""
    return sum(charDisplayWidth(c) for c in s)


def countDisplayLines(text: str, prefix: str, termWidth: int) -> int:
    """
    计算一段文本（含行首前缀）在终端中实际占用的显示行数。

    参数:
        text:      该行的正文内容
        prefix:    行首前缀（如提示符 ">> " 或续行符 ".. "）
        termWidth: 终端宽度（列数）

    返回:
        实际占用的显示行数（最少为 1）
    """
    total = strDisplayWidth(prefix) + strDisplayWidth(text)
    return max(1, (total + termWidth - 1) // termWidth)
