"""Keep resource probes isolated, budgeted, and free of dataset quality claims."""

import json
import sys
from types import SimpleNamespace

import pytest

from scripts import probeMemoryReranker as probe
from scripts import studyMemoryRetrieval as study




def _process(rss=1024, peak=2048):
    """Return deterministic memory observations without allocating model resources."""
    return SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=rss, peak_wset=peak))


def _command(events, *, exitCode=0, delay=0):
    """Run a tiny Python worker that emits protocol events without project imports."""
    source = "import json,time,sys\n"
    source += "events = " + repr(events) + "\n"
    source += "for event in events: print(json.dumps(event), flush=True)\n"
    source += "time.sleep(" + repr(delay) + ")\n"
    source += "sys.exit(" + repr(exitCode) + ")\n"
    return [sys.executable, "-X", "utf8", "-c", source]


@pytest.mark.parametrize("target", ["outside", "extension", "manifest", "temporaryManifest", "model"])
def test_outputValidationProtectsInputsAndCacheBoundary(tmp_path, monkeypatch, target):
    """Both atomic-write paths must stay in reports and outside all model inputs."""
    monkeypatch.setattr(probe, "PROJECT_ROOT", tmp_path)
    reports = tmp_path / ".cache/llmMemory/reports"
    reports.mkdir(parents=True)
    output = reports / "resource.json"
    manifest = tmp_path / "manifest.json"
    modelDir = tmp_path / "model"
    if target == "outside":
        output = tmp_path / "outside.json"
    elif target == "extension":
        output = reports / "resource.txt"
    elif target == "manifest":
        manifest = output
    elif target == "temporaryManifest":
        manifest = output.with_suffix(".json.tmp")
    else:
        modelDir = reports / "model"
        output = modelDir / "resource.json"
    manifest.write_bytes(b"preserve-input")
    with pytest.raises(ValueError):
        probe.validateOutputPath(str(output), str(manifest), str(modelDir))
    assert manifest.read_bytes() == b"preserve-input"


def test_outputValidationAcceptsSeparateJsonReport(tmp_path, monkeypatch):
    """A valid cache destination needs no preexisting directories or model files."""
    monkeypatch.setattr(probe, "PROJECT_ROOT", tmp_path)
    output = tmp_path / ".cache/llmMemory/reports/resource.json"
    assert probe.validateOutputPath(str(output), str(tmp_path / "manifest.json"), str(tmp_path / "model")) == output
    assert not output.exists()


@pytest.mark.parametrize("temporary", [False, True])
def test_cliRejectsManifestCollisionBeforeLaunchingWorker(tmp_path, monkeypatch, temporary):
    """Path rejection prevents both input replacement and subprocess launch."""
    monkeypatch.setattr(probe, "PROJECT_ROOT", tmp_path)
    reports = tmp_path / ".cache/llmMemory/reports"
    reports.mkdir(parents=True)
    output = reports / "resource.json"
    manifest = output.with_suffix(".json.tmp") if temporary else output
    manifest.write_bytes(b"preserve-manifest")
    monkeypatch.setattr(probe, "supervise", lambda *args, **kwargs: pytest.fail("launched worker for colliding output"))
    monkeypatch.setattr(sys, "argv", [
        "probeMemoryReranker.py", "--model-dir", str(tmp_path / "model"),
        "--manifest", str(manifest), "--output", str(output),
    ])
    assert probe.main() == 1
    assert manifest.read_bytes() == b"preserve-manifest"


@pytest.mark.parametrize("status", ["resource-probe-complete", "aborted", "failed"])
def test_cliWritesResourceStatusAndInputDigests(tmp_path, monkeypatch, status):
    """Only a complete resource probe exits successfully; reports bind their inputs."""
    monkeypatch.setattr(probe, "PROJECT_ROOT", tmp_path)
    script = tmp_path / "scripts/studyMemoryRetrieval.py"
    script.parent.mkdir()
    script.write_bytes(b"fake study source")
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"revision":"test-revision"}', encoding="utf-8")
    output = tmp_path / ".cache/llmMemory/reports/resource.json"
    commands = []

    def supervise(command):
        """Record the concrete worker launch command without starting a model."""
        commands.append(command)
        return {
            "status": status, "reason": None if status == "resource-probe-complete" else "test-failure",
            "rssBytes": {"limit": probe.MEMORY_LIMIT_BYTES}, "productionEligible": False, "trials": [],
        }

    monkeypatch.setattr(probe, "supervise", supervise)
    monkeypatch.setattr(sys, "argv", [
        "probeMemoryReranker.py", "--model-dir", str(tmp_path / "model"),
        "--manifest", str(manifest), "--output", str(output),
    ])
    assert probe.main() == (0 if status == "resource-probe-complete" else 1)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == status
    assert report["manifestSha256"] == probe.hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert report["studyScriptSha256"] == probe.hashlib.sha256(script.read_bytes()).hexdigest()
    assert report["trials"] == []
    assert report["productionEligible"] is False
    assert "qualityGate" not in report
    assert "thresholds" not in report
    assert not output.with_suffix(".json.tmp").exists()
    assert len(commands) == 1
    assert commands[0][:3] == [sys.executable, "-X", "utf8"]
    assert "--worker" in commands[0]
    assert "--cases" not in commands[0]




def _workerFixture(monkeypatch, *, maxLength=256):
    """Fake the reranker and process while preserving the worker's real stage flow."""
    events, calls = [], []
    ranker = SimpleNamespace(manifest={"maxTokens": 256, "revision": "fake-revision"}, pairCount=0)

    def encode(query, passage):
        """Represent the two synthetic token lengths without reading a tokenizer."""
        calls.append((query, passage))
        return SimpleNamespace(ids=[0] * (maxLength if (query, passage) == probe.LONG_PAIR else 12))

    def scorePairs(pairs):
        """Record actual batch sizes and expose finite synthetic output."""
        events.append(pairs)
        ranker.pairCount += len(pairs)
        return [0.5] * len(pairs)

    def makeReranker(modelDir, manifestPath, **kwargs):
        """Demand low-memory configuration and report one real loading boundary."""
        assert kwargs["runtimeProfile"] == "low-memory"
        kwargs["stageObserver"]("pair-session-start")
        return ranker

    ranker.tokenizer = SimpleNamespace(encode=encode)
    ranker.scorePairs = scorePairs
    ranker.close = lambda: events.append("close")
    monkeypatch.setattr(study, "StudyReranker", makeReranker)
    monkeypatch.setattr(probe.psutil, "Process", lambda *args: _process())
    monkeypatch.setattr(sys, "path", list(sys.path))
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: pytest.fail("read dotenv"))
    monkeypatch.setenv("BOT_TOKEN", "must-be-replaced-before-project-import")
    return SimpleNamespace(events=events, calls=calls)


def test_workerUsesOnlyTwoSyntheticSinglePairsAndReportsTokenBoundary(monkeypatch, capsys):
    """Completion proves short and max-token batch-one inference, without labels or scores."""
    fixture = _workerFixture(monkeypatch)
    assert probe.worker(SimpleNamespace(model_dir="fake-model", manifest="fake-manifest")) == 0
    assert fixture.events == [[probe.SHORT_PAIR], [probe.LONG_PAIR], "close"]
    assert fixture.calls == [probe.SHORT_PAIR, probe.LONG_PAIR]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["tokenCount"] for event in events if event["phase"].endswith("-inference-complete")] == [12, 256]
    assert events[-1]["phase"] == "complete"
    assert events[-1]["pairCount"] == 2
    assert events[-1]["modelRevision"] == "fake-revision"
    assert not any("scores" in event or "qualityGate" in event for event in events)
    assert probe.os.environ["BOT_TOKEN"] == "offline-fixture-test"


def test_workerRejectsTruncatedBoundaryProbeAndClosesModel(monkeypatch, capsys):
    """A long input that never reaches the manifest boundary cannot report completion."""
    fixture = _workerFixture(monkeypatch, maxLength=255)
    assert probe.worker(SimpleNamespace(model_dir="fake-model", manifest="fake-manifest")) == 1
    assert fixture.events == [[probe.SHORT_PAIR], "close"]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["phase"] == "error"
    assert events[-1]["errorType"] == "ValueError"
    assert not any(event["phase"] == "complete" for event in events)


def test_workerStopsAtInitialMemoryExcessBeforeModelLoad(monkeypatch, capsys):
    """Initial memory excess emits evidence and prevents expensive model construction."""
    _workerFixture(monkeypatch)
    monkeypatch.setattr(probe.psutil, "Process", lambda: _process(probe.MEMORY_LIMIT_BYTES + 1))
    monkeypatch.setattr(study, "StudyReranker", lambda *args, **kwargs: pytest.fail("loaded over-budget model"))
    assert probe.worker(SimpleNamespace(model_dir="fake-model", manifest="fake-manifest")) == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["phase"] == "worker-imports-start"
    assert events[0]["rss"] == probe.MEMORY_LIMIT_BYTES + 1
    assert events[-1]["errorType"] == "MemoryError"




@pytest.mark.parametrize("exitCode,complete,status", [
    (0, True, "resource-probe-complete"), (0, False, "failed"), (1, True, "failed"),
])
def test_supervisorRequiresSuccessfulExitAndCompletionEvent(monkeypatch, exitCode, complete, status):
    """Real short-lived subprocesses exercise pipe draining, exit codes, and status reporting."""
    monkeypatch.setattr(probe.psutil, "Process", lambda *args: _process())
    events = [{"phase": "pair-session-start", "rss": 100, "peak": 200}]
    if complete:
        events.append({"phase": "complete", "rss": 120, "peak": 200, "pairCount": 2})
    report = probe.supervise(_command(events, exitCode=exitCode), timeout=5)
    assert report["status"] == status
    assert report["exitCode"] == exitCode
    assert report["events"] == events
    assert report["runtimeProfile"] == "low-memory"
    assert report["trials"] == []
    assert report["productionEligible"] is False
    assert report["rssBytes"]["limit"] == 512 * 1024 * 1024
    assert report["reason"] == (None if status == "resource-probe-complete" else "worker-failed")
    assert "qualityGate" not in report
    assert "thresholds" not in report


def test_supervisorTerminatesTimedOutWorker(monkeypatch):
    """A hung synthetic worker is killed and reported as an aborted resource probe."""
    monkeypatch.setattr(probe.psutil, "Process", lambda *args: _process())
    report = probe.supervise(_command([], delay=10), timeout=0.05)
    assert report["status"] == "aborted"
    assert report["reason"] == "probe-timeout"
    assert report["exitCode"] != 0
    assert report["trials"] == []


def test_supervisorCountsParentRssAndChildHistoricalPeakBeforeContinuing(monkeypatch):
    """Parent overhead plus child historical peak can exceed the limit despite low live RSS."""
    megabyte = 1024 * 1024
    monkeypatch.setattr(probe.psutil, "Process", lambda *args: (
        _process(100 * megabyte, 400 * megabyte) if args else _process(200 * megabyte)
    ))
    report = probe.supervise(_command([], delay=10), timeout=5)
    assert report["status"] == "aborted"
    assert report["reason"] == "memory-budget-exceeded"
    assert report["exitCode"] != 0
    assert report["rssBytes"]["observedAggregateMaximum"] == 300 * megabyte
    assert report["rssBytes"]["conservativePeakSumMaximum"] == 600 * megabyte
    assert report["rssBytes"]["workerPeak"] == 400 * megabyte
    assert report["rssBytes"]["sampledWorkerPeak"] == 400 * megabyte
    assert report["trials"] == []


@pytest.mark.parametrize("rss,peak", [(511 * 1024 * 1024, 1), (1, 511 * 1024 * 1024)])
def test_supervisorIncludesLastWorkerEventAndParentInBudget(monkeypatch, rss, peak):
    """A completion event's larger RSS or peak must still be combined with parent memory."""
    monkeypatch.setattr(probe.psutil, "Process", lambda *args: _process(2 * 1024 * 1024, 1))
    event = {"phase": "complete", "rss": rss, "peak": peak, "pairCount": 2}
    report = probe.supervise(_command([event]), timeout=5)
    assert report["status"] == "aborted"
    assert report["reason"] == "memory-budget-exceeded"
    assert report["exitCode"] == 0
    assert report["rssBytes"]["workerPeak"] == 511 * 1024 * 1024
    assert report["rssBytes"]["conservativePeakSumMaximum"] == 513 * 1024 * 1024
    assert report["trials"] == []
