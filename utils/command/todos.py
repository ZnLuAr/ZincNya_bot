"""
utils/command/todos.py

控制台待办管理命令。支持两种模式：

    控制台模式（无 -u 参数）：
        以虚拟身份 "console" 管理属于控制台自己的待办。
        到期提醒会以 print() 输出到终端，而不是 Telegram 消息。

    管理员模式（有 -u <user_id> 参数）：
        以管理员身份查看、添加、编辑、删除指定用户的待办。
        目标用户的私聊 chat_id 与 user_id 相同。

================================================================================
命令格式
================================================================================

    /todos                              列出控制台自己的 todos（pending）
    /todos -l [all|done|pending]        按状态列出
    /todos -a "买猫粮"                  给控制台加一条 todo
    /todos -a "明天9点 买猫粮"           支持时间解析（相对/绝对均可）
    /todos -done <id>                   标记完成
    /todos -d <id>                      删除
    /todos -edit <id> <新内容>          修改内容

    /todos -u <user_id>                 列出目标用户的 todos
    /todos -u <user_id> -a "内容"       给目标用户加 todo
    /todos -u <user_id> -done <id>      标记完成
    /todos -u <user_id> -d <id>         删除
    /todos -u <user_id> -edit <id> 内容 修改内容

================================================================================
参数缩写
================================================================================

    -u      user_id（目标用户，管理员模式）
    -a      add（添加）
    -d      del（删除）
    -l      list（列表状态筛选）

"""


import re
from datetime import datetime, timedelta

from handlers.cli import parseArgsTokens
from utils.todoDB import (
    addTodo, getTodos, getTodoByID,
    updateTodo, deleteTodo, markDone, getTodosCount,
)




# 控制台虚拟身份
CONSOLE_ID = "console"

PRIORITY_EMOJI = {
    'P0': '[P0]', 'P1': '[P1]', 'P2': '[P2]', 'P3': '[P3]', 'P_': '[P_]',
}




# ============================================================================
# 时间解析（与 handlers/todos.py 中保持一致逻辑）
# ============================================================================

def _parseTime(text: str) -> datetime | None:
    """
    从文本头部识别时间表达式，返回 datetime 或 None。
    支持：2h / 30m / 1d 英文简写，以及 jionlp 自然语言解析。
    """
    # 英文简写相对时间：2h, 30m, 1d
    relMatch = re.match(r'^(\d+)([mhd])\b', text)
    if relMatch:
        value, unit = relMatch.groups()
        delta = {
            'm': timedelta(minutes=int(value)),
            'h': timedelta(hours=int(value)),
            'd': timedelta(days=int(value)),
        }[unit]
        return datetime.now() + delta

    # jionlp 自然语言解析
    try:
        import jionlp as jio
        base = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        result = jio.parse_time(text, time_base=base)
        parsed = datetime.strptime(result['time'][0], '%Y-%m-%d %H:%M:%S')
        if parsed > datetime.now():
            return parsed
    except Exception:
        pass

    return None


def _parsePriority(text: str) -> tuple[str, str]:
    """从文本中提取优先级（如 P0/P1/P2/P3/P_），返回 (优先级, 剩余文本)。"""
    tokens = text.split()
    for i, token in enumerate(tokens):
        if re.fullmatch(r'P[0-3_]', token):
            remaining = ' '.join(tokens[:i] + tokens[i+1:])
            return (token, remaining)
    return ('P_', text)




# ============================================================================
# 格式化工具
# ============================================================================

def _formatRemindTime(dt: datetime) -> str:
    now = datetime.now()
    today = now.date()
    tomorrow = today + timedelta(days=1)
    if dt.date() == today:
        return f"今天 {dt.strftime('%H:%M')}"
    elif dt.date() == tomorrow:
        return f"明天 {dt.strftime('%H:%M')}"
    else:
        return dt.strftime("%Y/%m/%d %H:%M")


def _printTodoList(todos: list, total: int, targetLabel: str, status: str):
    """将待办列表格式化输出到控制台。"""
    print(f"\n── {targetLabel} 的待办（{status}，共 {total} 条）──")
    if not todos:
        print("  （空）")
    else:
        for todo in todos:
            pri = PRIORITY_EMOJI.get(todo['priority'], '[P_]')
            remindStr = f"  ⏰ {_formatRemindTime(todo['remind_time'])}" if todo['remind_time'] else ""
            doneStr = " ✓" if todo['status'] == 'done' else ""
            print(f"  [{todo['id']}] {pri} {todo['content']}{remindStr}{doneStr}")
    print()




# ============================================================================
# execute 入口
# ============================================================================

async def execute(app, args: list[str]):
    """控制台 /todos 命令入口。"""

    parsed = {
        "u":    None,   # 目标 user_id（管理员模式）
        "add":  None,   # 添加内容（字符串）
        "del":  None,   # 删除 todo ID
        "done": None,   # 标记完成 todo ID
        "edit": [],     # [id, 内容词1, 内容词2, ...]
        "list": None,   # 状态筛选：pending / done / all
    }

    argAlias = {
        "a": "add",
        "d": "del",
        "l": "list",
    }

    parsed = parseArgsTokens(parsed, args, argAlias)

    # ── 确定操作目标身份 ──
    targetUserID = parsed["u"] if parsed["u"] and parsed["u"] != "NoValue" else CONSOLE_ID
    targetChatID = targetUserID   # 私聊中 chat_id == user_id；控制台同样用虚拟 ID
    isAdmin      = targetUserID != CONSOLE_ID
    targetLabel  = f"用户 {targetUserID}" if isAdmin else "控制台"

    # ── -a / --add：添加 todo ──
    addText = parsed["add"]
    if addText and addText != "NoValue":
        priority, content = _parsePriority(addText)
        remindTime = _parseTime(content)

        if not content.strip():
            print("添加失败：内容不能为空喵\n")
            return

        todoID = await addTodo(targetChatID, targetUserID, content, remindTime, priority)
        if todoID is None:
            print("添加失败，数据库写入出错了喵\n")
            return

        remindStr = f"  ⏰ {_formatRemindTime(remindTime)}" if remindTime else ""
        print(f"\n已为 {targetLabel} 添加 todo [ID {todoID}]：")
        print(f"  {PRIORITY_EMOJI.get(priority, '[P_]')} {content}{remindStr}\n")
        return

    # ── -done：标记完成 ──
    doneID = parsed["done"]
    if doneID and doneID != "NoValue":
        try:
            doneID = int(doneID)
        except ValueError:
            print(f"标记失败：ID 必须是数字喵（收到 {doneID!r}）\n")
            return

        todo = await getTodoByID(doneID)
        if not todo:
            print(f"标记失败：找不到 ID={doneID} 的 todo 喵\n")
            return
        if todo['user_id'] != targetUserID:
            print(f"标记失败：ID={doneID} 不属于 {targetLabel} 喵\n")
            return

        ok = await markDone(doneID)
        if ok:
            print(f"\n已标记完成 [ID {doneID}]：{todo['content']}\n")
        else:
            print(f"标记失败，数据库出错了喵\n")
        return

    # ── -d / --del：删除 ──
    delID = parsed["del"]
    if delID and delID != "NoValue":
        try:
            delID = int(delID)
        except ValueError:
            print(f"删除失败：ID 必须是数字喵（收到 {delID!r}）\n")
            return

        todo = await getTodoByID(delID)
        if not todo:
            print(f"删除失败：找不到 ID={delID} 的 todo 喵\n")
            return
        if todo['user_id'] != targetUserID:
            print(f"删除失败：ID={delID} 不属于 {targetLabel} 喵\n")
            return

        ok = await deleteTodo(delID)
        if ok:
            print(f"\n已删除 [ID {delID}]：{todo['content']}\n")
        else:
            print(f"删除失败，数据库出错了喵\n")
        return

    # ── -edit：修改内容 ──
    editTokens = parsed["edit"]
    if editTokens:
        if len(editTokens) < 2:
            print("修改失败：格式应为 -edit <id> <新内容> 喵\n")
            return

        try:
            editID = int(editTokens[0])
        except ValueError:
            print(f"修改失败：ID 必须是数字喵（收到 {editTokens[0]!r}）\n")
            return

        newContent = ' '.join(editTokens[1:])

        todo = await getTodoByID(editID)
        if not todo:
            print(f"修改失败：找不到 ID={editID} 的 todo 喵\n")
            return
        if todo['user_id'] != targetUserID:
            print(f"修改失败：ID={editID} 不属于 {targetLabel} 喵\n")
            return

        ok = await updateTodo(editID, content=newContent)
        if ok:
            print(f"\n已修改 [ID {editID}]：\n  旧：{todo['content']}\n  新：{newContent}\n")
        else:
            print(f"修改失败，数据库出错了喵\n")
        return

    # ── -l / --list 或无参数：列出 ──
    listStatus = parsed["list"]
    if listStatus is None or listStatus == "NoValue":
        listStatus = "pending"
    if listStatus not in ("pending", "done", "all"):
        print(f"无效状态 {listStatus!r}，可选：pending / done / all\n")
        return

    total = getTodosCount(targetChatID, targetUserID, status=listStatus)
    todos = await getTodos(targetChatID, targetUserID, status=listStatus)
    _printTodoList(todos, total, targetLabel, listStatus)




def getHelp():
    return {

        "name": "/todos",

        "description": "在控制台管理待办事项（支持自用模式和管理员模式）",

        "usage": (
            "/todos                              列出控制台自己的 todos（pending）\n"
            "/todos -l [all|done|pending]        按状态列出\n"
            "/todos -a \"买猫粮\"                添加 todo（P_ 优先级，无提醒）\n"
            "/todos -a \"P1 明天9点 买猫粮\"        支持优先级和时间解析\n"
            "/todos -done <id>                   标记完成\n"
            "/todos -d <id>                      删除\n"
            "/todos -edit <id> <新内容>          修改内容\n"
            "\n"
            "/todos -u <user_id>                 列出目标用户的 todos（管理员模式）\n"
            "/todos -u <user_id> -a \"内容\"       给目标用户加 todo\n"
            "/todos -u <user_id> -done <id>      标记完成\n"
            "/todos -u <user_id> -d <id>         删除\n"
            "/todos -u <user_id> -edit <id> 内容 修改内容"
        ),

        "example": (
            "列出所有状态的 todo：/todos -l all\n"
            "添加带提醒的 todo：/todos -a \"P0 明天上午9点 交周报\"\n"
            "标记完成：/todos -done 3\n"
            "查看用户 123456 的 todos：/todos -u 123456"
        ),

    }
