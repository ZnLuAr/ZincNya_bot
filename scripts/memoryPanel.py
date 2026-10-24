#!/usr/bin/env python3
"""
scripts/memoryPanel.py

Hybrid memory 上线面板：顶部呈现 Local / LLM 两条线的检测结果，
底部是动作菜单，选中后用 subprocess 调 scripts/llmMemory/ 下的子命令。
它是那组离线脚本的统一启动器——自己住在 scripts/ 顶层，被启动的住在
scripts/llmMemory/ 里。

检测层不另写判断逻辑——校准状态复用
utils.llm.memory.retrieval.loadCalibratedThresholds()，selector 配置复用
utils.llm.config.getMemorySelectorSettings()，模型校验复用
scripts.llmMemory.memoryModel.verifyModel()，全部只读。质量与资源结论
读 .cache/llmMemory/reports/ 下最近的报告（按 mtime 扫描，不认固定文件名）。

面板不替子命令解释错误，不自动批准 calibration（status: approved 永远
只能人工改），也不进 prompt_toolkit 备用屏——它是离线工具，不拉起 bot
运行时。
"""

import subprocess
import sys
from pathlib import Path


# 面板在 scripts/ 顶层，项目根是 parents[1]；子命令在 scripts/llmMemory/ 下
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts" / "llmMemory"
sys.path.insert(0, str(PROJECT_ROOT))

from config import LLM_MEMORY_REPORT_DIR, LLM_MEMORY_SELECTOR_API_KEY, LLM_MEMORY_SELECTOR_BASE_URL

from scripts.llmMemory.memoryModel import verifyModel
from utils.llm.config import getMemorySelectorSettings, loadLLMConfig
from utils.llm.memory.retrieval import loadCalibratedThresholds

from rich.console import Console
from rich.panel import Panel
from rich.table import Table


# 检测状态标记：渲染层与判定层共用，顺序即严重度
STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_NA = "na"

# 校准原因码 → 人话（loadCalibratedThresholds 的 11 种稳定英文码）
_CALIBRATION_REASON_TEXTS = {
    "calibrationRootInvalid": "校准文件不是合法对象",
    "calibrationSchemaMismatch": "校准文件 schema 版本不匹配",
    "calibrationCandidate": "候选阈值未批准（status 仍为 candidate）",
    "calibrationStatusInvalid": "从未配置（status 为 unconfigured）",
    "calibrationModelMismatch": "模型已更换，需重新校准",
    "calibrationEncodingMismatch": "编码版本已变更，需重新校准",
    "calibrationLexicalMismatch": "词面打分版本已变更，需重新校准",
    "calibrationRepresentationMismatch": "表示协议已变更，需重新校准",
    "calibrationDatasetMissing": "校准文件缺数据集散列",
    "calibrationDatasetInvalid": "数据集散列不合法",
    "calibrationThresholdsInvalid": "阈值字段不合法",
}
_CALIBRATION_REASON_FALLBACK = "校准不可用"

# 资源门槛：与 docs/llm-memory-hybrid.md「通过门槛」一致
_ENCODER_MEMORY_BUDGET_BYTES = 256 * 1024 * 1024
_ENCODER_QUERY_P95_BUDGET_SECONDS = 1.0
_BENCHMARK_P95_CONCURRENCY_KEY = "1"

# 动作菜单共享的默认参数
_DEFAULT_CASES = "tests/utils/llm/memory/fixtures/retrievalCases.json"
_PANEL_OUTPUT_DIR = Path(LLM_MEMORY_REPORT_DIR)

# 主菜单（1-10 + 0）：序号到动作的说明文本；key 用字符串以便统一 input 解析
_MAIN_MENU_ITEMS = (
    ("1", "重新检测（刷新面板）"),
    ("2", "安装模型", "memoryModel install"),
    ("3", "校验模型", "memoryModel verify"),
    ("4", "校验数据集", "evaluateMemory validate"),
    ("5", "编码器基准", "evaluateMemory encoder"),
    ("6", "生成校准候选", "evaluateMemory calibrate"),
    ("7", "评测 holdout", "evaluateMemory evaluate"),
    ("8", "检索基准", "evaluateMemory benchmark"),
    ("9", "Selector 批次", "（待实现）"),
    ("10", "研究脚本 ▸"),
    ("0", "退出"),
)

# 二级菜单：研究脚本只展示说明与示例命令，实际执行复制到 shell 跑
_RESEARCH_SCRIPTS = (
    ("studyMemoryRetrieval.py", "对照编码方式、候选覆盖与两阶段准入",
     "--cases ... --output ..."),
    ("studyMemoryAdmission.py", "用轻量联合准入模型替代独立阈值",
     "--cases ... --output ..."),
    ("studyMemoryPairPipeline.py", "分词与成对重排推理按进程串行",
     "--model-dir ... --manifest ... --output ..."),
    ("probeMemoryReranker.py", "探测重排模型的内存占用（只看资源）",
     "--model-dir ... --manifest ... --output ..."),
    ("buildMemoryCalibration.py", "把主题素材组合成大候选池 fixture",
     "--base ... --sources ... --output ..."),
)


def _makeCheck(status: str, detail: str) -> tuple[str, str]:
    """构造一条检测结果 (status, detail)。"""
    return (status, detail)


# === 检测层 ===


def checkDependencies() -> tuple[str, str]:
    """试探可选依赖 onnxruntime / tokenizers / numpy 是否可 import。"""
    missing = []
    for moduleName in ("onnxruntime", "tokenizers", "numpy"):
        try:
            __import__(moduleName)
        except ImportError:
            missing.append(moduleName)
    if not missing:
        return _makeCheck(STATUS_OK, "onnxruntime / tokenizers / numpy 可用")
    detail = "缺少: " + ", ".join(missing) + "（pip install -r requirements/memory.txt）"
    return _makeCheck(STATUS_FAIL if len(missing) == 3 else STATUS_WARN, detail)


def checkModelInstalled() -> tuple[str, str]:
    """复用 memoryModel.verifyModel() 校验固定模型产物。"""
    try:
        valid, statuses = verifyModel()
    except (OSError, RuntimeError, ValueError) as exc:
        return _makeCheck(STATUS_FAIL, f"校验失败: {exc}")
    badCount = sum(1 for status in statuses if not status["valid"])
    if valid:
        return _makeCheck(STATUS_OK, f"全部 {len(statuses)} 个产物校验通过")
    return _makeCheck(STATUS_FAIL, f"{badCount}/{len(statuses)} 个产物缺失或损坏（memoryModel install）")


def checkCalibration() -> tuple[str, str]:
    """复用 loadCalibratedThresholds()：approved 且与当前环境匹配才算 ok。"""
    thresholds, reasonCode = loadCalibratedThresholds()
    if reasonCode is None:
        thresholdText = ", ".join(
            f"{name}={value}" for name, value in thresholds.items()
        )
        return _makeCheck(STATUS_OK, f"approved（{thresholdText}）")
    text = _CALIBRATION_REASON_TEXTS.get(reasonCode, _CALIBRATION_REASON_FALLBACK)
    detail = f"{reasonCode}——{text}"
    if reasonCode == "calibrationStatusInvalid":
        return _makeCheck(STATUS_FAIL, detail)
    return _makeCheck(STATUS_WARN, detail)


def _loadReportCandidates() -> list[Path]:
    """列出 reports/ 下按 mtime 倒序的 JSON 文件；目录不存在返回空。"""
    reportDir = Path(LLM_MEMORY_REPORT_DIR)
    if not reportDir.is_dir():
        return []
    return sorted(
        (path for path in reportDir.glob("*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _readJson(path: Path):
    """读 JSON；任何 IO/解析失败都当作不可读（返回 None）。"""
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def findLatestReport(reportKind: str) -> tuple[dict | None, str | None]:
    """按 mtime 倒序扫 reports/，返回 (匹配的报告, 文件名)。

    reportKind 决定顶层字段判别式：
    - "holdout": split == "holdout" 的 evaluate 报告
    - "resource": rssBytes 含 maxObservedDelta 的 encoder / benchmark 报告
      （研究脚本的资源报告是 observedMaximum / workerPeak 等变体字段，
      不在上线判据内，用 maxObservedDelta 排除它们）
    找不到返回 (None, None)。单份文件不合法不中断扫描——跳过继续找。
    """
    for path in _loadReportCandidates():
        data = _readJson(path)
        if not isinstance(data, dict):
            continue
        if reportKind == "holdout":
            if data.get("split") == "holdout" and isinstance(data.get("modes"), dict):
                return data, path.name
        elif reportKind == "resource":
            rssBytes = data.get("rssBytes")
            if (
                isinstance(rssBytes, dict)
                and "maxObservedDelta" in rssBytes
                and "modes" not in data
            ):
                return data, path.name
    return None, None


def summarizeHoldoutReport(report: dict, fileName: str) -> tuple[str, str]:
    """读 evaluate 报告 modes.hybrid 的 qualityGate 判定质量门槛。"""
    modes = report.get("modes")
    hybridMode = modes.get("hybrid") if isinstance(modes, dict) else None
    if not isinstance(hybridMode, dict):
        return _makeCheck(STATUS_WARN, f"{fileName} 缺 hybrid 模式结果")
    metrics = hybridMode.get("metrics")
    qualityGate = metrics.get("qualityGate") if isinstance(metrics, dict) else None
    if not isinstance(qualityGate, dict):
        return _makeCheck(STATUS_WARN, f"{fileName} 缺 qualityGate 字段")
    if qualityGate.get("passed") is True:
        return _makeCheck(STATUS_OK, f"{fileName}: hybrid qualityGate 通过")
    failedItems = [name for name in (
        "minimumCaseCountPassed", "precisionPassed",
        "recallPassed", "forbiddenPassed", "pinnedPassed",
    ) if qualityGate.get(name) is False]
    return _makeCheck(
        STATUS_FAIL,
        f"{fileName}: hybrid 未达标（{', '.join(failedItems) or 'gate 不通过'}）",
    )


def checkHoldoutReport() -> tuple[str, str]:
    """扫 reports/ 找最近的 holdout evaluate 报告并判 qualityGate。"""
    report, fileName = findLatestReport("holdout")
    if report is None:
        return _makeCheck(STATUS_WARN, "无 holdout evaluate 报告（跑 evaluate 子命令）")
    return summarizeHoldoutReport(report, fileName)


def summarizeResourceReport(report: dict, fileName: str) -> tuple[str, str]:
    """判资源报告：内存看 rssBytes.maxObservedDelta，延迟看查询耗时。"""
    rssBytes = report.get("rssBytes")
    if not isinstance(rssBytes, dict):
        return _makeCheck(STATUS_WARN, f"{fileName} 缺 rssBytes 字段")
    deltaBytes = rssBytes.get("maxObservedDelta")
    if not isinstance(deltaBytes, (int, float)) or isinstance(deltaBytes, bool):
        return _makeCheck(STATUS_WARN, f"{fileName} rssBytes.maxObservedDelta 不合法")
    memoryOk = deltaBytes <= _ENCODER_MEMORY_BUDGET_BYTES

    # 延迟：benchmark 报告优先（有并发分档 P95），否则用 encoder 报告的总耗时均值
    latencyText = None
    latencyOk = None
    concurrencyReports = report.get("concurrencyReports")
    if isinstance(concurrencyReports, dict):
        single = concurrencyReports.get(_BENCHMARK_P95_CONCURRENCY_KEY)
        p95Seconds = single.get("p95Seconds") if isinstance(single, dict) else None
        if isinstance(p95Seconds, (int, float)) and not isinstance(p95Seconds, bool):
            latencyOk = p95Seconds <= _ENCODER_QUERY_P95_BUDGET_SECONDS
            latencyText = f"P95={p95Seconds:.3f}s（并发1）"
    if latencyOk is None:
        querySeconds = report.get("queryEncodeSeconds")
        queryCount = report.get("queryCount")
        if (
            isinstance(querySeconds, (int, float)) and not isinstance(querySeconds, bool)
            and isinstance(queryCount, int) and queryCount > 0
        ):
            average = querySeconds / queryCount
            latencyOk = average <= _ENCODER_QUERY_P95_BUDGET_SECONDS
            latencyText = f"平均单查询={average * 1000:.0f}ms"

    memoryText = f"{deltaBytes / 1024 / 1024:.0f} MiB"
    if memoryOk and (latencyOk is not False):
        return _makeCheck(
            STATUS_OK,
            f"{fileName}: 内存 {memoryText}{'，' + latencyText if latencyText else ''}",
        )
    parts = []
    if not memoryOk:
        parts.append(f"内存 {memoryText} 超 256 MiB 预算")
    if latencyOk is False:
        parts.append(f"{latencyText or '查询延迟'}超 1 秒门槛")
    return _makeCheck(STATUS_FAIL, f"{fileName}: " + "；".join(parts))


def checkEncoderReport() -> tuple[str, str]:
    """扫 reports/ 找最近的资源报告并判内存与延迟门槛。"""
    report, fileName = findLatestReport("resource")
    if report is None:
        return _makeCheck(STATUS_WARN, "无 encoder / benchmark 报告（跑 encoder 子命令）")
    return summarizeResourceReport(report, fileName)


def checkSelectorConfig() -> tuple[str, str]:
    """复用 getMemorySelectorSettings() 判 selector 配置合法性。"""
    try:
        settings = getMemorySelectorSettings(loadLLMConfig())
    except ValueError:
        return _makeCheck(STATUS_FAIL, "selectorConfig——llmConfig.json 字段不合法")
    return _makeCheck(
        STATUS_OK,
        f"{settings['protocol']} / {settings['model']}（timeout {settings['timeoutSeconds']:g}s）",
    )


def checkSelectorCredentials() -> tuple[str, str]:
    """判 selector 凭据是否齐全；只报告有无，不回显值。"""
    missingNames = []
    for name, value in (
        ("LLM_MEMORY_SELECTOR_BASE_URL", LLM_MEMORY_SELECTOR_BASE_URL),
        ("LLM_MEMORY_SELECTOR_API_KEY", LLM_MEMORY_SELECTOR_API_KEY),
    ):
        if not isinstance(value, str) or not value.strip():
            missingNames.append(name)
    if missingNames:
        return _makeCheck(STATUS_FAIL, "未设置 " + ", ".join(missingNames) + "（.env）")
    return _makeCheck(STATUS_OK, "BASE_URL / API_KEY 均已设置")


# === 检测汇总 ===


def collectLocalChecks() -> list[tuple[str, str, str]]:
    """收集 Local Hybrid 的 5 项检测，返回 (标签, status, detail) 列表。"""
    return [
        ("依赖", *checkDependencies()),
        ("模型", *checkModelInstalled()),
        ("校准", *checkCalibration()),
        ("质量报告", *checkHoldoutReport()),
        ("资源报告", *checkEncoderReport()),
    ]


def collectLLMChecks() -> list[tuple[str, str, str]]:
    """收集 LLM Selector 的检测项（压测脚本待实现，如实标注）。"""
    return [
        ("配置", *checkSelectorConfig()),
        ("凭据", *checkSelectorCredentials()),
        ("压测报告", STATUS_WARN, "stressTestSelector.py 待实现，无法判定"),
    ]


def _localNextStep(checks: list[tuple[str, str, str]]) -> str:
    """按检测顺序给 Local 线的第一个未达标步骤的下一步提示。"""
    for label, status, detail in checks:
        if status == STATUS_FAIL:
            if label == "依赖":
                return "pip install -r requirements/memory.txt"
            if label == "模型":
                return "跑 memoryModel install"
            if label == "校准":
                return "跑 calibrate 流程并人工批准"
            if label == "质量报告":
                return "跑 evaluate 子命令并用 approved 校准"
            if label == "资源报告":
                return "跑 encoder / benchmark 子命令"
        if status == STATUS_WARN and label in ("质量报告", "资源报告"):
            return f"补跑对应子命令（当前: {detail}）"
    return "全部通过"


def _llmNextStep(checks: list[tuple[str, str, str]]) -> str:
    """给 LLM 线的第一个未达标步骤的下一步提示。"""
    for label, status, _detail in checks:
        if status == STATUS_FAIL:
            if label == "配置":
                return "修正 llmConfig.json 的 selector 字段"
            if label == "凭据":
                return "在 .env 设置 selector 凭据"
    return "配置与凭据就绪（压测脚本待实现，见 Selector 验收章节）"


# === 渲染层 ===


_STATUS_MARKS = {
    STATUS_OK: ("[green][ OK ][/green]", "✓"),
    STATUS_FAIL: ("[red][FAIL][/red]", "✗"),
    STATUS_WARN: ("[yellow][WARN][/yellow]", "!"),
    STATUS_NA: ("[dim][ na ][/dim]", "-"),
}


def _buildCheckTable(title: str, checks: list[tuple[str, str, str]]) -> Table:
    """构造一个检测区块的 rich Table。"""
    table = Table(title=title, title_style="bold", show_header=False, box=None, padding=(0, 2))
    table.add_column("state", justify="left", min_width=8)
    table.add_column("item", justify="left")
    table.add_column("detail", justify="left", overflow="fold")
    for label, status, detail in checks:
        mark = _STATUS_MARKS.get(status, _STATUS_MARKS[STATUS_NA])[0]
        table.add_row(mark, label, detail)
    return table


def renderStatusPanel(localChecks: list, llmChecks: list, console: Console) -> None:
    """渲染顶部状态面板：Local 与 LLM 两个区块，各附结论行。"""
    localOk = all(status == STATUS_OK for _, status, _ in localChecks)
    llmOk = all(status in (STATUS_OK, STATUS_WARN) for _, status, _ in llmChecks)

    group = Table.grid(padding=(0, 1))
    group.add_row(_buildCheckTable("Local Hybrid", localChecks))
    group.add_row(f"[{'green' if localOk else 'red'}]→ "
                  f"{'达标' if localOk else '未达标'}。下一步: {_localNextStep(localChecks)}[/]")
    group.add_row(_buildCheckTable("LLM Selector", llmChecks))
    group.add_row(f"[{'green' if llmOk else 'red'}]→ "
                  f"{'达标' if llmOk else '未达标'}。下一步: {_llmNextStep(llmChecks)}[/]")
    console.print(Panel(group, title="Hybrid Memory 上线状态", title_align="left"))


def renderMainMenu(console: Console) -> None:
    """渲染主菜单。"""
    lines = ["[bold]上线流程[/bold]"]
    for item in _MAIN_MENU_ITEMS:
        key, *rest = item
        if len(rest) == 1:
            lines.append(f"  [cyan]{key:>2}.[/cyan] {rest[0]}")
        else:
            lines.append(f"  [cyan]{key:>2}.[/cyan] {rest[0]:<14} [dim]{rest[1]}[/dim]")
    console.print("\n".join(lines))


def renderResearchMenu(console: Console) -> None:
    """渲染研究脚本二级菜单（展示 + 示例命令，不代跑）。"""
    lines = ["[bold]研究脚本[/bold]（复制示例命令到 shell 执行；各脚本有 --help）"]
    for index, (scriptName, description, sampleArgs) in enumerate(_RESEARCH_SCRIPTS, start=1):
        lines.append(f"  [cyan]{index}.[/cyan] {scriptName}——{description}")
        lines.append(f"     [dim]python scripts/llmMemory/{scriptName} {sampleArgs}[/dim]")
    lines.append("  [cyan]0.[/cyan] 返回主菜单")
    console.print("\n".join(lines))


# === 动作分派 ===


def _askPath(prompt: str, default: str) -> str:
    """问一个路径参数；空输入用默认值。"""
    answer = input(f"{prompt}（默认 {default}）: ").strip()
    return answer or default


def _buildCommand(scriptName: str, *args: str) -> list[str]:
    """构造子命令的命令行：绝对路径，不依赖 cwd。"""
    scriptPath = SCRIPTS_DIR / scriptName
    return [sys.executable, str(scriptPath), *args]


def runSubcommand(scriptName: str, args: list[str]) -> int:
    """同步调子命令（继承 stdio），返回其退出码。"""
    return subprocess.run(_buildCommand(scriptName, *args)).returncode


def runAction(choice: str) -> bool:
    """执行主菜单动作。返回 True 继续面板循环（并刷新），False 退出。"""
    if choice == "1":
        return True

    if choice == "2":
        code = runSubcommand("memoryModel.py", ["install"])
    elif choice == "3":
        code = runSubcommand("memoryModel.py", ["verify"])
    elif choice == "4":
        casesPath = _askPath("fixture 路径", _DEFAULT_CASES)
        code = runSubcommand("evaluateMemory.py", ["validate", "--cases", casesPath])
    elif choice == "5":
        output = _askPath("输出路径", str(_PANEL_OUTPUT_DIR / "panel-encoder.json"))
        code = runSubcommand("evaluateMemory.py", ["encoder", "--output", output])
    elif choice == "6":
        casesPath = _askPath("fixture 路径", _DEFAULT_CASES)
        output = _askPath("输出路径", str(_PANEL_OUTPUT_DIR / "panel-calibrate.json"))
        code = runSubcommand(
            "evaluateMemory.py",
            ["calibrate", "--cases", casesPath, "--output", output],
        )
    elif choice == "7":
        print("提示: evaluate 只认 approved calibration；候选未批准时此命令会失败。")
        casesPath = _askPath("fixture 路径", _DEFAULT_CASES)
        output = _askPath("输出路径", str(_PANEL_OUTPUT_DIR / "panel-evaluate.json"))
        code = runSubcommand(
            "evaluateMemory.py",
            [
                "evaluate", "--cases", casesPath, "--output", output,
                "--calibration", str(PROJECT_ROOT / "utils/llm/memory/retrievalCalibration.json"),
            ],
        )
    elif choice == "8":
        output = _askPath("输出路径", str(_PANEL_OUTPUT_DIR / "panel-benchmark.json"))
        code = runSubcommand("evaluateMemory.py", ["benchmark", "--output", output])
    elif choice == "9":
        print("待实现: evaluateMemory.py selector / stressTestSelector.py。")
        print("规划与现状见 docs/llm-memory-hybrid.md 的「Selector 验收」章节。")
        input("按回车返回...")
        return True
    elif choice == "10":
        console = Console()
        while True:
            renderResearchMenu(console)
            answer = input("选择 (0 返回): ").strip()
            if answer == "0":
                return True
            if answer.isdigit() and 1 <= int(answer) <= len(_RESEARCH_SCRIPTS):
                scriptName = _RESEARCH_SCRIPTS[int(answer) - 1][0]
                print(f"请在 shell 中执行（各脚本有 --help）:")
                print(f"  python scripts/llmMemory/{scriptName} --help")
                input("按回车返回...")
            elif answer:
                print("无效输入")
        # while 循环只能经 "0" 退出，永不落至此行
    elif choice == "0":
        return False
    else:
        print("无效输入")
        input("按回车返回...")
        return True

    if code != 0:
        print(f"子命令失败（退出码 {code}），详见上方输出。")
    input("按回车刷新面板...")
    return True


def main() -> int:
    """面板主循环：渲染 → 选择 → 动作 → 刷新。"""
    console = Console()
    while True:
        console.clear()
        localChecks = collectLocalChecks()
        llmChecks = collectLLMChecks()
        renderStatusPanel(localChecks, llmChecks, console)
        renderMainMenu(console)
        choice = input("选择: ").strip()
        if not runAction(choice):
            return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        raise SystemExit(0)
