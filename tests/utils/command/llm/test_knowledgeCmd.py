"""
tests/utils/command/llm/test_knowledgeCmd.py

/llm knowledge 子命令分支（utils/command/llm/knowledgeCmd.py）：开关、
reindex、stats、search、maxresults/minscore、速查表契约。
utils.llm getter/setter 与检索 mock。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.command.llm import knowledgeCmd
from utils.command.llm.knowledgeCmd import _handleKnowledgeCommand, _KNOWLEDGE_SUBCOMMANDS



class TestSwitches:
    @patch.object(knowledgeCmd, "setKnowledgeEnabled")
    @patch.object(knowledgeCmd, "logAction", new_callable=AsyncMock)
    async def test_on(self, mockLog, mockSet):
        await _handleKnowledgeCommand(["on"])
        mockSet.assert_called_once_with(True)

    @patch.object(knowledgeCmd, "setKnowledgeEnabled")
    @patch.object(knowledgeCmd, "logAction", new_callable=AsyncMock)
    async def test_off(self, mockLog, mockSet):
        await _handleKnowledgeCommand(["off"])
        mockSet.assert_called_once_with(False)



class TestStatusAndStats:
    @patch.object(knowledgeCmd, "getKnowledgeMaxResults", return_value=3)
    @patch.object(knowledgeCmd, "getKnowledgeMinScore", return_value=0.5)
    @patch.object(knowledgeCmd, "getKnowledgeEnabled", return_value=True)
    async def test_no_args_status(self, mockEn, mockMin, mockMax, capsys):
        await _handleKnowledgeCommand([])
        out = capsys.readouterr().out
        assert "知识库：开启" in out and "召回数：3" in out

    @patch.object(knowledgeCmd, "getKnowledgeStats", new_callable=AsyncMock)
    async def test_stats_renders(self, mockStats, capsys):
        mockStats.return_value = {
            "total": 10, "enabled": 8, "byCategory": {"interests": 5, "style": 3},
            "avgTags": 2.4, "source_files": 2,
        }
        await _handleKnowledgeCommand(["stats"])
        out = capsys.readouterr().out
        assert "总条目数：10" in out and "interests: 5" in out



class TestSearch:
    async def test_search_without_query_usage(self, capsys):
        await _handleKnowledgeCommand(["search"])
        assert "用法" in capsys.readouterr().out

    @patch.object(knowledgeCmd, "retrieveKnowledge", new_callable=AsyncMock)
    async def test_search_renders_results(self, mockRetr, capsys):
        mockRetr.return_value = [
            {"id": 1, "category": "interests", "title": "猫", "content": "喜欢猫", "score": 3.2},
        ]
        await _handleKnowledgeCommand(["search", "猫"])
        out = capsys.readouterr().out
        assert "猫" in out and "3.20" in out

    @patch.object(knowledgeCmd, "retrieveKnowledge", new_callable=AsyncMock, return_value=[])
    async def test_search_no_results(self, mockRetr, capsys):
        await _handleKnowledgeCommand(["search", "不存在的话题"])
        assert "未找到" in capsys.readouterr().out



class TestParams:
    @patch.object(knowledgeCmd, "getKnowledgeMaxResults", return_value=3)
    async def test_maxresults_query(self, mockGet, capsys):
        await _handleKnowledgeCommand(["maxresults"])
        assert "当前召回数：3" in capsys.readouterr().out

    @patch.object(knowledgeCmd, "setKnowledgeMaxResults")
    @patch.object(knowledgeCmd, "logAction", new_callable=AsyncMock)
    async def test_maxresults_set(self, mockLog, mockSet):
        await _handleKnowledgeCommand(["maxresults", "5"])
        mockSet.assert_called_once_with(5)

    @patch.object(knowledgeCmd, "getKnowledgeMinScore", return_value=0.5)
    async def test_minscore_query(self, mockGet, capsys):
        await _handleKnowledgeCommand(["minscore"])
        assert "当前最低分：0.5" in capsys.readouterr().out



class TestFallbackAndContract:
    async def test_unknown_renders_table(self, capsys):
        await _handleKnowledgeCommand(["bogus"])
        assert "/llm knowledge 可用的子命令有" in capsys.readouterr().out

    def test_speedtable_covers_match_branches(self):
        assert "on | off" in _KNOWLEDGE_SUBCOMMANDS
        for sub in ("reindex", "list", "stats", "search", "maxresults", "minscore"):
            assert any(key.startswith(sub) for key in _KNOWLEDGE_SUBCOMMANDS), sub
