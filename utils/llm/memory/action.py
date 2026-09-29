"""
utils/llm/memory/action.py

LLM 自主记忆操作：
    - 解析 <MEMORY_ACTION> 块
    - 校验 add/update/delete 合法性
    - 执行对应的 memory 数据库操作
"""

import re
import json
import asyncio
from typing import Optional
from dataclasses import dataclass

from config import (
    LLM_MEMORY_PRIORITY_CAP,
    LLM_MEMORY_MAX_CONTENT_LEN,
    LLM_MEMORY_MAX_TAGS,
)

from utils.core.logger import logSystemEvent, logAction, LogLevel, LogChildType

# fire-and-forget task 引用持有，避免被 GC 回收
_backgroundTasks: set[asyncio.Task] = set()




def _fireAndForget(coro):
    """创建后台 task 并持有强引用，会在完成后自动移除。

    在没有事件循环（同步上下文调用，如测试）时关闭协程释放资源——
    不关闭会留下 never awaited 的 RuntimeWarning，且协程体永不执行。
    """
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        _backgroundTasks.add(task)
        task.add_done_callback(_backgroundTasks.discard)
    except RuntimeError:
        coro.close()

from .database import (
    addMemory,
    deleteMemory,
    getMemoryByID,
    normalizeMemoryMode,
    normalizeRetrievalHint,
    updateMemory,
    MEMORY_MODE_CONTEXTUAL,
    MEMORY_MODE_PINNED,
    MEMORY_SCOPE_CHAT,
    MEMORY_SCOPE_GLOBAL,
    MEMORY_SCOPE_USER,
)
from .types import MemoryWriteGuard, buildMemoryStateFingerprint


_VALID_ACTIONS = {"add", "update", "delete"}
_VALID_SCOPE_TYPES = {
    MEMORY_SCOPE_GLOBAL,
    MEMORY_SCOPE_CHAT,
    MEMORY_SCOPE_USER,
}
MEMORY_ACTION_PATTERN = re.compile(
    r"<MEMORY_ACTION>(.*?)</MEMORY_ACTION>",
    re.DOTALL,
)




@dataclass(frozen=True, kw_only=True)
class MemoryActionContext:
    """
    当前生成请求的可信身份，用于约束模型提交的 scope。

    `MemoryAction.scopeID` 来自模型输出，不能被当作授权凭据；这里的
    chat/user ID 则由 Telegram 入口在白名单校验后沿调用链传入。

    但 global 没有会话归属，模型将能在任意已授权的 LLM 请求中写入普通
    global memory，因此不要求对应的 chat/user ID。这是一个值得注意的
    注入攻击面。

    首次生成的自动批准策略比身份校验更窄：只有普通 global
    contextual action 可以绕过人工审核。chat/user 即使是 contextual
    也必须进入人工审核；pinned 则无论 scope 都必须人工批准。
    """

    chatID: str | int | None = None
    userID: str | int | None = None


@dataclass
class MemoryAction:
    """LLM 请求的单个 memory 操作的内部表示。

    模型输出使用 snake_case JSON，审核队列和数据库调用使用驼峰法；
    `_parseActionDict` 与 `toDict`/`fromDict` 是这两个边界之间的转换层。
    类保持可变，是因为校验阶段会把 mode 和 retrievalHint 规范化到最终值。

    `mode=None` 表示 add 使用默认 `contextual`、update 保留原值；
    `contextual` 需要通过相关性检索，`pinned` 使用常驻预算且任何模型自主
    写入都必须人工批准。`retrievalHint=None` 表示未提供，空字符串表示
    update 时显式清除，二者不能合并处理。
    """

    action: str
    scopeType: str
    scopeID: str = ""
    content: Optional[str] = None
    tags: Optional[list[str]] = None
    priority: Optional[int] = None
    memoryID: Optional[int] = None
    mode: Optional[str] = None
    retrievalHint: Optional[str] = None
    reason: str = ""

    def toDict(self) -> dict:
        """序列化为内部 dict（camelCase 键名）"""
        return {
            "action": self.action,
            "scopeType": self.scopeType,
            "scopeID": self.scopeID,
            "content": self.content,
            "tags": self.tags,
            "priority": self.priority,
            "memoryID": self.memoryID,
            "mode": self.mode,
            "retrievalHint": self.retrievalHint,
            "reason": self.reason,
        }

    @classmethod
    def fromDict(cls, data: dict) -> "MemoryAction":
        """从内部 dict（camelCase 键名）反序列化"""
        return cls(
            action=data["action"],
            scopeType=data["scopeType"],
            scopeID=data.get("scopeID", ""),
            content=data.get("content"),
            tags=data.get("tags"),
            priority=data.get("priority"),
            memoryID=data.get("memoryID"),
            mode=data.get("mode"),
            retrievalHint=data.get("retrievalHint"),
            reason=data.get("reason", ""),
        )




def _normalizeTags(tags) -> Optional[list[str]]:
    """把不可信 tags 转为去重字符串列表，并按模型写入上限截断。"""
    if tags is None:
        return None
    if not isinstance(tags, list):
        raise ValueError("tags 必须是数组")

    result: list[str] = []
    seen = set()
    for tag in tags:
        tag = str(tag).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        result.append(tag)
        if len(result) >= LLM_MEMORY_MAX_TAGS:
            break
    return result


def _optionalInt(raw, field: str) -> Optional[int]:
    """把模型给的整数字段转为 int；缺省返回 None，布尔值直接拒绝。

    JSON 的 true/false 在 Python 里是 bool，而 int(True) == 1：不拦下来，
    priority 会被悄悄当成 1，memory_id 会误指 #1。
    """
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{field} 必须是整数")
    return int(raw)


def _parseActionDict(data: dict) -> MemoryAction:
    """将模型的 snake_case JSON 对象规范化为内部的 `MemoryAction`。"""
    if not isinstance(data, dict):
        raise ValueError("MEMORY_ACTION 必须是 JSON 对象")

    # 这里是 LLM 不可信输出的第一道边界：先容忍类型差异并转成内部形态，
    # 再由 validateAction 统一检查 action、scope、长度和目标记录。
    action = str(data.get("action", "")).strip().lower()
    scopeType = str(data.get("scope_type", "")).strip().lower()

    scopeIDRaw = data.get("scope_id", "")
    scopeID = "" if scopeIDRaw is None else str(scopeIDRaw).strip()

    contentRaw = data.get("content")
    content = None if contentRaw is None else str(contentRaw).strip()

    priority = _optionalInt(data.get("priority"), "priority")
    memoryID = _optionalInt(data.get("memory_id"), "memory_id")

    reason = str(data.get("reason", "")).strip()

    modeRaw = data.get("mode")
    mode = None if modeRaw is None else str(modeRaw).strip().lower()

    retrievalHintRaw = data.get("retrieval_hint")
    # 空字符串有“清除旧 hint”的语义，不能与字段缺失混为一谈；数据库更新
    # 会据此区分显式清空、保留原值和内容变更后的自动失效。
    if isinstance(retrievalHintRaw, str) and not retrievalHintRaw.strip():
        retrievalHint = ""
        hintReason = None
    else:
        retrievalHint, hintReason = normalizeRetrievalHint(retrievalHintRaw)
    if hintReason:
        _fireAndForget(logSystemEvent(
            "LLM memory 检索说明已弃用",
            f"action={action or '?'}, reason={hintReason}",
            LogLevel.WARNING,
        ))

    return MemoryAction(
        action=action,
        scopeType=scopeType,
        scopeID=scopeID,
        content=content,
        tags=_normalizeTags(data.get("tags")),
        priority=priority,
        memoryID=memoryID,
        mode=mode,
        retrievalHint=retrievalHint,
        reason=reason,
    )


def formatActionDetail(act: MemoryAction) -> str:
    """格式化单个记忆操作的日志详情"""
    detail = f"{act.action} | scope={act.scopeType}:{act.scopeID}"
    if act.memoryID is not None:
        detail += f" | id=#{act.memoryID}"
    if act.mode is not None:
        detail += f" | mode={act.mode}"
    return detail


def parseMemoryActions(text: str) -> tuple[str, list[MemoryAction]]:
    """
    提取并剥离 LLM 输出中的 <MEMORY_ACTION> 块

    正常路径：
        - 正则匹配标准格式 <MEMORY_ACTION>...</MEMORY_ACTION>
        - 逐块解析 JSON 并构造 MemoryAction
        - 剥离匹配到的块，返回清理后的文本

    回退清理（仅在检测到 MEMORY_ACTION 关键字时启用）：
        - 清理残留的格式错误标签（大小写不敏感，容忍拼写错误如 ACTI0N）
        - 清理孤立的 JSON 块（疑似记忆操作但标签缺失，限单行）
        - 避免误删用户正常对话中的 JSON

    返回:
        清理后的文本, 即解析成功的 MemoryAction 列表
    """
    if not text:
        return "", []

    actions: list[MemoryAction] = []
    for match in MEMORY_ACTION_PATTERN.finditer(text):
        block = match.group(1).strip()
        if not block:
            continue

        # 外层 try：仅捕获 JSON 解析失败（整块无法解析）
        try:
            raw = json.loads(block)
        except Exception as e:
            try:
                _fireAndForget(
                    logSystemEvent(
                        "LLM memory action JSON 解析失败",
                        f"errorType={type(e).__name__}, inputType=memoryActionBlock",
                        LogLevel.WARNING,
                        childType=LogChildType.WITH_ONE_CHILD,
                    )
                )
            except Exception:
                pass
            continue

        # 兼容 LLM 在单个块中输出数组 [{...}, {...}] 的情况
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            # 内层 try：单 item 解析失败时跳过该 item，不影响后续
            try:
                act = _parseActionDict(item)
            except Exception as e:
                try:
                    _fireAndForget(
                        logSystemEvent(
                            "LLM memory action item 解析失败",
                            f"errorType={type(e).__name__}, inputType={type(item).__name__}",
                            LogLevel.WARNING,
                            childType=LogChildType.WITH_ONE_CHILD,
                        )
                    )
                except Exception:
                    pass
                continue

            actions.append(act)
            try:
                _fireAndForget(
                    logAction(
                        "System",
                        "LLM 请求操作 Memory",
                        formatActionDetail(act),
                        level=LogLevel.INFO,
                        childType=LogChildType.WITH_ONE_CHILD,
                    )
                )
            except Exception:
                pass

    cleaned = MEMORY_ACTION_PATTERN.sub("", text).strip()

    # 回退清理：仅在检测到 MEMORY_ACTION 关键字时启用（避免误删用户正常对话）
    if "MEMORY_ACTION" in text.upper() or "MEMORY_ACTI" in text.upper():
        # 清理残留的格式错误标签（大小写不敏感，容忍拼写错误）
        # 匹配 <MEMORY_ACTION 或 <MEMORY_ACTI 开头的标签（容忍常见拼写错误如 ACTI0N）
        cleaned = re.sub(r"</?MEMORY_ACTI(?:ON|0N)[^>]*>", "", cleaned, flags=re.IGNORECASE)
        # 清理孤立的 JSON 块（疑似记忆操作但标签缺失）
        # 同时覆盖两种残留形态：
        #   1) JSON 独占整行（^...$）
        #   2) JSON 夹在正常文本中间（如「Reply {"action":...} 谢谢」）——
        #      标签丢失时若只删整行 JSON，行内残留会把记忆操作负载泄露给用户
        # 限制 [^}] 匹配长度防止灾难性回溯
        cleaned = re.sub(
            r'^\s*\{[^}]{0,500}"action"\s*:\s*"(?:add|update|delete)"[^}]{0,500}\}\s*$',
            "",
            cleaned,
            flags=re.MULTILINE | re.IGNORECASE
        )
        cleaned = re.sub(
            r'\{[^}]{0,500}"action"\s*:\s*"(?:add|update|delete)"[^}]{0,500}\}',
            "",
            cleaned,
            flags=re.IGNORECASE
        )

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, actions


def _normalizeScopeID(scopeType: str, scopeID: str) -> str:
    """统一 action scope ID；global 始终使用固定值 `global`。"""
    if scopeType == MEMORY_SCOPE_GLOBAL:
        return "global"
    return str(scopeID).strip()




def _validateActionScopeAuthorization(
    action: MemoryAction,
    normalizedScopeID: str,
    actionContext: MemoryActionContext | None,
) -> str | None:
    """
    校验模型声明的 scope 是否落在当前请求的可信身份内。

    global 是有意保留的策略例外：它本来就不属于某个 chat/user，且
    生产中的模型记忆主要落在该层。相反，chat/user scope 没有可信身份时
    必须拒绝，不能让模型通过伪造 `scope_id` 跨会话或跨用户写入。

    这里的 global 例外是有意接受的产品取舍，而不是把 global 当成
    “安全可信”的输入：开启自动批准时，模型仍可能把不理想的内容写入
    共享记忆。因此自动批准开关本身代表 operator 对共享记忆风险的授权；
    ``requiresHumanReview`` 还会把 chat/user action 保留在人工审核路径；
    下面的身份绑定则负责阻断跨会话、跨用户写入。两者是不同门禁，不能
    用“身份匹配”替代人工批准。
    """
    if action.scopeType == MEMORY_SCOPE_GLOBAL:
        return None

    if actionContext is None:
        return "非 global memory 缺少当前请求身份，拒绝执行"

    expectedID = (
        actionContext.chatID
        if action.scopeType == MEMORY_SCOPE_CHAT
        else actionContext.userID
    )
    if expectedID is None or not str(expectedID).strip():
        return f"{action.scopeType} memory 缺少当前请求身份，拒绝执行"

    if normalizedScopeID != str(expectedID).strip():
        return f"{action.scopeType} memory scope 不属于当前请求"
    return None


async def validateAction(
    action: MemoryAction,
    *,
    actionContext: MemoryActionContext | None = None,
) -> str | None:
    """
    校验记忆操作是否合法，返回 None 表示通过。

    `actionContext` 是调用链提供的可信授权边界，不是模型输出的一部分。
    global action 可以不依赖身份；chat/user action 若没有匹配身份则 fail
    closed，即使其字段格式和目标 memory 本身都合法也不能进入审核或执行。
    """
    if action.action not in _VALID_ACTIONS:
        return f"不支持的 action：{action.action or '?'}"

    if action.scopeType not in _VALID_SCOPE_TYPES:
        return f"不支持的 scope_type：{action.scopeType or '?'}"

    normalizedScopeID = _normalizeScopeID(action.scopeType, action.scopeID)
    if action.scopeType != MEMORY_SCOPE_GLOBAL and not normalizedScopeID:
        return "非 global scope 必须提供 scope_id"

    scopeAuthorizationError = _validateActionScopeAuthorization(
        action,
        normalizedScopeID,
        actionContext,
    )
    if scopeAuthorizationError:
        return scopeAuthorizationError

    if action.priority is not None:
        if action.priority < 0:
            return "priority 不能小于 0"
        if action.priority > LLM_MEMORY_PRIORITY_CAP:
            return f"priority 不能超过 {LLM_MEMORY_PRIORITY_CAP}"

    if action.mode is not None:
        try:
            action.mode = normalizeMemoryMode(action.mode)
        except ValueError as exc:
            return str(exc)

    if action.retrievalHint is not None:
        if action.retrievalHint == "":
            pass
        else:
            normalizedHint, hintReason = normalizeRetrievalHint(action.retrievalHint)
            if hintReason:
                action.retrievalHint = None
                await logSystemEvent(
                    "LLM memory 检索说明已弃用",
                    f"action={action.action}, reason={hintReason}",
                    LogLevel.WARNING,
                )
            else:
                action.retrievalHint = normalizedHint

    if action.action in {"add", "update"}:
        if action.content is not None:
            if not action.content.strip():
                return "content 不能为空"
            if len(action.content) > LLM_MEMORY_MAX_CONTENT_LEN:
                return f"content 不能超过 {LLM_MEMORY_MAX_CONTENT_LEN} 字"

    if action.action == "add":
        if not action.content:
            return "add 必须提供 content"
        return None

    if action.memoryID is None:
        return f"{action.action} 必须提供 memory_id"

    # update/delete 必须重新读取目标，而不是相信模型提交的 scope；这样 scope
    # 校验和 inferred 限制都基于数据库真实状态。
    target = await getMemoryByID(action.memoryID)
    if not target:
        return f"memory #{action.memoryID} 不存在"

    if target.get("source") != "inferred":
        return f"memory #{action.memoryID} 不是 inferred，禁止修改"

    # mode 是审核策略的关键输入；数据库 schema 虽然声明了默认值，历史迁移或
    # 人工改库仍可能留下 NULL/未知值。无法判断它是否属于 pinned 时，直接拒绝
    # 模型操作，而不是把坏状态当 contextual 放行。
    targetMode = target.get("mode", MEMORY_MODE_CONTEXTUAL)
    if targetMode not in (MEMORY_MODE_CONTEXTUAL, MEMORY_MODE_PINNED):
        return f"memory #{action.memoryID} 的 mode 无效，拒绝模型操作"

    targetScopeType = str(target.get("scope_type", "")).strip().lower()
    targetScopeID = _normalizeScopeID(targetScopeType, str(target.get("scope_id", "")))
    if action.scopeType != targetScopeType or normalizedScopeID != targetScopeID:
        return f"memory #{action.memoryID} 的 scope 不匹配"

    if action.action == "update":
        if all(value is None for value in (
            action.content,
            action.tags,
            action.priority,
            action.mode,
            action.retrievalHint,
        )):
            return "update 至少要包含 content / tags / priority / mode / retrieval_hint 之一"

    return None




async def requiresHumanReview(
    action: MemoryAction,
    target: Optional[dict] = None,
) -> bool:
    """判断首次生成的 action 是否必须进入人工审核。

    自动批准是一个有意收窄的产品例外：只允许普通 global contextual
    action（add，或指向 contextual 目标的 update/delete）直接写入。chat
    和 user scope 即使内容是 contextual，也必须由 operator 确认；pinned
    新增、升级、修改和删除同样必须人工批准。此函数只判断审核策略，
    scope 是否属于当前请求仍由 ``MemoryActionContext`` 校验。

    retry/feedback 不依赖本函数决定最终路径，调用方会显式关闭自动批准。
    """
    # 私有 scope 不是生产中观察到的主要写入形态，且自动放行会扩大模型
    # 对具体聊天/用户数据的修改面；即使是 contextual，也保留人工复核。
    if action.scopeType != MEMORY_SCOPE_GLOBAL:
        return True

    if action.action == "add":
        # mode=None 在 add 时由执行层解释为 contextual；未知 mode 也不
        # 走自动路径，避免策略函数把未规范化输入误当成安全默认值。
        return action.mode not in (None, MEMORY_MODE_CONTEXTUAL)

    if target is None and action.memoryID is not None:
        target = await getMemoryByID(action.memoryID)

    # 目标读不到时采取保守策略：正常审核编排会在此前的 validateAction
    # 阶段丢弃它，但直接调用 executeAction 也不能因缺少快照而自动写入。
    if target is None:
        return True
    targetMode = target.get("mode", MEMORY_MODE_CONTEXTUAL)
    # 未知 mode 无法证明是普通 contextual；自动路径必须停下，交给人工或
    # 管理员先修复数据。正常 executeAction 还会在 validateAction 再次拒绝它。
    if targetMode not in (MEMORY_MODE_CONTEXTUAL, MEMORY_MODE_PINNED):
        return True
    if targetMode == MEMORY_MODE_PINNED:
        return True

    # update 未提供 mode 表示保留目标原值；只有显式升级为 pinned 时需要
    # 额外审核。delete 不会改变 mode，因此 contextual global delete 可自动执行。
    return action.action == "update" and action.mode not in (
        None,
        MEMORY_MODE_CONTEXTUAL,
    )




async def executeAction(
    action: MemoryAction,
    *,
    humanApproved: bool = False,
    expectedState: Optional[str] = None,
    actionContext: MemoryActionContext | None = None,
) -> bool:
    """校验并执行模型申请的 memory 操作。

    非 global contextual 或涉及 pinned 的操作只接受 `humanApproved=True`；
    update/delete 总是通过 `MemoryWriteGuard` 绑定执行前状态，人工批准还
    必须带审核卡保存的 `expectedState`，防止旧审核覆盖新数据。scope 授权
    在这里再次校验，因为审核队列和自动执行之间可能经过较长时间，不能只
    相信入队时的结果。
    """
    validationError = await validateAction(
        action,
        actionContext=actionContext,
    )
    if validationError:
        await logSystemEvent(
            "LLM memory 执行前校验失败",
            f"action={action.action}, reason=validation",
            LogLevel.WARNING,
        )
        return False

    if action.action == "add":
        # validateAction 已检查过 scope；在真正写入前保留同一授权上下文，
        # 让所有执行入口（自动执行、console、Telegram 审核）共享一套边界。
        # 这是最后一道写入门禁：即使调用方绕过 review 编排直接调用本函数，
        # chat/user 或 pinned action 也不能凭模型输出自动落库。
        if await requiresHumanReview(action) and not humanApproved:
            await logSystemEvent(
                "LLM memory 需要人工审核的操作被拒绝",
                "action=add, reason=humanApprovalRequired",
                LogLevel.WARNING,
            )
            return False
        memoryID = await addMemory(
            action.scopeType,
            _normalizeScopeID(action.scopeType, action.scopeID),
            action.content or "",
            tags=action.tags or [],
            priority=action.priority if action.priority is not None else 0,
            source="inferred",
            mode=action.mode or MEMORY_MODE_CONTEXTUAL,
            retrievalHint=action.retrievalHint,
        )
        return memoryID is not None

    target = await getMemoryByID(action.memoryID) if action.memoryID is not None else None
    if target is None:
        return False

    protected = await requiresHumanReview(action, target)
    if protected and not humanApproved:
        await logSystemEvent(
            "LLM memory 需要人工审核的操作被拒绝",
            f"action={action.action}, id={action.memoryID}, reason=humanApprovalRequired",
            LogLevel.WARNING,
        )
        return False
    if humanApproved and expectedState is None:
        await logSystemEvent(
            "LLM memory 审核状态缺失",
            f"action={action.action}, id={action.memoryID}",
            LogLevel.WARNING,
        )
        return False

    # 允许执行的 global contextual inferred memory 仍使用同一状态快照，
    # 防止模型生成到自动执行/人工批准之间发生静默覆盖；chat/user 和
    # pinned 会先在上面的 policy gate 被拦截或转入审核。
    guard = MemoryWriteGuard(
        expectedState=expectedState or buildMemoryStateFingerprint(target),
        scopeType=action.scopeType,
        scopeID=_normalizeScopeID(action.scopeType, action.scopeID),
        allowPinned=humanApproved,
    )

    if action.action == "update" and action.memoryID is not None:
        return await updateMemory(
            action.memoryID,
            content=action.content,
            tags=action.tags,
            priority=action.priority,
            mode=action.mode,
            retrievalHint=action.retrievalHint,
            guard=guard,
        )

    if action.action == "delete" and action.memoryID is not None:
        return await deleteMemory(action.memoryID, guard=guard)

    return False




async def buildMemoryActionReviewPayload(act: MemoryAction) -> dict:
    """
    构造可跨审核入口往返的 memory action payload。

    基础字段来自 `MemoryAction.toDict()`；若操作指向已有记录，再附加审核
    展示使用的 `originalContent` / `originalMode`，以及批准时用于数据库
    条件写入的 `targetState`。后者绑定审核时的完整记录状态，避免旧卡片
    覆盖审核期间发生的更新。
    """
    actDict = act.toDict()
    if act.memoryID is not None:
        target = await getMemoryByID(act.memoryID)
        if target:
            actDict["originalContent"] = target.get("content", "")
            actDict["originalMode"] = target.get("mode", MEMORY_MODE_CONTEXTUAL)
            actDict["targetState"] = buildMemoryStateFingerprint(target)
    return actDict
