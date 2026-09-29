"""
tests/utils/command/llm/test_memoryCmd.py

/llm memory 子命令分支（utils/command/llm/memoryCmd.py）：flag 开关、
list 过滤参数、add/edit/del 校验路径、速查表与 match 分支一致性契约。
数据库层 mock（行为已由 tests/utils/llm/memory/ 覆盖）。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.command.llm import memoryCmd
from utils.command.llm.memoryCmd import _handleMemoryCommand, _MEMORY_SUBCOMMANDS



def _app():
    return MagicMock()



class TestFlagSwitches:
    @patch.object(memoryCmd, "setMemoryEnabled")
    @patch.object(memoryCmd, "logAction", new_callable=AsyncMock)
    async def test_on(self, mockLog, mockSet):
        await _handleMemoryCommand(["-on"], _app())
        mockSet.assert_called_once_with(True)

    @patch.object(memoryCmd, "setMemoryEnabled")
    @patch.object(memoryCmd, "logAction", new_callable=AsyncMock)
    async def test_off(self, mockLog, mockSet):
        await _handleMemoryCommand(["-off"], _app())
        mockSet.assert_called_once_with(False)

    @patch.object(memoryCmd, "setContextOnce")
    @patch.object(memoryCmd, "logAction", new_callable=AsyncMock)
    async def test_once(self, mockLog, mockOnce):
        await _handleMemoryCommand(["-once"], _app())
        mockOnce.assert_called_once()

    @patch.object(memoryCmd, "setMemoryAutoApprove")
    @patch.object(memoryCmd, "getMemoryAutoApprove", return_value=False)
    @patch.object(memoryCmd, "logAction", new_callable=AsyncMock)
    async def test_autoapprove_toggles(self, mockLog, mockGet, mockSet):
        await _handleMemoryCommand(["-autoapprove"], _app())
        mockSet.assert_called_once_with(True)    # 当前 False → 切到 True

    async def test_invalid_flag_hint(self, capsys):
        await _handleMemoryCommand(["-bogus"], _app())
        assert "无效" in capsys.readouterr().out

    @patch.object(memoryCmd, "getMemoryEnabled", return_value=True)
    @patch.object(memoryCmd, "isContextOnceSet", return_value=False)
    async def test_no_args_status(self, mockOnce, mockEnabled, capsys):
        await _handleMemoryCommand([], _app())
        out = capsys.readouterr().out
        assert "记忆模式：开启" in out and "One-shot" in out



class TestListBranch:
    @patch.object(memoryCmd, "getMemories", new_callable=AsyncMock, return_value=[])
    async def test_list_calls_getMemories(self, mockGet):
        await _handleMemoryCommand(["list"], _app())
        kwargs = mockGet.await_args.kwargs
        assert kwargs["enabledOnly"] is True     # 默认只看启用

    @patch.object(memoryCmd, "getMemories", new_callable=AsyncMock, return_value=[])
    async def test_list_all(self, mockGet):
        await _handleMemoryCommand(["list", "-all"], _app())
        assert mockGet.await_args.kwargs["enabledOnly"] is False

    @patch.object(memoryCmd, "getMemories", new_callable=AsyncMock, return_value=[])
    async def test_list_scope(self, mockGet):
        await _handleMemoryCommand(["list", "-scope", "chat"], _app())
        assert mockGet.await_args.kwargs["scopeType"] == "chat"

    @patch.object(memoryCmd, "getMemories", new_callable=AsyncMock, return_value=[])
    async def test_list_limit(self, mockGet):
        await _handleMemoryCommand(["list", "-limit", "5"], _app())
        assert mockGet.await_args.kwargs["limit"] == 5

    @patch.object(memoryCmd, "getMemories", new_callable=AsyncMock, return_value=[])
    async def test_list_empty_hint(self, mockGet, capsys):
        await _handleMemoryCommand(["list"], _app())
        assert "没有找到条目" in capsys.readouterr().out

    @patch.object(memoryCmd, "getMemories", new_callable=AsyncMock)
    async def test_list_renders_items(self, mockGet, capsys):
        mockGet.return_value = [{
            "id": 7, "scope_type": "global", "scope_id": "global",
            "enabled": True, "priority": 1, "source": "manual",
            "content": "喜欢猫", "tags": ["宠物"], "mode": "contextual",
            "retrievalHint": "聊到宠物",
        }]
        await _handleMemoryCommand(["list"], _app())
        out = capsys.readouterr().out
        assert "#7" in out and "喜欢猫" in out and "宠物" in out
        assert "contextual" in out and "聊到宠物" in out



class TestAddEditDel:
    @patch.object(memoryCmd, "addMemory", new_callable=AsyncMock, return_value=9)
    @patch.object(memoryCmd, "logAction", new_callable=AsyncMock)
    async def test_add_success(self, mockLog, mockAdd, capsys):
        await _handleMemoryCommand(["add", "-scope", "global", "-text", "内容"], _app())
        assert "#9 已添加" in capsys.readouterr().out

    @patch.object(memoryCmd, "addMemory", new_callable=AsyncMock, return_value=9)
    async def test_add_passes_mode_and_hint(self, mockAdd):
        await _handleMemoryCommand([
            "add", "-scope", "global", "-text", "内容",
            "-mode", "pinned", "-hint", "昵称话题",
        ], _app())

        assert mockAdd.await_args.kwargs["mode"] == "pinned"
        assert mockAdd.await_args.kwargs["retrievalHint"] == "昵称话题"

    async def test_add_missing_scope_usage(self, capsys):
        await _handleMemoryCommand(["add", "-text", "内容"], _app())
        assert "用法" in capsys.readouterr().out

    @patch.object(memoryCmd, "getMemoryByID", new_callable=AsyncMock, return_value={"id": 3})
    @patch.object(memoryCmd, "updateMemory", new_callable=AsyncMock, return_value=True)
    async def test_edit_updates(self, mockUpd, mockGet, capsys):
        await _handleMemoryCommand(["edit", "-mid", "3", "-text", "新内容"], _app())
        assert "已更新" in capsys.readouterr().out
        kwargs = mockUpd.await_args.kwargs
        assert kwargs["content"] == "新内容"

    @patch.object(memoryCmd, "getMemoryByID", new_callable=AsyncMock, return_value={"id": 3})
    @patch.object(memoryCmd, "updateMemory", new_callable=AsyncMock, return_value=True)
    async def test_edit_clearhint_is_explicit_empty_string(self, mockUpd, mockGet):
        await _handleMemoryCommand(["edit", "-mid", "3", "-clearhint"], _app())
        assert mockUpd.await_args.kwargs["retrievalHint"] == ""

    @patch.object(memoryCmd, "getMemoryByID", new_callable=AsyncMock, return_value={"id": 3})
    @patch.object(memoryCmd, "updateMemory", new_callable=AsyncMock, return_value=True)
    async def test_edit_passes_mode_and_hint(self, mockUpd, mockGet):
        await _handleMemoryCommand([
            "edit", "-mid", "3", "-mode", "contextual", "-hint", "饮食话题",
        ], _app())
        kwargs = mockUpd.await_args.kwargs
        assert kwargs["mode"] == "contextual"
        assert kwargs["retrievalHint"] == "饮食话题"

    @patch.object(memoryCmd, "updateMemory", new_callable=AsyncMock)
    async def test_edit_rejects_hint_and_clearhint_together(self, mockUpd, capsys):
        await _handleMemoryCommand([
            "edit", "-mid", "3", "-hint", "x", "-clearhint",
        ], _app())
        assert "不能同时使用" in capsys.readouterr().out
        mockUpd.assert_not_awaited()

    async def test_edit_missing_mid_usage(self, capsys):
        await _handleMemoryCommand(["edit", "-text", "x"], _app())
        assert "用法" in capsys.readouterr().out

    @patch.object(memoryCmd, "getMemoryByID", new_callable=AsyncMock, return_value={"id": 3})
    @patch.object(memoryCmd, "deleteMemory", new_callable=AsyncMock, return_value=True)
    async def test_del(self, mockDel, mockGet, capsys):
        await _handleMemoryCommand(["del", "3"], _app())
        assert "已删除" in capsys.readouterr().out
        mockDel.assert_awaited_once_with(3)

    @patch.object(memoryCmd, "getMemoryByID", new_callable=AsyncMock, return_value=None)
    async def test_del_not_found(self, mockGet, capsys):
        await _handleMemoryCommand(["del", "99"], _app())
        assert "不存在" in capsys.readouterr().out



class TestFallbackAndContract:
    async def test_unknown_subcommand_renders_table(self, capsys):
        await _handleMemoryCommand(["bogus"], _app())
        out = capsys.readouterr().out
        assert "/llm memory 可用的子命令有" in out

    def test_speedtable_covers_match_branches(self):
        """速查表 ↔ match 分支一致性：表里每个 flag/子命令名都能走通分支"""
        for flag in ("-on", "-off", "-once", "-autoapprove"):
            assert any(flag in key for key in _MEMORY_SUBCOMMANDS), flag
        for sub in ("list", "add", "edit", "del", "retrieval", "status", "ui"):
            assert any(key.startswith(sub) for key in _MEMORY_SUBCOMMANDS), sub




class TestRetrievalManagement:
    @patch.object(memoryCmd, "setMemoryRetrievalMode")
    @patch.object(memoryCmd, "logAction", new_callable=AsyncMock)
    async def test_retrieval_switches_without_starting_encoder(self, mockLog, mockSet):
        await _handleMemoryCommand(["retrieval", "hybrid"], _app())
        mockSet.assert_called_once_with("hybrid")

    @patch.object(memoryCmd, "getMemoryRetrievalMode", return_value="legacy")
    async def test_retrieval_without_value_reads_mode(self, mockGet, capsys):
        await _handleMemoryCommand(["retrieval"], _app())
        assert "legacy" in capsys.readouterr().out

    @patch.object(memoryCmd, "loadLLMConfig", return_value={})
    @patch.object(memoryCmd, "getMemoryCounts", new_callable=AsyncMock, return_value={"total": 8, "enabled": 4})
    @patch.object(memoryCmd, "loadCalibratedThresholds", return_value=({
        "semanticCurrent": None,
        "semanticAssisted": None,
        "lexical": None,
    }, "calibrationDatasetMissing"))
    @patch.object(memoryCmd, "getMemoryRetrievalMode", return_value="hybrid")
    @patch.object(memoryCmd, "getStateManager")
    async def test_status_is_read_only_and_reports_degradation(
        self, mockState, mockMode, mockCalibration, mockCounts, mockConfig, capsys,
    ):
        runtime = MagicMock()
        runtime.getStatus.return_value = {
            "running": True,
            "closing": False,
            "encoderReady": False,
            "cacheEntries": 2,
            "cacheBytes": 4096,
            "queryQueued": 0,
            "indexPending": 1,
            "oldestIndexAgeMs": 12.5,
            "lastReason": "queryTimeout",
            "reconcileCapacitySaturated": True,
        }
        mockState.return_value.getMemoryRuntime.return_value = runtime

        await _handleMemoryCommand(["status"], _app())

        out = capsys.readouterr().out
        assert "启用 4 / 总计 8" in out
        assert "2/4 contextual (50.0%)" in out
        assert "calibrationDatasetMissing" in out
        assert "queryRejected=0" in out
        assert "workerFailures=0" in out
        assert "queryTimeout" in out
        assert "对账容量：已饱和" in out
        assert "reconcileCapacitySaturated=true" in out
        assert "选择后端：local" in out
        assert "检索会降级（calibrationDatasetMissing）" in out
        runtime.getStatus.assert_called_once_with()

    @staticmethod
    async def _runStatus(capsys, config, *, apiKey="selector-key", runtimeStatus=None):
        """按给定 llmConfig 与凭据跑一次 hybrid status，返回打印内容。"""
        if runtimeStatus is None:
            runtimeStatus = {"running": True, "closing": False, "encoderReady": True}
        runtime = MagicMock()
        runtime.getStatus.return_value = runtimeStatus
        with (
            patch.object(memoryCmd, "loadLLMConfig", return_value=config),
            patch.object(memoryCmd, "getMemoryRetrievalMode", return_value="hybrid"),
            patch.object(memoryCmd, "loadCalibratedThresholds", return_value=(
                dict.fromkeys(("semanticCurrent", "semanticAssisted", "lexical")),
                "calibrationStatusInvalid",
            )),
            patch.object(memoryCmd, "getMemoryCounts", new_callable=AsyncMock,
                         return_value={"total": 3, "enabled": 3}),
            patch.object(memoryCmd, "getStateManager") as mockState,
            patch.object(memoryCmd, "LLM_MEMORY_SELECTOR_BASE_URL", "https://selector.invalid"),
            patch.object(memoryCmd, "LLM_MEMORY_SELECTOR_API_KEY", apiKey),
        ):
            mockState.return_value.getMemoryRuntime.return_value = (
                None if runtimeStatus is False else runtime
            )
            await _handleMemoryCommand(["status"], _app())
        return capsys.readouterr().out

    async def test_status_llm_backend_ignores_calibration_and_shows_selector(self, capsys):
        """llm 后端不读校准：不能提示校准降级，要列出选择配置，且不回显凭据值。"""
        out = await self._runStatus(capsys, {"memoryHybridSelector": "llm"})
        assert "选择后端：llm（protocol=responses，model=gpt-5.6-terra，effort=high" in out
        assert "凭据已设置" in out
        assert "llm 后端不使用" in out
        assert "检索会降级" not in out
        assert "当前效果：hybrid（llm 选择）\n" in out
        assert "selector-key" not in out

    @pytest.mark.parametrize("apiKey, runtimeStatus, expected", [
        (None, None, "selector 凭据未设置，只保留常驻记忆"),
        ("selector-key", False, "Runtime 未注册，检索返回空结果"),
        ("selector-key", {"running": True, "closing": False, "encoderReady": False},
         "语义编码器尚未就绪，候选只来自词面通道"),
    ])
    async def test_status_llm_backend_explains_degraded_effect(self, capsys, apiKey, runtimeStatus, expected):
        """llm 后端的降级提示要对应它自己的失败路径。"""
        out = await self._runStatus(
            capsys, {"memoryHybridSelector": "llm"}, apiKey=apiKey, runtimeStatus=runtimeStatus)
        assert "凭据未设置" in out if apiKey is None else "凭据已设置" in out
        assert expected in out

    @pytest.mark.parametrize("config", [
        {"memoryHybridSelector": "llm", "memorySelectorProtocol": "message"},
        {"memoryHybridSelector": "typo"},
    ])
    async def test_status_invalid_selector_config_reports_empty_result(self, capsys, config):
        """选择配置无效时检索直接返回空结果，status 要如实说明。"""
        out = await self._runStatus(capsys, config)
        assert "配置无效：selectorConfig" in out
        assert "选择配置无效（selectorConfig），检索返回空结果" in out

    async def test_status_local_backend_ignores_llm_only_fields(self, capsys):
        """local 后端不读选择字段：其中的错误值不影响检索，不能报成配置无效。"""
        out = await self._runStatus(
            capsys, {"memoryHybridSelector": "local", "memorySelectorProtocol": "message"})
        assert "选择后端：local" in out
        assert "selectorConfig" not in out
        assert "检索会降级（calibrationStatusInvalid），只保留常驻记忆" in out
