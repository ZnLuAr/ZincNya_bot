"""
utils/llm/client/_router.py

模型名 → LLM 提供商的路由逻辑。
"""

from ._base import LLMProvider


# 路由表：模型名前缀 → provider 键名
_PREFIX_MAP: list[tuple[str, str]] = [
    ("claude-",    "anthropic"),
    ("gemini-",    "gemini"),
    ("gpt-",       "openai"),
    ("o1-",        "openai"),
    ("o3-",        "openai"),
    ("chatgpt-",   "openai"),
    ("deepseek-",  "deepseek"),
    ("doubao-",    "doubao"),
]

# 常见模型名拼写错误 → 正确名称的映射
_MODEL_TYPOS: dict[str, str] = {
    # Claude
    "claude-haiku-4-6":      "claude-haiku-4-5-20251001",
    "claude-opus-4-6":       "claude-opus-4-6",       # 正确，占位
    "claude-sonnet-4-6":     "claude-sonnet-4-6",     # 正确，占位
    # DeepSeek
    "deepsuck-chat":         "deepseek-chat",
    "deepsick-chat":         "deepseek-chat",
    "deepseep-chat":         "deepseek-chat",
    "deep-seek-chat":        "deepseek-chat",
    "deepsuck-reasoner":     "deepseek-reasoner",
    "deepseek-r1":           "deepseek-reasoner",
    # OpenAI
    "gpt4o":                 "gpt-4o",
    "gpt-4o-mini":           "gpt-4o-mini",
    # Gemini
    "gemini-pro":            "gemini-2.5-pro",
    "gemini-flash":          "gemini-2.5-flash",
    # Doubao
    "doubao-pro":            "doubao-pro-32k",
    "doubao-lite":           "doubao-lite-32k",
}

# 懒初始化的 provider 实例缓存
_providers: dict[str, LLMProvider] | None = None




def _buildProviders() -> dict[str, LLMProvider]:
    """构建所有 provider 实例。SDK 缺失时跳过，不阻塞启动。"""
    from config import (
        ANTHROPIC_API_KEY,
        GEMINI_API_KEY,
        OPENAI_API_KEY,
        DEEPSEEK_API_KEY,
        DOUBAO_API_KEY,
        LLM_OPENAI_BASE_URL,
    )

    providers: dict[str, LLMProvider] = {}

    try:
        from .anthropic import AnthropicProvider
        providers["anthropic"] = AnthropicProvider(ANTHROPIC_API_KEY)
    except ImportError:
        pass

    try:
        from .gemini import GeminiProvider
        providers["gemini"] = GeminiProvider(GEMINI_API_KEY)
    except ImportError:
        pass

    try:
        from .openaiCompat import OpenAICompatProvider
        providers["openai"] = OpenAICompatProvider(OPENAI_API_KEY)
        providers["deepseek"] = OpenAICompatProvider(
            DEEPSEEK_API_KEY,
            baseURL=LLM_OPENAI_BASE_URL or "https://api.deepseek.com",
        )
        providers["doubao"] = OpenAICompatProvider(
            DOUBAO_API_KEY,
            baseURL="https://ark.cn-beijing.volces.com/api/v3",
        )
    except ImportError:
        pass

    return providers




def getProvider(model: str) -> LLMProvider:
    """根据模型名前缀返回对应的 LLM 提供商实例。"""
    global _providers
    if _providers is None:
        _providers = _buildProviders()

    if not _providers:
        raise RuntimeError("没有可用的 LLM SDK 喵，需要安装 anthropic、openai 或 google-genai")

    # 检查常见拼写错误
    corrected = _MODEL_TYPOS.get(model.lower())
    if corrected and corrected != model:
        import warnings
        warnings.warn(
            f"模型名可能拼写有误：{model} → 建议使用 {corrected}",
            stacklevel=3,
        )

    for prefix, providerKey in _PREFIX_MAP:
        if model.startswith(prefix):
            provider = _providers.get(providerKey)
            if not provider:
                raise RuntimeError(
                    f"模型 {model} 对应的 provider ({providerKey}) 不可用"
                    f"——SDK 未安装或 API key 未配置"
                )
            if not provider.isAvailable():
                raise RuntimeError(
                    f"模型 {model} 需要 API key，但未配置"
                    f"（provider: {providerKey}）"
                )
            return provider

    # 未匹配任何前缀，尝试模糊建议
    suggestion = _suggestModel(model)
    hint = f"\n你是不是想用 {suggestion}？" if suggestion else ""
    raise ValueError(
        f"未知的模型名：{model}{hint}"
        f"\n支持的前缀：{', '.join(p for p, _ in _PREFIX_MAP)}"
    )


def _suggestModel(model: str) -> str | None:
    """为未识别的模型名给出最接近的建议。"""
    from difflib import get_close_matches

    allModels = list(_MODEL_TYPOS.keys()) + [p.rstrip("-") for p, _ in _PREFIX_MAP]
    matches = get_close_matches(model.lower(), allModels, n=1, cutoff=0.6)
    if matches:
        corrected = _MODEL_TYPOS.get(matches[0], matches[0])
        return corrected
    return None
