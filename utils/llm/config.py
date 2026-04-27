"""
utils/llm/config.py

LLM 配置管理：
    - 加载/保存 llmConfig.json（enabled、autoMode、model、visionModel、memoryEnabled、memoryAutoApprove）
    - 加载 prompts.json（不存在时 fallback 到 prompts.example.json）
"""




import os
import json
import threading

from config import (
    LLM_CONFIG_PATH,
    LLM_PROMPTS_PATH,
    LLM_DEFAULT_MODEL,
    PROJECT_ROOT,
)


# 配置 read-modify-write 串行化：避免 Telegram / console 并发改配置时丢更新
_configLock = threading.Lock()


# 默认配置（首次运行或文件损坏时使用）
_DEFAULT_CONFIG = {
    "enabled": False,
    "autoMode": "console",  # "on" | "off" | "console"
    "model": LLM_DEFAULT_MODEL,
    "visionModel": LLM_DEFAULT_MODEL,
    "groupTriggerMode": "mention",  # "mention" | "keyword"
    "groupTriggerKeywords": [],
    "memoryEnabled": False,
    "memoryAutoApprove": False,
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




# ── 提示词 ─────────────────────────────────────────

def loadPrompts() -> dict:
    """
    加载提示词配置。

    优先读取 prompts.json；若不存在，fallback 到 prompts.example.json。
    """
    prompts_path = LLM_PROMPTS_PATH
    if not os.path.exists(prompts_path):
        prompts_path = os.path.join(PROJECT_ROOT, "data", "prompts.example.json")

    try:
        with open(prompts_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "system_prompt": ["你是 ZincNya"],
            "max_tokens": 32768,
            "temperature": 0.8,
        }
