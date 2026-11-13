"""
utils/command/llm/taskCmd.py

llm tasks / llm stop 控制台子命令：查看运行中的 LLM 任务、按 taskID 前缀停止任务。
"""

import time

from utils.core.logger import logAction, LogLevel, LogChildType

from utils.llm.context.runtime import listRunningTasks, stopTask




def _formatTaskList() -> str:
    """渲染运行任务列表；无任务时返回一句提示。"""
    tasks = listRunningTasks()
    if not tasks:
        return "当前没有正在运行的 LLM 任务\n"
    lines = []
    for info in tasks:
        started = time.strftime("%H:%M:%S", time.localtime(info["startedAt"]))
        lines.append(f"  {info['taskID']}  chat {info['chatID']}  {started}  {info['preview']}")
    return "\n".join(lines) + "\n"


def _formatStopUsage() -> str:
    return "用法：llm tasks | llm stop <taskID>\n"


async def handleTaskCommand(rest: list[str], app):
    if not rest or rest[0].lower() == "tasks":
        print(_formatTaskList())
        return

    if rest[0].lower() != "stop":
        print(_formatStopUsage())
        return
    if len(rest) < 2:
        print(f"❌ 需要一个 taskID（可用 llm tasks 查看）\n{_formatStopUsage()}")
        return
    try:
        taskID = stopTask(rest[1])
    except ValueError as e:
        print(f"❌ {e}\n")
        return
    print(f"已向任务 {taskID} 发出停止信号\n")
    await logAction("System", "LLM 任务停止", f"taskID={taskID}（控制台）", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
