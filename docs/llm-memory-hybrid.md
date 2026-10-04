# LLM Memory Hybrid 检索详细文档

> 最后更新：2026-10-03
>
> 这份文档记载 Hybrid 混合检索的算法原理、实验数据、启用流程和验收门槛。面向想深入理解技术实现、或需要部署验收的大家。普通使用的话，请看 [LLM Memory 主文档](llm-memory.md)。
>
> Written by ZincNya~ ❤

---

## 目录

- [问题背景](#问题背景)
- [设计约束](#设计约束)
- [三路混合检索](#三路混合检索)
- [Base/Enhanced 双表示](#baseenhanced-双表示)
- [RRF 融合算法](#rrf-融合算法)
- [Runtime 增量索引](#runtime-增量索引)
- [LLM Selector（可选）](#llm-selector可选)
- [实验数据](#实验数据)
- [启用流程](#启用流程)
- [验收门槛](#验收门槛)
- [故障排查](#故障排查)

---

## 问题背景

最先的 Legacy 检索有致命缺陷：

### 候选缺失（Candidate Missing）

旧逻辑中，候选记忆按 `priority` 降序排列，每个 scope 只取前 20 条，汇总后取前 10 条。这会导致一条与当前消息高度相关的低优先级记忆，会被高优先级但无关的记忆挤出候选池，**根本没有机会参与相关性判断**。

**举例来说：**

>
> 数据库里有 100 条记忆：
> - 90 条 priority=3 的核心身份/安全约束（pinned）
> - 10 条 priority=0 的用户偏好（contextual）  ~~当然实际上大概不会有这么悬殊的比例……~~
>
> 用户问："我不喜欢吃什么？"
> 相关记忆："用户很讨厌香菜"（priority=0）
>
> Legacy 检索：前 10 条全被 priority=3 占据，这条讨厌的食物相关的记忆根本没进候选池。
>

此外，在设计方案时，也有想过单独使用 BM25 词面匹配（基于中文 2-gram）来打分，但发现了**语义断层**——用户换种说法时，词面匹配完全失效：

```
记忆："用户在备考研究生"
查询："考研复习得怎么样"

BM25 tokens:
  记忆: ["用户", "户在", "在备", "备考", "考研", "研究", "究生", "生"]
  查询: ["考研", "研复", "复习", "习得", "得怎", "怎么", "么样", "样"]
  共同: ["考研"] （1 个 token）

分数很低，就可能被其他记忆挤掉了。
```

思来想去，最终还是选择把几种能叫得上名字的算法混在一起，作为**混合检索（BM25 + 语义向量 + RRF 融合）**搞出来了……

---

## 设计约束

Hybrid 检索必须在以下约束内解决问题：

| 约束 | 数值 | 原因 |
|------|------|------|
| **不新增 LLM 调用** | 0 次 | 鲁棒性/延迟不可接受 |
| **不使用外部 embedding API** | - | 依赖外部服务、隐私风险 |
| **本地 CPU 模型** | 新增常驻内存 ≤ 256 MiB（当前生产目标） | 服务器内存预算 |
| **热态 P95 延迟** | ≤ 1 秒 | 用户体验要求 |
| **请求最多等待** | 2 秒 | 超时降级到 pinned only |
| **Memory 块字符数** | ≤ 2500 | Prompt 预算（pinned 1000 + contextual 1500） |

256 MiB 只是当前生产上线目标，可以根据实际情况装配更好的模型，或放宽对内存占用的要求，最终是否调整目标仍须依据目标机的内存和延迟结果决定。

**关键决策：**
- ✅ 本地 ONNX 固定模型（BGE-small-zh-v1.5，512 维）
- ✅ 三路并行打分（语义 × 2 + 词面）
- ✅ RRF 融合（不需要归一化分数尺度）
- ✅ 增量索引 + LRU 缓存（32 MiB 预算）
- ❌ Chromadb / 外部向量数据库（已废弃方案）

---

## 三路混合检索

**核心思路：** 不再按 priority 截断，而是对全部候选用三路通道并行打分，三路任一过阈值即准入。

### 三路通道

#### 1. 语义 - current（semanticCurrent）

**输入：** 当前用户消息
**输出：** 每条记忆与当前消息的 `base` 表示的余弦相似度（0-1）

**例子：**
```
查询："考研复习得怎么样"
记忆："用户在备考研究生"

这样，即使词面重叠少，语义向量也能识别"考研"和"备考研究生"是同一主题。
相似度：0.78（示例假设值，实际由模型决定）
```

#### 2. 语义 - assisted（semanticAssisted）

**输入：** 近期聊天历史（最多 20 条、30 分钟内、600 字）+ 当前消息
**输出：** 每条记忆与辅助构造的 `base` 表示的余弦相似度（0-1）

**用途：**
- **消解指代：** "还是那里吧" + 历史"萨莉亚" → 找到"用户通常在萨莉亚约饭"
- **话题连续性：** 跨轮次的讨论，current 只有追问，assisted 补上完整背景

**实现：** `retrieval.py:buildQueryTexts` 用近期历史辅助构造查询（详见代码注释）。

**注意：** 若 current 和 assisted 构造出相同文本，检索入口会通过 `seenTexts` 集合防止重复评分，第二份语义分数不会重复计入排序（`retrieval.py:buildSemanticQueryPlan`）。

#### 3. 词面 - lexical

**输入：** 当前用户消息（不含历史）
**输出：** BM25 分数（基于中文 2-gram + 完整英数词）

**字段加权：** `content + 2 × tags`
- **为什么 tags 权重 × 2？** tags 是人工/模型给的分类词，比正文更接近「这条记忆讲什么」

**实现：** `lexical.py:scoreLexicalCandidates`

**为什么词面不用历史？** BM25 依赖词频统计，历史会稀释当前消息的关键词权重。

**为什么 retrievalHint 不进 BM25？** Hint 是辅助排序的，不该影响准入。详见 [Base/Enhanced 双表示](#baseenhanced-双表示)。

### 准入阈值

每路独立比对校准阈值（从 `retrievalCalibration.json` 加载）：

```python
# 伪代码
semanticCurrentPassed = (score >= semanticCurrentThreshold)  # 如 0.72
semanticAssistedPassed = (score >= semanticAssistedThreshold)
lexicalPassed = (
    lexicalThreshold is not None and lexicalScore >= lexicalThreshold
)

admitted = semanticCurrentPassed or semanticAssistedPassed or lexicalPassed
```

**三路任一通过即准入** — 这保证了：
- 语义相关但词面不重叠的记忆（如"考研" vs "备考研究生"）能通过语义路
- 精确关键词匹配的记忆（如"花生过敏"）能通过词面路
- 在 calibration 已 approved、但语义缓存尚未就绪时，词面路仍可独立工作

**当前状态（2026-10-02）：**
- `retrievalCalibration.json` 的 `status` 为 `unconfigured`，所有阈值为 `null`
- local 后端在此状态下 fail-closed：三个 contextual 通道均关闭，只保留 pinned
- 历史阈值实验见 [实验数据 - 本地阈值路线](#本地阈值路线)，不代表当前线上质量

---

## Base/Enhanced 双表示

一条记忆要编码**两个向量**，各司其职：

| 表示 | 编码内容 | 用在哪一步 | 为什么 |
|------|---------|-----------|--------|
| `base` | 正文 + 标签 | 准入阈值比较 | 保证正文本身足够相关 |
| `enhanced` | 正文 + 标签 + retrievalHint | 已通过记忆的排序 | hint 辅助调名次，不能让无关记忆混进来 |

### 问题场景

假设有两条记忆：

```python
# 记忆 A（正文很短，但加了详细的 hint）
{
    "content": "用户喜欢喝咖啡",
    "tags": ["饮食"],
    "retrievalHint": "咖啡、拿铁、美式、卡布奇诺、星巴克、瑞幸、咖啡因、提神"
}

# 记忆 B（正文详细，无 hint）
{
    "content": "用户对咖啡过敏，绝对不能喝含咖啡因的饮料",
    "tags": ["健康", "饮食"],
    "retrievalHint": None
}
```

用户问："推荐个饮料"。

**如果用单一向量（content + tags + hint）：**
- A 的向量会因为 hint 的大量咖啡关键词而"语义增强"
- A 的分数可能超过准入阈值 0.72，B 却没过（B 的正文虽详细，但向量没 hint 加持）
- 模型只看到"用户喜欢喝咖啡" → 推荐咖啡 → 用户过敏 ❌

**问题根源：** A 的正文本身（"用户喜欢喝咖啡"）与"推荐饮料"的相关性并不高，是 hint 硬把它拽进来的。但 hint 本该只影响"选中后排第几"，不该影响"该不该被选中"。

### 解决方案：双表示隔离

```python
# 编码时
baseVector_A = encode("用户喜欢喝咖啡 饮食")  # 只用 content + tags
enhancedVector_A = encode("用户喜欢喝咖啡 饮食 咖啡、拿铁、美式...")  # + hint

baseVector_B = encode("用户对咖啡过敏，绝对不能喝含咖啡因的饮料 健康 饮食")
enhancedVector_B = baseVector_B  # 无 hint，共享同一向量（省内存）

# 准入阶段：用 base 与阈值比较
score_A_base = cosine_similarity(queryVector, baseVector_A)  # 0.65（示例假设值）
score_B_base = cosine_similarity(queryVector, baseVector_B)  # 0.78（示例假设值）

# A 没过阈值 0.72，B 过了 → B 进入候选，A 被拒
admitted = [B]

# 排序阶段：只对已通过的 B，用 enhanced 重新打分
# （A 已经被拒，enhanced 无法挽回）
score_B_enhanced = cosine_similarity(queryVector, enhancedVector_B)  # 仍是 0.78（示例假设值）
```

**结果：** 模型看到"用户对咖啡过敏"，不推荐咖啡 ✅

### 约束：enhanced 只能重排，不能新增

```python
# retrieval.py 的逻辑
admittedIDs = {id for id, score in baseScores.items() if score >= threshold}
enhancedScores = {id: score for id, score in allEnhancedScores.items() if id in admittedIDs}
# enhanced 只能给 admittedIDs 里的记忆调名次，一条都不能新增
```

### Hint 的正确用途

记忆："用户在备考研究生"，hint："研究生备考、复习安排、考研"

用户问："考研复习得怎么样"

1. **准入阶段（base）：** 正文"用户在备考研究生"与"考研复习"语义相关 → base 分数 0.75（示例假设值） → 通过阈值 0.72 ✅
2. **排序阶段（enhanced）：** hint 里的"考研"让分数提升到 0.82（示例假设值） → 在候选里排得更靠前

这就是 hint 该起的作用：**帮助已经相关的记忆排得更高**，而不是让不相关的记忆混进来。

### 实现细节

**数据结构：**

```python
@dataclass(frozen=True)
class MemoryVectorRepresentations:
    base: ndarray      # shape (512,), float32
    enhanced: ndarray  # shape (512,), float32
```

**缓存优化：** 无 hint 时 `enhanced = base`（指向同一对象），缓存只占一份内存。

**编码位置：** `MemoryEncoder.encodeMemoryRepresentations()`（`utils/llm/memory/encoder.py`）

**Runtime 行为：** 新增/修改记忆时，立即失效旧缓存，加入待编码队列；后台 worker 异步编码（不阻塞回复）。

---

## RRF 融合算法

**问题：** 三路的分数尺度不同：
- 语义：余弦相似度 0-1
- 词面：BM25 可达数十

如果直接相加，词面路会因为数值大而主导排序。

**解决方案：** Reciprocal Rank Fusion (RRF) — 只看排名，不看分数。

### 公式

对每条已通过的记忆，合并三路的排名：

```
rrf_score = Σ (1 / (K + rank_i))
```

其中：
- `K = 60`（`LLM_MEMORY_RRF_K`）
- `rank_i` 是该记忆在第 i 路的排名（第 1 名、第 2 名...）

**例子：**

```
记忆 #42 在三路的排名：
  semanticCurrent: 第 3 名
  semanticAssisted: 第 1 名
  lexical: 第 5 名

rrf_score = 1/(60+3) + 1/(60+1) + 1/(60+5)
         = 1/63 + 1/61 + 1/65
         ≈ 0.01587 + 0.01639 + 0.01538
         ≈ 0.04764
```

### 为什么用 RRF？

1. **尺度无关：** 不需要归一化各通道的分数
2. **容错性好：** 某一路全部打低分也不会拖垮其他路
3. **简单高效：** 只需排序，不需要学习权重

### 打平兜底

RRF 分数相同时，用 `(priority, scope_rank, updated_at, id)` 降序兜底（与 Legacy 保持一致）。

实现见 `retrieval.py:selectContextualCandidates()`。

---

## Runtime 增量索引

**职责：** 维护内存中的向量缓存，响应数据库写入事件，周期对账。

### 三个核心机制

#### 1. 增量编码队列

```python
# 记忆写入/修改后
contentFingerprint = computeMemoryContentFingerprint(newContent, tags, hint)
_vectorCache.pop(oldFingerprint, None)  # 立即失效旧缓存
_pendingEncodings.add(memoryID)  # 加入待编码队列

# 后台 worker 异步编码（不阻塞回复）
async def _reconcileWorker():
    while True:
        batch = list(_pendingEncodings)[:32]  # 每批最多 32 条
        vectors = await encode_in_thread(batch)
        for memoryID, representations in vectors.items():
            _vectorCache[memoryID] = representations
            _pendingEncodings.discard(memoryID)
```

**不阻塞回复：** 编码在专门的线程池执行，新记忆写入后立即返回，编码异步完成。

#### 2. 容量感知对账

```python
# 向量缓存预算：32 MiB
CACHE_BUDGET = 32 * 1024 * 1024  # LLM_MEMORY_VECTOR_CACHE_BYTES

# 容量超限时，按 LRU（最久未使用）淘汰旧条目
while cacheSize > CACHE_BUDGET and _lruQueue:
    oldestID = _lruQueue[0]
    _evict(oldestID)

# 容量饱和时，冷记忆采用 best-effort 跳过
if cacheSize > CACHE_BUDGET and not isHotMemory(memoryID):
    # 标记未完全覆盖，但不驱逐已有缓存
    _reconcileCapacitySaturated = True
```

**设计哲学：**
- ✅ 热记忆（最近查询用到的）优先保留
- ✅ 容量饱和时不驱逐已有缓存（宁缺毋滥）
- ❌ 不会无限增长（LRU 保证上限）

#### 3. 30 秒全库巡检

```python
# 每 30 秒对账一轮，分页读取已启用的 contextual 记忆
async def _reconcileWorker():
    while True:
        await asyncio.sleep(30)  # LLM_MEMORY_RECONCILE_SECONDS
        offset = 0
        while True:
            page = await getEnabledContextualMemoryPage(offset, 128)  # LLM_MEMORY_INDEX_PAGE_SIZE
            if not page:
                break
            for memory in page:
                if memory["id"] not in _vectorCache:
                    _pendingIndex.add(memory["id"])
            offset += len(page)
```

**为什么需要巡检？** 增量通知可能因为并发、异常或 Bot 重启丢失，巡检确保最终一致性。

### 资源隔离

- **编码任务：** 在专门的 `ThreadPoolExecutor` 执行（单 worker）
- **ONNX Runtime：** 会话在线程间复用，但推理是串行的
- **峰值内存：** 1000 条带 hint 的记忆约 221 MiB（含编码器、缓存、数据库连接）

### 生命周期

```python
# 启动
await runtime.start()  # 注册到 ResourceManager，优先级 30

# 检索时；返回结果按查询文本排列，每项含 base 准入分和 enhanced 排序分
scores = await runtime.scoreSemantic(
    ["当前用户消息"], candidates, deadline=time.monotonic() + 2.0
)

# 写入后通知
runtime.notifyMemoryChanged(memoryID)

# 关闭
await runtime.close()  # 停止 worker，清空缓存，关闭编码器
```

实现见 `runtime.py`。

---

## LLM Selector（可选）

**本地 vs 远程选择的权衡：**

| 维度 | Local（本地阈值） | LLM Selector（远程） |
|------|-----------------|-------------------|
| 准入逻辑 | 经过 approved calibration 的固定阈值 | 模型理解语义关系 |
| 判断依据 | 向量余弦相似度 | 查询+候选原文+历史 |
| 优势 | 零延迟、零费用、可预测 | 理解复杂指代、对象边界 |
| 劣势 | 无法理解"同对象背景" | 30 秒超时、API 费用 |
| 当前状态 | calibration 未 approved；contextual 通道关闭 | 独立于 calibration，由 selector 验收和运行状态决定 |

### 工作流程

```python
# 1. 三路各取 top 32，取并集（最多 96 条）
candidates = union(
    semanticCurrent[:32],
    semanticAssisted[:32],
    lexical[:32]
)

# 2. 构造匿名请求（防止模型看到数据库 ID）
handles = {f"m{i:03d}": memoryID for i, memoryID in enumerate(candidateIDs)}
payload = {
    "query": {
        "turns": [{"current": "用户当前消息"}],
        "history": [...]
    },
    "candidates": [
        {
            "handle": "m000",
            "content": "...",
            "tags": [...],
            "source": "inferred"
        },
        ...
    ],
    "instructionMarker": generate_random_hex(12)  # 防绕过指令
}

# 3. 调用独立的 LLM API
response = await selector_client.request(
    payload,
    timeout=30,  # LLM_MEMORY_SELECTOR_MAX_SECONDS
)

# 4. 严格校验
validate_response(response, payload, marker)
# - instructionMarker 必须原样返回
# - 返回的句柄必须都在候选列表中
# - primaryOrder/optionalOrder 格式正确

# 5. 恢复数据库 ID
finalIDs = [handles[h] for h in response["primaryOrder"]]
```

### 关键安全设计

1. **匿名句柄：** 模型看不到数据库 ID，防止"记住"特定 ID 的偏好
2. **instructionMarker：** 12 字节随机码，模型必须原样返回，防止绕过指令
3. **严格校验：** 任何不在候选中的句柄都会导致整个请求失败
4. **独立凭据：** selector 有自己的 API key/proxy/超时，不复用主生成链路
5. **只消费 primaryOrder：** optional 字段不注入，防止模型自由添加背景

### 默认配置

```python
# config.py
LLM_MEMORY_SELECTOR_PROTOCOL = "responses"  # 或 "messages"（需 Claude 兼容模型）
LLM_MEMORY_SELECTOR_MODEL = "gpt-5.6-terra"
LLM_MEMORY_SELECTOR_EFFORT = "high"
LLM_MEMORY_SELECTOR_MAX_SECONDS = 30.0
LLM_MEMORY_SELECTOR_TOP_K = 32
```

### 超时问题

早期固定批次的 Messages 测试（2026-09-22）：
- 成功：32/32（0% 超时）
- 中位延迟：5.90 秒
- P99 延迟：9.25 秒

后续使用正式检索入口的 Terra 批次：
- 成功：23/32（28.1% 超时）
- 超时发生在"上传后等待响应头"阶段（trace 证据）

这两组结果说明，固定批次中的传输表现不能代表正式入口的生产稳定性。上线前仍需在目标机器和实际服务条件下单独验收超时率。

实现见 `selector.py` 和 `memorySelection.py`。

---

## 实验数据

### 研究问题

本章记录 Hybrid 从问题定义到上线判断的实验。我们在本章依次探究四个问题：候选池为什么需要扩大、如何评价一次检索、各阶段方案解决了什么、当前哪些条件仍未满足。

Legacy 的候选池先按 `priority` 截断，相关但低优先级的记忆可能根本没有机会参与判断。单独使用 BM25 又会遇到语义断层，例如记忆里写的是“用户在备考研究生”，查询却是“考研复习得怎么样”，两边只共享很少几个词。

实验围绕以下问题展开：

1. 扩大候选池后，系统能否覆盖低优先级但真正相关的记忆；
2. BM25、当前消息语义、带历史辅助的语义，这三条路径应该如何共同提供候选记忆；
3. `hint` 是否会把正文无关的记忆推入候选，而 `base/enhanced` 双表示能否阻止这种情况；
4. 无答案、话题切换、多条 required、真正回指和相反事实，能不能被正确处理；
5. 检索质量、Selector 延迟、超时和内存是否达到上线门槛；
6. 候选进入主回答后，错误记忆会怎样影响最终回答。

### 采用的方法

实验采用“先检索、后回答”的分阶段方法。先构造接近生产规模的候选记忆集，为每个查询人工标注 `required`、`allowed` / `background` 和 `forbidden`，再分别测试候选覆盖、误召回和无答案时的弃权行为。

local Hybrid 使用 BM25、当前消息语义和历史辅助语义扩大候选范围，并用 RRF 合并三路结果；语义检索先用 `base` 表示判断准入，再用包含 `hint` 的 `enhanced` 表示调整已准入记忆的顺序。候选进入主回答前，另用单次 LLM Selector 结合对话上下文复核相关性。最后用未参与调参的 holdout、主回答对照和目标机器上的延迟、超时及内存测试，分别检查质量和运行条件。

### 评价方法与口径

每个测试场景为查询和候选记忆提供人工标注：

| 标注 | 含义 |
|------|------|
| `required` | 回答查询必须召回的记忆 |
| `allowed` / `background` | 可以返回，但不承担回答所需事实的记忆 |
| `forbidden` | 本轮不应返回的记忆，包括无关、冲突或 scope 不匹配的记忆 |

主要指标的含义如下：

- **Precision（准确度）**：返回的 `required`、`allowed` 或 `background` 数量占全部返回数量的比例；背景噪声也计入分母。
- **Required Recall**：需要被召回的记忆的记忆的召回率，即返回的 `required` 数量占该场景全部 `required` 数量的比例。
- **多事实完整率**：需要多条 `required` 的场景中，全部 required 都返回的场景比例。
- **Forbidden hit**：任一场景返回 `forbidden` 即记录命中；local calibration 要求为 0。
- **有效响应率与超时率**：记录 Selector 是否在预算内返回，避免只看成功请求的质量分数。
- **竞争事实裁决**：对“正确事实/错误旧说法”逐条检查，单个总分不能替代这项检查。

当前门槛分为两条路线：local holdout 至少 30 个场景，要求 Precision ≥ 0.95、Required Recall ≥ 0.80、Forbidden hit = 0；LLM Selector 分别按 P ≥ 0.85 和 P ≥ 0.90 两条参考线报告结果，同时要求 Required Recall ≥ 0.65，并逐条裁决竞争事实。质量、有效响应率、延迟和内存需要分别记录，任何一项未达标都不进入生产启用。

### 第一阶段：扩大候选并测试本地阈值

第一版方案用 BM25、当前消息语义和历史辅助语义分别取候选，再通过 RRF 合并。候选池由 `legacy` 的池子扩大后，使用固定本地阈值检验三条路径的准入能否直接形成可用的 local 后端。

| 批次 | Precision | Required Recall | Forbidden | 观察 |
|------|-----------|----------------|-----------|------|
| 旧 66 题 | - | 31/62 = 0.50 | - | 召回不足 |
| 新 138 题 | 0.9286 | 11/163 = 0.0675 | 1 | 固定阈值下召回崩溃 |

固定阈值 `0.72` 在数据集扩大后只召回 6.75% 的 required。扩大候选池解决了“记忆没有进入判断范围”的结构问题，但小型语义模型的绝对分数不足以充当稳定的 local 准入门槛。阈值随候选分布和数据集规模漂移，必须依靠贴近生产分布的数据重新校准。

### 第二阶段：降低 `hint` 对准入的影响

为处理“正文无关、hint 相关”的样本，一条记忆保存两种表示：

- `base = content + tags`：用于判断记忆是否有资格进入候选；
- `enhanced = content + tags + retrievalHint`：只用于已经准入记忆的排序；
- `enhanced` 不能把未通过 `base` 门槛的记忆重新加入候选。

这项改动没有增加生成型 LLM 调用，代价是额外的本地向量、编码任务和校准工作。它修正了 `hint` 影响准入的机制，但它本身不提供质量验收结果；完成表示协议变化后，必须用扩充后的 calibration 数据重新生成阈值，并通过人工批准。

### 第三阶段：用 Selector 处理宽候选

由 local scorer 负责扩大候选范围，LLM Selector 负责在候选原文和对话上下文中判断哪些记忆真正服务于当前问题。当前实现把三路各自的 top 32 取并集，最多提交 96 条候选；Selector 单次请求、无重试，只消费返回的 `primaryOrder`。

#### 固定 Messages 批次

2026-09-22 使用 `claude-opus-4-8` 和 Messages 协议，测试了 32 个主题。其中有 192 条记忆、32 条 required、8 道双 required、8 个竞争项、4 道纯无答案和 4 道仅背景题。预算为 30 秒，单次请求且零重试。

| 指标 | 结果 |
|------|------|
| 成功率 | 32/32 |
| Precision | 34/39 = 0.8718 |
| Required Recall | 32/32 = 1.000 |
| 多事实完整率 | 8/8 |
| 竞争误选 | 3/8 |
| 选择阶段延迟 | 中位 5.90 秒，P99 9.25 秒 |
| 内存峰值 | 221.20 MiB |

该批次达到当时 P ≥ 0.85、Required Recall ≥ 0.65 的参考线，但并没有达到 P ≥ 0.90。竞争误选仍有 3/8，且标注尚未完成人工复核；4 道无答案题不足以评估拒答能力。它说明 Selector 在该固定批次能保住 required，但不足以批准生产启用。

#### 正式检索入口验证

后续测试使用 `retrieveMemoryContext` 正式入口，经过真实 SQLite、解密、候选构造、BGE Runtime 和 Selector 链路，在 Terra medium 上发送 32 次单发请求：23 次有效返回，9 次在 30 秒内超时。

| 指标 | 结果 |
|------|------|
| Precision（有效返回） | 43/44 = 0.9773 |
| 更严格背景口径 Precision | 42/44 = 0.9545 |
| Required Recall | 24/32 = 0.750 |
| 多事实完整率 | 6/8 |
| Forbidden hit | 1 |
| 竞争事实误选 | 1/6 次有效机会 |
| Candidate missing / 最终渲染漏召 | 0 / 0 |

8 条漏掉的 required 全部对应超时请求，质量分数较高的部分不能抵消 28.1% 的超时率和 1 次 forbidden hit。该批次说明候选构造和最终渲染链路已经覆盖 required，Selector 服务稳定性和边界判断还是需要处理。

### 第四阶段：验证主回答对检索结果的影响

主回答对照使用“灯管回收地点”场景：正确事实为“南门值班室专用收集柜”，错误旧说法为“北门快递架”。四个条件各测 2 次：

| 条件 | 注入记忆 | 结果 |
|------|---------|------|
| `correctOnly` | 仅正确事实 | 2/2 回答南门 |
| `wrongOnly` | 仅错误事实 | 2/2 回答北门 |
| `observedPair` | 正确与错误同时存在 | 2/2 回答南门并指出北门错误 |
| `neither` | 无情境事实 | 2/2 不猜地点，建议询问物业 |

这个单案例显示，主回答能够处理明确的更正，但只有错误记忆时会跟随错误事实；没有相关记忆时会选择不猜。它用来说明检索结果怎么影响主回答；更大规模的检索评测还需要单独进行。

早期冲突测试还出现过相反建议、凭空补充未发生的商量结果，以及申请删除正确记忆的情况；后续复测没有重现。这条记录保留了“主回答对错误上下文敏感”这个风险，后续仍然需要增加相反事实和模糊回指样本。

### 失败尝试与中间发现

| 尝试 | 观察 | 结论 |
|------|------|------|
| 只调固定阈值 | 降低阈值能补回部分 required，同时增加错误记忆；P95 组合的 Precision 只有 0.808～0.843 | 不能单靠阈值解决 |
| MiniLM 成对重排 | 默认配置触发 512 MiB 预算中止；串行结果 P=0.9091、R=9/163 | 内存和召回都不满足 |
| ALBERT 抽取式 QA | 单独准入 R=0/163；fusion44 为 P=0.9556、R=37/163 | 召回不足 |
| Jina v2 | 内嵌权重 523.09 MiB；外置后仍未达标 | 暂不采用 |
| 特征与规则补丁 | 浅树 R=0/163；对象排除会误删 allowed；字面片段会排除 required | 表层规则不稳定 |
| 缩小 Selector 候选池、双请求或竞速 | 减少请求量的同时可能移出 required，且没有稳定解决超时 | 不进入生产方案 |

这些结果推动了两项架构决策：用宽候选保留召回机会，用 `base/enhanced` 分离准入和排序，再把 Selector 作为可选的候选判定后端单独验收。

### 当前结论

- Hybrid 的三路候选、RRF 合并、`base/enhanced` 表示和正式入口链路已经完成工程验证。
- local calibration 当前为 `unconfigured`，local 后端 fail-closed，contextual 通道关闭；local Hybrid 尚未达到上线状态。
- Selector 固定 Messages 批次达到 P ≥ 0.85 和 Required Recall ≥ 0.65，但未达到 P ≥ 0.90，竞争误选为 3/8。
- 正式入口批次出现 9/32 超时和 1 次 forbidden hit；当前数据不足以批准 Selector 生产启用。
- 后续验收需要冻结候选规则，准备未查看答案的新 holdout，重新生成 candidate calibration，人工批准后再测目标机器的延迟、内存、缓存对账和关闭流程。

---

## 启用流程

### 前置条件

**需要先安装依赖：**
   ```bash
   pip install -r requirements-memory.txt
   ```

**再安装语义模型：**
   ```bash
   python scripts/llmMemory/memoryModel.py install
   ```

   这个模型的信息是：
   - 仓库：Qdrant/bge-small-zh-v1.5
   - 维度：512
   - 大小：约 95 MiB（model + tokenizer）
   - 安装位置：`.cache/llmMemory/model/`

**之后可以选择验证安装：**
   ```bash
   python scripts/llmMemory/memoryModel.py verify
   ```

### 启用 Hybrid 检索

```bash
# 控制台命令
/llm memory retrieval hybrid
```

或修改 `llmConfig.json`：

```json
{
  "memoryRetrievalMode": "hybrid"
}
```

### 检查状态

```bash
/llm memory status
```

输出形如：

```
记忆检索状态
  模式：hybrid
  校准：不可用（calibrationStatusInvalid）
  记忆条目：启用 42 / 总计 50
  
Runtime 状态
  编码器：就绪
  向量缓存：35/42 contextual（83.3%），13107200 bytes
  队列：query=0，index=3，oldest=120.0 ms
  累计：queryRejected=0，queryTimedOut=0，indexDropped=0，staleResults=0，encodeFailures=0，workerFailures=0
  Native：active=0，blocked=0
  对账容量：正常（reconcileCapacitySaturated=false）
  最近运行时降级：-
  
  当前效果：hybrid 已配置，但检索会降级（calibrationStatusInvalid），只保留常驻记忆
```

这里的缓存覆盖率、队列和累计失败计数描述 Runtime 的索引维护状态，不代表 contextual 记忆已经通过准入；在 `calibrationStatusInvalid` 时，local Hybrid 仍然关闭全部 contextual 通道。

**关键字段解读：**

- **calibrationStatusInvalid：** local 阈值未配置或失效；Hybrid fail-closed，只保留 pinned，不会自动退回 Legacy。LLM Selector 后端独立于 calibration。
- **覆盖率 < 100%：** 部分记忆未编码（容量饱和 / 新增未追上）
- **reconcileCapacitySaturated=true：** 缓存已满（32 MiB），冷条目暂不编码
- **最近运行时降级：** `matrixTooLarge` / `queryTimeout` / `indexQueueFull` 等

### 配置 LLM Selector（可选）

先在 `.env` 填 selector 独立凭据 `LLM_MEMORY_SELECTOR_BASE_URL`（https，无账号密码和查询参数）、`LLM_MEMORY_SELECTOR_API_KEY`，可选 `LLM_MEMORY_SELECTOR_PROXY`，改完重启。

再修改 `data/llm/llmConfig.json`（字段都是平铺的字符串或数字）：

```json
{
  "memoryRetrievalMode": "hybrid",
  "memoryHybridSelector": "llm",
  "memorySelectorProtocol": "messages",
  "memorySelectorModel": "claude-opus-4-8",
  "memorySelectorEffort": "high",
  "memorySelectorTimeoutSeconds": 30.0
}
```

- `memoryHybridSelector` 只能是 `"local"` 或 `"llm"`，模型名填在 `memorySelectorModel`；
- `memorySelectorProtocol` 为 `"responses"` 或 `"messages"`，端点和模型需要支持对应格式；
- `memorySelectorEffort` 为 `low` / `medium` / `high` / `xhigh` / `max` / `ultra`，messages 协议不发送；
- `memorySelectorTimeoutSeconds` 大于 0、不超过 30（`LLM_MEMORY_SELECTOR_MAX_SECONDS`）。

默认值在 `utils/llm/config.py` 的 `_DEFAULT_CONFIG`：`local` / `responses` / `gpt-5.6-terra` / `high` / 30 秒。上面的示例是 32 题验收时用的组合。任一字段不合法，`/llm memory status` 会显示「选择配置无效（selectorConfig），检索返回空结果」，见[故障排查](#故障排查)。

### 回退到 Legacy

```bash
/llm memory retrieval legacy
```

或修改 `llmConfig.json`：

```json
{
  "memoryRetrievalMode": "legacy"
}
```

---

## 验收门槛

验收分为四层：自动化回归、离线评测、目标机部署冒烟、Telegram 人工验收。每层能证明的东西不同，不能互相替代。每次验收都记录日期、代码版本、Python 版本、模型 revision 和结果；没通过的层级保持 `legacy`。

### 通过门槛

两种选择后端的质量门槛不同：

| 项目 | local 后端 | llm 后端 |
| --- | --- | --- |
| 质量 | holdout ≥ 30 个场景：contextual precision ≥ 0.95、required recall ≥ 0.80、forbidden hit = 0；有 pinned 标注时 pinned recall ≥ 0.80 | 同时报告 P ≥ 0.85 与 P ≥ 0.90、required R ≥ 0.65；竞争事实逐项裁决 |
| 时间 | 热态 P95 ≤ 1 秒，单次检索不超过 2 秒 | 选择阶段最多 30 秒；本地候选、最终复核和主回答另计 |
| 调用 | 不新增任何 LLM 调用 | 单次 selector，无重试 |
| calibration | 必须 approved | 不读取 |
| 内存 | 相对基线的新增常驻内存 ≤ 256 MiB；研究阶段可暂测至 512 MiB，但不算生产通过 | 与左侧共享本地 Runtime 预算；selector 的远程请求不改变该生产目标 |

两种后端都要满足：

- 相关 pytest 全部通过，`python scripts/module.py validate` 与 `scan` 通过；
- 新增、更新、删除、禁用、重新启用和 mode 切换后，检索结果与 SQLite 一致：旧向量不能覆盖新内容，删除或禁用的记忆不能复活；
- memory 开关只影响上下文检索，不新增主模型生成调用。

> 事件循环的 heartbeat 目前还没有代码里的正式阈值。目标机上需要记录 P50/P95/P99 和最大间隔；如果出现持续超过 500 ms 的停顿、任务积压不下降，或者主回复被 memory 阻塞，那即使平均值正常也判失败。

### 自动化回归

**必须通过：**

```bash
# 完整测试套件
python scripts/test.py

# Memory 专项测试
pytest tests/utils/llm/memory/
pytest tests/scripts/test_memoryModel.py
pytest tests/scripts/test_evaluateMemory.py

# 模块登记
python scripts/module.py validate
python scripts/module.py scan
```

**覆盖范围：**
- 接口契约、边界、竞态、降级
- 编码器资源基准

**不能证明：** 目标机真实 RSS、模型实际质量、Telegram 网络链路

### 离线评测

**工具：** `scripts/llmMemory/evaluateMemory.py`

**输入：** 人工标注的脱敏 fixture（`tests/utils/llm/memory/fixtures/retrievalCases.json`）

**命令：**

```bash
# 只校验 fixture
python scripts/llmMemory/evaluateMemory.py validate --cases <path>

# 编码器资源基准（默认 1000 条记忆、100 次查询，报告写到 .cache/llmMemory/reports/encoder.json）
python scripts/llmMemory/evaluateMemory.py encoder --memories 1000 --queries 100

# 校准阈值（生成候选文件）
python scripts/llmMemory/evaluateMemory.py calibrate \
  --cases <path> \
  --output .cache/llmMemory/reports/candidateCalibration.json
```

**人工审查：** `calibrate` 产出的只是候选文件。经人工审查后，才能替换 `utils/llm/memory/retrievalCalibration.json`。

**覆盖范围：**
- 查询构造、BM25、语义分数、RRF、字符预算
- 标注集质量

**不能证明：** 生产数据库、runtime 队列、事件循环、完整生命周期

### 目标机部署冒烟

在 staging Bot 上执行，不直接改生产的 `data/llm/llmMemory.db`。

```bash
python scripts/llmMemory/memoryModel.py verify
python bot.py
```

1. **模型加载：** 启动日志没有 ONNX 错误；legacy 下不加载模型，runtime worker 休眠
2. **Runtime 就绪：** `/llm memory status` 显示「Runtime：运行中；编码器已就绪」
3. **索引追赶：** 向量缓存覆盖率逐步接近 100%
4. **资源占用：** 用 `ps` / `htop` 记录峰值，按[通过门槛](#通过门槛)里对应后端的内存要求判断
5. **关闭与重启：** 正常停止后没有挂起线程；重启后 SQLite 记录还在，缓存由后台对账重建

**CRUD 与索引新鲜度：** 在专用 chat 里依次执行下面的操作。每一步都用一次真实对话触发检索，并记下 status 里的 index 队列和缓存计数。命令返回成功不等于索引已经完成。

| 操作 | 通过条件 |
| --- | --- |
| 新增一条带独特新词的 contextual 记忆 | llm 后端：索引完成前可经词面通道进入 selector 候选池，完成后同义表达也能进入候选池，最终是否选中由模型决定。local 后端（已 approved）：只有过了对应通道阈值才进入；lexical 阈值为 null 时词面通道关闭 |
| 对同一 ID 快速连续改为 A、B、C | 最终只有 C 生效，A/B 不会迟到覆盖 |
| 删除一条已缓存的记忆 | 用旧词查询不再召回 |
| `edit -enabled off`，查询后再 `-enabled on` | 禁用期间不进候选；启用后补回索引 |
| contextual ↔ pinned 切换 | 转成 pinned 后退出语义竞争、进入常驻段；转回后重新按相关性竞争 |

> local 后端在 calibration 未 approved 时是 fail-closed：contextual 通道缺席，只保留 pinned。这个阶段只能验证失败关闭、CRUD、生命周期和 prompt 安全，不能据此宣称词面或语义质量达标。llm 后端不读 calibration，不受这条限制。

**不能证明：** 人工回复是否真正符合产品预期

### Telegram 人工验收

**测试场景：**

1. **回指：** "还是那里吧" → 能找到"用户通常在萨莉亚约饭"
2. **话题切换：** 跨轮次讨论，历史辅助理解
3. **写入审核：** 模型申请记忆操作 → 审核 → 执行
4. **Prompt 质量：** 记忆注入后，回复是否符合预期
5. **Ops 操作：** `/llm memory add`、`/llm memory edit`、`/llm memory del`
6. **Scope 与预算：** 用两个 chat、两个 user 分别建记忆，确认：
   - 只读到 global 和当前 chat/user/session 的记忆（Telegram 链路不传 session ID，实际只有前三类），其他 chat 的高 priority 记忆进不来；
   - disabled 记忆不进 prompt；pinned 段不超过 1000 字符，整块不超过 2500 字符，超长条目整条跳过；
   - `retrievalHint` 不出现在 `<UNTRUSTED_MEMORY>` 块里；
   - 模型的 chat/user 操作必须匹配当前请求身份；开启自动批准后只有普通 global contextual 自动执行，retry / `:fb` 和 pinned 仍需审核。

**不能证明：** 大规模统计质量（人工场景不能替代 holdout）

### 故障注入矩阵

故障逻辑由现有 pytest 覆盖，目标机只观察降级语义、日志和状态展示，**不宜**在生产进程里改代码或数据库。测试都在 `tests/utils/llm/memory/` 下。

| 故障 | 自动化测试 | 目标机观察点 | 正确结果 |
| --- | --- | --- | --- |
| manifest 无效 / 模型缺失 | `test_memoryRuntime::test_invalidManifestDegradesWhenEncoderIsActuallyRequested` | status 的「最近运行时降级」（hybrid 下首次需要编码器时才触发，legacy 下不会出现） | 编码器不可用，语义通道缺席 |
| 语义查询队列满 | `test_memoryRuntime::test_query_queue_limit_rejects_without_unbounded_executor_submission` | `queryRejected` | 语义分数为空，主回复继续 |
| 索引队列满 | `test_memoryRuntime::test_index_queue_limit_is_bounded` | `indexDropped` | 队列有上限，对账在容量允许时补回 |
| 缓存预算满 / 单矩阵过大 | `test_memoryRuntime::test_lru_byte_limit_and_reconcile_publish_does_not_evict`、`test_publish_rejects_matrix_larger_than_cache_budget` | `reconcileCapacitySaturated`、`blocked` | 对账不驱逐热缓存，过大条目进 blocked |
| 词面通道异常 / 超时 | `test_retrieval::test_lexicalFailureKeepsPinnedMemory`、`test_lexicalTimeoutKeepsFinalizeBudgetForPinned` | 「LLM memory 检索」日志里的 `degradedReasons`（status 不显示） | 保留 pinned，不抛到主链路 |
| 语义超时 | `test_memoryRuntime::test_query_timeout_keeps_native_job_active_until_it_really_finishes` | status 的 `queryTimedOut`、「Native：active」 | 底层作业真正结束才释放名额 |
| 候选读库超时 | `test_retrieval::test_timeout_keeps_retrieval_slot_until_thread_finishes` | 「LLM memory 检索」日志里的 `retrievalTimeout` | 读库线程真正结束才释放检索名额 |
| 对账读库失败 | `test_memoryRuntime::test_reconcile_read_error_preserves_partial_scan_and_cache` | status 的「最近运行时降级」显示 `reconcileReadFailed` | 保留已有缓存，不清空 |
| worker 单轮异常 | `test_memoryRuntime::test_worker_recovers_after_unexpected_iteration_error` | `workerFailures` | 退避后继续工作 |
| 编码期间更新 / 删除 | `test_memoryRuntime::test_stale_encode_result_is_rejected`、`test_delete_or_disable_invalidates_cached_matrix` | 新旧事实的查询结果 | 旧矩阵丢弃，删除的记忆不复活 |
| 关闭时 native 作业仍在跑 | `test_memoryRuntime::test_cancelled_native_wrapper_waits_for_underlying_thread`、`test_close_waits_for_native_job_then_closes_encoder_and_clears_state` | 关闭时长 | 等作业结束再释放编码器 |
| selector 配置无效 | `test_retrieval::test_llmInvalidSelectorConfigDoesNotSend` | status 的「当前效果」 | 不发远程请求，诊断记 `selectorConfig` |

诊断和日志只能记原因码，不能出现查询正文、记忆正文、hint、向量或密钥；出现了按安全问题处理。

### 回滚与停止条件

出现以下任一项，停止 hybrid 灰度，回到 `legacy`：

- 错误 scope 或错误的 contextual 记忆进入 prompt，或 holdout forbidden hit 非零；
- 质量、内存、延迟任一项未达到[通过门槛](#通过门槛)；
- worker 不能自恢复，或关闭不干净；
- 删除或禁用的记忆仍被召回，或旧向量覆盖了新内容；
- memory 让主模型多发一次生成请求；
- 分隔符、hint、正文或加密字段泄露；
- （local 后端）calibration 绑定的模型 revision、编码版本或 lexical version 与当前不一致。

回滚步骤：

1. 在受信任的 console 执行 `/llm memory retrieval legacy`，确认 status 显示 legacy。
2. 保存 status、诊断和日志，暂停写入新的验收数据。
3. **不删除** SQLite，也**不用缓存状态反向改记忆**；SQLite 是唯一正本。
4. 还有 native 作业在跑时，按正常流程停机，等 runtime 关闭完成再重启。
5. 问题出在 calibration 时，恢复为 `unconfigured`，不要把失败的候选标成 approved。
6. 修复后从自动化回归重新开始，不要直接跳回 Telegram 灰度。

---

## 故障排查

### 问题：检索一直降级

**症状：** `/llm memory status` 显示 `当前效果：hybrid 已配置，但检索会降级（...）`

**排查步骤：**

1. **检查校准状态：**
   ```bash
   /llm memory status
   ```
   看 `校准：` 那一行的原因码。

2. **常见原因码：**

   | 原因码 | 含义 | 解决方案 |
   |--------|------|---------|
   | `calibrationStatusInvalid` | 校准文件未处于 `approved` 状态 | 运行 `calibrate`，人工检查后再替换配置文件 |
   | `calibrationModelMismatch` | 模型 revision 不匹配 | 重新校准或回退模型 |
   | `calibrationEncodingMismatch` | encodingVersion 不匹配 | 重新校准 |
   | `calibrationLexicalMismatch` | `LEXICAL_VERSION` 不匹配 | 重新校准 |
   | `calibrationRepresentationMismatch` | base/enhanced 表示协议不匹配 | 使用当前代码重新校准 |
   | `calibrationDatasetMissing` | 校准数据集文件不存在 | 检查文件路径 |

3. **检查编码器：**
   - `/llm memory status` 显示「Runtime：运行中；编码器未就绪」→ 模型未就绪，运行 `memoryModel.py install`
   - `/llm memory status` 显示「Runtime：未注册」→ Runtime 未初始化，检查日志

### 问题：覆盖率 < 100%

**症状：** `/llm memory status` 显示 `向量缓存：35/42 contextual（83.3%）`

**原因：**

1. **新增记忆未追上：** 索引异步编码，等待 30 秒对账
2. **容量饱和：** `reconcileCapacitySaturated=true`，缓存已满（32 MiB）

**解决方案：**

- **等待对账：** 30 秒后自动补齐（容量允许时）
- **容量饱和：** 删除/禁用不常用记忆，或重启 Bot（清空缓存，LRU 重新选择）

### 问题：查询超时

**症状：** `/llm memory status` 显示 `timeout: 2`

**原因：** 查询在 2 秒内未完成（`LLM_MEMORY_RETRIEVAL_TIMEOUT_SECONDS=2.0`）

**排查步骤：**

1. **检查候选数：** 超过 8192 条会触发 `matrixTooLarge`
2. **检查 CPU 负载：** 编码器在 CPU 上运行，高负载会拖慢
3. **检查日志：** 搜索 `memoryTimeout` / `queryTimeout`

> 其有**临时缓解**办法：若是 Runtime 语义异常，pinned 和 lexical 仍可用；若是 `calibrationStatusInvalid`，local Hybrid 会关闭全部 contextual 通道，只保留 pinned。需要完整可用性时先回退到 Legacy，或配置已批准的校准文件。

### 问题：选择配置无效（selectorConfig）

**症状：** `/llm memory status` 显示「选择后端：xxx（配置无效：selectorConfig）」，当前效果为「选择配置无效（selectorConfig），检索返回空结果」。

**原因：** `llmConfig.json` 里的 selector 字段有一项不合法（`utils/llm/config.py:getMemorySelectorSettings`）。最常见的是把模型名填进了 `memoryHybridSelector`，它只接受 `"local"` 或 `"llm"`，模型名应该填在 `memorySelectorModel`。其余字段的允许值见[配置 LLM Selector](#配置-llm-selector可选)。

**影响：** 检索在读库之前就返回，连常驻记忆也不注入。主回复照常生成。

### 问题：selector 凭据未设置

**症状：** 选择后端一行显示「凭据未设置」，当前效果为「selector 凭据未设置，只保留常驻记忆」。

**处理：** 在 `.env` 补上 `LLM_MEMORY_SELECTOR_BASE_URL` 和 `LLM_MEMORY_SELECTOR_API_KEY`，然后重启 Bot。环境变量只在启动时读取。

### 问题：LLM Selector 超时

**症状：** 选择阶段耗时 > 30 秒

**排查步骤：**

1. **检查网络：** curl 测试 API 端点
2. **检查模型：** 端点是否支持所选协议和模型
3. **检查日志：** 搜索 `selectorTimeout` / `selectorTransportTimeout`

> **临时缓解：** `/llm memory retrieval legacy` 回退到 legacy。改成 `memoryHybridSelector: "local"` 没有用：calibration 未批准时，local 后端只保留常驻记忆。

---

## 扩展阅读

- **[LLM Memory 主文档](llm-memory.md)** — 面向所有用户的使用指南
- 冒烟测试、部署、灰度与回滚检查属于当前仓库的测试材料，本页不展开
- 历史实验材料已归档，本页只保留可复核的结论和指标
