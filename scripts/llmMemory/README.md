# scripts/llmMemory

LLM Memory 语义检索的离线工具：模型安装、评测，以及一组研究脚本。它们都不是 bot 运行时依赖，也不读生产数据库。

这组脚本有一个统一启动器——上线面板（住在上一级 `scripts/memoryPanel.py`，面板自己是入口、这些脚本是它拉起的子命令）：

```bash
python scripts/memoryPanel.py
```

面板顶部呈现 Local Hybrid / LLM Selector 两条线的上线检测结果（依赖、模型、校准、质量报告、资源报告、selector 配置与凭据），底部动作菜单可直接拉起 `memoryModel` / `evaluateMemory` 的子命令，跑完刷新即看新结果。检测逻辑复用线上的 `loadCalibratedThresholds()`、`getMemorySelectorSettings()` 与 `memoryModel.py` 的 `verifyModel()`，不另写一套判断；面板全程只读，不碰 `retrievalCalibration.json`（`status: approved` 永远只能人工改）。质量与资源结论来自 `.cache/llmMemory/reports/` 下最近一次报告（按 mtime 扫描，无论报告是从面板还是命令行产出都能识别）。研究脚本在面板的二级菜单里有说明和示例命令。

依赖与语义检索相同，需先安装 `requirements/memory.txt`。系统说明见 [LLM Memory 文档](../../docs/llm-memory.md)。

## 常用工具

| 脚本 | 用途 |
|------|------|
| `memoryModel.py` | 安装或校验固定版本的本地语义模型 |
| `evaluateMemory.py` | 用标注 fixture 校准阈值、评测检索质量、跑资源基准 |

### memoryModel.py

模型身份（仓库、commit SHA、逐文件 SHA-256）以 `utils/llm/memory/modelManifest.json` 为准，安装到 `.cache/llmMemory/model`。安装先在临时目录完整下载并校验，再整体替换；任一步失败都会保留原有安装。

```bash
python scripts/llmMemory/memoryModel.py verify
python scripts/llmMemory/memoryModel.py install
python scripts/llmMemory/memoryModel.py install --endpoint https://hf-mirror.com
```

### evaluateMemory.py

输入是人工标注的脱敏 fixture：每个场景写明哪些记忆必须召回、哪些可以召回、哪些禁止召回。评测复用线上的查询构造、评分和预算代码。

| 子命令 | 作用 |
|--------|------|
| `validate` | 只校验 fixture，不加载模型 |
| `encoder` | 编码器资源基准 |
| `calibrate` | 只用 calibration split 生成待审查的阈值候选 |
| `evaluate` | 用固定 calibration 评估 holdout |
| `margin` | 在 calibration 上研究「绝对阈值 + top-margin」，不产出上线配置 |
| `benchmark` | 热查询与并发基准 |

```bash
python scripts/llmMemory/evaluateMemory.py validate --cases tests/utils/llm/memory/fixtures/retrievalCases.json
python scripts/llmMemory/evaluateMemory.py calibrate --cases tests/utils/llm/memory/fixtures/retrievalCases.json --output .cache/llmMemory/reports/candidateCalibration.json
```

`calibrate` 产出的只是候选文件。它要经人工审查，才能替换 `utils/llm/memory/retrievalCalibration.json`；在此之前，线上阈值保持 `unconfigured`。

## 研究脚本

以下脚本服务于检索方案研究，只在 calibration split 上运行，不产出可上线的配置。

| 脚本 | 研究内容 |
|------|---------|
| `buildMemoryCalibration.py` | 把主题素材组合成大候选池 fixture，扩充 calibration；生成结果需人工复核 |
| `studyMemoryRetrieval.py` | 对照编码方式、候选覆盖与两阶段准入，含分组交叉验证 |
| `studyMemoryAdmission.py` | 用轻量联合准入模型替代三通道独立阈值 |
| `studyMemoryPairPipeline.py` | 分词与成对重排推理按进程串行执行，研究成对重排准入 |
| `probeMemoryReranker.py` | 在独立子进程里探测重排模型的内存占用；只看资源，不评质量 |

研究脚本都有 `--help`。它们之间有依赖：`studyMemoryRetrieval` 和 `buildMemoryCalibration` 依赖 `evaluateMemory`；`studyMemoryAdmission` 依赖 `studyMemoryRetrieval`；`studyMemoryPairPipeline` 依赖 `studyMemoryRetrieval` 和 `probeMemoryReranker`。

## 输出与测试

报告默认写入 `.cache/llmMemory/reports/`（已被 git 忽略）。对应测试在 `tests/scripts/` 下，与脚本同名：

```bash
pytest tests/scripts/test_evaluateMemory.py tests/scripts/test_memoryModel.py
```
