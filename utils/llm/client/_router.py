"""
utils/llm/client/_router.py

调用配置组 → 协议、凭据与端点；未显式指定时兼容旧模型前缀路由。
"""

from urllib.parse import urlsplit, urlunsplit

from ..config import validateModelName, validateModelProvider
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




def _providerBaseURLs() -> dict[str, str]:
    """供请求与状态展示共用；不初始化 SDK 或读取凭据。"""
    from config import ANTHROPIC_BASE_URL, GOOGLE_GEMINI_BASE_URL, OPENAI_BASE_URL, LLM_OPENAI_BASE_URL

    return {
        "anthropic": ANTHROPIC_BASE_URL or "https://api.anthropic.com",
        "gemini": GOOGLE_GEMINI_BASE_URL or "https://generativelanguage.googleapis.com",
        "openai": OPENAI_BASE_URL or "https://api.openai.com/v1",
        "deepseek": LLM_OPENAI_BASE_URL or "https://api.deepseek.com",
        "doubao": "https://ark.cn-beijing.volces.com/api/v3",
    }


def _buildProviders() -> dict[str, LLMProvider]:
    """构建调用配置组；SDK 缺失时跳过，凭据在实际调用前检查。"""
    from config import (
        ANTHROPIC_API_KEY,
        ANTHROPIC_AUTH_TOKEN,
        GEMINI_API_KEY,
        OPENAI_API_KEY,
        DEEPSEEK_API_KEY,
        DOUBAO_API_KEY,
        LLM_PROXY,
    )

    endpoints = _providerBaseURLs()
    providers: dict[str, LLMProvider] = {}

    try:
        from .anthropic import AnthropicProvider
        providers["anthropic"] = AnthropicProvider(
            ANTHROPIC_API_KEY,
            proxy=LLM_PROXY,
            authToken=ANTHROPIC_AUTH_TOKEN,
            baseUrl=endpoints["anthropic"],
        )
    except ImportError:
        pass

    try:
        from .gemini import GeminiProvider
        providers["gemini"] = GeminiProvider(
            GEMINI_API_KEY, proxy=LLM_PROXY, baseUrl=endpoints["gemini"],
        )
    except ImportError:
        pass

    try:
        from .openaiCompat import OpenAICompatProvider
        providers["openai"] = OpenAICompatProvider(
            OPENAI_API_KEY, baseURL=endpoints["openai"], proxy=LLM_PROXY,
        )
        providers["deepseek"] = OpenAICompatProvider(
            DEEPSEEK_API_KEY, baseURL=endpoints["deepseek"], proxy=LLM_PROXY,
        )
        providers["doubao"] = OpenAICompatProvider(
            DOUBAO_API_KEY, baseURL=endpoints["doubao"], proxy=LLM_PROXY,
        )
    except ImportError:
        pass

    return providers




def resolveProviderKey(model: str, providerKey: str = "auto") -> str:
    """显式配置优先；auto 只兼容旧前缀，不根据凭据猜测端点。"""
    validateModelName(model)
    validateModelProvider(providerKey)
    if providerKey != "auto":
        return providerKey

    for prefix, key in _PREFIX_MAP:
        if model.startswith(prefix):
            return key

    suggestion = _suggestModel(model)
    hint = f"\n你是不是想用 {suggestion}？" if suggestion else ""
    raise ValueError(
        f"未知的模型名：{model}{hint}"
        "\n无法按前缀选择调用配置组，请用 /llm model provider <配置组> 显式指定"
        "（视觉模型使用 /llm visionmodel provider <配置组>）"
    )


def describeRoute(model: str, providerKey: str = "auto") -> str:
    """解释当前配置，不要求 SDK、凭据或网络；端点隐藏认证及查询参数。"""
    try:
        key = resolveProviderKey(model, providerKey)
    except ValueError as error:
        return f"配置无效：{error}"

    protocol = {
        "anthropic": "Anthropic Messages",
        "gemini": "Gemini GenerateContent",
        "openai": "OpenAI Chat Completions",
        "deepseek": "OpenAI Chat Completions",
        "doubao": "OpenAI Chat Completions",
    }[key]
    source = "按模型前缀" if providerKey == "auto" else "显式配置"
    try:
        parsed = urlsplit(_providerBaseURLs()[key])
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        endpoint = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        if not host or parsed.scheme not in ("http", "https"):
            endpoint = "（端点格式无效）"
    except ValueError:
        endpoint = "（端点格式无效）"
    return f"{key}（{source}；{protocol}；端点：{endpoint}）"


def getProvider(model: str, providerKey: str = "auto") -> LLMProvider:
    """取得显式或兼容推断的调用配置组，模型名称始终原样交给上游。"""
    validateModelName(model)
    validateModelProvider(providerKey)
    global _providers
    if _providers is None:
        _providers = _buildProviders()

    if not _providers:
        raise RuntimeError(
            "没有可用的 LLM SDK 喵，需要安装 anthropic、openai 或 google-genai"
            "（pip install -r requirements/llm.txt）"
        )

    # 自定义上游可能有同名别名；只有兼容前缀路由才给拼写建议，绝不改名。
    if providerKey == "auto":
        corrected = _MODEL_TYPOS.get(model.lower())
        if corrected and corrected != model:
            import warnings
            warnings.warn(
                f"模型名可能拼写有误：{model} → 建议使用 {corrected}",
                stacklevel=3,
            )

    key = resolveProviderKey(model, providerKey)
    provider = _providers.get(key)
    if provider is None:
        raise RuntimeError(
            f"模型 {model} 的调用配置组 {key} 不可用——对应 SDK 未安装"
        )
    if not provider.isAvailable():
        raise RuntimeError(
            f"模型 {model} 的调用配置组 {key} 未配置 API key 或认证 token"
        )
    return provider


def _suggestModel(model: str) -> str | None:
    """为未识别的模型名给出最接近的建议。"""
    from difflib import get_close_matches

    allModels = list(_MODEL_TYPOS.keys()) + [p.rstrip("-") for p, _ in _PREFIX_MAP]
    matches = get_close_matches(model.lower(), allModels, n=1, cutoff=0.6)
    if matches:
        corrected = _MODEL_TYPOS.get(matches[0], matches[0])
        return corrected
    return None
