"""
utils/command/llm/review/consoleReview.py

控制台审核（/llm review）：独立 CLI 模式的交互式审核。

从审核队列取一条消息，通过 asyncInput/asyncMultilineInput 交互后执行操作，
支持 reply 和 memory 两类审核项。与 chatScreenReview.py 共享 utils.llm.review 原语，
但本模块是同步阻塞式（print + stdin），一条 item 取出后在同一函数内完成全部交互。
"""

import sys

from utils.llm.review import (
    canEditReviewItem,
    canRetryReviewItem,
    formatReviewItemText,
    getReviewItemActions,
    reviewCancel,
    reviewEditSubmit,
    reviewRetry,
    reviewRetryWithFeedback,
    reviewSend,
)
from utils.llm.state import getReviewQueue


# 编辑提示中当前内容的预览长度（截断展示）
_CONTENT_PREVIEW_LEN = 100




async def handleConsoleReview(bot):
    """
    独立 CLI 模式的控制台审核（/llm review）。

    从审核队列取一条消息，通过 asyncInput 交互后执行操作。
    支持 reply 和 memory 两类审核项。
    """
    from utils.inputHelper import asyncInput, asyncMultilineInput

    queue = getReviewQueue()

    if queue.empty():
        print("\n[审核队列] 目前没有待审核的消息喵~\n")
        return

    item = queue.get_nowait()
    kind = item.get("kind", "reply")
    actionsHint = getReviewItemActions(item)

    if kind == "memory":
        print(f"\n{formatReviewItemText(item)}\n")
        print(f"操作：{actionsHint} > ", end="")
    else:
        print(
            f"""
[消息待审核喵]
原始消息：{item['originalMsg']}

生成的回复：
{'-' * 30}

{item['reply']}

{'-' * 30}

操作：{actionsHint} > """,
end=""
        )

    sys.stdout.flush()

    try:
        choice = (await asyncInput("")).strip().lower()
    except Exception:
        queue.put_nowait(item)
        return

    if choice in ("a", ""):
        try:
            await reviewSend(bot, item)
        except Exception as e:
            print(f"[审核] 操作失败喵：{e}\n\n")
            queue.put_nowait(item)

    elif choice in ("e", "edit"):
        if not canEditReviewItem(item):
            print("[审核] 当前审核项不可编辑喵\n")
            queue.put_nowait(item)
            return

        if kind == "memory":
            currentContent = item.get("action", {}).get("content") or ""
            print(f"编辑记忆内容（当前：{currentContent[:_CONTENT_PREVIEW_LEN]}）：")
        else:
            print("编辑回复（Alt+Enter 提交，:q 取消）：")

        newText = await asyncMultilineInput(prompt=">> ", continuation_prompt=".. ")
        if newText.strip() and newText.strip() != ":q":
            try:
                editedItem = await reviewEditSubmit(item, newText.rstrip('\n'))
                if kind == "memory":
                    # console memory 编辑后立即批准
                    await reviewSend(bot, editedItem)
                    print("[审核] 记忆操作编辑后已执行喵\n")
                else:
                    await reviewSend(bot, editedItem)
                    print(f"[审核] 已发送编辑内容至 {item['chatID']}\n")
            except Exception as e:
                print(f"[审核] 操作失败喵：{e}\n")
                queue.put_nowait(item)
        else:
            print("[审核] 取消编辑喵\n")
            queue.put_nowait(item)

    elif choice in ("r", "retry"):
        if not canRetryReviewItem(item):
            print("[审核] 当前审核项不支持重试喵\n")
            queue.put_nowait(item)
            return

        print("[审核] 重新生成喵……")
        try:
            newQueueItem = await reviewRetry(item)
            queue.put_nowait(newQueueItem)
            print("[审核] 已重新生成喵，再次输入 /llm review 就可以查看了\n\n")
        except Exception as e:
            print(f"[审核] 重试失败喵：{e}\n\n")
            queue.put_nowait(item)

    elif choice in ("f", "feedback"):
        if not canRetryReviewItem(item):
            print("[审核] 当前审核项不支持重试喵\n")
            queue.put_nowait(item)
            return

        print("请输入背景信息补充（Alt+Enter 提交，:q 取消）:")
        feedback = await asyncMultilineInput(prompt=">> ", continuation_prompt=".. ")
        if feedback.strip() and feedback.strip() != ":q":
            print("[审核] 重新生成喵……")
            try:
                newQueueItem = await reviewRetryWithFeedback(item, feedback.rstrip('\n'))
                queue.put_nowait(newQueueItem)
                print("[审核] 已重新生成喵，再次输入 /llm review 就可以查看了\n\n")
            except Exception as e:
                print(f"[审核] 重试失败喵：{e}\n\n")
                queue.put_nowait(item)
        else:
            print("[审核] 取消喵\n")
            queue.put_nowait(item)

    elif choice in ("c", "cancel"):
        await reviewCancel(item)

    else:
        print(f"[审核] 无效选项：{choice!r}\n")
        queue.put_nowait(item)
