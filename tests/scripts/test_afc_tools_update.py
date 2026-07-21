"""
tests/scripts/test_afc_tools_update.py

测试 afc_tools 的 update 命令：raw 字节比对检测（四分类）、备份-落盘-回滚原子性、
资格检查、source 反解、版本比较、--check / --all。

fixture 范式同 test_afc_tools_uninstall.py：tmp_path 重定向 PROJECT_ROOT，
网络层 patch 抽取接缝（_fetchRemoteManifest / _downloadToolFiles），
toolManager 换内存假实现。
"""

import os
import sys
import copy
import shutil

import pytest

# scripts/ 不在包路径里，手动加入以便 import
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import afc_tools  # noqa: E402
import utils.afc.toolManager as toolManager  # noqa: E402


_INIT = "utils/afc/tools/mytool/__init__.py"
_TRIG = "utils/afc/tools/mytool/triggers.py"
_TEST = "tests/utils/afc/tools/test_mytool.py"

_LOCAL_FILES = {
    _INIT: b"# init v1\n",
    _TRIG: b"KEYWORDS = ['x']\n",
    _TEST: b"# test v1\n",
}


def _makeManifest(**overrides):
    manifest = {
        "id": "mytool",
        "name": "My Tool",
        "version": "1.0.0",
        "files": [_INIT, _TRIG],
        "testFile": _TEST,
    }
    manifest.update(overrides)
    return manifest


@pytest.fixture
def updateEnv(tmp_path, monkeypatch):
    """隔离 update 的文件系统、网络接缝与状态读写。"""
    for rel, content in _LOCAL_FILES.items():
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    env = {
        "remoteManifest": _makeManifest(),
        "stagedFiles": {},          # relPath -> bytes，缺省给默认内容
        "downloadResult": None,     # 覆盖 _downloadToolFiles 返回值
        "fetchResult": None,        # 覆盖 _fetchRemoteManifest 返回值
        "builtin": {},
        "localDrafts": [],
        "saveOk": True,
        "logs": [],
        "saved": None,
        "sha": None,                # _fetchRemoteSha 的返回值（None = 无快车道）
        "fetchCalled": False,
        "custom": {
            "mytool": {
                "enabled": True,
                "manifest": _makeManifest(),
                "source": "github:https://github.com/user/repo@main",
                "installedAt": "2026-07-01T00:00:00",
            }
        },
    }

    def fakeFetch(user, repo, branches):
        env["fetchCalled"] = True
        if env["fetchResult"] is not None:
            return env["fetchResult"]
        return copy.deepcopy(env["remoteManifest"]), "main", None

    def fakeDownload(githubUrl, rawBase, files, testFile, tempDir):
        for relPath in list(files) + ([testFile] if testFile else []):
            dest = os.path.join(tempDir, relPath)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if relPath in env["stagedFiles"]:
                content = env["stagedFiles"][relPath]
            elif relPath.endswith("triggers.py"):
                content = b"KEYWORDS = ['y']\n"
            else:
                content = b"# staged\n"
            with open(dest, "wb") as f:
                f.write(content)
        if env["downloadResult"] is not None:
            return env["downloadResult"]
        return True, None

    def fakeGetAll():
        result = {}
        for k, v in env["builtin"].items():
            result[k] = {"enabled": True, "source": "builtin", "manifest": v}
        for k, v in env["custom"].items():
            result[k] = {"enabled": v.get("enabled", True), "source": v.get("source"), "manifest": v.get("manifest")}
        for k in env["localDrafts"]:
            result[k] = {"enabled": True, "source": "local-draft", "manifest": None}
        return result

    monkeypatch.setattr(afc_tools, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(afc_tools, "_log", lambda event, details="", level=None: env["logs"].append((event, details, level)))
    monkeypatch.setattr(afc_tools, "_fetchRemoteManifest", fakeFetch)
    monkeypatch.setattr(afc_tools, "_fetchRemoteSha", lambda user, repo, branch: env["sha"])
    monkeypatch.setattr(afc_tools, "_downloadToolFiles", fakeDownload)
    monkeypatch.setattr(toolManager, "getAllAfcTools", fakeGetAll)
    monkeypatch.setattr(toolManager, "loadBuiltinAfcTools", lambda: env["builtin"])
    monkeypatch.setattr(toolManager, "loadCustomAfcTools", lambda: copy.deepcopy(env["custom"]))

    def fakeSave(config):
        if env["saveOk"]:
            env["saved"] = copy.deepcopy(config)
        return env["saveOk"]

    monkeypatch.setattr(toolManager, "saveCustomAfcTools", fakeSave)

    env["tmpPath"] = tmp_path
    return env


def _localBytes(env, relPath):
    path = env["tmpPath"] / relPath
    return path.read_bytes() if path.exists() else None


def _stageLikeLocal(env):
    """让暂存内容与本地逐字节相同（已是最新场景）。"""
    env["stagedFiles"] = dict(_LOCAL_FILES)


class TestUpToDate:
    def test_uptodate_no_writes(self, updateEnv, capsys):
        _stageLikeLocal(updateEnv)

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "uptodate"
        assert "已是最新" in capsys.readouterr().out
        assert updateEnv["saved"] is None
        assert _localBytes(updateEnv, _INIT) == _LOCAL_FILES[_INIT]
        assert not (updateEnv["tmpPath"] / "data" / ".afc_install" / "backup_mytool").exists()
        # 有「更新开始」，无「更新成功」——已是最新不产生结果日志
        assert [e[0] for e in updateEnv["logs"]] == ["AFC 工具更新开始"]


class TestVersionBump:
    def test_version_bump_update(self, updateEnv, capsys):
        updateEnv["custom"]["mytool"]["enabled"] = False  # enabled 状态应保留
        updateEnv["remoteManifest"] = _makeManifest(version="1.1.0")
        updateEnv["stagedFiles"] = {_INIT: b"# init v2\n", _TRIG: b"KEYWORDS = ['y']\n", _TEST: b"# test v1\n"}

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "updated"
        out = capsys.readouterr().out
        assert "v1.0.0 → v1.1.0" in out
        assert "提示：重启 bot 后生效" in out
        assert _localBytes(updateEnv, _INIT) == b"# init v2\n"
        entry = updateEnv["saved"]["mytool"]
        assert entry["manifest"]["version"] == "1.1.0"
        assert entry["enabled"] is False
        assert entry["installedAt"] == "2026-07-01T00:00:00"
        assert entry["updatedAt"]
        assert not (updateEnv["tmpPath"] / "data" / ".afc_install" / "backup_mytool").exists()


class TestContentChangeWithoutVersionBump:
    def test_content_changed_same_version(self, updateEnv, capsys):
        updateEnv["stagedFiles"] = {_INIT: b"# init v2\n", _TRIG: b"KEYWORDS = ['x']\n", _TEST: b"# test v1\n"}

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "updated"
        assert "版本未变" in capsys.readouterr().out
        assert _localBytes(updateEnv, _INIT) == b"# init v2\n"


class TestManifestOnlyUpdate:
    def test_manifest_only_no_file_writes(self, updateEnv, capsys):
        _stageLikeLocal(updateEnv)
        updateEnv["remoteManifest"] = _makeManifest(description="新描述")  # 版本未变，仅清单变

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "updated"
        out = capsys.readouterr().out
        assert "仅清单信息更新" in out
        assert "更新文件..." not in out  # 空集豁免：不印落盘表头
        assert updateEnv["saved"]["mytool"]["manifest"]["description"] == "新描述"
        assert not (updateEnv["tmpPath"] / "data" / ".afc_install" / "backup_mytool").exists()

    def test_version_bump_without_file_changes(self, updateEnv, capsys):
        _stageLikeLocal(updateEnv)
        updateEnv["remoteManifest"] = _makeManifest(version="1.0.1")  # 只 bump 版本

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "updated"
        out = capsys.readouterr().out
        assert "v1.0.0 → v1.0.1" in out
        assert "更新文件..." not in out
        assert updateEnv["saved"]["mytool"]["manifest"]["version"] == "1.0.1"


class TestFileAddDelete:
    def test_add_and_delete(self, updateEnv, capsys):
        oldExtra = "utils/afc/tools/mytool/old.py"
        newExtra = "utils/afc/tools/mytool/new.py"
        (updateEnv["tmpPath"] / oldExtra).write_bytes(b"# old\n")
        updateEnv["custom"]["mytool"]["manifest"]["files"] = [_INIT, _TRIG, oldExtra]
        updateEnv["remoteManifest"] = _makeManifest(files=[_INIT, _TRIG, newExtra], version="1.1.0")
        updateEnv["stagedFiles"] = dict(_LOCAL_FILES) | {newExtra: b"# new\n"}

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "updated"
        out = capsys.readouterr().out
        assert "新增（1）" in out and "删除（1）" in out
        assert _localBytes(updateEnv, newExtra) == b"# new\n"
        assert not (updateEnv["tmpPath"] / oldExtra).exists()


class TestTestFileTransitions:
    def test_testfile_path_changed(self, updateEnv):
        newTest = "tests/utils/afc/tools/test_mytool_v2.py"
        updateEnv["remoteManifest"] = _makeManifest(testFile=newTest)
        updateEnv["stagedFiles"] = dict(_LOCAL_FILES) | {newTest: b"# test v2\n"}

        assert afc_tools.cmdUpdate("mytool", assumeYes=True) == "updated"
        assert _localBytes(updateEnv, newTest) == b"# test v2\n"
        assert not (updateEnv["tmpPath"] / _TEST).exists()  # 旧测试文件被删

    def test_testfile_removed_from_manifest(self, updateEnv):
        remote = _makeManifest()
        del remote["testFile"]
        updateEnv["remoteManifest"] = remote
        updateEnv["stagedFiles"] = dict(_LOCAL_FILES)

        assert afc_tools.cmdUpdate("mytool", assumeYes=True) == "updated"
        assert not (updateEnv["tmpPath"] / _TEST).exists()

    def test_testfile_download_failed_keeps_old(self, updateEnv, capsys):
        updateEnv["downloadResult"] = (False, None)  # testDownloaded=False
        updateEnv["stagedFiles"] = dict(_LOCAL_FILES)  # 不会走到下载，但备好

        # 新 manifest 仍声明 testFile，但下载失败 → 旧测试文件保留不误删
        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "uptodate"  # 无写入集变化，仅 manifest 相同
        assert (updateEnv["tmpPath"] / _TEST).exists()


class TestCheckMode:
    def test_check_only_reports(self, updateEnv, capsys):
        updateEnv["remoteManifest"] = _makeManifest(version="1.1.0")
        updateEnv["stagedFiles"] = {_INIT: b"# init v2\n", _TRIG: b"KEYWORDS = ['y']\n", _TEST: b"# test v1\n"}

        outcome = afc_tools.cmdUpdate("mytool", checkOnly=True)

        assert outcome == "updated"
        out = capsys.readouterr().out
        assert "v1.0.0 → v1.1.0" in out
        assert "未做任何更改" in out
        assert updateEnv["saved"] is None
        assert _localBytes(updateEnv, _INIT) == _LOCAL_FILES[_INIT]


class TestConfirmFlow:
    def test_cancel_on_no(self, updateEnv, monkeypatch, capsys):
        updateEnv["stagedFiles"] = {_INIT: b"# init v2\n", _TRIG: b"KEYWORDS = ['x']\n", _TEST: b"# test v1\n"}
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")

        outcome = afc_tools.cmdUpdate("mytool")

        assert outcome == "cancelled"
        assert "已取消更新" in capsys.readouterr().out
        assert _localBytes(updateEnv, _INIT) == _LOCAL_FILES[_INIT]
        assert updateEnv["saved"] is None

    def test_eof_treated_as_no(self, updateEnv, monkeypatch):
        updateEnv["stagedFiles"] = {_INIT: b"# init v2\n", _TRIG: b"KEYWORDS = ['x']\n", _TEST: b"# test v1\n"}

        def raiseEof(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", raiseEof)
        assert afc_tools.cmdUpdate("mytool") == "cancelled"

    def test_assume_yes_skips_input(self, updateEnv, monkeypatch):
        updateEnv["stagedFiles"] = {_INIT: b"# init v2\n", _TRIG: b"KEYWORDS = ['x']\n", _TEST: b"# test v1\n"}

        def forbidden(prompt=""):
            raise AssertionError("input 不应被调用")

        monkeypatch.setattr("builtins.input", forbidden)
        assert afc_tools.cmdUpdate("mytool", assumeYes=True) == "updated"


class TestEligibility:
    def test_builtin_guided_to_git_pull(self, updateEnv, capsys):
        updateEnv["custom"] = {}
        updateEnv["builtin"] = {"weather": {"id": "weather"}}

        assert afc_tools.cmdUpdate("weather") == "failed"
        assert "随仓库更新，请用 git pull" in capsys.readouterr().out

    def test_local_draft_no_source(self, updateEnv, capsys):
        updateEnv["custom"] = {}
        updateEnv["localDrafts"] = ["draft1"]

        assert afc_tools.cmdUpdate("draft1") == "failed"
        assert "没有登记来源" in capsys.readouterr().out

    def test_nonexistent_tool(self, updateEnv, capsys):
        updateEnv["custom"] = {}

        assert afc_tools.cmdUpdate("ghost") == "failed"
        assert "工具 ghost 不存在" in capsys.readouterr().out

    def test_builtin_shadow_rejected(self, updateEnv, capsys):
        updateEnv["builtin"] = {"mytool": {"id": "mytool"}}  # custom 与内置同名（json 异常）

        assert afc_tools.cmdUpdate("mytool") == "failed"
        assert "与内置工具同名" in capsys.readouterr().out

    def test_tool_id_format_rejected(self, updateEnv, capsys):
        assert afc_tools.cmdUpdate("../../../x") == "failed"
        assert "工具名格式无效" in capsys.readouterr().out


class TestSourceParsing:
    @pytest.mark.parametrize("source", [
        "https://github.com/user/repo@main",   # 缺 github: 前缀
        "github:https://github.com/user/repo",  # 缺 @分支
        "github:https://example.com/user/repo@main",  # URL 非 github.com
        "github:https://github.com/user/repo@",  # 分支为空
    ])
    def test_corrupted_source(self, updateEnv, source, capsys):
        updateEnv["custom"]["mytool"]["source"] = source

        assert afc_tools.cmdUpdate("mytool") == "failed"
        assert "来源信息损坏" in capsys.readouterr().out


class TestRemoteValidation:
    def test_remote_id_mismatch(self, updateEnv, capsys):
        updateEnv["remoteManifest"] = _makeManifest(id="othertool")

        assert afc_tools.cmdUpdate("mytool") == "failed"
        assert "与本地工具名" in capsys.readouterr().out

    @pytest.mark.parametrize("manifest,keyword", [
        (_makeManifest(name=""), "缺少必需字段"),
        (_makeManifest(files=[_INIT, "utils/afc/tools/mytool/../../x.py"]), "非法文件路径"),
        (_makeManifest(dependencies="notalist"), "dependencies 必须是字符串列表"),
    ])
    def test_manifest_validation_failures(self, updateEnv, manifest, keyword, capsys):
        updateEnv["remoteManifest"] = manifest

        assert afc_tools.cmdUpdate("mytool") == "failed"
        assert keyword in capsys.readouterr().out
        assert updateEnv["saved"] is None


class TestDowngrade:
    def test_downgrade_warns_but_updates(self, updateEnv, capsys):
        updateEnv["custom"]["mytool"]["manifest"]["version"] = "2.0.0"
        updateEnv["remoteManifest"] = _makeManifest(version="1.0.0")
        updateEnv["stagedFiles"] = {_INIT: b"# init v2\n", _TRIG: b"KEYWORDS = ['x']\n", _TEST: b"# test v1\n"}

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "updated"
        assert "低于本地" in capsys.readouterr().out


class TestLocalStateRecovery:
    def test_local_file_missing_restored(self, updateEnv, capsys):
        (updateEnv["tmpPath"] / _INIT).unlink()
        updateEnv["stagedFiles"] = dict(_LOCAL_FILES)

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "updated"
        assert "本地缺失" in capsys.readouterr().out
        assert _localBytes(updateEnv, _INIT) == _LOCAL_FILES[_INIT]

    def test_tool_dir_missing_full_restore(self, updateEnv):
        shutil.rmtree(updateEnv["tmpPath"] / "utils" / "afc" / "tools" / "mytool")
        updateEnv["stagedFiles"] = dict(_LOCAL_FILES)

        assert afc_tools.cmdUpdate("mytool", assumeYes=True) == "updated"
        assert _localBytes(updateEnv, _INIT) == _LOCAL_FILES[_INIT]
        assert _localBytes(updateEnv, _TRIG) == _LOCAL_FILES[_TRIG]


class TestDeletionPathEscape:
    def test_tampered_old_manifest_skipped(self, updateEnv, capsys):
        escapeTarget = updateEnv["tmpPath"] / "core" / "crypto.py"
        escapeTarget.parent.mkdir(parents=True)
        escapeTarget.write_bytes(b"# do not delete\n")
        updateEnv["custom"]["mytool"]["manifest"]["files"] = [_INIT, _TRIG, "../../core/crypto.py"]
        updateEnv["stagedFiles"] = dict(_LOCAL_FILES)

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "updated"  # 有删除集（逃逸路径）→ 走更新流程
        out = capsys.readouterr().out
        assert "[SKIP] 跳过非法路径" in out
        assert escapeTarget.exists()
        assert any("更新路径逃逸" in e[0] for e in updateEnv["logs"])


class TestDependencies:
    def test_missing_deps_warns_not_blocks(self, updateEnv, capsys):
        updateEnv["remoteManifest"] = _makeManifest(dependencies=["nonexistent-pkg-xyz"])
        updateEnv["stagedFiles"] = {_INIT: b"# init v2\n", _TRIG: b"KEYWORDS = ['x']\n", _TEST: b"# test v1\n"}

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "updated"
        assert "nonexistent-pkg-xyz" in capsys.readouterr().out
        assert any("依赖缺失" in e[0] for e in updateEnv["logs"])


class TestNetworkFailures:
    @pytest.mark.parametrize("fetchResult,keyword", [
        ((None, None, "afcTool.json 超过 1MB 上限（分支 main）"), "超过 1MB 上限"),
        ((None, None, "afcTool.json 不是合法 JSON（分支 main）"), "不是合法 JSON"),
        ((None, None, None), "分支 main 不存在或无法访问（分支可能已改名"),
    ])
    def test_fetch_failures(self, updateEnv, fetchResult, keyword, capsys):
        updateEnv["fetchResult"] = fetchResult

        assert afc_tools.cmdUpdate("mytool") == "failed"
        assert keyword in capsys.readouterr().out
        assert updateEnv["saved"] is None

    def test_download_failure(self, updateEnv, capsys):
        updateEnv["downloadResult"] = (False, "下载失败或超过 2MB 上限：x.py")

        assert afc_tools.cmdUpdate("mytool") == "failed"
        assert "下载失败" in capsys.readouterr().out
        assert _localBytes(updateEnv, _INIT) == _LOCAL_FILES[_INIT]

    def test_bad_staging_triggers(self, updateEnv, capsys):
        updateEnv["stagedFiles"] = {_TRIG: b"KEYWORDS = []\nPATTERNS = []\n"}

        assert afc_tools.cmdUpdate("mytool") == "failed"
        assert "缺少 KEYWORDS 和 PATTERNS" in capsys.readouterr().out
        assert _localBytes(updateEnv, _INIT) == _LOCAL_FILES[_INIT]


class TestRollback:
    def test_apply_failure_rolls_back(self, updateEnv, monkeypatch, capsys):
        updateEnv["stagedFiles"] = {_INIT: b"# init v2\n", _TRIG: b"KEYWORDS = ['y']\n", _TEST: b"# test v1\n"}
        tmpPath = updateEnv["tmpPath"]

        def flakyApply(tempDir, fileList, pendingTmps):
            # 先真应用第一个文件再抛——验证已落盘的文件也被还原
            first = fileList[0]
            shutil.copy(os.path.join(tempDir, first), tmpPath / first)
            raise OSError("disk full")

        monkeypatch.setattr(afc_tools, "_applyStagedFiles", flakyApply)

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "failed"
        out = capsys.readouterr().out
        assert "已回滚到原版本" in out
        assert _localBytes(updateEnv, _INIT) == _LOCAL_FILES[_INIT]  # v1 还原
        assert _localBytes(updateEnv, _TRIG) == _LOCAL_FILES[_TRIG]
        assert updateEnv["saved"] is None
        assert not (tmpPath / "data" / ".afc_install" / "backup_mytool").exists()

    def test_save_failure_rolls_back(self, updateEnv, capsys):
        updateEnv["saveOk"] = False
        updateEnv["stagedFiles"] = {_INIT: b"# init v2\n", _TRIG: b"KEYWORDS = ['x']\n", _TEST: b"# test v1\n"}

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "failed"
        assert "已回滚到原版本" in capsys.readouterr().out
        assert _localBytes(updateEnv, _INIT) == _LOCAL_FILES[_INIT]  # 已落盘的 v2 被还原
        assert updateEnv["saved"] is None

    def test_rollback_partial_failure_keeps_backup(self, updateEnv, monkeypatch, capsys):
        updateEnv["saveOk"] = False
        updateEnv["stagedFiles"] = {_INIT: b"# init v2\n", _TRIG: b"KEYWORDS = ['x']\n", _TEST: b"# test v1\n"}
        monkeypatch.setattr(afc_tools, "_rollbackToolFiles", lambda backupDir, newWritten: (0, ["x.py"]))

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "failed"
        out = capsys.readouterr().out
        assert "回滚不完整" in out
        assert "backup_mytool" in out
        assert (updateEnv["tmpPath"] / "data" / ".afc_install" / "backup_mytool").exists()


class TestCompareVersions:
    @pytest.mark.parametrize("localVer,remoteVer,expected", [
        ("1.0.0", "1.1.0", 1),
        ("1.1.0", "1.0.0", -1),
        ("1.2", "1.2.0", 0),
        ("1.9.9", "1.10.0", 1),       # int 比较，非字典序
        ("1.2.0-beta", "1.2.0", 1),   # release > prerelease
        ("1.2.0", "1.2.0-beta", -1),
        ("1.2.0-alpha", "1.2.0-beta", 1),
        (None, "1.0", None),
        ("", "1.0", None),
        ("1.0", None, None),
        (1.0, "1.0.0", 0),            # 数字容忍
        ("abc", "1.0", 1),            # 非数字段按 (0, "abc") 可比
    ])
    def test_compare(self, localVer, remoteVer, expected):
        assert afc_tools._compareVersions(localVer, remoteVer) == expected


class TestFastLane:
    """remoteSha 快车道：加速器不是裁决，--check 始终全量比对"""

    def test_sha_match_uptodate_skips_download(self, updateEnv, capsys):
        updateEnv["custom"]["mytool"]["remoteSha"] = "abc123"
        updateEnv["sha"] = "abc123"

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "uptodate"
        assert "远端提交未变" in capsys.readouterr().out
        assert updateEnv["fetchCalled"] is False  # 快车道直接返回，未抓 manifest
        assert updateEnv["saved"] is None
        assert updateEnv["logs"] == []  # 不记「更新开始」（防孤儿日志）

    def test_sha_mismatch_falls_through(self, updateEnv):
        updateEnv["custom"]["mytool"]["remoteSha"] = "abc123"
        updateEnv["sha"] = "def456"  # 远端已变
        updateEnv["stagedFiles"] = {_INIT: b"# init v2\n", _TRIG: b"KEYWORDS = ['x']\n", _TEST: b"# test v1\n"}

        outcome = afc_tools.cmdUpdate("mytool", assumeYes=True)

        assert outcome == "updated"
        assert updateEnv["fetchCalled"] is True  # 退化到完整流程
        assert _localBytes(updateEnv, _INIT) == b"# init v2\n"
        assert updateEnv["saved"]["mytool"]["remoteSha"] == "def456"  # 成功后刷新 sha

    def test_no_sha_full_flow(self, updateEnv):
        _stageLikeLocal(updateEnv)

        assert afc_tools.cmdUpdate("mytool", assumeYes=True) == "uptodate"
        assert updateEnv["fetchCalled"] is True  # 无 sha → 完整比对

    def test_check_skips_fast_lane(self, updateEnv, capsys):
        updateEnv["custom"]["mytool"]["remoteSha"] = "abc123"
        updateEnv["sha"] = "abc123"  # 即使 sha 相同，--check 也不走进车道
        _stageLikeLocal(updateEnv)

        outcome = afc_tools.cmdUpdate("mytool", checkOnly=True)

        assert outcome == "uptodate"
        assert updateEnv["fetchCalled"] is True  # --check 始终全量比对
        assert "已是最新" in capsys.readouterr().out


class TestUpdateAll:
    def test_all_summary(self, updateEnv, monkeypatch, capsys):
        # 两个工具：mytool 有更新，othertool 已是最新
        otherInit = "utils/afc/tools/othertool/__init__.py"
        otherTrig = "utils/afc/tools/othertool/triggers.py"
        otherTest = "tests/utils/afc/tools/test_othertool.py"
        updateEnv["custom"]["othertool"] = {
            "enabled": True,
            "manifest": _makeManifest(id="othertool", files=[otherInit, otherTrig], testFile=otherTest),
            "source": "github:https://github.com/user/repo2@main",
            "installedAt": "2026-07-01T00:00:00",
        }
        for rel, content in ((otherInit, b"# same\n"), (otherTrig, b"KEYWORDS = ['o']\n"), (otherTest, b"# same\n")):
            dest = updateEnv["tmpPath"] / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)

        def fakeDownload2(githubUrl, rawBase, files, testFile, tempDir):
            for relPath in list(files) + ([testFile] if testFile else []):
                dest = os.path.join(tempDir, relPath)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if relPath == _INIT:
                    content = b"# init v2\n"
                elif relPath == _TRIG:
                    content = b"KEYWORDS = ['y']\n"
                elif relPath == otherTrig:
                    content = b"KEYWORDS = ['o']\n"
                else:
                    content = b"# same\n"
                with open(dest, "wb") as f:
                    f.write(content)
            return True, None

        def fakeFetch2(user, repo, branches):
            if repo == "repo":
                return _makeManifest(), "main", None
            return _makeManifest(id="othertool", files=[otherInit, otherTrig], testFile=otherTest), "main", None

        monkeypatch.setattr(afc_tools, "_downloadToolFiles", fakeDownload2)
        monkeypatch.setattr(afc_tools, "_fetchRemoteManifest", fakeFetch2)

        afc_tools.cmdUpdateAll(checkOnly=False, assumeYes=True)

        out = capsys.readouterr().out
        assert "共 2 个：更新 1 / 已是最新 1 / 失败 0 / 取消 0" in out

    def test_all_empty(self, updateEnv, capsys):
        updateEnv["custom"] = {}

        afc_tools.cmdUpdateAll(checkOnly=True, assumeYes=False)
        assert "没有已安装的第三方工具" in capsys.readouterr().out

    def test_main_rejects_bare_all(self, updateEnv, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["afc_tools.py", "update", "--all"])

        afc_tools.main()
        assert "--all 需要配合 --check 或 --yes 使用" in capsys.readouterr().out
