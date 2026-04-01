"""
utils/llm/config.py

LLM 配置管理：
    - 加载/保存 llmConfig.json（enabled、autoMode、model、memoryEnabled）
    - 加载 prompts.json（不存在时 fallback 到 prompts.example.json）
"""




import os
import json

from config import (
    LLM_CONFIG_PATH,
    LLM_PROMPTS_PATH,
    LLM_DEFAULT_MODEL,
    PROJECT_ROOT,
)


# 默认配置（首次运行或文件损坏时使用）
_DEFAULT_CONFIG = {
    "enabled": False,
    "autoMode": "console",  # "on" | "off" | "console"
    "model": LLM_DEFAULT_MODEL,
    "memoryEnabled": False,
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
    """保存 LLM 配置到文件，失败时返回 False"""
    try:
        with open(LLM_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        return True
    except OSError as e:
        from utils.core.stateManager import safePrint
        safePrint(f"[LLM] 配置保存失败：{e}")
        return False


def _setConfig(**kwargs):
    """内部：更新配置项并保存"""
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




# ── 记忆 ────────────────────────────────────────────

def getMemoryEnabled() -> bool:
    return loadLLMConfig()["memoryEnabled"]


def setMemoryEnabled(enabled: bool):
    _setConfig(memoryEnabled=enabled)




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
