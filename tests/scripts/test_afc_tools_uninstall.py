"""
tests/scripts/test_afc_tools_uninstall.py

测试 afc_tools 的 uninstall 命令行为，重点是残留 afcTool.json 的清理：
afcTool.json 不在 manifest 的 files 清单里，若卸载后留在目录里，
工具会被 getAllAfcTools 当成 local-draft「复活」。
"""

import os
import sys

import pytest

# scripts/ 不在包路径里，手动加入以便 import
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import afc_tools  # noqa: E402
import utils.afc.toolManager as toolManager  # noqa: E402


@pytest.fixture
def fakeTool(tmp_path, monkeypatch):
    """在临时项目根里造一个 custom 工具，并把 afc_tools/toolManager 的读写都引到那里。

    - afc_tools.PROJECT_ROOT 重定向到 tmp_path，文件操作不碰真实项目
    - toolManager 的查询/保存换成内存假实现，不读写真实 data/*.json
    - afc_tools._log 换成空操作，不写真实日志
    """
    toolDir = tmp_path / "utils" / "afc" / "tools" / "mytool"
    toolDir.mkdir(parents=True)
    (toolDir / "__init__.py").write_text("# init\n", encoding="utf-8")
    (toolDir / "triggers.py").write_text("KEYWORDS = ['x']\n", encoding="utf-8")
    (toolDir / "afcTool.json").write_text("{}\n", encoding="utf-8")

    testDir = tmp_path / "tests" / "utils" / "afc" / "tools"
    testDir.mkdir(parents=True)
    (testDir / "test_mytool.py").write_text("# test\n", encoding="utf-8")

    manifest = {
        "id": "mytool",
        "name": "My Tool",
        "version": "1.0.0",
        "files": [
            "utils/afc/tools/mytool/__init__.py",
            "utils/afc/tools/mytool/triggers.py",
        ],
        "testFile": "tests/utils/afc/tools/test_mytool.py",
    }

    savedCustom = {}

    monkeypatch.setattr(afc_tools, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(afc_tools, "_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        toolManager, "getAllAfcTools",
        lambda: {"mytool": {"enabled": True, "source": "github:https://example.com/x/y@main", "manifest": manifest}},
    )
    monkeypatch.setattr(toolManager, "loadCustomAfcTools", lambda: {"mytool": {"manifest": manifest}})
    monkeypatch.setattr(
        toolManager, "saveCustomAfcTools",
        lambda config: savedCustom.clear() or savedCustom.update(config) or True,
    )

    return {"toolDir": toolDir, "testDir": testDir, "savedCustom": savedCustom}


class TestUninstallLeftoverCleanup:
    """卸载时残留 afcTool.json 的清理"""

    def test_leftover_afctool_json_removed(self, fakeTool):
        """afcTool.json 不在 files 清单里，也应被顺手删掉，空目录一并清理"""
        toolDir = fakeTool["toolDir"]

        afc_tools.cmdUninstall("mytool", force=False)

        assert not (toolDir / "afcTool.json").exists()
        assert not (toolDir / "__init__.py").exists()
        assert not (fakeTool["testDir"] / "test_mytool.py").exists()
        # 目录清空后应被移除——否则工具会作为 local-draft「复活」
        assert not toolDir.exists()
        assert "mytool" not in fakeTool["savedCustom"]

    def test_dir_kept_when_other_files_remain(self, fakeTool):
        """目录里还有 manifest 之外的文件时，目录保留，但 afcTool.json 仍清掉"""
        toolDir = fakeTool["toolDir"]
        (toolDir / "extra.py").write_text("# not in manifest\n", encoding="utf-8")

        afc_tools.cmdUninstall("mytool", force=False)

        assert not (toolDir / "afcTool.json").exists()
        assert (toolDir / "extra.py").exists()
        assert toolDir.exists()