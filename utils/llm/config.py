"""
utils/llm/config.py

LLM 配置管理：
    - 加载/保存 llmConfig.json（开关、审核模式、主模型、视觉模型、
      群聊触发模式与关键词、长期记忆、知识库）
    - 加载 prompts.json（不存在或解析失败时 fallback 到内嵌的"褪色"占位 prompt）

settable 配置都会通过 _setConfig 加锁串行写盘（read-modify-write 安全）。
"""

import os
import json
import threading
import math
from enum import IntEnum

from config import (
    LLM_CONFIG_PATH,
    LLM_PROMPTS_PATH,
    LLM_DEFAULT_MODEL,
    LLM_MEMORY_SELECTOR_MAX_SECONDS,
    PROJECT_ROOT,
)




# ── 上下文层级 ──────────────────────────────────────

class ContextTier(IntEnum):
    """上下文块层级（数值越小越靠前，优先级越高）。

    各模块按"自己是什么内容"认领档位，无需关心其它模块。
    档位间留 100 的间隔，方便未来插入新档。
    """
    CRITICAL      = 100   # 强约束 / 安全指令
    TOOLS         = 200   # 工具与能力声明（AFC 等）
    KNOWLEDGE     = 300   # 高信任背景知识（知识库）
    SUPPLEMENTARY = 400   # 一般补充信息
    LOW_TRUST     = 500   # 低信任内容（记忆 / 历史）


# 配置 read-modify-write 串行化，避免 Telegram / console 并发改配置时丢更新
_configLock = threading.Lock()


# 默认配置（首次运行或文件损坏时使用）
_DEFAULT_CONFIG = {
    "enabled": False,
    "autoMode": "console",  # "on" | "off" | "console"
    "model": LLM_DEFAULT_MODEL,
    "visionModel": LLM_DEFAULT_MODEL,
    "forceFallbackPrompt": False,
    "groupTriggerMode": "mention",  # "mention" | "keyword"
    "groupTriggerKeywords": [],
    "memoryEnabled": False,
    "memoryAutoApprove": False,
    "memoryRetrievalMode": "legacy",
    "memoryHybridSelector": "local",
    "memorySelectorProtocol": "responses",
    "memorySelectorModel": "gpt-5.6-terra",
    "memorySelectorEffort": "high",
    "memorySelectorTimeoutSeconds": 30.0,
    "knowledgeEnabled": True,
    "knowledgeMaxResults": 3,
    "knowledgeMinScore": 0.5,
}




def loadLLMConfig() -> dict:
    """加载 LLM 配置，失败时返回默认值"""
    try:
        if os.path.exists(LLM_CONFIG_PATH):
            with open(LLM_CONFIG_PATH, "r", encoding="utf-8") as f:
                return _DEFAULT_CONFIG | json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return _DEFAULT_CONFIG.copy()


def saveLLMConfig(config: dict) -> bool:
    """
    保存 LLM 配置到文件，失败时返回 False

    使用 tmpfile + os.replace 原子写：避免崩溃 / 并发时出现半截文件。
    """
    tmpPath = LLM_CONFIG_PATH + ".tmp"
    try:
        with open(tmpPath, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        os.replace(tmpPath, LLM_CONFIG_PATH)
        return True
    except OSError as e:
        from utils.core.stateManager import safePrint
        safePrint(f"[LLM] 配置保存失败：{e}")
        try:
            os.remove(tmpPath)
        except OSError:
            pass
        return False


def _setConfig(**kwargs):
    """更新配置项并保存（加锁，避免并发 read-modify-write 丢更新）"""
    with _configLock:
        cfg = loadLLMConfig()
        cfg.update(kwargs)
        saveLLMConfig(cfg)




# ── 开关 ──────────────────────────────────────────────

def getLLMEnabled() -> bool:
    return loadLLMConfig()["enabled"]


def setLLMEnabled(enabled: bool):
    _setConfig(enabled=enabled)




# ── 审核模式 ────────────────────────────────────────

def getAutoMode() -> str:
    """返回 "on" | "off" | "console" """
    return loadLLMConfig()["autoMode"]


def setAutoMode(mode: str):
    if mode not in ("on", "off", "console"):
        raise ValueError(f"无效的 autoMode：{mode}")
    _setConfig(autoMode=mode)




# ── 模型 ────────────────────────────────────────────

def getModel() -> str:
    return loadLLMConfig()["model"]


def setModel(model: str):
    _setConfig(model=model)


def getVisionModel() -> str:
    """返回图片描述专用模型名（默认 claude-sonnet-4-6）。"""
    return loadLLMConfig()["visionModel"]


def setVisionModel(model: str):
    _setConfig(visionModel=model)




# ── 强制提示词回退 ──────────────────────────────────

def getForceFallbackPrompt() -> bool:
    return loadLLMConfig()["forceFallbackPrompt"]


def setForceFallbackPrompt(enabled: bool):
    _setConfig(forceFallbackPrompt=enabled)




# ── 群聊触发 ────────────────────────────────────────

_GROUP_TRIGGER_MODES = {"mention", "keyword"}
_GROUP_TRIGGER_KEYWORD_MAX_LEN = 32


def _normalizeGroupTriggerKeyword(keyword: str) -> str:
    keyword = str(keyword).strip().lower()
    if not keyword:
        raise ValueError("关键词不能为空")
    if any(ch.isspace() for ch in keyword):
        raise ValueError("关键词不能包含空白字符")
    if len(keyword) > _GROUP_TRIGGER_KEYWORD_MAX_LEN:
        raise ValueError(f"关键词最长 {_GROUP_TRIGGER_KEYWORD_MAX_LEN} 字符")
    return keyword


def getGroupTriggerMode() -> str:
    mode = loadLLMConfig().get("groupTriggerMode", "mention")
    return mode if mode in _GROUP_TRIGGER_MODES else "mention"


def setGroupTriggerMode(mode: str):
    mode = str(mode).strip().lower()
    if mode not in _GROUP_TRIGGER_MODES:
        raise ValueError(f"无效的 groupTriggerMode：{mode}")
    _setConfig(groupTriggerMode=mode)


def getGroupTriggerKeywords() -> list[str]:
    raw = loadLLMConfig().get("groupTriggerKeywords", [])
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    seen = set()
    for item in raw:
        try:
            keyword = _normalizeGroupTriggerKeyword(item)
        except ValueError:
            continue
        if keyword not in seen:
            seen.add(keyword)
            result.append(keyword)
    return result


def setGroupTriggerKeywords(keywords: list[str]):
    result: list[str] = []
    seen = set()
    for item in keywords:
        keyword = _normalizeGroupTriggerKeyword(item)
        if keyword not in seen:
            seen.add(keyword)
            result.append(keyword)
    _setConfig(groupTriggerKeywords=result)


def addGroupTriggerKeyword(keyword: str):
    keywords = getGroupTriggerKeywords()
    keyword = _normalizeGroupTriggerKeyword(keyword)
    if keyword not in keywords:
        keywords.append(keyword)
        _setConfig(groupTriggerKeywords=keywords)


def removeGroupTriggerKeyword(keyword: str):
    keyword = _normalizeGroupTriggerKeyword(keyword)
    keywords = [kw for kw in getGroupTriggerKeywords() if kw != keyword]
    _setConfig(groupTriggerKeywords=keywords)




# ── 记忆 ────────────────────────────────────────────

def getMemoryEnabled() -> bool:
    return loadLLMConfig()["memoryEnabled"]


def setMemoryEnabled(enabled: bool):
    _setConfig(memoryEnabled=enabled)


def getMemoryAutoApprove() -> bool:
    return loadLLMConfig()["memoryAutoApprove"]


def setMemoryAutoApprove(enabled: bool):
    _setConfig(memoryAutoApprove=enabled)


def getMemoryRetrievalMode() -> str:
    """读取检索模式；未知配置值按兼容模式 `legacy` 处理。"""
    mode = loadLLMConfig().get("memoryRetrievalMode", "legacy")
    return mode if mode in {"legacy", "hybrid"} else "legacy"


def setMemoryRetrievalMode(mode: str):
    """保存检索模式，并尝试唤醒已注册 runtime 重新评估工作状态。"""
    mode = str(mode).strip().lower()
    if mode not in {"legacy", "hybrid"}:
        raise ValueError(f"无效的 memoryRetrievalMode：{mode}")
    _setConfig(memoryRetrievalMode=mode)
    # 配置持久化不依赖 runtime；通知失败时，后台仍会在下一次正常唤醒后读取新值。
    try:
        from utils.core.stateManager import getStateManager
        runtime = getStateManager().getMemoryRuntime()
        if runtime is not None:
            runtime.notifyModeChanged()
    except Exception:
        pass


def getMemorySelectorSettings(configSnapshot: dict) -> dict:
    """验证单次请求的选择配置；非法值拒绝远程选择，不扩大等待预算。"""
    backend = configSnapshot.get("memoryHybridSelector", _DEFAULT_CONFIG["memoryHybridSelector"])
    protocol = configSnapshot.get("memorySelectorProtocol", _DEFAULT_CONFIG["memorySelectorProtocol"])
    model = configSnapshot.get("memorySelectorModel", _DEFAULT_CONFIG["memorySelectorModel"])
    effort = configSnapshot.get("memorySelectorEffort", _DEFAULT_CONFIG["memorySelectorEffort"])
    timeout = configSnapshot.get("memorySelectorTimeoutSeconds", _DEFAULT_CONFIG["memorySelectorTimeoutSeconds"])
    # 不把拼错的后端或非有限时限悄悄转成另一种远程行为。
    if backend not in ("local", "llm") or protocol not in ("responses", "messages"):
        raise ValueError("selectorConfig")
    if not isinstance(model, str) or not model.strip() or len(model) > 128:
        raise ValueError("selectorConfig")
    if effort not in ("low", "medium", "high", "xhigh", "max", "ultra"):
        raise ValueError("selectorConfig")
    if isinstance(timeout, bool):
        raise ValueError("selectorConfig")
    try:
        timeout = float(timeout)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("selectorConfig") from None
    if not math.isfinite(timeout) or not 0 < timeout <= LLM_MEMORY_SELECTOR_MAX_SECONDS:
        raise ValueError("selectorConfig")
    return {"backend": backend, "protocol": protocol, "model": model.strip(),
            "effort": effort, "timeoutSeconds": timeout}




# ── 提示词 ─────────────────────────────────────────

_FALLBACK_PROMPTS = {
    "system_prompt": [
        "你是锌酱，当你看到这段提示词时，你应该知道你原本的系统提示词文件丢失了。"
        "你需要以平淡的语气提醒用户：你的“心”丢了，现在的你并不完整。"
        "回答保持简短。"
    ],
    "max_tokens": 1024,
    "temperature": 0.5,
}


def loadPrompts() -> dict:
    """
    加载提示词配置。

    优先读取 prompts.json；若不存在，fallback 到 prompts.example.json。
    两者都不可用时返回内嵌的回退提示词。
    """
    promptsPath = LLM_PROMPTS_PATH
    if not os.path.exists(promptsPath):
        promptsPath = os.path.join(PROJECT_ROOT, "data", "prompts.example.json")

    try:
        with open(promptsPath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return _FALLBACK_PROMPTS.copy()




# Knowledge 配置项

def getKnowledgeEnabled() -> bool:
    """获取知识库开关"""
    return loadLLMConfig()["knowledgeEnabled"]


def setKnowledgeEnabled(enabled: bool):
    """设置知识库开关"""
    _setConfig(knowledgeEnabled=bool(enabled))


def getKnowledgeMaxResults() -> int:
    """获取知识库最大召回数"""
    return loadLLMConfig()["knowledgeMaxResults"]


def setKnowledgeMaxResults(value: int):
    """设置知识库最大召回数"""
    if not isinstance(value, int) or value < 1 or value > 10:
        raise ValueError("knowledgeMaxResults 必须在 1-10 之间")
    _setConfig(knowledgeMaxResults=value)


def getKnowledgeMinScore() -> float:
    """获取知识库最低分数阈值"""
    return loadLLMConfig()["knowledgeMinScore"]


def setKnowledgeMinScore(value: float):
    """设置知识库最低分数阈值"""
    if not isinstance(value, (int, float)) or value < 0:
        raise ValueError("knowledgeMinScore 必须是大于等于 0 的数")
    _setConfig(knowledgeMinScore=float(value))
