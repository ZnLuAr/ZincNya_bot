"""
utils/llm/config.py

LLM 配置管理：
    - 加载/保存 llmConfig.json（开关、审核模式、主模型、视觉模型、
      群聊触发模式与关键词、长期记忆、URL 读取配置与黑名单）
    - 加载 prompts.json（不存在或解析失败时 fallback 到内嵌的"褪色"占位 prompt）

settable 配置都会通过 _setConfig 加锁串行写盘（read-modify-write 安全）。
"""

import os
import json
import threading
from enum import IntEnum
from urllib.parse import urlsplit

from config import (
    LLM_CONFIG_PATH,
    LLM_PROMPTS_PATH,
    LLM_DEFAULT_MODEL,
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
    LOW_TRUST     = 500   # 低信任内容（记忆 / 历史 / URL）


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
    "urlReadEnabled": False,
    "urlReadMaxUrls": 3,
    "urlReadMaxBytes": 512 * 1024,
    "urlReadMaxChars": 12000,
    "urlReadTotalMaxChars": 24000,
    "urlReadTimeoutSeconds": 10,
    "urlReadMaxRetries": 1,
    "urlReadRedirectLimit": 3,
    "urlReadBlockedHosts": [
        "localhost",
        "metadata",
        "metadata.google.internal",
        "169.254.169.254",
    ],
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




# ── URL 读取 ────────────────────────────────────────

def _boundedInt(value, *, default: int, minValue: int, maxValue: int) -> int:
    """边界兜底，确保配置值在合理范围内"""
    try:
        val = int(value)
        return max(minValue, min(maxValue, val))
    except (TypeError, ValueError):
        return default


def _boundedFloat(value, *, default: float, minValue: float, maxValue: float) -> float:
    """边界兜底，但是是浮点数"""
    try:
        val = float(value)
        return max(minValue, min(maxValue, val))
    except (TypeError, ValueError):
        return default


# 常见公共 TLD / 二级后缀，拒绝单独作为 blocked host —— 否则
# 规则 `host == bh or host.endswith(f".{bh}")` 会一次性屏蔽整个 TLD。
_FORBIDDEN_BARE_SUFFIXES = {
    # gTLD
    "com", "net", "org", "info", "biz", "name", "pro",
    "app", "dev", "xyz", "io", "ai", "me", "co", "tv",
    "site", "online", "store", "blog", "tech", "cloud",
    # ccTLD
    "cn", "jp", "kr", "hk", "tw", "sg", "th", "vn", "in", "id",
    "us", "uk", "de", "fr", "it", "es", "pl", "nl", "se", "ru",
    "ca", "au", "nz", "br", "mx",
    # 受限类
    "gov", "edu", "mil", "int",
}


def _normalizeBlockedHost(host: str) -> str:
    """
    规范化 blocked host
        - 去掉 scheme/path，只保留 host
        - 小写、去尾点、IDNA normalize
        - 拒绝单独的常见 TLD（防止误输 "com" 屏蔽整个 .com）
    """
    host = str(host).strip()
    if not host:
        raise ValueError("blocked host 不能为空")

    # 如果包含 scheme，用 urlsplit 提取 netloc
    if "://" in host:
        parsed = urlsplit(host)
        host = parsed.netloc or parsed.path.split("/")[0]
    elif host.startswith("//"):
        host = host[2:].split("/")[0]
    else:
        host = host.split("/")[0]

    # 去掉 port
    if ":" in host and not host.startswith("["):
        host = host.rsplit(":", 1)[0]
    elif host.startswith("[") and "]:" in host:
        host = host.split("]:")[0] + "]"

    # 去掉 IPv6 方括号用于规范化
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]

    host = host.lower().rstrip(".")

    # IDNA 编解码
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        pass

    if not host:
        raise ValueError("blocked host 规范化后为空")

    if host in _FORBIDDEN_BARE_SUFFIXES:
        raise ValueError(
            f"blocked host {host!r} 是常见公共后缀；单独使用会屏蔽整个 TLD，"
            f"请用具体子域名（例如 foo.{host}）"
        )

    return host


def getURLReadEnabled() -> bool:
    return loadLLMConfig().get("urlReadEnabled", False)


def setURLReadEnabled(enabled: bool):
    _setConfig(urlReadEnabled=bool(enabled))


def getURLReadMaxUrls() -> int:
    val = loadLLMConfig().get("urlReadMaxUrls", 3)
    return _boundedInt(val, default=3, minValue=1, maxValue=5)


def setURLReadMaxUrls(value: int):
    value = _boundedInt(value, default=3, minValue=1, maxValue=5)
    _setConfig(urlReadMaxUrls=value)


def getURLReadMaxBytes() -> int:
    val = loadLLMConfig().get("urlReadMaxBytes", 512 * 1024)
    return _boundedInt(val, default=512 * 1024, minValue=64 * 1024, maxValue=1024 * 1024)


def setURLReadMaxBytes(value: int):
    value = _boundedInt(value, default=512 * 1024, minValue=64 * 1024, maxValue=1024 * 1024)
    _setConfig(urlReadMaxBytes=value)


def getURLReadMaxChars() -> int:
    val = loadLLMConfig().get("urlReadMaxChars", 12000)
    return _boundedInt(val, default=12000, minValue=1000, maxValue=30000)


def setURLReadMaxChars(value: int):
    value = _boundedInt(value, default=12000, minValue=1000, maxValue=30000)
    _setConfig(urlReadMaxChars=value)


def getURLReadTotalMaxChars() -> int:
    val = loadLLMConfig().get("urlReadTotalMaxChars", 24000)
    return _boundedInt(val, default=24000, minValue=1000, maxValue=60000)


def setURLReadTotalMaxChars(value: int):
    value = _boundedInt(value, default=24000, minValue=1000, maxValue=60000)
    _setConfig(urlReadTotalMaxChars=value)


def getURLReadTimeoutSeconds() -> float:
    """
    读取 URL 抓取的总 timeout（秒）。

    注意：该值在 aiohttp Session 创建时绑定，运行时修改 llmConfig.json 中的
    urlReadTimeoutSeconds 需要重启 bot 才能生效；因此不提供 setter。
    """
    val = loadLLMConfig().get("urlReadTimeoutSeconds", 10)
    return _boundedFloat(val, default=10.0, minValue=2.0, maxValue=20.0)


def getURLReadMaxRetries() -> int:
    val = loadLLMConfig().get("urlReadMaxRetries", 1)
    return _boundedInt(val, default=1, minValue=0, maxValue=2)


def setURLReadMaxRetries(value: int):
    value = _boundedInt(value, default=1, minValue=0, maxValue=2)
    _setConfig(urlReadMaxRetries=value)


def getURLReadRedirectLimit() -> int:
    val = loadLLMConfig().get("urlReadRedirectLimit", 3)
    return _boundedInt(val, default=3, minValue=0, maxValue=5)


def setURLReadRedirectLimit(value: int):
    value = _boundedInt(value, default=3, minValue=0, maxValue=5)
    _setConfig(urlReadRedirectLimit=value)


def getURLReadBlockedHosts() -> list[str]:
    raw = loadLLMConfig().get("urlReadBlockedHosts", [])
    if not isinstance(raw, list):
        return ["localhost", "metadata", "metadata.google.internal", "169.254.169.254"]

    result: list[str] = []
    seen = set()
    for item in raw:
        try:
            host = _normalizeBlockedHost(item)
        except ValueError:
            continue
        if host not in seen:
            seen.add(host)
            result.append(host)

    # 确保默认 blocked hosts 始终存在
    defaults = ["localhost", "metadata", "metadata.google.internal", "169.254.169.254"]
    for dh in defaults:
        if dh not in seen:
            result.append(dh)

    return result


def setURLReadBlockedHosts(hosts: list[str]):
    result: list[str] = []
    seen = set()
    for item in hosts:
        host = _normalizeBlockedHost(item)
        if host not in seen:
            seen.add(host)
            result.append(host)
    _setConfig(urlReadBlockedHosts=result)


def addURLReadBlockedHost(host: str):
    hosts = getURLReadBlockedHosts()
    host = _normalizeBlockedHost(host)
    if host not in hosts:
        hosts.append(host)
        _setConfig(urlReadBlockedHosts=hosts)


def removeURLReadBlockedHost(host: str):
    host = _normalizeBlockedHost(host)
    hosts = [h for h in getURLReadBlockedHosts() if h != host]
    _setConfig(urlReadBlockedHosts=hosts)




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

