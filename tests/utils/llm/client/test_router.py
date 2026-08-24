"""
tests/utils/llm/client/test_router.py

模型路由（utils/llm/client/_router.py）：前缀匹配、拼写纠错建议、
provider 可用性错误。_buildProviders 全 mock（不依赖真实 SDK/密钥）。
"""

from unittest.mock import MagicMock, patch

import pytest

from utils.llm.client import _router
from utils.llm.client._router import _suggestModel, getProvider



def _fakeProvider(available=True):
    p = MagicMock()
    p.isAvailable.return_value = available
    return p



@pytest.fixture
def providers(monkeypatch):
    """注入假 provider 集合，绕过懒初始化"""
    fake = {
        "anthropic": _fakeProvider(),
        "gemini": _fakeProvider(),
        "openai": _fakeProvider(),
        "deepseek": _fakeProvider(),
        "doubao": _fakeProvider(),
    }
    monkeypatch.setattr(_router, "_providers", fake)
    return fake



class TestPrefixRouting:
    def test_claude_routes_anthropic(self, providers):
        assert getProvider("claude-opus-4-6") is providers["anthropic"]

    def test_gemini_routes(self, providers):
        assert getProvider("gemini-2.5-pro") is providers["gemini"]

    def test_gpt_routes_openai(self, providers):
        assert getProvider("gpt-4o") is providers["openai"]

    def test_o1_routes_openai(self, providers):
        assert getProvider("o1-mini") is providers["openai"]

    def test_deepseek_routes(self, providers):
        assert getProvider("deepseek-chat") is providers["deepseek"]

    def test_doubao_routes(self, providers):
        assert getProvider("doubao-pro-32k") is providers["doubao"]

    def test_unknown_prefix_raises(self, providers):
        with pytest.raises(ValueError, match="未知的模型名"):
            getProvider("llama-3-70b")

    def test_provider_missing_raises(self, providers, monkeypatch):
        providers.pop("gemini")
        with pytest.raises(RuntimeError, match="不可用"):
            getProvider("gemini-2.5-pro")

    def test_provider_unavailable_raises(self, providers):
        providers["anthropic"].isAvailable.return_value = False
        with pytest.raises(RuntimeError, match="API key"):
            getProvider("claude-opus-4-6")

    def test_no_providers_at_all(self, monkeypatch):
        monkeypatch.setattr(_router, "_providers", {})
        with pytest.raises(RuntimeError, match="没有可用的 LLM SDK"):
            getProvider("claude-opus-4-6")



class TestTypoWarning:
    """typo 词表只在「前缀命中的模型名恰好在表里」时警告——如 gpt4o（缺连字符，仍 gpt- 前缀）。
    前缀都不命中的 typo（deepsuck-chat 不以 deepseek- 开头）走 ValueError + 模糊建议。"""

    def test_typo_with_valid_prefix_warns(self, providers):
        """claude-haiku-4-6 在 typo 表（建议 4-5）且以 claude- 开头——警告但正常路由"""
        with pytest.warns(UserWarning, match="claude-haiku-4-6"):
            provider = getProvider("claude-haiku-4-6")
        assert provider is providers["anthropic"]

    def test_correct_name_no_warning(self, providers):
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            getProvider("deepseek-chat")
        assert not caught

    def test_broken_prefix_raises_with_suggestion(self, providers):
        """前缀不命中的 typo：先发拼写警告（表里查得到），再 ValueError 带建议"""
        with pytest.warns(UserWarning, match="deepsuck-chat"):
            with pytest.raises(ValueError, match="deepseek-chat"):
                getProvider("deepsuck-chat")



class TestSuggestModel:
    def test_close_match_suggested(self):
        assert _suggestModel("deepsuck-chat") == "deepseek-chat"

    def test_gemini_pro_suggests_full_name(self):
        assert _suggestModel("gemini-pro") == "gemini-2.5-pro"

    def test_no_match_for_garbage(self):
        assert _suggestModel("zzzzz") is None


