#!/usr/bin/env python3
"""
scripts/test.py

ZincNya_bot 测试运行脚本

用法：
    python scripts/test.py                    # 运行所有测试
    python scripts/test.py -m llm             # 只运行 llm 模块测试（tests/utils/llm/）
    python scripts/test.py -m utils.llm       # 同上（显式指定路径）
    python scripts/test.py -m handlers.todos  # 运行 todos handler 测试（tests/handlers/）
    python scripts/test.py -m core            # 运行 core 模块测试（tests/utils/core/）
    python scripts/test.py -m integration     # 运行集成测试（tests/integration/）
    python scripts/test.py -k tokenizer       # 运行名称包含 tokenizer 的测试
    python scripts/test.py --cov              # 生成覆盖率报告
    python scripts/test.py --integration      # 只运行带 integration marker 的测试
    python scripts/test.py --unit             # 只运行单元测试
    python scripts/test.py --fast             # 跳过慢速测试
"""

import sys
import os
import subprocess
import argparse


# ============================================================================
# 颜色输出
# ============================================================================

class Color:
    """ANSI 颜色代码"""
    RESET = "\033[0m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    GRAY = "\033[90m"


def enableWindowsVT():
    """启用 Windows VT 模式（支持 ANSI 颜色）"""
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


enableWindowsVT()


def colorPrint(text, color=Color.RESET):
    """彩色打印"""
    print(f"{color}{text}{Color.RESET}")


# ============================================================================
# 主函数
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="ZincNya_bot 测试运行脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python scripts/test.py                      # 运行所有测试
  python scripts/test.py -m llm               # 只运行 llm 模块测试
  python scripts/test.py -m utils.llm         # 同上（显式路径）
  python scripts/test.py -m handlers.todos    # 运行 todos handler 测试
  python scripts/test.py -m core              # 运行 core 模块测试
  python scripts/test.py -k tokenizer         # 运行名称包含 tokenizer 的测试
  python scripts/test.py --cov                # 生成覆盖率报告
  python scripts/test.py --integration        # 只运行集成测试
  python scripts/test.py --unit               # 只运行单元测试
  python scripts/test.py --fast               # 跳过慢速测试
        """
    )

    parser.add_argument(
        "-m", "--module",
        help="只运行指定模块的测试（支持：llm, utils.llm, handlers.todos, core, integration）"
    )
    parser.add_argument(
        "-k", "--keyword",
        help="运行名称包含指定关键词的测试"
    )
    parser.add_argument(
        "--cov",
        action="store_true",
        help="生成覆盖率报告"
    )
    parser.add_argument(
        "--integration",
        action="store_true",
        help="只运行集成测试"
    )
    parser.add_argument(
        "--unit",
        action="store_true",
        help="只运行单元测试"
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="跳过慢速测试"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="详细输出"
    )

    args = parser.parse_args()

    # 构建 pytest 命令
    cmd = ["pytest"]

    # 模块过滤
    if args.module:
        # 将模块名转换为测试路径
        # 支持：llm / utils.llm / handlers.nya / core / todos 等
        module = args.module
        if '.' in module:
            # utils.llm → tests/utils/llm
            # handlers.nya → tests/handlers (handler 测试不按模块分子目录)
            parts = module.split('.')
            if parts[0] == 'handlers':
                cmd.append("tests/handlers")
            elif parts[0] == 'scripts':
                cmd.append("tests/scripts")
            else:
                cmd.append(f"tests/{'/'.join(parts)}")
        else:
            # llm → tests/utils/llm (默认假设在 utils 下)
            # 特殊处理 integration（独立目录）
            if module == 'integration':
                cmd.append("tests/integration")
            else:
                cmd.append(f"tests/utils/{module}")

    # 关键词过滤
    if args.keyword:
        cmd.extend(["-k", args.keyword])

    # Marker 过滤
    if args.integration:
        cmd.extend(["-m", "integration"])
    elif args.unit:
        cmd.extend(["-m", "unit or not integration"])

    if args.fast:
        cmd.extend(["-m", "not slow"])

    # 覆盖率
    if not args.cov:
        cmd.append("--no-cov")

    # 详细输出
    if args.verbose:
        cmd.append("-vv")

    # 打印命令
    colorPrint(f"\n运行命令: {' '.join(cmd)}\n", Color.CYAN)

    # 运行测试
    try:
        result = subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(__file__)))
        sys.exit(result.returncode)
    except KeyboardInterrupt:
        colorPrint("\n\n测试已中断", Color.YELLOW)
        sys.exit(1)
    except Exception as e:
        colorPrint(f"\n\n运行测试时出错: {e}", Color.RED)
        sys.exit(1)


if __name__ == "__main__":
    main()