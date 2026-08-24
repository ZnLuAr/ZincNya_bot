import os
import importlib

from config import COMMAND_DIR, COMMAND_MODULE

from utils.core.logger import logAction, LogLevel, LogChildType




async def execute(app, args):
    # 响应 /help 命令

    try:
        commandsList = []

        # 单文件命令（name.py）与包命令（name/ 目录，如 llm/）都扫——
        # 与 handlers/cli 的包命令支持对齐（cli.py 接受两种入口）
        for entry in os.listdir(COMMAND_DIR):
            if entry.startswith("__") or entry.endswith(".pyc"):
                continue
            if entry.endswith(".py"):
                fileName = entry[:-3]
            elif os.path.isdir(os.path.join(COMMAND_DIR, entry)):
                fileName = entry
            else:
                continue
            # （实现命令的）模块的路径，将目录路径 utils/command 转化为 utils.command
            moudlePath = f"{COMMAND_MODULE}.{fileName}"
            try:
                mod = importlib.import_module(moudlePath)
                if hasattr(mod, "getHelp"):
                    # info 包含 getHelp 返回的"name" "description" "usage" "example" 信息
                    info = mod.getHelp()
                    commandsList.append(info)
            except Exception as e:
                print(f"\n❌ 加载 {fileName} 时出错了喵：{e}\n")

        # 无参数，即列出命令列表
        if not args:
            print("所有可用命令喵：")
            print("—" * 64, "\n")
            # 按命令名排序 commands 列表，格式化打印各条命令的名称和简短描述（左对齐宽度15）
            for info in sorted(commandsList, key=lambda x: x["name"]):
                print(f"{info['name']:<15}      {info.get('description', '')}")
            print("\n", "—" * 64)
            print("使用 /help <command> 查看详细说明——\n")
            return
        
        # 有参数，打印对应命令信息
        command = args[0].lstrip("/")
        targetMoudlePath = f"{COMMAND_MODULE}.{command}"
        try:
            # 尝试导入终端指定的命令模块（单个）
            mod = importlib.import_module(targetMoudlePath)
            if hasattr(mod, "getHelp"):
                info = mod.getHelp()
                print(f"\n\n{info['name']}        {info.get('description', '')}")
                print(f"\n{info.get('usage', 'ないです（即答')}\n\n")
                if info.get("example"):
                    print(f"{info['example']}")
                    print("—" * 87 + "\n")
            else:
                print(f"命令 /{command} 好像并没有定义 getHelp() 函数……\n")
        except ModuleNotFoundError:
            print(f"\n没有找到 /{command} 命令喵……")
            print("提示：输入 /help 查看所有可用命令\n")

    except Exception as e:
        await logAction(
            "System",
            "执行 /help 时出错了喵……",
            str(e),
            LogLevel.ERROR,
            LogChildType.WITH_ONE_CHILD
        )
        raise




def getHelp():
    return {

        "name": "/help",

        "description": "提供 ZincNya bot 命令的帮助信息",

        "usage": "/help [<command>]",

        "example": "展示此页面：/help\n获取 /shutdown 命令的帮助：/help shutdown"

    }