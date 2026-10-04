"""按进程串行分词和成对推理，仅在固定 calibration 上研究准入。

候选直接复用已绑定数据与模型的 union-32 报告；不会读 holdout 答案选参。
分词进程完全退出后才启动无 tokenizer 的推理进程，资源结果不代表常驻部署。
"""

import argparse
import gc
import hashlib
import json
import math
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.llmMemory import probeMemoryReranker as probe


STUDY_TIMEOUT_SECONDS = 1800
LIMIT_PER_CHANNEL = 32




def loadStudy():
    """研究导入前禁用 dotenv，避免读取生产配置中的凭据。"""
    import dotenv

    dotenv.load_dotenv = lambda *a, **k: False
    os.environ["BOT_TOKEN"] = "offline-fixture-test"
    from scripts.llmMemory import studyMemoryRetrieval as study

    return study


def fileDigest(path) -> str:
    """流式绑定交接文件，避免为摘要额外复制完整输入。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checkParentBudget() -> None:
    """父进程准备和回放也检查历史峰值，避免只约束两个模型 worker。"""
    info = probe.psutil.Process().memory_info()
    if max(info.rss, getattr(info, "peak_wset", 0)) > probe.MEMORY_LIMIT_BYTES:
        raise MemoryError("父进程准备或回放超过 512 MiB")


def writeJSON(path: Path, value) -> None:
    """仅对已验证的报告或本次专属缓存进行原子写入。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def validatePaths(args) -> Path:
    """在启动任何 worker 前保护输入、模型及最终和临时输出路径。"""
    output = probe.validateOutputPath(args.output, args.manifest, args.model_dir)
    writes = {output.resolve(), output.with_suffix(output.suffix + ".tmp").resolve()}
    inputs = [args.manifest, args.dense_manifest, args.cases, args.candidate_report]
    if writes & {Path(item).resolve() for item in inputs if item}:
        raise ValueError("串行报告不能覆盖输入或临时输入路径")
    if not args.probe_only and (not args.cases or not args.candidate_report):
        raise ValueError("完整实验需要 calibration fixture 和候选报告")
    if args.probe_only and (args.cases or args.candidate_report):
        raise ValueError("合成资源探测不接受数据集")
    return output


def readCandidateSelection(cases: list[dict], reportPath: str, manifestPath: str) -> tuple[dict, dict]:
    """仅复用精确数据版本的唯一 union-32 缓存，不按本次标签重新缩池。"""
    study = loadStudy()
    report = json.loads(Path(reportPath).read_text(encoding="utf-8"))
    manifest = json.loads(Path(manifestPath).read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not isinstance(manifest, dict):
        raise ValueError("候选报告和模型清单必须是对象")
    expected = {
        "reportType": "retrieval-improvement-study", "status": "experimental", "split": "calibration",
        "calibrationSha256": study.evaluation._casesDigest(cases),
        "modelManifestSha256": fileDigest(manifestPath), "modelRevision": manifest["revision"],
        "baselineEncodingVersion": manifest["encodingVersion"],
    }
    if any(report.get(key) != value for key, value in expected.items()):
        raise ValueError("候选缓存的数据、模型或报告类型不匹配")
    candidateStudy = report.get("candidateStudy")
    if (not isinstance(candidateStudy, dict) or not isinstance(candidateStudy.get("trials"), list)
            or any(not isinstance(trial, dict) for trial in candidateStudy["trials"])):
        raise ValueError("候选缓存缺少有效对照列表")
    trials = [trial for trial in candidateStudy["trials"]
              if trial.get("strategy") == "union" and trial.get("limitPerChannel") == LIMIT_PER_CHANNEL]
    if len(trials) != 1:
        raise ValueError("候选缓存必须恰有一个 union-32 对照")
    selected = trials[0]["candidateIDsByCase"]
    if not isinstance(selected, dict) or set(selected) != {case["caseID"] for case in cases}:
        raise ValueError("候选缓存场景集合不一致")
    for case in cases:
        values = selected[case["caseID"]]
        if (not isinstance(values, list) or any(type(value) is not int for value in values)
                or len(set(values)) != len(values) or len(values) > 3 * LIMIT_PER_CHANNEL):
            raise ValueError("候选缓存 ID 或容量无效")
        eligible = {int(memory["id"]) for memory in study.evaluation._contextualCandidates(study.evaluation._scopeCandidates(case))}
        if set(values) - eligible:
            raise ValueError("候选缓存包含不可检索的记忆")
    provenance = {**expected, "candidateReportSha256": fileDigest(reportPath),
                  "candidateGeneratorScriptSha256": report.get("studyScriptSha256"),
                  "note": "复用历史候选缓存；该报告未记录生成脚本摘要时保持 null，未重新执行 embedding。"}
    return {caseID: set(values) for caseID, values in selected.items()}, provenance


def preparePairs(cases: list[dict], selected: dict) -> tuple[list[dict], dict]:
    """按原格式生成不含标签/hint 的文本 pair，并保留多通道逻辑映射。"""
    study = loadStudy()
    pairs = {}
    mapping = {}
    for case in cases:
        caseID = case["caseID"]
        current, assisted, _ = study.buildQueryTexts(case["query"], now=case.get("queryNow"))
        candidates = study.evaluation._contextualCandidates(study.evaluation._scopeCandidates(case))
        byID = {int(memory["id"]): memory for memory in candidates}
        if selected[caseID] - byID.keys():
            raise ValueError("pair 计划包含不可检索的记忆")
        channels = {}
        for channel, query in (("semanticCurrent", current), ("semanticAssisted", assisted)):
            channels[channel] = {}
            if not query:
                continue
            for memoryID in sorted(selected[caseID]):
                # 此格式化函数不访问实例状态；不为纯文本格式化创建模型。
                memory = study.MemoryEncoder._formatMemoryText(None, byID[memoryID], includeHint=False)
                pairID = hashlib.sha256(json.dumps([query, memory], ensure_ascii=False).encode("utf-8")).hexdigest()
                pair = {"pairID": pairID, "query": query, "memory": memory}
                if pairID in pairs and pairs[pairID] != pair:
                    raise ValueError("文本 pair 摘要碰撞")
                pairs[pairID] = pair
                channels[channel][memoryID] = pairID
        mapping[caseID] = {"sameQuery": current == assisted, "channels": channels}
    return list(pairs.values()), mapping


def readScores(path: Path, expected: set[str]) -> dict:
    """用稳定 pairID 验证完整结果，拒绝缺失、重复、额外或非有限分数。"""
    scores = {}
    with path.open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            pairID, score = row["pairID"], row["score"]
            if (pairID not in expected or pairID in scores or type(score) not in (int, float)
                    or not math.isfinite(score) or not 0 <= score <= 1):
                raise ValueError("成对评分结果无效或重复")
            scores[pairID] = score
    if set(scores) != expected:
        raise ValueError("成对评分结果缺失")
    return scores


def restoreRecords(mapping: dict, scores: dict) -> dict:
    """恢复原两语义通道，词面不独立放行，后续由原回放处理相同查询。"""
    records = {}
    for caseID, item in mapping.items():
        semantic = {channel: {memoryID: scores[pairID] for memoryID, pairID in values.items()}
                    for channel, values in item["channels"].items()}
        records[caseID] = {"sameQuery": item["sameQuery"], "scores": {**semantic, "lexical": {}},
                           "ranking": {channel: dict(values) for channel, values in semantic.items()}}
    return records




def worker(args) -> int:
    """仅接收文本或整数张量交接，分词和 ONNX 从不在同一 worker 常驻。"""
    study = loadStudy()
    process = probe.psutil.Process()
    model = None
    temporary = Path(args.worker_output).with_suffix(".jsonl.tmp")
    started = time.monotonic()

    def stage(name: str, **extra):
        """边界证据先输出，再检查进程预算；父进程持续检查父子合计。"""
        info = process.memory_info()
        print(json.dumps({"phase": name, "rss": info.rss, "peak": getattr(info, "peak_wset", None), **extra}), flush=True)
        study.checkPairStudyMemory(process, [])

    try:
        if fileDigest(args.manifest) != args.manifest_sha or fileDigest(args.worker_input) != args.input_sha:
            raise ValueError("worker 输入摘要变化")
        stage(args.worker + "-start")
        if args.worker == "tokenize":
            manifest = study.readRerankerManifest(args.model_dir, args.manifest)
            tokenizer = study.loadRerankerTokenizer(args.model_dir, manifest)
            stage("tokenizer-ready", tokenizersVersion=getattr(sys.modules.get("tokenizers"), "__version__", None))
        else:
            model = study.StudyReranker(args.model_dir, args.manifest, runtimeProfile="low-memory",
                                        loadTokenizer=False, stageObserver=stage)
            manifest = model.manifest
            stage("inference-ready", tokenizerLoaded=model.tokenizer is not None,
                  tokenizersImported="tokenizers" in sys.modules,
                  onnxruntimeVersion=getattr(sys.modules.get("onnxruntime"), "__version__", None))
        seen = set()
        maximum = 0
        with Path(args.worker_input).open(encoding="utf-8") as source, temporary.open("w", encoding="utf-8") as target:
            for line in source:
                row = json.loads(line)
                pairID = row["pairID"]
                if not isinstance(pairID, str) or pairID in seen:
                    raise ValueError("交接 pairID 重复或无效")
                seen.add(pairID)
                if args.worker == "tokenize":
                    # 单对编码保留原 batch=1 的动态 padding；绝不按整个数据集补齐。
                    fields = study.encodeRerankerPairs(tokenizer, [(row["query"], row["memory"])])[0]
                    maximum = max(maximum, len(fields["input_ids"]))
                    result = {"pairID": pairID, "fields": fields}
                else:
                    fields = row["fields"]
                    maximum = max(maximum, len(fields["input_ids"]))
                    result = {"pairID": pairID, "score": model.scoreEncodedPairs([fields])[0]}
                target.write(json.dumps(result, ensure_ascii=False) + "\n")
                if len(seen) % 100 == 0:
                    target.flush()
                    stage(args.worker + "-progress", pairCount=len(seen))
        if args.probe_only and (len(seen) != 2 or maximum != manifest["maxTokens"]):
            raise ValueError("合成探测必须覆盖短输入及完整 token 上限")
        temporary.replace(args.worker_output)
        stage("complete", pairCount=len(seen), maximumTokens=maximum,
              seconds=time.monotonic() - started, revision=manifest["revision"],
              singlePairInferenceSecondsP95=study.evaluation._percentile(model.timings, 95) if model is not None else None)
        return 0
    except Exception as exc:
        print(json.dumps({"phase": "error", "errorType": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), flush=True)
        return 1
    finally:
        if model is not None:
            model.close()




def main() -> int:
    """先合成探测，再由调用者显式发起完整 calibration；每次报告绑定交接。"""
    parser = argparse.ArgumentParser(description="串行分词/推理的 calibration 成对研究")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--cases")
    parser.add_argument("--candidate-report")
    parser.add_argument("--dense-manifest", default=str(PROJECT_ROOT / "utils/llm/memory/modelManifest.json"))
    parser.add_argument("--worker", choices=("tokenize", "infer"), help=argparse.SUPPRESS)
    parser.add_argument("--worker-input", help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", help=argparse.SUPPRESS)
    parser.add_argument("--input-sha", help=argparse.SUPPRESS)
    parser.add_argument("--manifest-sha", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args)
    output = report = None
    try:
        output = validatePaths(args)
        report = {"schemaVersion": 1, "reportType": "serial-pair-resource-probe" if args.probe_only else "serial-pair-calibration-study",
                  "status": "running", "productionEligible": False, "trials": [],
                  "runtimeProfile": "low-memory-serial-tokenization", "modelManifestSha256": fileDigest(args.manifest),
                  "scriptSha256": fileDigest(__file__),
                  "studyScriptSha256": fileDigest(PROJECT_ROOT / "scripts/llmMemory/studyMemoryRetrieval.py"),
                  "supervisorScriptSha256": fileDigest(PROJECT_ROOT / "scripts/llmMemory/probeMemoryReranker.py"),
                  "pythonVersion": platform.python_version(), "stages": [],
                  "note": "仅串行离线流程；父子采样不是操作系统硬限额，不证明模型同时常驻或上线质量。"}
        if args.probe_only:
            pairs = [{"pairID": name, "query": pair[0], "memory": pair[1]}
                     for name, pair in (("short", probe.SHORT_PAIR), ("max-length", probe.LONG_PAIR))]
            mapping = {}
        else:
            study = loadStudy()
            cases = study.loadCalibrationCases(args.cases)
            selected, provenance = readCandidateSelection(cases, args.candidate_report, args.dense_manifest)
            pairs, mapping = preparePairs(cases, selected)
            report.update({"split": "calibration", "calibrationSha256": study.evaluation._casesDigest(cases),
                           "candidateSource": provenance, "candidateStrategy": "union", "limitPerChannel": LIMIT_PER_CHANNEL})
        output.parent.mkdir(parents=True, exist_ok=True)
        # 专属新目录使两个 worker 的交接不会覆盖输入；保留文件供摘要复核。
        taskDir = Path(tempfile.mkdtemp(prefix="pair-pipeline-", dir=output.parent))
        textsPath, encodedPath, scoresPath = (taskDir / name for name in ("texts.jsonl", "encoded.jsonl", "scores.jsonl"))
        with textsPath.open("w", encoding="utf-8") as target:
            for pair in pairs:
                target.write(json.dumps(pair, ensure_ascii=False) + "\n")
        expected = {pair["pairID"] for pair in pairs}
        report["uniquePairCount"] = len(expected)
        report["logicalChannelPairCount"] = sum(len(values) for item in mapping.values() for values in item["channels"].values()) if mapping else len(expected)
        report["handoffs"] = {"directory": str(taskDir), "textsSha256": fileDigest(textsPath)}
        del pairs
        gc.collect()
        checkParentBudget()
        for name, source, target in (("tokenize", textsPath, encodedPath), ("infer", encodedPath, scoresPath)):
            command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--worker", name,
                       "--model-dir", str(Path(args.model_dir).resolve()), "--manifest", str(Path(args.manifest).resolve()),
                       "--output", str(output.resolve()), "--worker-input", str(source.resolve()),
                       "--worker-output", str(target.resolve()), "--input-sha", fileDigest(source),
                       "--manifest-sha", report["modelManifestSha256"]]
            if args.probe_only:
                command.append("--probe-only")
            print(json.dumps({"stage": name, "status": "starting", "uniquePairCount": len(expected)}), flush=True)
            result = probe.supervise(command, timeout=probe.PROBE_TIMEOUT_SECONDS if args.probe_only else STUDY_TIMEOUT_SECONDS)
            result["note"] = "本项只记录一个独立 worker 的资源与完成状态；质量指标仅见顶层 trials。轮询预算包含父进程，非操作系统硬限额。"
            report["stages"].append({"stage": name, **result})
            if result["status"] != "resource-probe-complete":
                report.update({"status": result["status"], "reason": result["reason"]})
                writeJSON(output, report)
                return 1
            report["handoffs"][name + "OutputSha256"] = fileDigest(target)
            # supervise 已 wait 子进程终态；只有前阶段完全成功才创建下一进程。
        scores = readScores(scoresPath, expected)
        report["scoredPairCount"] = len(scores)
        if args.probe_only:
            report["probeInputs"] = [{"pairID": row["pairID"], "tokenCount": len(row["fields"]["input_ids"])}
                                     for row in (json.loads(line) for line in encodedPath.read_text(encoding="utf-8").splitlines())]
            report["status"] = "resource-probe-complete"
        else:
            checkParentBudget()
            records = restoreRecords(mapping, scores)
            coverage = study.candidateCoverageMetrics(cases, selected)
            coverage["cohorts"] = {name: study.candidateCoverageMetrics([case for case in cases if ("expandedCalibration" in case["subsets"]) == expanded], selected)
                                   for name, expanded in (("oldCalibration", False), ("expandedCalibration", True))}
            report["candidateCoverage"] = coverage
            report["trials"] = [{"encoding": "baseline", **study.studyScores(cases, records, None)}]
            report["pairRecords"] = records
            report["status"] = "experimental"
        report["rssBytes"] = {"conservativePeakSumMaximum": max(stage["rssBytes"]["conservativePeakSumMaximum"] for stage in report["stages"]),
                              "limit": probe.MEMORY_LIMIT_BYTES}
        checkParentBudget()
        writeJSON(output, report)
        print(json.dumps({"output": str(output), "status": report["status"], "rssBytes": report["rssBytes"]}), flush=True)
        return 0
    except Exception as exc:
        # CLI 统一记录自定义评估异常，避免失败路径留下半套质量成绩。
        if output is not None and report is not None:
            report.update({"status": "aborted" if isinstance(exc, MemoryError) else "failed",
                           "reason": "parent-memory-budget-exceeded" if isinstance(exc, MemoryError) else "invalid-or-failed-handoff",
                           "errorType": type(exc).__name__, "error": str(exc), "trials": []})
            output.parent.mkdir(parents=True, exist_ok=True)
            writeJSON(output, report)
        print(f"串行成对实验失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
