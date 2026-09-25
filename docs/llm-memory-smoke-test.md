# LLM Memory 冒烟测试方案

> 最后更新：2026-09-25
>
> 这份文档是 Structured Memory 重构后的验收 runbook：把自动化回归、离线检索评测、目标机运行时冒烟和 Telegram 人工验收分开，说明每一层能证明什么、不能证明什么，以及什么时候允许进入 `hybrid` 灰度。它面向开发者、部署管理员和负责人工验收的 operator；完整架构仍以 [LLM Structured Memory 设计与运维文档](llm-memory.md) 为准。
>
> ……不妨问问神奇的魔法 AI 吧？（目移
>
> Written by ZincNya~ ❤

---

## 概述

Memory 是在线变化的数据。数据库写入成功、向量索引追上、检索结果进入 prompt、主模型生成回复，这几件事之间存在明确的异步边界，不能用一次离线 benchmark 代替全部验收。

本方案因此分成四层：

| 层级 | 主要工具 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| 自动化回归 | pytest、`scripts/module.py` | 接口契约、边界、竞态、降级和不增加生成调用 | 目标机真实 RSS、模型实际质量、Telegram 网络链路 |
| 本地离线评测 | `scripts/evaluateMemory.py`、脱敏 fixture | 查询构造、BM25、语义分数、RRF、字符预算和标注集质量 | 生产数据库、runtime 队列、事件循环和完整生命周期 |
| 目标机部署冒烟 | `python bot.py`、`/llm memory status`、运行时观测 | 固定模型在目标 Python/CPU 上能启动、索引能追赶、资源和关闭行为可接受 | 人工回复是否真正符合产品预期 |
| Telegram 人工验收 | staging Bot 与测试 chat | 回指、话题切换、写入审核、prompt 质量和 operator 操作闭环 | 大规模统计质量；人工场景不能替代 holdout |

所有层级都必须记录测试日期、代码版本、Python 版本、模型 revision、fixture hash、结果和 operator。没有通过的层级保持 `legacy`，不能用扩大 prompt 或手工猜阈值掩盖失败。

## 目录

- [Messages交付候选的验收范围](#messages交付候选的验收范围)
- [测试边界](#测试边界)
- [通过门槛](#通过门槛)
- [一自动化回归](#一自动化回归)
- [二离线评测与校准](#二离线评测与校准)
- [三目标机运行时冒烟](#三目标机运行时冒烟)
- [四 Telegram 人工验收](#四-telegram-人工验收)
- [故障注入矩阵](#故障注入矩阵)
- [观测与记录](#观测与记录)
- [回滚与停止条件](#回滚与停止条件)
- [维护原则](#维护原则)

---

## Messages交付候选的验收范围

咱当前验收的是已获批、默认关闭的LLM选择后端：本地BGE/BM25宽池交一次 `claude-opus-4-8` / Messages选ID，再由程序按原文、预算和数据库状态复核，主回答另外生成。下面旧章节的calibration/holdout gate、总等待2秒、256MiB增量和“不能增加生成调用”描述的是local后端；不能拿它们要求本路线重新拟合阈值、批准旧holdout或删去已获准的单次selector调用。

| 项目 | 本轮验收口径 |
| --- | --- |
| 质量 | 同时报告P≥0.85与P≥0.90、required R≥0.65；背景计P分母，多R/无答案单列 |
| 竞争事实 | 按错误内容、有效竞争机会与主回答后果逐项裁决；本批三例受委托审阅已完成，结论限于这些样本，不是普遍F许可 |
| 时间 | 选择阶段最多30秒，组装和清理占同一预算；本地候选和最终复核另计，主回答另计 |
| 内存 | 本轮研究进程先限制512MiB；同时记录原生峰值，不自动转成生产容量承诺 |
| 调用 | 单次selector，无重试、竞速或生成式修复；仍有独立主回答。离线mock零网络 |
| 失败 | 仅保留通过复核且预算内的pinned，无本地情境回退；慢清理占原lease直到实际结束 |
| 开关 | 默认 `legacy/local/responses`；生产启用另行批准，正式calibration不为LLM后端伪造approved |

[固定32题结果及错误清单](archive/llm-memory-hybrid-research-2026-09.md#2026-09-22messages固定最终验收)已经完成，P34/39、R32/32、零超时；3个竞争项的[六次主回答检查](archive/llm-memory-hybrid-research-2026-09.md#2026-09-25三例冲突的六次主回答裁决)也已完成，两组各 3/3 正确。错误标签与原检索分数保留。旧holdout已经被观察，不能复用成新盲测，也不重复发送固定最终批次。所有新实验读写都使用 `tmp/` 中的合成材料、模型及报告，下面历史 `.cache/` 命令不是本轮执行路径。

### 离线目标机副本

目标为用户指定的Linux虚拟机，代码与依赖放在该账户独立的 `tmp/` 副本中。打包入口 `tmp/buildMessagesDeploymentBundle.py` 只包含已验证源码、schema、合成作者素材及manifest绑定的BGE文件；不包含生产data、`.env`、数据库、API凭据、SSH私钥或旧holdout。先核对 `tmp/messagesDeployment-bundle-manifest-v1.json` 的逐文件SHA256，再运行。环境安装使用该副本 `tmp/` 内的venv、pip缓存和系统临时目录，不安装到系统Python。

以下是目标机包装器的命令形式；用户带回的执行记录及实际解释器路径见 [Linux 验证报告](../tmp/linux311-final-validation.md)。复现时使用新输出目录，保留已完成报告，不覆盖原证据：

```bash
python3.11 -B -X utf8 tmp/runMessagesDeploymentTests.py --output tmp/messagesDeployment-linux311-v1
python3.11 -B -X utf8 tmp/runMessagesDeploymentRuntime.py --prepare
python3.11 -B -X utf8 tmp/runMessagesDeploymentRuntime.py --mock normal
python3.11 -B -X utf8 tmp/runMessagesDeploymentRuntime.py --mock stop
python3.11 -B -X utf8 tmp/runMessagesNativeLifecycle.py --output tmp/messagesNativeLifecycle-linux311-v1
```

`runMessagesDeploymentTests.py`复用原隔离guard，禁用dotenv、网络及生产数据访问。根配置声明data目录时只跳过精确的无副作用声明，不放行该目录内容；Linux用 `resource.ru_maxrss × 1024` 记录累计峰值，Windows用 `peak_wset`。报告保留OS、Python、依赖、源码散列、JUnit原始失败数及 `targetLinux311`。Windows3.13先前335项通过；随后 Debian 13.4 / Python 3.11.16 独立执行也为335项通过，Linux结论来自目标机报告。

`runMessagesDeploymentRuntime.py`仅提供prepare和两种mock，没有live或凭据入口。它经真实BGE、加密SQLite及正式检索入口，记录心跳与系统峰值；normal应完成32题，stop应在第4次身份错误后保留28未发分母。mock模拟选第一候选，其P/R不衡量模型效果。运行结束核验runtime/native、lease和transport状态；实际远端服务、目标主回答profile、并发生产容量及Telegram操作闭环不由这些mock证明。

静态mock之外，`tmp/runMessagesNativeLifecycle.py`补1000条真实BGE负载、并发1/2/4评分、正式CRUD通知和同进程runtime重建，见[本机补验](archive/llm-memory-hybrid-research-2026-09.md#2026-09-22真实索引生命周期补验)。Windows3.13.9单次峰值221.69MiB；后续 Linux 执行峰值344.76MiB，首建26.44秒、同进程重建27.27秒，并发1/2/4热查询 P95 为0.174/0.230/0.321秒。这些查询覆盖加密库读取与本地评分，不含远程选择和主回答。Linux正常mock32/32、身份错误停批mock及清理均按预期完成，详见[目标机独立审计](../tmp/linux311-final-audit.json)。

模板负载不计质量分数，同进程重建不等于 Bot 进程重启，也不承诺长期容量。Linux 实际使用部署包及生命周期补充包，三个研究包装器有适配改动，正式文件保持与原包一致；不要将当前 Windows 包装器未经替换的重跑称作同一 Linux 执行。目标报告还保留了初次 ONNX 导入在项目根生成 `:memory:.ses` 及后续迁移的隔离疏漏；不能将其改写为全程无越界。本地收到的是汇总和独立审计，未带回全部远端原日志。

### 审阅结果和启用顺序

用户已授权六次主回答并委托助手完成裁决。[审阅报告](../tmp/messagesFinalAnswers-review-v1.md)记录三例各“实际双事实原顺序”和“仅正确事实”一次，6/6有效、全部核心答案正确；没有重新选择或修改提示。编号题双事实组申请删除错误项，仅正确组申请追加“已向用户说明”的低价值状态，两项均未执行。三例的错误标签仍成立；不再要求用户重复评分，也不把助手审阅写成人类批准整份 draft 数据集。

结合 Linux 离线验证，目前可以进入实际应用验收，依次完成：

1. 在目标机器核对显式的 `hybrid/llm/messages` 配置、selector模型与服务，以及主回答人设、模型和参数。主回答快照目前仅与本地配置匹配；LLM后端不要求批准local阈值。
2. 在测试chat验证一条实际消息经检索进入主回复，并验证新增/更新/删除、global自动批准策略与pinned审核。故障时保留合格pinned、放弃情境选择，主回复继续。记录真实调用总耗时，不能把Linux本地查询P95当作整体回复耗时。
3. 检查 `/llm memory status`，执行实际Bot停止和重新启动，确认后台索引恢复、在途请求清理和回滚到 `legacy` 的操作。已完成的同进程runtime重建不替代这一步。

完成这些应用检查后再决定小范围生产启用。当前保持 `legacy/local/responses`，本轮六次诊断既未改开关，也未执行模型申请的记忆写入。不要为了收尾重新拟合local阈值、重发固定32题或追加同题回答。

---

## 测试边界

### 测试环境

目标机验收至少使用生产同级的 Python 3.11、CPU、内存限制、操作系统和依赖版本。当前开发环境的 Python 3.13 回归可以提前发现代码问题，但不能替代目标机结果。

Hybrid 需要单独安装可选依赖：

```bash
pip install -r requirements-memory.txt
```

模型身份固定在 `utils/llm/memory/modelManifest.json`。安装或校验只能使用以下入口：

```bash
python scripts/memoryModel.py verify
python scripts/memoryModel.py install
```

`install` 才会访问模型下载端点；启动 bot、收到消息、后台索引和 `/llm memory status` 都不得隐式下载模型。需要兼容镜像时，必须显式传入无凭据、无查询参数的 HTTPS `--endpoint`。

### 数据与隐私

- 自动化测试使用临时 SQLite、临时密钥、fake encoder 和脱敏 fixture，不读取现有 `data/` 或真实 `.chatKey`。
- 目标机使用 staging 数据目录或可恢复的测试副本，不直接对生产 `data/llm/llmMemory.db` 做增删改。
- Telegram 人工验收使用专用 staging Bot、测试 chat 和合成事实，不把真实用户隐私写进 fixture、报告或日志。
- `retrievalHint` 属于检索导流字段，不应出现在最终 prompt、普通日志或非管理员导出；人工验收发现泄露时立即停止。
- `.cache/llmMemory/` 下的模型和报告是本地生成物，不作为正式提交内容；报告内不得包含真实 memory 正文、hint、向量或密钥。

### 初始状态

开始前确认：

1. `utils/llm/memory/retrievalCalibration.json` 仍为 `status: "unconfigured"`，三个 threshold 为 `null`，除非已经完成本文档的人工批准流程。
2. `memoryRetrievalMode` 默认是 `legacy`。
3. `tests/utils/llm/memory/fixtures/retrievalCases.json` 的 split 和 `groupID` 没有为了迎合结果临时修改。
4. 目标机已经记录当前数据库备份、进程启动方式和回滚联系人。

---

## 通过门槛

### 自动化与离线硬门槛

- 相关 pytest 全部通过。
- `python scripts/module.py validate` 与 `python scripts/module.py scan` 通过。
- 两份 fixture 都能通过 schema 校验；calibration 和 holdout 的 `groupID` 不交叉。
- 评测模式的输出仍经过正式 `buildQueryTexts`、评分、选择和 `renderMemoryContext`，不能在脚本中复制一套评分逻辑。
- holdout 至少 30 个场景，contextual precision ≥ `0.95`、required recall ≥ `0.80`、forbidden hit = `0`。
- 有 pinned 标注时，pinned recall ≥ `0.80` 且 pinned forbidden hit = `0`。
- 不满足质量 gate 时，不生成可上线的 approved calibration，不切换默认模式。

### 目标机硬门槛

- 1000 条以上合成记忆的固定模型能加载、索引、查询和关闭；进程内只存在一个 `MemoryEncoder`。
- 热态检索 P95 ≤ `1` 秒；任何请求等待不得超过 `2` 秒；超时不增加无界替代任务。
- RSS 峰值相对基线的新增常驻内存 ≤ `256 MiB`，并单独记录模型加载、索引、热查询和关闭阶段的峰值。
- 事件循环在语义编码和数据库读取期间保持可响应；不能出现持续阻塞、未处理后台异常或关闭时 native worker 仍在访问已释放 encoder。
- 新增、更新、删除、禁用、重新启用和 mode 切换最终都与 SQLite 正本一致；旧向量不能覆盖新内容，也不能在删除后复活。
- memory 开关只影响上下文检索，不新增一次主模型生成调用。

`event loop heartbeat` 的具体数值目前不是代码中的正式产品阈值。目标机必须记录 P50/P95/P99 和最大间隔；出现持续超过 `500 ms` 的停顿、任务积压不下降或主回复被 memory 阻塞，即使平均值正常也判失败，先回到 `legacy`。

---

## 一、自动化回归

以下命令从项目根目录执行。Windows 环境若系统 pytest 临时目录没有写权限，显式使用仓库内 `--basetemp`；测试结束后删除本轮生成的临时目录，不删除仓库原有的 `tmp/pytest-memory-*`。

### 1. Memory 与真实链路回归

```bash
python -m pytest tests/scripts/test_evaluateMemory.py tests/scripts/test_memoryModel.py tests/utils/llm/memory tests/utils/llm/test_review.py tests/utils/command/llm/test_memoryCmd.py -q --basetemp .pytest-tmp-memory-smoke
```

这组测试覆盖：

| 区域 | 必须确认的行为 |
|---|---|
| `database.py` | 加密、scope、enabled、`contextual/pinned`、CRUD 成功后通知 runtime |
| `retrieval.py` | 宽候选、base 语义准入、enhanced 仅重排已准入 ID、lexical 独立贡献、RRF、去重、字符预算、注入前快照复核和降级 |
| `runtime.py` | 双表示评分、共享矩阵不重复比较/计费、队列上限、缓存字节预算、迟到结果、对账、超时、worker 自恢复和关闭 |
| `encoder.py` | base 文本排除 hint、enhanced 文本包含 hint、无 hint 复用矩阵、256 token 输入边界、分片、归一化以及可选依赖缺失时的导入行为 |
| `review.py` 与 handler | pinned 独立审核、`targetState`、retry/feedback 和 memory query 透传 |
| `memoryCmd.py` | mode/hint/clearhint、status、retrieval 切换和人工 CRUD |
| `evaluateMemory.py` | fixture schema、split 隔离、calibration 只读 base、`hybrid+hint` 只改变排序、approved 校验和零 LLM 调用 |
| `memoryModel.py` | 固定 revision、artifact hash、路径边界、事务发布和失败回滚 |

此前双表示实现阶段运行上述命令得到 `282 passed`；后续新增了实验和测试，不能把这份历史数量当成当前工作区的验证结果。目标机和 Telegram 层仍须独立执行。

### 2. 模块登记与差异检查

```bash
python scripts/module.py validate
python scripts/module.py scan
git diff --check HEAD
```

通过条件：

- 新增 memory Python、JSON、fixture 和脚本都已经登记或明确属于文档/本地报告。
- `scan` 没有发现未登记的模块文件。
- `git diff --check HEAD` 没有空白错误（覆盖 staged 与 unstaged 改动）。
- 不把 `.cache/llmMemory/`、pytest 临时目录或模型 artifact 加入提交。

### 3. Fixture schema 校验

```bash
python scripts/evaluateMemory.py validate --cases tests/utils/llm/memory/fixtures/retrievalCases.json
python scripts/evaluateMemory.py validate --cases tests/utils/llm/memory/fixtures/retrievalSmokeCases.json
```

正式集 `retrievalCases.json` 用于 calibration/holdout 质量评测，当前包含 96 个场景（calibration 66、holdout 30）；合成集 `retrievalSmokeCases.json` 只有 6 个结构边界场景，覆盖 pinned、disabled、scope、history、空查询、宽候选和预算。合成集不能替代人工标注集。

校验时特别检查：

- `caseID`、`groupID` 唯一且 split 声明与实际数量一致；
- 同一 `groupID` 不同时出现在 calibration 与 holdout；
- `requiredIDs`、`allowedIDs`、`forbiddenIDs` 互不冲突，pinned 标注同理；
- 含 `history` 的 case 提供固定 ISO `queryNow`；
- disabled、跨 scope 和 pinned 条目不会被当作普通 contextual 正例；
- `allowAbstain` 是布尔值，而不是缺省值或字符串。

---

## 二、离线评测与校准

离线评测不读取生产数据库，也不调用生成型 LLM。它只使用脱敏 fixture 和正式检索原语，因此结果代表检索契约，不代表完整 bot 体验。

### 1. 模型与编码器资源初测

模型已按 manifest 安装后运行：

```bash
python scripts/memoryModel.py verify
python scripts/evaluateMemory.py encoder --memories 1000 --queries 100 --output .cache/llmMemory/reports/encoder-smoke.json
```

记录报告中的 `modelRevision`、`encodingVersion`、加载时间、memory/query 编码时间、RSS 阶段采样和 `maxObservedDelta`。`encoder` 模式不测数据库、不测 runtime 队列，也不包含事件循环 heartbeat。

### 2. 生成候选 calibration

```bash
python scripts/evaluateMemory.py calibrate --cases tests/utils/llm/memory/fixtures/retrievalCases.json --output .cache/llmMemory/reports/candidateCalibration.json
```

候选文件必须满足：

- `status` 是 `candidate`，不是 `approved`；
- `schemaVersion` 是 `2`，并声明语义使用 `base` 准入、`enhanced` 排序；
- 只使用 calibration split；
- `datasetSha256` 与 `validate` 报告一致；
- threshold 是从实际分数边界产生的有限非负数，不能手工填写；
- 没有满足精确率和 forbidden 约束的通道必须是 `null`；
- 输出路径不能是正式 `utils/llm/memory/retrievalCalibration.json`。

四处标注与题意修订后的 fixture 数据集 SHA-256 为 `f24e48f2711925ba6464cc031edf39070ebdf4d1414a3d1cbf39fa3394b9f7e6`。旧 hash 对应的 candidate 已失效；本次重新生成的报告位于 `.cache/llmMemory/reports/candidateCalibration-reviewed-baseline.json`，仍保持 `status: "candidate"`，必须与新增场景的人工标注一起复核：

| 通道 | candidate threshold | 放行正样本 / 观察到的正样本 |
|---|---:|---:|
| `semanticCurrent` | `0.6696333885192871` | `16 / 64` |
| `semanticAssisted` | `0.782344400882721` | `18 / 64` |
| `lexical` | `20.80071401306478` | `2 / 44` |

语义 threshold 只从不含 hint 的 base 分数产生；enhanced 分数不参与校准。这里的分数是各通道独立统计，不等于 RRF 融合后的整体 recall；但较低的放行比例提示阈值可能偏严，不能仅因三个通道 precision 都是 `1.0` 就批准。

#### 可选：相对分数研究

需要判断 top-margin 是否值得加入线上规则时，在 calibration 内运行：

```bash
python scripts/evaluateMemory.py margin --cases tests/utils/llm/memory/fixtures/retrievalCases.json --output .cache/llmMemory/reports/margin-study-base-hint-v2.json
```

该命令要求 calibration 同时存在无答案和多 required 场景，只生成 `status: "experimental"`、`productionEligible: false` 的研究报告。它重新计算绝对阈值基线，并比较固定绝对阈值加 margin、配合 margin 重校准和 top-1 对照。语义差值只读 base；通过 margin 的通道仍保留所有满足绝对阈值的记忆。报告不作为 approved calibration 使用，也不评分 holdout。

核对以下结果：

- `trials[].metrics` 同时看 precision、recall、forbidden、`noAnswerFalseRecallCount`、`multiRequiredCompleteCount`；必须连同对应场景总数一起读，不能把零预测当成 100% precision。
- `lostRequiredCount` / `recoveredRequiredCount` 与逐场景 ID 解释相对基线的损失和收益；不要只比较净变化。
- `channelEvidence` 中找第一名标为 forbidden 且 `topGap` 很大的案例，确认绝对阈值仍在阻止它进入；不要用普通报告中的 enhanced 排序差值替代 base 差值。
- 检查缺少第二个有效分数时弃权、第二名低于绝对阈值仍参与差值、多 required 可以同时通过、pinned 不受 top-1 截断影响。对应行为由 `test_evaluateMemory.py` 回归覆盖。
- 基线 `trials[0].cases[].diagnostics.candidateEvidence` 保留每条事实的准入、排序和预算证据，供追查多 required 遗漏。

2026-09-16 的 26 组 calibration 内实验中，绝对阈值基线召回 31/62 条 required；assisted margin=0.01 或 0.02 配合重校准后为 34/62，precision=1.0、forbidden=0、无答案误召回=0/8。但多 required 完整召回仍为 0/4。top-1 对照没有额外损失，仅因基线每场景本来最多选到一条，不能据此批准 top-1。

相对分数实验定义及默认网格见 [evaluateMemory.py](../scripts/evaluateMemory.py)，当前数据扩充与基线结果见 [Calibration 扩充与检索研究](llm-memory-calibration-expansion.md)。上述 margin 数字仅属于旧 66 题上的历史实验，新增标注仍待人工复核；若据此修改线上准入规则，必须更新相应版本契约、重校准，再用新的盲测 holdout 验收。

### 3. Holdout 验收

**当前尚未进入这一步。** `scripts/buildMemoryCalibration.py` 已生成多版 calibration 草稿：v1 为 120 条、v2 为 138 条、v3/v4 为 162 条；它们都保留 30 条旧 holdout，且仍是 `draft`，不等于正式 fixture。`scripts/studyMemoryRetrieval.py` 仅做 calibration 对照与分组验证，不批准配置，也不评分 holdout。本轮基线分组验证 precision=0.9286、recall=0.0675、forbidden=1；旧/新增样本、多 required、无答案误召回、201 项相关测试和待人审问题见 [Calibration 扩充与检索研究](llm-memory-calibration-expansion.md)。待规则成熟后再冻结并准备新盲测，不能反复使用已经查看过的旧 holdout。

`evaluate` 拒绝 `candidate` calibration，并要求 calibration 与当前模型、词面版本和 fixture hash 绑定。只有人工审查通过后，才可以把 approved calibration 放在一个临时路径进行验收：

```bash
python scripts/evaluateMemory.py evaluate --cases tests/utils/llm/memory/fixtures/retrievalCases.json --split holdout --calibration <approved-calibration-copy.json> --output .cache/llmMemory/reports/holdout-approved.json
```

同时运行所有模式以保留旧行为基线：

```bash
python scripts/evaluateMemory.py evaluate --cases tests/utils/llm/memory/fixtures/retrievalCases.json --split holdout --calibration <approved-calibration-copy.json> --modes legacy lexical hybrid hybrid+hint --output .cache/llmMemory/reports/holdout-approved-all-modes.json
```

重点看 `hybrid+hint`，因为它与线上一样先用 `content + tags` 的 base 表示准入，再用加入 hint 的 enhanced 表示排序；`hybrid` 使用同一 base 准入集合但继续按 base 排序，是“不给 hint 排序权”的对照。报告必须同时检查整体指标、每个 case、每个 `subsets` 子集和 pinned 独立指标，不能只看平均 precision。定位失败项时再查看 `diagnostics.channelEvidence` 的准入/排序 top 与 gap，以及 `candidateEvidence` 的 `admissionScore`、`rankingScore`、base threshold margin、`rankingBlockedByAdmission`、RRF 支持、去重和预算结果；这些离线字段不得包含正文或 hint。

旧单表示编码下的 `hybrid+hint` precision `0.900000`、recall `0.692308`、forbidden hit `1`，以及 `hybrid` 对照 precision `0.882353`、recall `0.576923`、forbidden hit `1`，现在都只属于历史诊断。encoding version 与 calibration schema 均已变化，评测器会拒绝继续绑定旧 threshold；正式 `retrievalCalibration.json` 保持 `unconfigured`，三个 threshold 保持 `null`，默认模式保持 `legacy`。

当前 calibration 有 66 个场景，其中新增 8 个无答案/应弃权场景、4 个多 required 场景，并覆盖错误 hint、明确话题切换、真实回指和相反近邻。新增 16 个场景均标记为 `metadata.reviewStatus: "draft"`，必须先人工复核；hint 权限已经拆分，而当前 holdout 又已被多轮查看，因此必须换一份未参与本次设计的新盲测 holdout，不能在现有 holdout 上观察结果后直接批准。

### 4. 资源 benchmark

```bash
python scripts/evaluateMemory.py benchmark --memories 1000 --queries 100 --concurrency 1 2 4 --output .cache/llmMemory/reports/benchmark.json
```

当前 benchmark 报告必须如实保留：

- `isolated: false`，因为它运行在调用进程内；
- `eventLoopHeartbeatP95Ms: null`；
- `lifecycleScenarios.*` 仍为 `not-run`，除非目标机运行时冒烟另行填充；
- 加载、索引、关闭、矩阵字节、RSS、P50/P95/P99 和 2 秒超时比例。

本次开发机双表示基线（1000 条均带 hint、100 个查询）为：`matrixBytes=4096000`（约 3.91 MiB）、索引约 29.98 s；并发 1/2/4 的热查询 P95 约为 30/64/100 ms，2 秒超时率均为 0；调用进程 RSS 最大增量约 139.6 MiB。后续目标机报告应与这组数值对照，明显回退时先排查模型、CPU 和候选规模。

因此 benchmark 通过只能说明本地 encoder、双表示缓存和串行评分初步可行，不能单独宣布 256 MiB、事件循环或增量生命周期验收通过。

### 5. 批准流程

只有在以下条件全部满足时才允许批准：

1. calibration fixture 经人工复核，尤其是 zero lexical overlap、回指、话题切换、多义短句、中文单字、scope、pinned、hint 和 abstain。
2. holdout 未被用于调阈值或移动标签；同一 `groupID` 没有泄漏。
3. holdout 通过 precision、recall、forbidden 和 pinned gate。
4. 目标机通过资源、延迟、索引新鲜度和关闭验收。
5. Telegram 人工验收确认新增 context 没有降低回复质量。

批准文件必须保留模型 revision、encoding version、lexical version、fixture hash、threshold、审查人和日期，并经过代码审查后再更新正式 `retrievalCalibration.json`。任何绑定字段改变都必须重新 calibration；不能只改 threshold 继续沿用旧 hash。

---

## 三、目标机运行时冒烟

这一节必须在 staging Bot 上执行。当前正式 calibration 未通过，所以只能验证失败关闭、legacy 以及运行时基础行为；不能把显式切到 `hybrid` 后的结构性结果称为语义质量上线。

### 1. 冷启动与配置状态

从项目根目录启动：

```bash
python scripts/memoryModel.py verify
python bot.py
```

在控制台逐项执行：

```text
/llm memory status
/llm memory retrieval
/llm memory retrieval legacy
/llm memory status
```

之后需要检查：

- bot 能正常启动，数据库初始化和 `registerMemoryRuntime()` 只创建空 runtime，不在 legacy 下加载模型；
- status 能分别显示 mode、calibration 原因、memory 计数、cache 覆盖率、query/index 队列、累计运行时故障计数、容量饱和标记和最近降级原因；实际请求 diagnostics 还要记录 `degradedReasons`、`channelDiagnostics` 与 `semanticCache`；
- status 不输出正文或 hint，也不因为查看状态创建 encoder；
- legacy 下 runtime worker 休眠，没有持续索引任务；
- 普通请求的 memory/history 开关行为不改变主模型生成调用次数。

本节后续步骤必须先标明所处阶段。`unconfigured` 不是“语义通道尚未
测试通过”，而是线上明确的 fail-closed 状态；在该阶段，contextual
词面/语义产出不属于通过判据。

| 阶段 | 前置条件 | 可以验证 | 不可以据此宣称 |
|---|---|---|---|
| 未配置/校准无效 | `calibrationStatusInvalid`（或其他 calibration 绑定失败原因） | legacy、失败关闭、pinned 可见性、数据库 CRUD、队列/缓存/生命周期和 prompt 安全 | lexical/semantic contextual 质量，或 hybrid 已可用 |
| approved（词面） | calibration 为 `approved`，绑定当前 fixture、模型与 `LEXICAL_VERSION` | lexical 产出、候选准入、RRF、预算和降级 | semantic 已经 ready |
| approved（语义） | 上一行条件成立，模型依赖与 artifact 校验通过，runtime encoder ready，目标 memory 已建立热缓存 | semantic/assisted 产出、冷缓存追赶和完整 hybrid 质量 | 把单次热态结果替代 holdout 或全链路资源验收 |

### 2. 失败关闭路径

在 staging 环境显式执行：

```text
/llm memory retrieval hybrid
/llm memory status
```

当正式 calibration 仍为 `unconfigured` 时，必须看到明确的校准不可用原因；检索不能使用猜测 threshold，也不能偷偷转为 legacy priority 候选。若有 pinned 且 scope 可见，只允许 pinned 按独立预算进入；contextual 语义通道应缺席。

随后恢复：

```text
/llm memory retrieval legacy
/llm memory status
```

检查恢复后 runtime 停止继续加载或索引语义模型，主回复仍可正常生成。这个步骤只验证 fail-closed 和回滚，不构成 hybrid 质量验收。

### 3. CRUD 与索引新鲜度

在一个专用 staging chat 中使用 `/llm memory` 管理命令，记录每次返回的 memory ID、写入时间、`/llm memory status` 的 `indexPending`、`cacheEntries` 和 `oldestIndexAgeMs`。每一步都用一次真实对话触发检索，并区分“数据库已提交”“词面可见”“语义索引 ready”三个时点。

按以下顺序执行：

| 前置条件 | 步骤 | 操作 | 通过条件 |
|---|---|---|---|
| 两个阶段都适用 | 新增 | `add` 一条带独特新词的 `contextual` memory | CRUD 成功；数据库事实存在；通知进入有界 index 队列 |
| 未配置：只验结构；approved + lexical：验词面；approved + runtime ready：再验语义 | 首次查询 | 立即用新词提问，再在索引完成后用同义表达提问 | 未配置时 contextual 可以缺席且原因明确；approved 后词面按校准阈值工作，热态语义结果不丢失也不重复 |
| 两个阶段都适用 | 同 ID 连续更新 | 对同一 ID 快速连续改为 A、B、C | 最终只允许 C 的状态和向量生效；旧 A/B 不能迟到覆盖 |
| 两个阶段都适用 | 不同 ID 更新 | 新增或修改另一个 ID | 两个 ID 的 index job 不互相覆盖；缓存计数和诊断可解释 |
| 两个阶段都适用 | 删除 | 删除已缓存的 ID，并立即查询旧词 | 数据库和 cache 都不再提供该事实；在途矩阵不能复活它 |
| 两个阶段都适用 | 禁用/启用 | `edit -enabled off`，查询，再 `-enabled on` | disabled 不进入候选；重新启用后由通知或对账补建 |
| 两个阶段都适用 | contextual → pinned | 编辑 mode 为 `pinned` | 旧 contextual 向量被驱逐；不进入语义竞争；在独立 pinned 预算内可见 |
| 两个阶段都适用 | pinned → contextual | 编辑 mode 为 `contextual` | 不再无条件常驻；重新获得有界索引资格并按 query 竞争 |
| 两个阶段都适用 | 重启 | 正常停止 bot，再次运行 `python bot.py` | SQLite 记录保留；RAM cache 可为空；后台对账按 best-effort 重新建立可用索引 |

不以“通知函数返回”作为索引完成的证明。`runtime.getStatus()` 中的 `cacheEntries`、`indexPending`、`lastReason` 和实际查询 diagnostics 才是验收依据。

### 4. Scope、pinned、disabled 与预算

使用两个 staging chat、两个测试 user 和需要时的 session，分别建立 global、chat、user、session 记录。每个 query 都要包含一个同义但不含相同关键词的目标事实、一个高 priority 的错误 scope 干扰项和一个 disabled 干扰项。

必须按阶段记录以下判据：

| 前置条件 | 判据 |
|---|---|
| 两个阶段都适用 | 只读取 global 与当前 chat/user/session scope；其他 chat 的高 priority 记录不能越过 scope 隔离；LLM 的 `chat/user` action 必须匹配当前请求身份；首次生成中普通 global contextual action 可按产品策略自动执行，retry/feedback 仍强制审核，`pinned` 始终需人工批准 |
| 两个阶段都适用 | disabled 记录不因 priority、旧 cache 或对账延迟进入 prompt |
| 两个阶段都适用 | pinned 不参加 contextual 的 BM25/semantic/RRF 竞争，也不占语义向量缓存 |
| 两个阶段都适用 | pinned 先按独立 `500` 字符预算裁剪，context 总块不超过 `1500` Unicode 字符 |
| 两个阶段都适用 | 超长条目整条跳过，不把半条事实截进 prompt；后续短条目仍有机会进入 |
| 两个阶段都适用 | `retrievalHint` 只影响 base 已准入条目的 enhanced 排序，不出现在最终 `<UNTRUSTED_MEMORY>` 块、诊断日志或普通导出 |
| 两个阶段都适用 | 最终 prompt 中的 memory 内容仍被 `<UNTRUSTED_MEMORY>` 包裹并经过分隔符中和 |

### 5. 历史窗口与主模型调用次数

在没有图片、URL、AFC 工具和视觉模型的 staging 对话中，分别执行。历史
构造、调用次数和 prompt 安全在两个阶段都适用；只有标注为 approved 的
步骤才把 contextual 词面/语义命中当作通过条件：

1. 直接消息：未配置阶段确认请求安全降级；approved 阶段再确认 current text 可以触发已批准通道。
2. 进行多轮聊天后切换话题，再回指早前事实；确认 memory 只使用调用方已经加载的最近 30 条历史，其中最多取最近 20 条、30 分钟内、总计 600 字符用于辅助语义查询。未配置阶段只验查询构造和不误注入，approved 阶段才验 assisted semantic 召回。
3. 使用 `/llm memory -once` 验证下一次调用带入 memory/history，随后 one-shot 自动清除。
4. 使用 `/llm memory -off` 发送同等请求，确认不读取 history、不调用 memory 检索。
5. 使用 `/llm memory -on` 重复请求，比较主模型 provider 请求计数；memory 的本地 encoder、BM25 和数据库读取不能造成第二次生成型 LLM 调用。

查询编码窗口必须保持 `256` tokens（包含特殊 token）；长 memory 以 `32` token overlap 分片，不静默丢弃尾部事实。

### 6. 队列、超时与关闭

自动化测试已经验证这些故障的逻辑；目标机需要确认状态和用户体验没有异常：

- 语义 query queue 满时，当前请求放弃语义分数但不阻塞主回复；
- index queue 满时，通知可以丢弃；周期对账会在容量允许时补排，容量饱和时持续跳过冷条目，直到驱逐/删除/重启释放空间；
- 单条记忆的 base/enhanced 唯一矩阵合计超过 `32 MiB`，或任一矩阵没有有效字节大小时，不驱逐全缓存硬塞，条目进入 blocked 诊断；
- lexical、semantic、数据库或最终复核超时不会抛到主生成链路；
- timeout 后底层 native 作业仍占用名额直到真正完成，不会无限创建替代作业；
- 连续 query 流量达到 `8` 次后，worker 会让出一次 index 机会；
- 正常关闭先停止接收工作、等待 native job、关闭 encoder/executor，最后清 cache 和 StateManager 引用；
- 关闭后无活跃 worker、无未完成 query future、无第二个 runtime 或 encoder。

---

## 四、Telegram 人工验收

人工验收只在 holdout 和目标机门槛通过后进行。当前正式 calibration
未配置时，以下场景只能验证链路、安全边界和 pinned；不能把 contextual
词面/语义命中或主模型使用效果称为 hybrid 质量通过。

| 阶段 | 前置条件 | 人工验收范围 |
|---|---|---|
| 未配置/校准无效 | status 显示 `calibrationStatusInvalid` 或其他绑定失败原因 | scope 隔离、pinned、disabled、prompt 安全、写入审核与主模型不增加生成调用 |
| approved + 词面 | approved calibration 与 fixture/model/version 绑定有效 | lexical 命中、误召回、预算和 diagnostics |
| approved + 语义 | 上一行条件成立，模型/runtime ready 且目标 memory 热缓存可用 | semantic/assisted 回指、话题切换、冷缓存追赶和主模型回复质量 |

### 1. 记忆读取质量

准备一组只含合成事实的 staging memory，至少覆盖。每个场景同时记录
阶段；未配置阶段只记录“应缺席/应降级”，approved 阶段才记录命中质量：

- 当前消息与事实零词面重叠，但语义明确（approved semantic）；
- “还是之前那个”“刚才说的地方”一类回指（approved assisted semantic）；
- 多轮后切换话题再回到早前问题（approved assisted semantic）；
- 多义短句和中文单字，确认不会因为单字造成大面积误召回（两阶段）；
- 当前 chat/user 与其他 chat/user 的同主题冲突（两阶段，含 action 授权）；
- 正确 hint 能在 base 已准入的同类事实间改善顺序，但不能改变准入集合（approved semantic）；
- 错误或过度宽泛的 hint 即使 enhanced 分数很高，base 未过阈值时仍不得获得 semantic RRF 贡献（approved semantic）；
- 空查询在 `hybrid`/fail-closed 阶段只保留可见 pinned；`legacy` 仍遵循旧的 priority/配额选择，可能返回 contextual，必须分别记录。

人工记录的不只是“回复听起来对不对”，还要记录当轮 diagnostics 中的 mode、候选数量、各通道 qualified 数、最终 selected 数、预算淘汰数和降级原因。错误记忆比少记一条更严重：出现 forbidden 条目时停止继续灰度。

### 2. 写入与审核闭环

分别验证：

1. 管理员 `add/edit/del` 可以完成 CRUD，并在随后查询中体现新状态。
2. LLM 申请普通 `contextual` add/update/delete 时，按当前 `memoryAutoApprove` 和审核模式进入正确路径；首次生成的普通 `global + contextual` action 可自动执行，但 `chat/user` 即使身份匹配也仍须人工审核；自动执行失败时 action 仍回到审核队列，不应静默消失。
3. LLM 申请 `pinned` 时不能只凭模型输出自动获得人工授权；审核卡必须明确显示目标和 mode。
4. 审核卡打开后，另一个操作先修改目标记录，再点击批准，旧 `targetState` 必须失效，不能覆盖新状态。
5. 编辑 hint 时，`-clearhint` 才清空；正文或 tags 改变而未提供有效新 hint 时，旧 hint 失效。
6. 审核、retry 和 `:fb` 重试保留原始 `MemoryQuery` 的 current/reply/history，不从展示文本反解析。

### 3. Prompt 安全与内容质量

写入一条内容包含 `</UNTRUSTED_MEMORY>`、`<TRUSTED_KNOWLEDGE>` 和伪造指令的合成 memory。确认：

- 最终 prompt 仍只有一个合法的低信任 memory 块；
- 内容中的伪造分隔符不能闭合外层结构或升级信任级别；
- 模型不因为 memory 中写着“必须提及”就强行提及；
- hint 不被模型复述；
- 与当前消息无关的 memory 不会为了填预算而出现在回复中。

---

## 故障注入矩阵

故障注入优先使用现有 pytest fake 和可控 future，不在生产进程中直接修改代码或数据库。目标机只验证经过测试的降级语义、日志和状态展示。

| 故障 | 现有自动化覆盖 | 目标机观察点 | 正确结果 |
|---|---|---|---|
| 模型 manifest 无效/模型缺失 | `tests/utils/llm/memory/test_memoryRuntime.py::test_invalidManifestDegradesWhenEncoderIsActuallyRequested` | `/llm memory status`、启动日志 | encoder 不可用；bot 仍可启动；不访问外部 embedding API |
| semantic queue 满 | `tests/utils/llm/memory/test_memoryRuntime.py::test_query_queue_limit_rejects_without_unbounded_executor_submission` | `queryRejected`、回复延迟 | 语义结果为空，主回复继续 |
| index queue 满 | `tests/utils/llm/memory/test_memoryRuntime.py::test_index_queue_limit_is_bounded` | `indexDropped`、`oldestIndexAgeMs` | 队列有上限；后续对账在容量允许时补偿，饱和时保持 best-effort |
| cache 字节预算满 | `tests/utils/llm/memory/test_memoryRuntime.py::test_lru_byte_limit_and_reconcile_publish_does_not_evict` | `cacheBytes`、`reconcileCapacitySaturated` | 对账不抖动；在线更新可按资格驱逐 |
| 单矩阵过大 | `tests/utils/llm/memory/test_memoryRuntime.py::test_publish_rejects_matrix_larger_than_cache_budget` | `blockedFingerprints`、`lastReason` | 不驱逐已有热缓存，不硬塞大矩阵 |
| lexical 异常/超时 | `tests/utils/llm/memory/test_retrieval.py::test_lexicalFailureKeepsPinnedMemory`、`test_lexicalTimeoutKeepsFinalizeBudgetForPinned` | `degradedReason` / `degradedReasons` / `channelDiagnostics` | 已完成 semantic 与 pinned 保留，不能抛主链路 |
| semantic 超时 | `tests/utils/llm/memory/test_retrieval.py::test_timeout_keeps_retrieval_slot_until_thread_finishes`、`tests/utils/llm/memory/test_memoryRuntime.py::test_query_timeout_keeps_native_job_active_until_it_really_finishes` | `queryTimedOut`、`activeNativeJobs` | 不生成替代洪峰；真实 native 完成后才释放名额 |
| 数据库读取失败 | `tests/utils/llm/memory/test_memoryRuntime.py::test_reconcile_read_error_preserves_partial_scan_and_cache` | `lastReason=reconcileReadFailed` | 保留已有 cache、游标和已见集合，不清空全库 |
| worker 单轮异常 | `tests/utils/llm/memory/test_memoryRuntime.py::test_worker_recovers_after_unexpected_iteration_error` | `workerFailures`、错误日志 | 退避后继续工作，不产生未处理 task exception |
| 编码期间同 ID 更新/删除 | `tests/utils/llm/memory/test_memoryRuntime.py::test_stale_encode_result_is_rejected`、`test_delete_or_disable_invalidates_cached_matrix` | 查询旧/新事实、cache 状态 | 旧矩阵丢弃；删除和禁用不会被迟到结果复活 |
| 关闭时 native 仍在运行 | `tests/utils/llm/memory/test_memoryRuntime.py::test_cancelled_native_wrapper_waits_for_underlying_thread`、`test_close_waits_for_native_job_then_closes_encoder_and_clears_state` | 关闭时长、进程线程、encoder 状态 | 等待真实完成后再释放模型和 executor |

每个故障都要保存原因码、发生阶段、恢复时间和是否影响主模型回复。诊断不得保存查询正文、memory 正文、hint 或向量。

---

## 观测与记录

### 运行时状态

`/llm memory status` 是 operator 的第一入口，至少记录：

- 配置 mode 与 calibration reason；
- 实际检索的 `degradedReasons`、各通道 `channelDiagnostics` 和 `semanticCache` 状态；
- enabled/total/contextual enabled 计数；
- encoder 是否 ready、cache entries、cache bytes 和 coverage；
- query/index pending、最老 index age；
- `queryRejected`、`queryTimedOut`、`indexDropped`、`staleResults`、`encodeFailures`、`workerFailures` 累计值；
- `lastReason`、`reconcileCapacitySaturated` 和关闭是否完成；容量标记表示 best-effort 缺口，不表示 SQLite 数据丢失。

状态页不显示正文或 hint。`buildStructuredMemoryContext()` 写入的检索日志只能包含 diagnostics；如果日志出现 query、content、hint、向量或密钥，立即按安全问题处理。

### 报告清单

建议每次验收在 `.cache/llmMemory/reports/` 保存：

| 报告 | 来源 |
|---|---|
| `fixture-validation.json` | `evaluateMemory.py validate` |
| `encoder-smoke.json` | `evaluateMemory.py encoder` |
| `candidateCalibration.json` | `evaluateMemory.py calibrate` |
| `holdout-*.json` | `evaluateMemory.py evaluate` |
| `benchmark.json` | `evaluateMemory.py benchmark` |
| 目标机记录 | operator 手工填写的启动、状态、RSS、heartbeat、生命周期结果 |

报告应绑定代码版本、manifest revision、fixture hash、测试机规格和时间。报告只供审查，不直接作为生产配置加载。

### 验收记录模板

| 项目 | 记录 |
|---|---|
| 测试日期/时区 | 待填写 |
| 代码版本 | 待填写 |
| Python/OS/CPU | 待填写 |
| `modelRevision` / `encodingVersion` | 待填写 |
| fixture dataset SHA-256（由 `evaluateMemory.py validate` 输出的规范化 digest，非 raw file hash） | 待填写 |
| 自动化回归 | 通过 / 失败，附报告路径 |
| offline holdout | precision / recall / forbidden / pinned |
| RSS 峰值增量 | 待填写 |
| 热态 P50/P95/P99 | 待填写 |
| 最大 heartbeat 间隔 | 待填写 |
| CRUD 生命周期 | 通过 / 失败，附 memory ID 与时间线 |
| Telegram 人工验收 | 通过 / 失败，附 operator |
| 最终决策 | 保持 `legacy` / 允许 staging `hybrid` / 允许灰度 |

---

## 回滚与停止条件

### 立即停止条件

出现以下任一项，停止 hybrid 灰度并回到 `legacy`：

- holdout forbidden hit 非零，或人工发现错误 scope/contextual 记忆进入 prompt；
- precision、recall、pinned gate 任一不达标；
- RSS 新增常驻内存超过 `256 MiB`，或热态 P95 超过 `1` 秒；
- 请求超过 `2` 秒、事件循环持续停顿、worker 不能自恢复或关闭不干净；
- 删除/禁用后的事实仍可被召回，或旧矩阵覆盖连续更新的新事实；
- memory 导致主模型多发一次生成请求；
- prompt delimiter、hint、正文隐私或加密字段泄露；
- calibration 与 model manifest、lexical version 或 fixture hash 不一致。

### 回滚步骤

1. 通过受信任的 console 执行 `/llm memory retrieval legacy`，确认 status 显示 `legacy`。
2. 暂停继续写入新的 hybrid 验收数据，保存 status、diagnostics、进程 RSS 和报告。
3. 不删除 SQLite 正本，不用缓存状态反向修改 memory；SQLite 是唯一事实来源。
4. 若进程仍有 native job，按正常关停流程退出，等待 `runtime.close()` 完成后再重启。
5. 恢复代码时保留并审查数据库 schema 迁移结果；不要用 destructive git 命令覆盖其他工作区改动。
6. 若问题来自 calibration，恢复经过代码审查的 `unconfigured` 文件；不要把失败候选标成 `approved`。
7. 修复后从自动化回归、fixture 校验和 holdout 重新开始，不能直接跳回 Telegram 灰度。

模型缓存可以保留在本地，因为 `legacy` 不会加载它；是否清理模型由部署管理员依据磁盘策略决定，不影响数据库回滚。

---

## 维护原则

- 任何改变 query 构造、分词、encoder 输入、RRF、预算、scope 或 runtime 生命周期的代码，都必须同步更新本方案对应的自动化和目标机步骤。
- 任何 fixture 标签修改都要记录原因、审查人和新的 SHA-256；修改后必须重新划分并重新 calibration，不能复用旧 threshold。
- 新增 synthetic smoke case 只能补结构边界，不能把它当作人工质量正例。
- 新增报告字段时保持 `isolated`、`eventLoopHeartbeatP95Ms` 和 `lifecycleScenarios` 的真实含义，不把 `not-run` 改写成通过。
- 观察到错误召回时优先收紧准入或修正标签/查询构造，不增加另一轮生成型 LLM；额外生成会增加延迟和错误传播风险。
- 保持 `legacy` 作为可用回退路径，直到质量、资源、生命周期和人工回复四类证据同时满足门槛。
- 代码、`docs/llm-memory.md`、本方案和 `docs/internal/llm-memory-refactor.md` 的状态描述必须一起更新，避免文档宣布尚未实测的能力。
