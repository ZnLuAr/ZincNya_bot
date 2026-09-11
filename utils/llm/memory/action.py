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




@dataclass
class MemoryAction:
    """LLM 请求的单个 memory 操作的内部表示。

    模型输出使用 snake_case JSON，审核队列和数据库调用使用驼峰法；
    `_parseActionDict` 与 `toDict`/`fromDict` 是这两个边界之间的转换层。
    类保持可变，是因为校验阶段会把 mode 和 retrievalHint 规范化到最终值。
    """

    action: str
    scopeType: str
    scopeID: str = ""
    content: Optional[str] = None
    tags: Optional[list[str]] = None
    priority: Optional[int] = None
    memoryID: Optional[int] = None
    mode: Optional[str] = None              # mode 可能的值为：
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

    priorityRaw = data.get("priority")
    if priorityRaw in (None, ""):
        priority = None
    else:
        priority = int(priorityRaw)

    memoryIDRaw = data.get("memory_id")
    if memoryIDRaw in (None, ""):
        memoryID = None
    else:
        memoryID = int(memoryIDRaw)

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




async def validateAction(action: MemoryAction) -> str | None:
    """校验记忆操作是否合法返回 None 表示通过"""
    if action.action not in _VALID_ACTIONS:
        return f"不支持的 action：{action.action or '?'}"

    if action.scopeType not in _VALID_SCOPE_TYPES:
        return f"不支持的 scope_type：{action.scopeType or '?'}"

    normalizedScopeID = _normalizeScopeID(action.scopeType, action.scopeID)
    if action.scopeType != MEMORY_SCOPE_GLOBAL and not normalizedScopeID:
        return "非 global scope 必须提供 scope_id"

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
    """判断操作是否越过 contextual memory 的自动写入边界。"""
    if action.action == "add":
        return action.mode == MEMORY_MODE_PINNED

    if target is None and action.memoryID is not None:
        target = await getMemoryByID(action.memoryID)
    if target and target.get("mode") == MEMORY_MODE_PINNED:
        return True
    return action.action == "update" and action.mode == MEMORY_MODE_PINNED


async def executeAction(
    action: MemoryAction,
    *,
    humanApproved: bool = False,
    expectedState: Optional[str] = None,
) -> bool:
    """校验并执行模型申请的 memory 操作。

    涉及 pinned 的操作只接受 `humanApproved=True`；update/delete 总是通过
    `MemoryWriteGuard` 绑定执行前状态，人工批准还必须带审核卡保存的
    `expectedState`，防止旧审核覆盖新数据。
    """
    validationError = await validateAction(action)
    if validationError:
        await logSystemEvent(
            "LLM memory 执行前校验失败",
            f"action={action.action}, reason=validation",
            LogLevel.WARNING,
        )
        return False

    if action.action == "add":
        if await requiresHumanReview(action) and not humanApproved:
            await logSystemEvent(
                "LLM memory pinned 操作被拒绝",
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
            "LLM memory pinned 操作被拒绝",
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

    # 非 pinned 的 inferred memory 可以自动更新，但所有已有记录仍使用同一
    # 状态快照，防止模型生成到审核批准之间发生静默覆盖。
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
    构造记忆操作的审核展示 payload：toDict 后，对带 memoryID 且无 content 的操作（典型为
    update/delete）补 originalContent（查库取原内容），供审核卡片显示「改前内容」。
    """
    actDict = act.toDict()
    if act.memoryID is not None:
        target = await getMemoryByID(act.memoryID)
        if target:
            actDict["originalContent"] = target.get("content", "")
            actDict["originalMode"] = target.get("mode", MEMORY_MODE_CONTEXTUAL)
            actDict["targetState"] = buildMemoryStateFingerprint(target)
    return actDict
