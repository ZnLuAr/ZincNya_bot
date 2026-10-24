"""测试 Hybrid Memory 上线面板的检测层与动作分派。

交互主循环（input() 流程）不测——人机交互靠手动验收。
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import memoryPanel
from scripts.memoryPanel import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    _buildCommand,
    checkCalibration,
    checkDependencies,
    checkHoldoutReport,
    checkModelInstalled,
    checkSelectorConfig,
    checkSelectorCredentials,
    findLatestReport,
    runAction,
    summarizeHoldoutReport,
    summarizeResourceReport,
)


# === checkDependencies ===


def test_checkDependenciesOk(monkeypatch):
    """三个依赖都可 import 时判 ok。"""
    realImport = __import__

    def fakeImport(name, *args, **kwargs):
        if name in ("onnxruntime", "tokenizers", "numpy"):
            return object()
        return realImport(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fakeImport)
    status, detail = checkDependencies()
    assert status == STATUS_OK
    assert "可用" in detail


def test_checkDependenciesFail(monkeypatch):
    """三个依赖都缺时判 fail。"""
    realImport = __import__

    def fakeImport(name, *args, **kwargs):
        if name in ("onnxruntime", "tokenizers", "numpy"):
            raise ImportError(name)
        return realImport(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fakeImport)
    status, detail = checkDependencies()
    assert status == STATUS_FAIL
    assert "requirements/memory.txt" in detail


def test_checkDependenciesPartialMissingIsWarn(monkeypatch):
    """部分依赖缺失时判 warn（不是 fail）。"""
    realImport = __import__

    def fakeImport(name, *args, **kwargs):
        if name == "tokenizers":
            raise ImportError(name)
        return realImport(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fakeImport)
    status, _ = checkDependencies()
    assert status == STATUS_WARN


# === checkModelInstalled ===


def test_checkModelInstalledOk(monkeypatch):
    """verifyModel 返回全部有效时判 ok。"""
    monkeypatch.setattr(
        memoryPanel, "verifyModel",
        lambda: (True, [{"valid": True}, {"valid": True}]),
    )
    status, detail = checkModelInstalled()
    assert status == STATUS_OK
    assert "2" in detail


def test_checkModelInstalledFail(monkeypatch):
    """verifyModel 返回有损坏时判 fail。"""
    monkeypatch.setattr(
        memoryPanel, "verifyModel",
        lambda: (False, [{"valid": True}, {"valid": False}]),
    )
    status, detail = checkModelInstalled()
    assert status == STATUS_FAIL
    assert "1/2" in detail


def test_checkModelInstalledVerifyRaisesIsFail(monkeypatch):
    """verifyModel 抛 OSError 时判 fail，不向上冒泡。"""
    def raiseOSError():
        raise OSError("boom")

    monkeypatch.setattr(memoryPanel, "verifyModel", raiseOSError)
    status, detail = checkModelInstalled()
    assert status == STATUS_FAIL
    assert "boom" in detail


# === checkCalibration ===


def test_checkCalibrationApproved(monkeypatch):
    """loadCalibratedThresholds 返回 (阈值, None) 时判 ok。"""
    monkeypatch.setattr(
        memoryPanel, "loadCalibratedThresholds",
        lambda: ({"semanticCurrent": 0.7, "semanticAssisted": 0.65, "lexical": None}, None),
    )
    status, detail = checkCalibration()
    assert status == STATUS_OK
    assert "approved" in detail


def test_checkCalibrationUnconfiguredIsFail(monkeypatch):
    """reasonCode=calibrationStatusInvalid（从未配置）判 fail。"""
    monkeypatch.setattr(
        memoryPanel, "loadCalibratedThresholds",
        lambda: (
            {"semanticCurrent": None, "semanticAssisted": None, "lexical": None},
            "calibrationStatusInvalid",
        ),
    )
    status, detail = checkCalibration()
    assert status == STATUS_FAIL
    assert "calibrationStatusInvalid" in detail
    assert "从未配置" in detail


def test_checkCalibrationCandidateIsWarn(monkeypatch):
    """reasonCode=calibrationCandidate（候选未批准）判 warn。"""
    monkeypatch.setattr(
        memoryPanel, "loadCalibratedThresholds",
        lambda: (
            {"semanticCurrent": None, "semanticAssisted": None, "lexical": None},
            "calibrationCandidate",
        ),
    )
    status, detail = checkCalibration()
    assert status == STATUS_WARN
    assert "候选" in detail


def test_checkCalibrationModelMismatchIsWarn(monkeypatch):
    """reasonCode=calibrationModelMismatch 判 warn，detail 含人话。"""
    monkeypatch.setattr(
        memoryPanel, "loadCalibratedThresholds",
        lambda: (
            {"semanticCurrent": None, "semanticAssisted": None, "lexical": None},
            "calibrationModelMismatch",
        ),
    )
    status, detail = checkCalibration()
    assert status == STATUS_WARN
    assert "模型已更换" in detail


# === checkSelectorConfig ===


def test_checkSelectorConfigOk(monkeypatch):
    """getMemorySelectorSettings 正常返回时判 ok。"""
    monkeypatch.setattr(memoryPanel, "loadLLMConfig", lambda: {})
    monkeypatch.setattr(
        memoryPanel, "getMemorySelectorSettings",
        lambda config: {
            "backend": "llm", "protocol": "messages",
            "model": "claude-opus-4-8", "effort": "high", "timeoutSeconds": 30.0,
        },
    )
    status, detail = checkSelectorConfig()
    assert status == STATUS_OK
    assert "messages" in detail
    assert "claude-opus-4-8" in detail


def test_checkSelectorConfigFail(monkeypatch):
    """getMemorySelectorSettings 抛 ValueError 时判 fail。"""
    monkeypatch.setattr(memoryPanel, "loadLLMConfig", lambda: {})

    def raiseValueError(config):
        raise ValueError("selectorConfig")

    monkeypatch.setattr(memoryPanel, "getMemorySelectorSettings", raiseValueError)
    status, detail = checkSelectorConfig()
    assert status == STATUS_FAIL
    assert "selectorConfig" in detail


# === checkSelectorCredentials ===


def test_checkSelectorCredentialsSet(monkeypatch):
    """两个凭据都非空时判 ok。"""
    monkeypatch.setattr(memoryPanel, "LLM_MEMORY_SELECTOR_BASE_URL", "https://example.com")
    monkeypatch.setattr(memoryPanel, "LLM_MEMORY_SELECTOR_API_KEY", "sk-secret")
    status, _ = checkSelectorCredentials()
    assert status == STATUS_OK


def test_checkSelectorCredentialsMissing(monkeypatch):
    """凭据缺失时判 fail，且 detail 不回显值。"""
    monkeypatch.setattr(memoryPanel, "LLM_MEMORY_SELECTOR_BASE_URL", None)
    monkeypatch.setattr(memoryPanel, "LLM_MEMORY_SELECTOR_API_KEY", "sk-secret")
    status, detail = checkSelectorCredentials()
    assert status == STATUS_FAIL
    assert "BASE_URL" in detail
    assert "sk-secret" not in detail


# === findLatestReport ===


def _writeReport(tmp_path: Path, name: str, data: dict, *, mtimeOffset: float = 0.0):
    """在 tmp_path 下写一份报告，可选设置 mtime（越大的越新）。"""
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    if mtimeOffset:
        target = time.time() + mtimeOffset
        os.utime(path, (target, target))
    return path


def test_findLatestReportHoldoutPicksNewestMatching(tmp_path, monkeypatch):
    """按 mtime 倒序扫，split==holdout 的最新一份胜出。"""
    monkeypatch.setattr(memoryPanel, "LLM_MEMORY_REPORT_DIR", str(tmp_path))
    _writeReport(tmp_path, "old-holdout.json", {"split": "holdout", "modes": {}}, mtimeOffset=-100)
    _writeReport(tmp_path, "encoder.json", {"rssBytes": {}}, mtimeOffset=-50)
    _writeReport(tmp_path, "new-holdout.json", {"split": "holdout", "modes": {}}, mtimeOffset=-10)
    report, fileName = findLatestReport("holdout")
    assert fileName == "new-holdout.json"
    assert report["split"] == "holdout"


def test_findLatestReportSkipsUnparseable(tmp_path, monkeypatch):
    """损坏 JSON 不中断扫描，跳过继续找。"""
    monkeypatch.setattr(memoryPanel, "LLM_MEMORY_REPORT_DIR", str(tmp_path))
    (tmp_path / "broken.json").write_text("not json", encoding="utf-8")
    _writeReport(tmp_path, "good.json", {"split": "holdout", "modes": {}})
    report, fileName = findLatestReport("holdout")
    assert fileName == "good.json"


def test_findLatestReportResourceMatchesRssWithoutModes(tmp_path, monkeypatch):
    """资源报告判别式：rssBytes 含 maxObservedDelta 且无 modes。"""
    monkeypatch.setattr(memoryPanel, "LLM_MEMORY_REPORT_DIR", str(tmp_path))
    _writeReport(tmp_path, "encoder.json", {
        "rssBytes": {"maxObservedDelta": 100}, "queryEncodeSeconds": 1.0, "queryCount": 100,
    })
    report, fileName = findLatestReport("resource")
    assert fileName == "encoder.json"
    assert "rssBytes" in report


def test_findLatestReportResourceSkipsResearchVariants(tmp_path, monkeypatch):
    """研究报告的 rssBytes 变体（observedMaximum 系）不算资源报告。"""
    monkeypatch.setattr(memoryPanel, "LLM_MEMORY_REPORT_DIR", str(tmp_path))
    _writeReport(tmp_path, "research-resource.json", {
        "rssBytes": {"observedMaximum": 1, "processPeakWorkingSet": 2, "limit": 3},
    })
    report, fileName = findLatestReport("resource")
    assert report is None and fileName is None


def test_findLatestReportNoneWhenNoMatch(tmp_path, monkeypatch):
    """无匹配报告返回 (None, None)。"""
    monkeypatch.setattr(memoryPanel, "LLM_MEMORY_REPORT_DIR", str(tmp_path))
    _writeReport(tmp_path, "unrelated.json", {"foo": "bar"})
    report, fileName = findLatestReport("holdout")
    assert report is None and fileName is None


def test_findLatestReportDirMissing(monkeypatch):
    """reports/ 目录不存在时返回 (None, None)。"""
    monkeypatch.setattr(memoryPanel, "LLM_MEMORY_REPORT_DIR", "/nonexistent/path/xyz")
    report, fileName = findLatestReport("holdout")
    assert report is None and fileName is None


# === summarizeHoldoutReport ===


def _holdoutReport(passed: bool, modeKey: str = "hybrid", **gateFields):
    """构造一份 evaluate 报告，hybrid 模式的 qualityGate 由参数决定。"""
    gate = {"passed": passed}
    gate.update(gateFields)
    return {
        "split": "holdout",
        "modes": {modeKey: {"metrics": {"qualityGate": gate}}},
    }


def test_summarizeHoldoutReportPassed(monkeypatch):
    """hybrid qualityGate.passed=True 判 ok。"""
    report = _holdoutReport(True)
    status, detail = summarizeHoldoutReport(report, "r.json")
    assert status == STATUS_OK


def test_summarizeHoldoutReportFailed(monkeypatch):
    """hybrid qualityGate.passed=False 判 fail，detail 列未通过项。"""
    report = _holdoutReport(False, precisionPassed=False, recallPassed=False)
    status, detail = summarizeHoldoutReport(report, "r.json")
    assert status == STATUS_FAIL
    assert "precisionPassed" in detail
    assert "recallPassed" in detail


def test_summarizeHoldoutReportMissingHybridKey():
    """报告缺 hybrid 模式时降级 warn。"""
    report = {"split": "holdout", "modes": {"legacy": {"metrics": {"qualityGate": {"passed": True}}}}}
    status, detail = summarizeHoldoutReport(report, "r.json")
    assert status == STATUS_WARN
    assert "hybrid" in detail


def test_summarizeHoldoutReportMissingQualityGate():
    """qualityGate 字段缺失时降级 warn。"""
    report = {"split": "holdout", "modes": {"hybrid": {"metrics": {}}}}
    status, _ = summarizeHoldoutReport(report, "r.json")
    assert status == STATUS_WARN


# === summarizeResourceReport ===


def _resourceReport(deltaBytes: int, *, querySeconds=None, queryCount=None, p95=None):
    """构造资源报告。p95 不为 None 时构造 benchmark 风格的 concurrencyReports。"""
    report = {"rssBytes": {"maxObservedDelta": deltaBytes}}
    if querySeconds is not None:
        report["queryEncodeSeconds"] = querySeconds
    if queryCount is not None:
        report["queryCount"] = queryCount
    if p95 is not None:
        report["concurrencyReports"] = {"1": {"p95Seconds": p95}}
    return report


def test_summarizeResourceReportMemoryUnderBudgetOk():
    """内存达标、无延迟数据时判 ok。"""
    report = _resourceReport(100 * 1024 * 1024)  # 100 MiB
    status, _ = summarizeResourceReport(report, "r.json")
    assert status == STATUS_OK


def test_summarizeResourceReportMemoryOverBudgetFail():
    """内存超 256 MiB 判 fail。"""
    report = _resourceReport(300 * 1024 * 1024)  # 300 MiB
    status, detail = summarizeResourceReport(report, "r.json")
    assert status == STATUS_FAIL
    assert "内存" in detail


def test_summarizeResourceReportAverageLatencyOverBudgetFail():
    """平均单查询超 1 秒判 fail。"""
    report = _resourceReport(100 * 1024 * 1024, querySeconds=200.0, queryCount=100)
    status, detail = summarizeResourceReport(report, "r.json")
    assert status == STATUS_FAIL
    assert "延迟" in detail or "查询" in detail


def test_summarizeResourceReportBenchmarkP95PreferredOverAverage():
    """有 concurrencyReports 时优先用 P95，忽略 queryEncodeSeconds。"""
    report = _resourceReport(
        100 * 1024 * 1024,
        querySeconds=200.0, queryCount=100,  # 平均 2 秒（超标）
        p95=0.5,  # 但 benchmark P95 只 0.5 秒（达标）
    )
    status, detail = summarizeResourceReport(report, "r.json")
    assert status == STATUS_OK
    assert "P95" in detail


def test_summarizeResourceReportBenchmarkP95OverBudgetFail():
    """benchmark P95 超标判 fail。"""
    report = _resourceReport(100 * 1024 * 1024, p95=1.5)
    status, detail = summarizeResourceReport(report, "r.json")
    assert status == STATUS_FAIL


def test_summarizeResourceReportMissingRssBytes():
    """缺 rssBytes 字段降级 warn。"""
    report = {"concurrencyReports": {"1": {"p95Seconds": 0.5}}}
    status, _ = summarizeResourceReport(report, "r.json")
    assert status == STATUS_WARN


# === _buildCommand（绝对路径回归） ===


def test_buildCommandUsesAbsolutePath():
    """命令路径是绝对路径，不依赖 cwd（回归审计⚠️3）。"""
    cmd = _buildCommand("memoryModel.py", "install")
    scriptPath = cmd[-2]
    assert Path(scriptPath).is_absolute()
    assert scriptPath.endswith("memoryModel.py")


def test_buildCommandContainsSysExecutable():
    """命令首项是 sys.executable。"""
    cmd = _buildCommand("memoryModel.py", "install")
    assert cmd[0] == sys.executable


# === runAction（mock subprocess.run） ===


def test_runActionExitReturnsFalse(monkeypatch):
    """选 0 返回 False（退出循环）。"""
    assert runAction("0") is False


def test_runActionRefreshReturnsTrue(monkeypatch):
    """选 1（刷新）返回 True，且不打 subprocess。"""
    called = []
    monkeypatch.setattr(memoryPanel, "runSubcommand", lambda *a: called.append(a) or 0)
    assert runAction("1") is True
    assert called == []


def test_runActionInstallCallsMemoryModel(monkeypatch):
    """选 2 调 memoryModel.py install。"""
    captured = {}
    monkeypatch.setattr(memoryPanel, "runSubcommand", lambda script, args: captured.setdefault("call", (script, args)) or 0)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    runAction("2")
    script, args = captured["call"]
    assert script == "memoryModel.py"
    assert args == ["install"]


def test_runActionEvaluateUsesApprovedCalibration(monkeypatch):
    """选 7 的命令含 --calibration 指向正式文件（approved 才不抛错）。"""
    captured = {}
    monkeypatch.setattr(memoryPanel, "runSubcommand", lambda script, args: captured.setdefault("call", (script, args)) or 0)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    runAction("7")
    script, args = captured["call"]
    assert script == "evaluateMemory.py"
    assert "--calibration" in args
    calibrationArg = args[args.index("--calibration") + 1]
    assert Path(calibrationArg).is_absolute()
    assert calibrationArg.endswith("retrievalCalibration.json")


def test_runActionNotImplementedDoesNotRunSubprocess(monkeypatch):
    """选 9（待实现）不打 subprocess，只打印提示。"""
    called = []
    monkeypatch.setattr(memoryPanel, "runSubcommand", lambda *a: called.append(a) or 0)
    inputs = iter([""])  # 按回车返回
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    result = runAction("9")
    assert result is True
    assert called == []


def test_runActionInvalidInputReturnsTrue(monkeypatch):
    """无效输入返回 True（继续循环），不崩。"""
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert runAction("xyz") is True


def test_runActionNonZeroExitDoesNotRaise(monkeypatch):
    """子命令退出码非 0 时不抛错，提示后返回 True。"""
    monkeypatch.setattr(memoryPanel, "runSubcommand", lambda *a: 1)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert runAction("3") is True
