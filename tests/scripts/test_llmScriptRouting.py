"""离线脚本继承主模型路由，临时模型与配置组可以分别覆盖。"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[2]




def _loadScript(name):
    spec = importlib.util.spec_from_file_location(name, _PROJECT_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_expand = _loadScript("expand_knowledge_tags")
_evaluate = _loadScript("evaluate_llm_quality")
_SETTINGS = {"model": "tenant/主模型:latest", "modelProvider": "anthropic"}




@pytest.mark.parametrize("model, provider, expectedModel, expectedProvider", [
    (None, None, _SETTINGS["model"], "anthropic"),
    (None, "openai", _SETTINGS["model"], "openai"),
    ("gpt-4o", None, "gpt-4o", "auto"),
    ("tenant/kimi:preview", "openai", "tenant/kimi:preview", "openai"),
    (None, "auto", _SETTINGS["model"], "auto"),
])
async def test_tagExpansionUsesOneSnapshotAndExplicitOverrides(
    monkeypatch, model, provider, expectedModel, expectedProvider,
):
    loadConfig = MagicMock(return_value=_SETTINGS.copy())
    client = object()
    getProvider = MagicMock(return_value=client)
    request = AsyncMock(return_value="alpha, beta, alpha")
    monkeypatch.setattr(_expand, "loadLLMConfig", loadConfig)
    monkeypatch.setattr(_expand, "getProvider", getProvider)
    monkeypatch.setattr(_expand, "requestWithRetry", request)

    result = await _expand.expandTags("标题", "正文", ["原标签"], model, providerKey=provider)

    assert result == ["alpha", "beta"]
    loadConfig.assert_called_once_with()
    getProvider.assert_called_once_with(expectedModel, expectedProvider)
    assert request.await_args.args == (client,)
    assert request.await_args.kwargs["model"] == expectedModel


async def test_tagScriptPassesCLIProviderThroughFileProcessing(tmp_path, monkeypatch):
    entry = tmp_path / "entry.md"
    entry.write_text("---\ntitle: 测试\ntags: [原标签]\n---\n正文", encoding="utf-8")
    monkeypatch.setattr(_expand, "LLM_KNOWLEDGE_DIR", str(tmp_path))
    monkeypatch.setattr(_expand, "loadLLMConfig", lambda: _SETTINGS.copy())
    route = MagicMock(return_value=object())
    monkeypatch.setattr(_expand, "getProvider", route)
    monkeypatch.setattr(_expand, "requestWithRetry", AsyncMock(return_value="alpha, beta"))
    monkeypatch.setattr(sys, "argv", ["expand_knowledge_tags.py", "--model", "tenant/kimi:v1", "--provider", "openai"])

    await _expand.main()

    route.assert_called_once_with("tenant/kimi:v1", "openai")
    assert "tags_expanded: [alpha, beta]" in entry.read_text(encoding="utf-8")


@pytest.mark.parametrize("extraArgs, expectedModel, expectedProvider", [
    ([], _SETTINGS["model"], "anthropic"),
    (["--judge-provider", "openai"], _SETTINGS["model"], "openai"),
    (["--judge-model", "gpt-4o"], "gpt-4o", "auto"),
    (["--judge-model", "tenant/kimi:v1", "--judge-provider", "openai"], "tenant/kimi:v1", "openai"),
    (["--judge-provider", "auto"], _SETTINGS["model"], "auto"),
])
async def test_judgeCLIInheritsMainRouteOrUsesExplicitOverride(
    tmp_path, monkeypatch, extraArgs, expectedModel, expectedProvider,
):
    queries = tmp_path / "queries.txt"
    queries.write_text("测试问题\n", encoding="utf-8")
    output = tmp_path / "results.json"
    loadConfig = MagicMock(return_value=_SETTINGS.copy())
    monkeypatch.setattr(_evaluate, "loadLLMConfig", loadConfig)
    monkeypatch.setattr(_evaluate, "initKnowledgeDB", AsyncMock())
    monkeypatch.setattr(_evaluate, "getKnowledgeEntries", AsyncMock(return_value=[]))
    evaluate = AsyncMock(return_value={
        "context_hit": {"hit": True}, "persona": {"consistent": True}, "relevance": {"relevant": True},
    })
    monkeypatch.setattr(_evaluate, "evalQuery", evaluate)
    monkeypatch.setattr(sys, "argv", [
        "evaluate_llm_quality.py", "--queries", str(queries), "--chat-id", "-1001",
        "--out", str(output), *extraArgs,
    ])

    await _evaluate.main()

    loadConfig.assert_called_once_with()
    assert evaluate.await_args.kwargs == {"judgeModel": expectedModel, "judgeProvider": expectedProvider}
    summary = json.loads(output.read_text(encoding="utf-8"))["summary"]
    assert summary["judge_model"] == expectedModel
    assert summary["judge_provider"] == expectedProvider


async def test_judgeRouteReachesAllThreeModelCalls(monkeypatch):
    monkeypatch.setattr(_evaluate, "generateReply", AsyncMock(return_value="测试回复"))
    monkeypatch.setattr(_evaluate, "retrieveKnowledge", AsyncMock(return_value=[
        {"category": "test", "title": "条目", "content": "资料"},
    ]))
    client = object()
    route = MagicMock(return_value=client)
    request = AsyncMock(return_value='{"hit": true, "consistent": true, "relevant": true}')
    monkeypatch.setattr(_evaluate, "getProvider", route)
    monkeypatch.setattr(_evaluate, "requestWithRetry", request)

    result = await _evaluate.evalQuery(
        "问题", "chat", {"style": [{"content": "简洁"}], "pitfalls": []},
        judgeModel="tenant/kimi:latest", judgeProvider="openai",
    )

    assert result["reply"] == "测试回复"
    assert route.call_count == request.await_count == 3
    assert all(call.args == ("tenant/kimi:latest", "openai") for call in route.call_args_list)
    assert all(call.args == (client,) and call.kwargs["model"] == "tenant/kimi:latest" for call in request.await_args_list)


@pytest.mark.parametrize("script, requiredArgs, providerFlag", [
    (_expand, [], "--provider"),
    (_evaluate, ["--queries", "unused.txt", "--chat-id", "123"], "--judge-provider"),
])
async def test_invalidCLIProviderStopsBeforeModelCall(monkeypatch, script, requiredArgs, providerFlag):
    route = MagicMock(side_effect=AssertionError("非法参数不应发起模型调用"))
    monkeypatch.setattr(script, "getProvider", route)
    monkeypatch.setattr(sys, "argv", ["script.py", *requiredArgs, providerFlag, "typo"])

    with pytest.raises(SystemExit) as error:
        await script.main()

    assert error.value.code == 2
    route.assert_not_called()
