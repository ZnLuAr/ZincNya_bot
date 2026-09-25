"""在独立子进程探测低内存重排配置；资源通过不代表检索质量通过。

父进程只监测自己创建的 worker。512 MiB 同时约束父子采样 RSS 合计，
并以子进程历史峰值加父进程当前 RSS 保守检查；轮询不是瞬时硬隔离。
"""

import argparse
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
POLL_SECONDS = 0.02
PROBE_TIMEOUT_SECONDS = 120
SHORT_PAIR = ("用户在电脑上使用哪种输入法？", "用户在电脑上使用小鹭输入法。")
LONG_PAIR = ("这段合成文本描述什么？", "这只是用于检查内存的合成文本，不对应任何实际记忆。" * 100)




def validateOutputPath(output: str, manifest: str, modelDir: str) -> Path:
    """探针仅写本项目缓存报告目录，保护模型及 manifest 的最终/临时路径。"""
    raw = Path(output)
    paths = {raw.resolve(), raw.with_suffix(raw.suffix + ".tmp").resolve()}
    reportRoot = (PROJECT_ROOT / ".cache/llmMemory/reports").resolve()
    modelRoot = Path(modelDir).resolve()
    if raw.suffix != ".json" or len(paths) != 2:
        raise ValueError("资源报告必须使用独立的 .json 路径")
    if any(reportRoot not in path.parents or path == Path(manifest).resolve() or path == modelRoot or modelRoot in path.parents for path in paths):
        raise ValueError("资源报告只能写入缓存报告目录，且不能覆盖模型或 manifest")
    return raw


def worker(args) -> int:
    """禁用 dotenv 后加载已校验模型，记录各边界并推理两个合成 pair。"""
    import dotenv

    dotenv.load_dotenv = lambda *a, **k: False
    os.environ["BOT_TOKEN"] = "offline-fixture-test"
    sys.path.insert(0, str(PROJECT_ROOT))
    process = psutil.Process()
    samples = []
    reranker = None

    def stage(name: str, **extra):
        """先输出观测证据再检查预算，父进程仍能看到失败边界。"""
        info = process.memory_info()
        event = {"phase": name, "rss": info.rss, "peak": getattr(info, "peak_wset", None), **extra}
        print(json.dumps(event, ensure_ascii=False), flush=True)
        samples.append(event)
        if max(info.rss, event["peak"] or 0) > MEMORY_LIMIT_BYTES:
            raise MemoryError("worker-memory-budget-exceeded")

    try:
        stage("worker-imports-start")
        from scripts.studyMemoryRetrieval import StudyReranker

        stage("worker-imports-complete")
        reranker = StudyReranker(args.model_dir, args.manifest, runtimeProfile="low-memory", stageObserver=stage)
        for name, pair in (("short", SHORT_PAIR), ("max-length", LONG_PAIR)):
            stage(name + "-encoding-start")
            tokens = reranker.tokenizer.encode(*pair)
            tokenCount = len(tokens.ids)
            if name == "max-length" and tokenCount != reranker.manifest["maxTokens"]:
                raise ValueError("合成边界输入未达到清单 token 上限")
            stage(name + "-inference-start", tokenCount=tokenCount)
            scores = reranker.scorePairs([pair])
            stage(name + "-inference-complete", tokenCount=tokenCount, outputCount=len(scores))
        stage("complete", pairCount=reranker.pairCount, modelRevision=reranker.manifest["revision"])
        return 0
    except Exception as exc:
        print(json.dumps({"phase": "error", "errorType": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), flush=True)
        return 1
    finally:
        if reranker is not None:
            reranker.close()


def supervise(command: list[str], *, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict:
    """监测唯一 worker 的进程句柄，超限终止并保留最后阶段而非质量成绩。"""
    started = time.monotonic()
    parent = psutil.Process()
    child = subprocess.Popen(command, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    observations = queue.Queue()

    def readEvents():
        """持续排空管道，避免 native 警告填满缓冲而使 worker 阻塞。"""
        for line in child.stdout:
            try:
                value = json.loads(line)
            except ValueError:
                value = {"phase": "worker-output", "message": line.strip()[:500]}
            if not isinstance(value, dict):
                value = {"phase": "worker-output", "message": line.strip()[:500]}
            observations.put(value)

    reader = threading.Thread(target=readEvents, daemon=True)
    reader.start()
    events = []
    samples = []
    reason = None
    try:
        try:
            childProcess = psutil.Process(child.pid)
        except psutil.NoSuchProcess:
            childProcess = None
        while child.poll() is None:
            try:
                if childProcess is None:
                    childProcess = psutil.Process(child.pid)
                info = childProcess.memory_info()
                parentRSS = parent.memory_info().rss
                sample = {"childRSS": info.rss, "childPeak": getattr(info, "peak_wset", None), "parentRSS": parentRSS}
                sample["aggregateRSS"] = info.rss + parentRSS
                sample["conservativePeakSum"] = max(info.rss, sample["childPeak"] or 0) + parentRSS
                samples.append(sample)
                if sample["conservativePeakSum"] > MEMORY_LIMIT_BYTES:
                    reason = "memory-budget-exceeded"
                    child.kill()
                    break
            except psutil.NoSuchProcess:
                # 进程可能恰好已退出；仍以 Popen 句柄的终态和退出码为准。
                pass
            while not observations.empty():
                events.append(observations.get_nowait())
            if time.monotonic() - started > timeout:
                reason = "probe-timeout"
                child.kill()
                break
            time.sleep(POLL_SECONDS)
        exitCode = child.wait(timeout=10)
        reader.join(timeout=3)
        while not observations.empty():
            events.append(observations.get_nowait())
    finally:
        # 异常退出时也只清理本次创建的子进程，不枚举/终止其他 Python。
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        reader.join(timeout=3)
        child.stdout.close()
    workerEventPeak = max((max(event.get("peak") or 0, event.get("rss") or 0) for event in events), default=0)
    sampledWorkerPeak = max((max(sample["childRSS"], sample["childPeak"] or 0) for sample in samples), default=0)
    workerPeak = max(workerEventPeak, sampledWorkerPeak)
    # worker 最后一次边界采样可能晚于父进程最后一次轮询；合并它，避免
    # 快速退出时漏掉已明确记录的峰值。父进程当前 RSS 与历史峰值分开说明。
    conservativePeak = max([workerPeak + parent.memory_info().rss, *(sample["conservativePeakSum"] for sample in samples)])
    if conservativePeak > MEMORY_LIMIT_BYTES:
        reason = "memory-budget-exceeded"
    complete = exitCode == 0 and any(event.get("phase") == "complete" for event in events)
    return {
        "schemaVersion": 1, "reportType": "reranker-resource-probe", "runtimeProfile": "low-memory",
        "status": "resource-probe-complete" if complete and reason is None else "aborted" if reason else "failed",
        "reason": reason or (None if complete else "worker-failed"),
        "productionEligible": False, "exitCode": exitCode, "seconds": time.monotonic() - started,
        "events": events, "rssBytes": {
            "workerPeak": workerPeak,
            "workerEventPeak": workerEventPeak,
            "sampledWorkerPeak": sampledWorkerPeak,
            "observedAggregateMaximum": max((sample["aggregateRSS"] for sample in samples), default=None),
            "conservativePeakSumMaximum": conservativePeak,
            "limit": MEMORY_LIMIT_BYTES,
        },
        "trials": [],
        "note": "仅合成短输入与 token 上限、batch=1 的资源探测；没有数据集质量成绩。父子采样并非操作系统硬限额，也不代表 embedding 与重排模型共同常驻验收。",
    }


def main() -> int:
    """父进程写入带模型摘要的资源记录，worker 模式不读取数据集。"""
    parser = argparse.ArgumentParser(description="低内存重排模型的受限资源探测")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args)
    try:
        output = validateOutputPath(args.output, args.manifest, args.model_dir)
        manifestHash = hashlib.sha256(Path(args.manifest).read_bytes()).hexdigest()
        command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--worker",
                   "--model-dir", str(Path(args.model_dir).resolve()), "--manifest", str(Path(args.manifest).resolve()),
                   "--output", str(output.resolve())]
        report = supervise(command)
        report["manifestSha256"] = manifestHash
        report["studyScriptSha256"] = hashlib.sha256((PROJECT_ROOT / "scripts/studyMemoryRetrieval.py").read_bytes()).hexdigest()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)
        print(json.dumps({"output": str(output), "status": report["status"], "reason": report["reason"], "rssBytes": report["rssBytes"]}, ensure_ascii=False))
        return 0 if report["status"] == "resource-probe-complete" else 1
    except (OSError, ValueError, RuntimeError, psutil.Error) as exc:
        print(f"资源探测失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
