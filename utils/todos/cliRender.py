"""
utils/todos/cliRender.py

控制台侧渲染模块。

提供总览表格和待办列表的终端输出，供 utils/command/todos.py 调用。

主要接口：
    printOverview(rows)                            用户总览表格
    printTodoList(todos, total, targetLabel, status) 待办列表输出
"""


from utils.todos.utils import PRIORITY_TEXT as PRIORITY_EMOJI, formatRemindTime




def printOverview(rows: list):
    """将用户 todos 总览格式化输出到控制台。"""
    if not rows:
        print("\n  （数据库中暂无任何 todo 记录）\n")
        return

    # 列宽（动态按 user_id 最长值撑宽，最小 10）
    idWidth = max(10, max(len(r["user_id"]) for r in rows))

    header = (
        f"  {'用户 ID':<{idWidth}}  "
        f"{'待办':>4}  {'已完成':>6}  {'合计':>4}  {'已过期':>6}  最近活跃"
    )
    sep = "  " + "─" * (len(header) - 2)

    # 汇总行数据
    totalPending = sum(r["pending"] for r in rows)
    totalDone    = sum(r["done"]    for r in rows)
    totalAll     = sum(r["total"]   for r in rows)
    totalOverdue = sum(r["overdue"] for r in rows)

    print(f"\n── todos 用户总览（共 {len(rows)} 人，{totalAll} 条记录）──")
    print(sep)
    print(header)
    print(sep)

    for r in rows:
        overdueStr = f"[!]{r['overdue']}" if r["overdue"] else "  —"
        print(
            f"  {r['user_id']:<{idWidth}}  "
            f"{r['pending']:>4}  {r['done']:>6}  {r['total']:>4}  "
            f"{overdueStr:>6}  {r['last_active']}"
        )

    print(sep)
    print(
        f"  {'合计':<{idWidth}}  "
        f"{totalPending:>4}  {totalDone:>6}  {totalAll:>4}  "
        f"{f'[!]{totalOverdue}' if totalOverdue else '  —':>6}"
    )
    print()


def printTodoList(todos: list, total: int, targetLabel: str, status: str):
    """将待办列表格式化输出到控制台。"""
    print(f"\n── {targetLabel} 的待办（{status}，共 {total} 条）──")
    if not todos:
        print("  （空）")
    else:
        for todo in todos:
            pri = PRIORITY_EMOJI.get(todo['priority'], '[P_]')
            remindStr = f"  ⏰ {formatRemindTime(todo['remind_time'])}" if todo['remind_time'] else ""
            doneStr = " ✓" if todo['status'] == 'done' else ""
            print(f"  [{todo['id']}] {pri} {todo['content']}{remindStr}{doneStr}")
    print()
