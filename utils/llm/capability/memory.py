"""
utils/llm/capability/memory.py

记忆操作能力（memory_action）：模型申请新增、修改或删除长期记忆。

说明、校验与送出都在本文件。校验沿用 memory/action.py 的 validateAction，scope 归属
按任务的可信身份判断。送出有两种方式：

    - makeSubmittingMemoryCapability：自动批准并执行，或交给人工审核
      （off 发 Telegram 审核卡，console 加入控制台审核队列）
    - makeCollectingMemoryCapability：只收集不送出，审核重试用它拿到新的记忆操作

只在进入聊天上下文的任务（includeContext）里提供。
"""

from typing import Awaitable, Callable

from utils.core.logger import logAction, logSystemEvent, LogLevel, LogChildType
from utils.llm.config import getMemoryAutoApprove
from utils.llm.memory.action import (
    MemoryAction,
    MemoryActionContext,
    buildMemoryActionReviewPayload,
    executeAction,
    formatActionDetail,
    parseActionDict,
    requiresHumanReview,
    validateAction,
)
from utils.llm.models import DisplayBlocks, TaskRequest
from utils.llm.operation.types import (
    Capability, Operation, OperationResult, OperationStatus, TaskInfo,
)
from utils.llm.review.cards import renderMemoryReviewCard
from utils.llm.review.items import addMemoryReviewItem, putMemoryReview


MEMORY_CAPABILITY_NAME = "memory_action"

_DESCRIPTION = (
    "【memory_action】申请修改长期记忆。常驻记忆操作必须由人类独立审核，其他操作按系统审核设置处理。\n"
    "\n"
    "add（新增）必需字段：\n"
    '  {"op": "memory_action", "action": "add", "scope_type": "global", "scope_id": "global", "content": "记忆内容", "mode": "contextual"}\n'
    "  可选字段：tags, priority, retrieval_hint, reason\n"
    "update（修改）必需字段：\n"
    '  {"op": "memory_action", "action": "update", "scope_type": "global", "scope_id": "global", "memory_id": 123}\n'
    "  可选字段：content, tags, priority, mode, retrieval_hint, reason（至少提供一个可选字段）\n"
    "delete（删除）必需字段：\n"
    '  {"op": "memory_action", "action": "delete", "scope_type": "global", "scope_id": "global", "memory_id": 123}\n'
    "  可选字段：reason\n"
    "\n"
    "字段类型：\n"
    '  - scope_type: 字符串，仅可为 "global" | "chat" | "user"（注意：不是 src 或 source）\n'
    '  - scope_id: 字符串，global 时必须为 "global"，chat/user 时为对应 ID\n'
    "  - priority: 整数 0-3（0=日常闲聊，1=一般偏好，2=重要事实，3=关键信息）\n"
    "  - memory_id: 整数，update/delete 时为目标记忆的 ID\n"
    '  - tags: 字符串数组，如 ["标签1", "标签2"]\n'
    '  - mode: "contextual"（按对话相关性召回）或 "pinned"（常驻）；大部分情况使用 contextual\n'
    "  - retrieval_hint: 可选的单行检索说明，最多 80 字；只描述未来可能相关的话题、措辞或别名，不增加事实\n"
    "\n"
    "约束：\n"
    "  - 你只能修改通过推断产生的记忆（source=inferred），不能修改手动创建的记忆（source=manual）\n"
    "  - 只有确有持续常驻价值时才申请 mode=pinned；新增、升级、修改、降级或删除 pinned 记忆都必须人工审核\n"
    "  - retrieval_hint 只在本来就要 add/update 时顺带给出，不要为补写说明单独制造记忆操作\n"
    "  - retrieval_hint 不得推断 content 未支持的新事实；不确定时省略\n"
    "  - update 中不写 tags 字段 = 保留原有标签；\"tags\": [] = 清空；\"tags\": [\"新标签\"] = 替换\n"
    "  - 结果为「已送审」时，这项修改在管理员批准前不会生效"
)

_COLLECTED_NOTE = "已交给管理员审核"




# ============================================================================
# 能力构建
# ============================================================================

def makeMemoryCapability(
    submit: Callable[[MemoryAction, Operation, TaskInfo], Awaitable[OperationResult]],
) -> Capability:
    """
    构造记忆操作能力；只在进入聊天上下文的任务（includeContext）里提供。

    submit(action, operation, task) 负责执行或送审并交回结果。
    """

    def _describe(task: TaskInfo) -> str:
        return _DESCRIPTION if task.includeContext else ""

    async def _validate(args: dict, task: TaskInfo) -> str | None:
        try:
            action = parseActionDict(args)
        except ValueError as e:
            return str(e)
        return await validateAction(
            action, actionContext=MemoryActionContext(chatID=task.chatID, userID=task.userID),
        )

    async def _handle(operation: Operation, task: TaskInfo) -> OperationResult:
        return await submit(parseActionDict(operation.args), operation, task)

    return Capability(
        name=MEMORY_CAPABILITY_NAME,
        describe=_describe,
        validate=_validate,
        handle=_handle,
    )


def makeSubmittingMemoryCapability(
    request: TaskRequest,
    *,
    bot,
    botData: dict,
    autoMode: str,
    opsList: list,
) -> Capability:
    """记忆操作自动执行或送审的能力；参数含义见 submitMemoryAction。"""

    async def _submit(action, operation, task) -> OperationResult:
        origin = request.origin
        return await submitMemoryAction(
            action,
            bot=bot,
            botData=botData,
            autoMode=autoMode,
            opsList=opsList,
            chatID=origin.chatID,
            userID=origin.userID,
            originalMsg=request.displayOriginalMsg,
            displayBlocks=request.displayBlocks,
        )

    return makeMemoryCapability(_submit)


def makeCollectingMemoryCapability(actions: list[MemoryAction]) -> Capability:
    """记忆操作只追加到 actions、不执行也不送审的能力。"""

    async def _collect(action, operation, task) -> OperationResult:
        actions.append(action)
        return OperationResult(status=OperationStatus.SUBMITTED, modelText=_COLLECTED_NOTE)

    return makeMemoryCapability(_collect)




# ============================================================================
# 发送的相关操作
# ============================================================================

async def submitMemoryAction(
    action: MemoryAction,
    *,
    bot,
    botData: dict,
    autoMode: str,
    opsList: list,
    chatID,
    userID,
    originalMsg: str,
    displayBlocks: DisplayBlocks | None = None,
    respectAutoApprove: bool = True,
) -> OperationResult:
    """
    处理一个校验通过的记忆操作：自动批准并执行，或交给第一位 ops 审核。

    参数:
        bot / botData: Telegram Bot 与存放 Telegram 审核项的 bot_data
        autoMode: "console" 时进控制台审核队列，其余发 Telegram 审核卡
        opsList: 审核人列表；首次生成传完整列表，审核重试传 [当前审核人]。为空时操作不提交
        chatID / userID: 入口校验过的可信身份
        originalMsg / displayBlocks: 审核卡上展示的触发消息；控制台队列只用 originalMsg
        respectAutoApprove:
            - True（首次生成）：读 memoryAutoApprove；开启时仅直接执行普通 global
              contextual action，其余 action 进入审核
            - False（审核重试）：不读——重试产生的操作要 ops 看过新回复才能定夺
    """
    if respectAutoApprove and getMemoryAutoApprove() and not await requiresHumanReview(action):
        # 自动批准是有意收窄的生产策略：模型目前几乎总把记忆写入 global，
        # 因而普通 global contextual 可以减少审核延迟；chat/user 与 pinned
        # 仍必须让 operator 看见并确认。executeAction 内还有同一策略的末端门禁。
        success = await executeAction(
            action,
            humanApproved=False,
            actionContext=MemoryActionContext(chatID=chatID, userID=userID),
        )
        status = "成功" if success else "失败"
        await logAction(
            "System", f"LLM 记忆操作自动执行 ({status})",
            formatActionDetail(action),
            LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )
        if success:
            return OperationResult(status=OperationStatus.DONE, modelText="已执行")
        # 自动批准失败也不能让 action 静默消失：失败可能来自暂时的
        # 数据库/并发状态，也可能来自目标在检查后变成受保护状态。
        # 保留到人工队列既不放宽 Global 自动批准策略，又给 operator
        # 一次重试或取消的机会。

    if not opsList:
        await logSystemEvent(
            "LLM 记忆操作无审核人",
            f"操作未提交（无 LLM ops）：{formatActionDetail(action)}",
            LogLevel.WARNING,
        )
        return OperationResult(
            status=OperationStatus.FAILED,
            modelText="没有可用的审核人，操作未提交",
        )

    opsID = opsList[0]
    actDict = await buildMemoryActionReviewPayload(action)
    if autoMode == "console":
        addMemoryReviewItem(
            action=actDict,
            chatID=str(chatID),
            originalMsg=originalMsg,
            opsID=str(opsID),
            userID=userID,
        )
    else:
        await _sendMemoryReviewCard(
            actDict,
            bot=bot,
            botData=botData,
            opsID=int(opsID),
            chatID=int(chatID),
            userID=userID,
            originalMsg=originalMsg,
            displayBlocks=displayBlocks,
        )
    return OperationResult(
        status=OperationStatus.SUBMITTED,
        modelText="已交给管理员审核，批准前不会生效",
    )


async def _sendMemoryReviewCard(
    action: dict,
    *,
    bot,
    botData: dict,
    opsID: int,
    chatID: int,
    userID,
    originalMsg: str,
    displayBlocks: DisplayBlocks | None,
) -> None:
    """给一位 ops 发记忆操作审核卡（带 inline keyboard），并把审核项存进 bot_data。"""
    text, markup = renderMemoryReviewCard(action, originalMsg, chatID, displayBlocks=displayBlocks)
    sent = await bot.send_message(chat_id=opsID, text=text, reply_markup=markup, parse_mode="HTML")

    putMemoryReview(
        botData,
        chatID=chatID,
        reviewMsgID=sent.message_id,
        action=action,
        originalMsg=originalMsg,
        opsID=opsID,
        userID=userID,
        displayBlocks=displayBlocks,
    )
