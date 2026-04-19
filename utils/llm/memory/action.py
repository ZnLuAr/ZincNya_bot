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

from utils.logger import logSystemEvent, logAction, LogLevel, LogChildType

# fire-and-forget task 引用持有，避免被 GC 回收
_backgroundTasks: set[asyncio.Task] = set()


def _fireAndForget(coro):
    """创建后台 task 并持有强引用，会在完成后自动移除。"""
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        _backgroundTasks.add(task)
        task.add_done_callback(_backgroundTasks.discard)
    except RuntimeError:
        pass

from .database import (
    addMemory,
    deleteMemory,
    getMemoryByID,
    updateMemory,
    MEMORY_SCOPE_CHAT,
    MEMORY_SCOPE_GLOBAL,
    MEMORY_SCOPE_USER,
)


LLM_MEMORY_PRIORITY_CAP = 3
LLM_MEMORY_MAX_CONTENT_LEN = 500
LLM_MEMORY_MAX_ACTIONS = 3
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
    action: str
    scopeType: str
    scopeID: str = ""
    content: Optional[str] = None
    tags: Optional[list[str]] = None
    priority: Optional[int] = None
    memoryID: Optional[int] = None
    reason: str = ""

    def toDict(self) -> dict:
        """序列化为内部 dict（camelCase 键名）。"""
        return {
            "action": self.action,
            "scopeType": self.scopeType,
            "scopeID": self.scopeID,
            "content": self.content,
            "tags": self.tags,
            "priority": self.priority,
            "memoryID": self.memoryID,
            "reason": self.reason,
        }

    @classmethod
    def fromDict(cls, data: dict) -> "MemoryAction":
        """从内部 dict（camelCase 键名）反序列化。"""
        return cls(
            action=data["action"],
            scopeType=data["scopeType"],
            scopeID=data.get("scopeID", ""),
            content=data.get("content"),
            tags=data.get("tags"),
            priority=data.get("priority"),
            memoryID=data.get("memoryID"),
            reason=data.get("reason", ""),
        )




def _normalizeTags(tags) -> Optional[list[str]]:
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
    return result


def _parseActionDict(data: dict) -> MemoryAction:
    if not isinstance(data, dict):
        raise ValueError("MEMORY_ACTION 必须是 JSON 对象")

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

    return MemoryAction(
        action=action,
        scopeType=scopeType,
        scopeID=scopeID,
        content=content,
        tags=_normalizeTags(data.get("tags")),
        priority=priority,
        memoryID=memoryID,
        reason=reason,
    )


def formatActionDetail(act: MemoryAction) -> str:
    """格式化单个记忆操作的日志详情。"""
    detail = f"{act.action} | scope={act.scopeType}:{act.scopeID}"
    if act.memoryID is not None:
        detail += f" | id=#{act.memoryID}"
    if act.content:
        detail += f" | {act.content[:100]}"
    return detail


def parseMemoryActions(text: str) -> tuple[str, list[MemoryAction]]:
    """提取并剥离 LLM 输出中的 <MEMORY_ACTION> 块。"""
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
            details = block if len(block) <= 300 else block[:300] + "……"
            try:
                _fireAndForget(
                    logSystemEvent(
                        "LLM memory action JSON 解析失败",
                        f"{e} | {details}",
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
                preview = json.dumps(item, ensure_ascii=False)[:200] if isinstance(item, dict) else str(item)[:200]
                try:
                    _fireAndForget(
                        logSystemEvent(
                            "LLM memory action item 解析失败",
                            f"{e} | {preview}",
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
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, actions


def _normalizeScopeID(scopeType: str, scopeID: str) -> str:
    if scopeType == MEMORY_SCOPE_GLOBAL:
        return "global"
    return str(scopeID).strip()




async def validateAction(action: MemoryAction) -> str | None:
    """校验记忆操作是否合法。返回 None 表示通过。"""
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
        if action.content is None and action.tags is None and action.priority is None:
            return "update 至少要包含 content / tags / priority 之一"

    return None




async def executeAction(action: MemoryAction) -> bool:
    """执行已审核通过的记忆操作。"""
    if action.action == "add":
        memoryID = await addMemory(
            action.scopeType,
            _normalizeScopeID(action.scopeType, action.scopeID),
            action.content or "",
            tags=action.tags or [],
            priority=action.priority if action.priority is not None else 0,
            source="inferred",
        )
        return memoryID is not None

    if action.action == "update" and action.memoryID is not None:
        return await updateMemory(
            action.memoryID,
            content=action.content,
            tags=action.tags,
            priority=action.priority,
        )

    if action.action == "delete" and action.memoryID is not None:
        return await deleteMemory(action.memoryID)

    return False