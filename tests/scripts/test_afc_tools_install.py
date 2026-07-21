"""
tests/scripts/test_afc_tools_install.py

install 输出钉扎测试：patch 网络接缝（_fetchRemoteManifest / _downloadWithLimit），
保留 _downloadToolFiles / _validateStagingTriggers / _applyStagedFiles 为真实实现，
capsys 逐字断言 happy path 输出序列（对照 docs/afc-tool-management.md 的 install 示例）。

Phase 1 抽取重构的安全网：重构后 install 的输出与行为必须逐字不变。
"""

import os
import sys
import copy

import pytest

# scripts/ 不在包路径里，手动加入以便 import
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import afc_tools  # noqa: E402
import utils.afc.toolManager as toolManager  # noqa: E402


_MANIFEST = {
    "id": "mytool",
    "name": "My Tool",
    "version": "1.0.0",
    "files": [
        "utils/afc/tools/mytool/__init__.py",
        "utils/afc/tools/mytool/triggers.py",
    ],
    "testFile": "tests/utils/afc/tools/test_mytool.py",
}


@pytest.fixture
def installEnv(tmp_path, monkeypatch):
    """隔离 install 的文件系统与网络：PROJECT_ROOT 重定向到 tmp_path，
    网络层 patch 接缝函数（triggers.py 写入合法内容，供真实的 AST 校验通过）。
    """
    env = {"saveOk": True, "savedCustom": {}}

    def fakeFetchManifest(user, repo, branches):
        return _MANIFEST, "main", None

    def fakeDownloadWithLimit(url, destPath, maxBytes):
        os.makedirs(os.path.dirname(destPath), exist_ok=True)
        if url.endswith("triggers.py"):
            content = b"KEYWORDS = ['x']\nPATTERNS = []\n"
        else:
            content = b"# file\n"
        with open(destPath, "wb") as f:
            f.write(content)
        return True

    def fakeSave(config):
        if env["saveOk"]:
            env["savedCustom"] = copy.deepcopy(config)
        return env["saveOk"]

    monkeypatch.setattr(afc_tools, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(afc_tools, "_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(afc_tools, "_fetchRemoteManifest", fakeFetchManifest)
    monkeypatch.setattr(afc_tools, "_downloadWithLimit", fakeDownloadWithLimit)
    # 必须 patch，否则会真实访问 api.github.com
    monkeypatch.setattr(afc_tools, "_fetchRemoteSha", lambda user, repo, branch: "sha123")
    monkeypatch.setattr(toolManager, "loadBuiltinAfcTools", lambda: {})
    monkeypatch.setattr(toolManager, "loadCustomAfcTools", lambda: dict(env["savedCustom"]))
    monkeypatch.setattr(toolManager, "saveCustomAfcTools", fakeSave)

    env["tmpPath"] = tmp_path
    return env


class TestInstallHappyPath:
    """happy path 输出逐字钉扎 + 落盘与登记行为"""

    def test_output_sequence_and_side_effects(self, installEnv, capsys):
        result = afc_tools.cmdInstall("https://github.com/user/repo", None, False)
        assert result is True

        out = capsys.readouterr().out
        expected = (
            "正在从 https://github.com/user/repo (分支: main) 下载 2 个文件...\n"
            "  [OK] utils/afc/tools/mytool/__init__.py\n"
            "  [OK] utils/afc/tools/mytool/triggers.py\n"
            "  [OK] tests/utils/afc/tools/test_mytool.py\n"
            "安装文件...\n"
            "  [OK] utils/afc/tools/mytool/__init__.py\n"
            "  [OK] utils/afc/tools/mytool/triggers.py\n"
            "  [OK] tests/utils/afc/tools/test_mytool.py\n"
            "\n"
            "[OK] 工具 mytool 安装成功\n"
            "\n"
            "提示：重启 bot 后生效\n"
            "\n"
        )
        assert out == expected

        # 文件真实落盘
        tmpPath = installEnv["tmpPath"]
        assert (tmpPath / "utils" / "afc" / "tools" / "mytool" / "__init__.py").exists()
        assert (tmpPath / "utils" / "afc" / "tools" / "mytool" / "triggers.py").exists()
        assert (tmpPath / "tests" / "utils" / "afc" / "tools" / "test_mytool.py").exists()

        # 暂存目录与 .tmp 无残留（finally 清理）
        staging = tmpPath / "data" / ".afc_install"
        assert not staging.exists() or list(staging.iterdir()) == []

        # 登记 custom json：字段齐全（含快车道加速器字段 remoteSha）
        entry = installEnv["savedCustom"]["mytool"]
        assert entry["enabled"] is True
        assert entry["manifest"] == _MANIFEST
        assert entry["source"] == "github:https://github.com/user/repo@main"
        assert entry["installedAt"]
        assert entry["remoteSha"] == "sha123"


class TestInstallFailFast:
    """失败路径的关键出口钉扎"""

    def test_invalid_url(self, capsys):
        assert afc_tools.cmdInstall("https://example.com/user/repo", None, False) is False
        assert "[X] 无效的 GitHub URL" in capsys.readouterr().out

    def test_builtin_rejected(self, installEnv, monkeypatch, capsys):
        monkeypatch.setattr(toolManager, "loadBuiltinAfcTools", lambda: {"mytool": {}})

        assert afc_tools.cmdInstall("https://github.com/user/repo", None, False) is False
        assert "[X] 工具 mytool 是内置工具，禁止覆盖安装" in capsys.readouterr().out
        # 失败于下载前：无任何文件写入
        assert not (installEnv["tmpPath"] / "utils" / "afc" / "tools" / "mytool").exists()


class TestInstallRollback:
    """落盘/登记失败时的备份-回滚（与 update 同款的工具级原子）"""

    def test_save_failure_rolls_back(self, installEnv, capsys):
        """登记 json 失败 → 已落盘的文件全部清走，不留 local-draft 半成品"""
        installEnv["saveOk"] = False

        assert afc_tools.cmdInstall("https://github.com/user/repo", None, False) is False

        out = capsys.readouterr().out
        assert "已回滚清理" in out
        tmpPath = installEnv["tmpPath"]
        assert not (tmpPath / "utils" / "afc" / "tools" / "mytool").exists()
        assert not (tmpPath / "tests" / "utils" / "afc" / "tools" / "test_mytool.py").exists()
        assert not (tmpPath / "data" / ".afc_install" / "backup_install_mytool").exists()

    def test_apply_failure_restores_overwritten(self, installEnv, monkeypatch, capsys):
        """-f 覆盖安装时落盘中途失败 → 被覆盖的旧文件从备份还原"""
        tmpPath = installEnv["tmpPath"]
        for rel in _MANIFEST["files"]:
            dest = tmpPath / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"# old version\n")

        def flakyApply(tempDir, fileList, pendingTmps):
            # 先真应用第一个文件再抛——验证已落盘的覆盖被还原
            import shutil as _shutil
            first = fileList[0]
            _shutil.copy(os.path.join(tempDir, first), tmpPath / first)
            raise OSError("disk full")

        monkeypatch.setattr(afc_tools, "_applyStagedFiles", flakyApply)

        assert afc_tools.cmdInstall("https://github.com/user/repo", None, True) is False

        assert "已回滚清理" in capsys.readouterr().out
        for rel in _MANIFEST["files"]:
            assert (tmpPath / rel).read_bytes() == b"# old version\n"
        assert not (tmpPath / "data" / ".afc_install" / "backup_install_mytool").exists()
