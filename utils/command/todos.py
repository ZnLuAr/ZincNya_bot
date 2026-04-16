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


from handlers.cli import parseArgsTokens

from utils.todos.cliRender import printOverview, printTodoList, PRIORITY_EMOJI
from utils.todos.database import (
    addTodo, getTodos, getTodoByID,
    updateTodo, deleteTodo, markDone, reopenTodo, getTodosCount,
    getUsersTodosSummary,
)
from utils.todos.utils import parseTime, parsePriority, formatRemindTime




# 控制台虚拟身份
CONSOLE_ID = "console"


# ============================================================================
# execute 入口
# ============================================================================

async def execute(app, args: list[str]):
    """控制台 /todos 命令入口。"""

    parsed = {
        "u":        None,   # 目标 user_id（管理员模式）
        "add":      None,   # 添加内容（字符串）
        "del":      None,   # 删除 todo ID
        "done":     None,   # 标记完成 todo ID
        "reopen":   None,   # 重新打开 todo ID
        "edit":     [],     # [id, 内容词1, 内容词2, ...]
        "pri":      [],     # [id, P0|P1|P2|P3|P_]
        "time":     [],     # [id, 时间表达式...] 或 [id, clear]
        "list":     None,   # 状态筛选：pending / done / all
        "overview": None,   # 用户总览表格
    }

    argAlias = {
        "a": "add",
        "d": "del",
        "l": "list",
        "ov": "overview",
    }

    parsed = parseArgsTokens(parsed, args, argAlias)

    # ── 确定操作目标身份 ──
    targetUserID = parsed["u"] if parsed["u"] and parsed["u"] != "NoValue" else CONSOLE_ID
    targetChatID = targetUserID   # 私聊中 chat_id == user_id；控制台同样用虚拟 ID
    isAdmin      = targetUserID != CONSOLE_ID
    targetLabel  = f"用户 {targetUserID}" if isAdmin else "控制台"

    # ── -overview / -ov：用户总览表格 ──
    if parsed["overview"] is not None:
        rows = await getUsersTodosSummary()
        printOverview(rows)
        return

    # ── -a / --add：添加 todo ──
    addText = parsed["add"]
    if addText and addText != "NoValue":
        priority, content = parsePriority(addText)
        remindTime, content = parseTime(content)

        if not content.strip():
            print("添加失败：内容不能为空喵\n")
            return

        todoID = await addTodo(targetChatID, targetUserID, content, remindTime, priority)
        if todoID is None:
            print("添加失败，数据库写入出错了喵\n")
            return

        remindStr = f"  ⏰ {formatRemindTime(remindTime)}" if remindTime else ""
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

    # ── -reopen：重新打开 ──
    reopenID = parsed["reopen"]
    if reopenID and reopenID != "NoValue":
        try:
            reopenID = int(reopenID)
        except ValueError:
            print(f"重新打开失败：ID 必须是数字喵（收到 {reopenID!r}）\n")
            return

        todo = await getTodoByID(reopenID)
        if not todo:
            print(f"重新打开失败：找不到 ID={reopenID} 的 todo 喵\n")
            return
        if todo['user_id'] != targetUserID:
            print(f"重新打开失败：ID={reopenID} 不属于 {targetLabel} 喵\n")
            return

        ok = await reopenTodo(reopenID)
        if ok:
            print(f"\n已重新打开 [ID {reopenID}]：{todo['content']}\n")
        else:
            print(f"重新打开失败，数据库出错了喵\n")
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

    # ── -pri：修改优先级 ──
    priTokens = parsed["pri"]
    if priTokens:
        if len(priTokens) < 2:
            print("修改失败：格式应为 -pri <id> <P0|P1|P2|P3|P_> 喵\n")
            return

        try:
            priID = int(priTokens[0])
        except ValueError:
            print(f"修改失败：ID 必须是数字喵（收到 {priTokens[0]!r}）\n")
            return

        newPri = priTokens[1]
        if newPri not in ('P0', 'P1', 'P2', 'P3', 'P_'):
            print(f"修改失败：无效优先级 {newPri!r}，可选：P0 / P1 / P2 / P3 / P_\n")
            return

        todo = await getTodoByID(priID)
        if not todo:
            print(f"修改失败：找不到 ID={priID} 的 todo 喵\n")
            return
        if todo['user_id'] != targetUserID:
            print(f"修改失败：ID={priID} 不属于 {targetLabel} 喵\n")
            return

        ok = await updateTodo(priID, priority=newPri)
        if ok:
            oldPri = PRIORITY_EMOJI.get(todo['priority'], '[P_]')
            newPriStr = PRIORITY_EMOJI.get(newPri, '[P_]')
            print(f"\n已修改 [ID {priID}] 优先级：{oldPri} → {newPriStr}\n")
        else:
            print(f"修改失败，数据库出错了喵\n")
        return

    # ── -time：修改提醒时间 ──
    timeTokens = parsed["time"]
    if timeTokens:
        if len(timeTokens) < 2:
            print("修改失败：格式应为 -time <id> <时间表达式> 或 -time <id> clear 喵\n")
            return

        try:
            timeID = int(timeTokens[0])
        except ValueError:
            print(f"修改失败：ID 必须是数字喵（收到 {timeTokens[0]!r}）\n")
            return

        todo = await getTodoByID(timeID)
        if not todo:
            print(f"修改失败：找不到 ID={timeID} 的 todo 喵\n")
            return
        if todo['user_id'] != targetUserID:
            print(f"修改失败：ID={timeID} 不属于 {targetLabel} 喵\n")
            return

        timeExpr = ' '.join(timeTokens[1:])

        if timeExpr.lower() == 'clear':
            ok = await updateTodo(timeID, remind_time=None)
            if ok:
                print(f"\n已清除 [ID {timeID}] 的提醒时间\n")
            else:
                print(f"修改失败，数据库出错了喵\n")
            return

        newTime, _ = parseTime(timeExpr)
        if newTime is None:
            print(f"修改失败：无法识别时间表达式 {timeExpr!r} 喵\n")
            return

        ok = await updateTodo(timeID, remind_time=newTime, reminded=0)
        if ok:
            print(f"\n已修改 [ID {timeID}] 提醒时间：{formatRemindTime(newTime)}\n")
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

    total = await getTodosCount(targetChatID, targetUserID, status=listStatus)
    todos = await getTodos(targetChatID, targetUserID, status=listStatus)
    printTodoList(todos, total, targetLabel, listStatus)




def getHelp():
    return {

        "name": "/todos",

        "description": "在控制台管理待办事项（支持自用模式和管理员模式）",

        "usage": (
            "/todos                                 列出控制台自己的 todos（pending）\n"
            "/todos -l [all|done|pending]           按状态列出\n"
            "/todos -a \"买猫粮\"                     添加 todo（P_ 优先级，无提醒）\n"
            "/todos -a \"P1 明天9点 买猫粮\"          支持优先级和时间解析\n"
            "/todos -done <id>                      标记完成\n"
            "/todos -reopen <id>                    重新打开已完成的 todo\n"
            "/todos -d <id>                         删除\n"
            "/todos -edit <id> <新内容>             修改内容\n"
            "/todos -pri <id> <P0|P1|P2|P3|P_>      修改优先级\n"
            "/todos -time <id> <时间表达式>         修改提醒时间\n"
            "/todos -time <id> clear                清除提醒时间\n"
            "/todos -overview                       总览所有用户的 todos 统计表\n"
            "\n"
            "/todos -u <user_id>                    列出目标用户的 todos（管理员模式）\n"
            "/todos -u <user_id> -a \"内容\"          给目标用户加 todo\n"
            "/todos -u <user_id> -done <id>         标记完成\n"
            "/todos -u <user_id> -d <id>            删除\n"
            "/todos -u <user_id> -edit <id> 内容    修改内容"
        ),

        "example": (
            "列出所有状态的 todo：/todos -l all\n"
            "添加带提醒的 todo：/todos -a \"P0 明天上午9点 交周报\"\n"
            "标记完成：/todos -done 3\n"
            "查看用户总览：/todos -overview\n"
            "查看用户 123456 的 todos：/todos -u 123456"
        ),

    }
