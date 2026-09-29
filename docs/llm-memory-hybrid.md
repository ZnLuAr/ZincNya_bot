# LLM Memory Hybrid 检索详细文档

> 最后更新：2026-09-28
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

最先的 Legacy 检索有两个致命缺陷：

### 1. 候选缺失（Candidate Missing）

旧逻辑中，候选记忆按 `priority` 降序排列，每个 scope 只取前 20 条，汇总后取前 10 条。这会导致一条与当前消息高度相关的低优先级记忆，会被高优先级但无关的记忆挤出候选池，**根本没有机会参与相关性判断**。

**举例来说：**
```
数据库里有 100 条记忆：
- 90 条 priority=3 的核心身份/安全约束（pinned）
- 10 条 priority=0 的用户偏好（contextual）

用户问："我对什么过敏？"
相关记忆："用户对青椒严重过敏"（priority=0）

Legacy 检索：前 10 条全被 priority=3 占据，这条过敏相关的记忆根本没进候选池。
```

### 2. 语义断层（Semantic Gap）

**问题：** 旧逻辑只用 BM25 词面匹配（基于中文 2-gram）。

**后果：** 当用户换了一种说法，但意思相同时，词面匹配完全失效。

**例子：**
```
记忆："用户在备考研究生"
查询："考研复习得怎么样"

BM25 tokens:
  记忆: ["用户", "户在", "在备", "备考", "考研", "研究", "究生", "生"]
  查询: ["考研", "研复", "复习", "习得", "得怎", "怎么", "么样", "样"]
  共同: ["考研"] （1 个 token）

分数很低，可能被其他记忆挤掉。
```

---

## 设计约束

Hybrid 检索必须在以下约束内解决问题：

| 约束 | 数值 | 原因 |
|------|------|------|
| **不新增 LLM 调用** | 0 次 | 鲁棒性/延迟不可接受 |
| **不使用外部 embedding API** | - | 依赖外部服务、隐私风险 |
| **本地 CPU 模型** | ≤ 256 MiB | 服务器内存预算 |
| **热态 P95 延迟** | ≤ 1 秒 | 用户体验要求 |
| **请求最多等待** | 2 秒 | 超时降级到 pinned only |
| **Memory 块字符数** | ≤ 1500 | Prompt 预算（pinned 500 + contextual 1000） |

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

**实现：** `contextBuilder.py:buildQueryTexts` 用近期历史辅助构造查询（详见代码注释）。

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
lexicalPassed = (score > 0)  # 词面只要正分就算过

admitted = semanticCurrentPassed or semanticAssistedPassed or lexicalPassed
```

**三路任一通过即准入** — 这保证了：
- 语义相关但词面不重叠的记忆（如"考研" vs "备考研究生"）能通过语义路
- 精确关键词匹配的记忆（如"花生过敏"）能通过词面路
- 冷启动时语义编码器未就绪，词面路仍能工作

**当前状态（2026-09-28）：**
- `retrievalCalibration.json` 的 `status` 为 `unconfigured`，所有阈值为 `null`
- 本地阈值校准未完成（召回仅 0.0675，不可用）
- 详见 [实验数据 - 本地阈值路线](#本地阈值路线)

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

**编码位置：** `encoder.py:_encodeMemoryRepresentations`

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

实现见 `retrieval.py:_selectAndRankContextualMemories`。

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

# 检索时
scores = await runtime.scoreSemantic(queryVector, candidates, representation="base")

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
| 准入逻辑 | 固定阈值 0.72 | 模型理解语义关系 |
| 判断依据 | 向量余弦相似度 | 查询+候选原文+历史 |
| 优势 | 零延迟、零费用、可预测 | 理解复杂指代、对象边界 |
| 劣势 | 无法理解"同对象背景" | 30 秒超时、API 费用 |
| 当前状态 | 未完成校准（R=0.0675） | 已验收（P=0.87, R=0.75） |

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

**Messages 32 题验收（2026-09-22）：**
- 成功：32/32（0% 超时）
- 中位延迟：5.90 秒
- P99 延迟：9.25 秒

**早期 Terra 批次（旧数据）：**
- 成功：23/32（28.1% 超时）
- 超时发生在"上传后等待响应头"阶段（trace 证据）

**结论：** 超时是传输/服务问题，不能归因于模型理解能力。Messages 协议 + Claude Opus 4.8 的组合已证明可行。

实现见 `selector.py` 和 `memorySelection.py`。

---

---

## 实验数据

### Messages 32 题最终验收（2026-09-22）

**配置：**
- 模型：claude-opus-4-8
- 协议：Messages
- 候选池：三路各 top 32 并集
- 预算：30 秒
- 重试：单次，零重试

**素材：**
- 32 个主题
- 192 条记忆
- 32 条 required（必须召回）
- 8 道双 required（需要多条记忆）
- 8 个竞争项（正确 vs 错误旧说法）
- 4 道纯无答案
- 4 道仅背景

**核心结果：**

| 指标 | 结果 | 说明 |
|------|------|------|
| 成功率 | 32/32 (100%) | 零超时 |
| Precision | 34/39 = 0.8718 | 5 条背景噪声 |
| Required Recall | 32/32 = 1.000 | 零漏召 |
| 多事实完整率 | 8/8 = 1.000 | 需要多条记忆的问题全部齐全 |
| 竞争误选 | 3/8 | 8 次有效竞争机会，误选 3 次 |
| 延迟（中位） | 5.90 秒 | 仅选择阶段 |
| 延迟（P99） | 9.25 秒 | - |
| 内存峰值 | 221.20 MiB | 符合 256 MiB 预算 |

**验收标准对照：**

| 标准 | 要求 | 实际 | 状态 |
|------|------|------|------|
| Precision | 0.85-0.90 | 0.8718 | ✅ 达到 0.85 |
| Required Recall | ≥ 0.65 | 1.000 | ✅ 超过 |
| 超时率 | 应降低 | 0% | ✅ |
| 竞争误选 | 逐条审查 | 3/8 | ⚠️ 已接受本批，需复核 |

**分项指标：**

| 子类 | Precision | Recall | 说明 |
|------|-----------|--------|------|
| 直接问句 | 10/12 = 0.8333 | 10/10 | 2 条背景噪声 |
| 指代/追问 | 8/9 = 0.8889 | 8/8 | 1 条背景噪声 |
| 话题切换 | 8/9 = 0.8889 | 8/8 | 1 条背景噪声 |
| 历史辅助 | 8/9 = 0.8889 | 6/6 | 1 条背景噪声 |

**未解决问题：**
1. 标注仍为 draft，未经人工复核
2. 只有 4 道无答案题，样本不足以估误召率
3. 0/32 超时不等于生产零超时
4. 只覆盖显式更正，未测试模糊时间、同名对象

完整数据见 [calibration-expansion](llm-memory-calibration-expansion.md) 和 [hybrid-research 归档](archive/llm-memory-hybrid-research-2026-09.md)。

### 三例冲突的主回答裁决（2026-09-25）

**实验设计：** 检验正确与错误事实同时存在时，主回答能否消解冲突。

**场景：** 灯管回收地点
- 正确答案："南门"
- 错误旧说法："北门"

**四个条件各测 2 次：**

| 条件 | 注入记忆 | 结果 |
|------|---------|------|
| `correctOnly` | 仅正确更正 | 2/2 答"南门" ✅ |
| `wrongOnly` | 仅错误旧说法 | 2/2 答"北门" ❌ |
| `observedPair` | 双事实同时存在 | 2/2 答"南门"并指出"北门是写错的" ✅ |
| `neither` | 无情境记忆 | 2/2 说"不知道，建议问物业" ✅ |

**结论：**
- ✅ 正确证据在场时，模型能消解冲突
- ❌ 仅错误记忆时，模型会给出错误答案
- **检索质量直接决定主回答质量**

**发现的问题（09-21 早期测试）：**
- 回退冲突场景：同一请求给出相反建议
- 编造决定："商量过填江城"（实际未商量）
- 申请删除正确记忆：8100141（正确的出生地记忆）

这些问题在 09-25 复测时未重现，但说明检索精度对主回答的影响极大。

### 本地阈值路线（未完成）

| 批次 | Precision | Required Recall | Forbidden | 状态 |
|------|-----------|----------------|-----------|------|
| 旧 66 题 | - | 31/62 = 0.50 | - | 召回不足 |
| 新 138 题 | 0.9286 | 11/163 = 0.0675 | 1 | **召回崩溃** |

**问题分析：**
- 固定阈值 0.72 在扩充数据集后召回只有 6.75%
- 说明标注数据分布与模型分数分布不匹配
- 需要重新校准阈值，或调整数据集

**当前决策：** Local 路线暂停，不能要求 LLM selector 等 local 校准完成。

### 负面结果（走不通的方案）

以下方案都未能同时满足 P ≥ 0.85 和 R ≥ 0.65：

**换模型：**
- MiniLM 成对重排：默认配置触发 512 MiB 预算中止；串行跑完后 P=0.9091、R=9/163，比 dense 基线差
- 抽取式 QA（ALBERT）：单独准入全拒，R=0/163；fusion44 P=0.9556、R=37/163
- Jina v2：内嵌权重 523.09 MiB 超限；外置后最好情况也未达标

**阈值规则变体：**
- 最高桶专用判断：8 个组合无一达标
- 补低分记忆：同样
- 额外 precision 支持：竞争降为 0，但 R 只有 34/62、26/62

**特征与规则：**
- 浅树：全部关闭，R=0/163
- Anchor28：R 68→62
- Token32：短题上 F=2~14，比 dense24 的 F=1~9 更差
- 明示对象排除规则：误删 allowed，还有 60/60 条件残留错对象
- 查询开头字面片段：排除 22 条 R

**降阈值效果：**
- 放宽 t-0.01：R 68→98、F 5→9（看似改善）
- 全开后旧 138 四组的 R 都只剩 56/163（反而变差）
- P95 时 P 只有 0.808~0.843，每组 5 个确认近邻

**结论：** 简单调参、换模型、规则补丁都无法解决问题，需要从架构层面改进（即 Hybrid 混合检索）。

---

## 启用流程

### 前置条件

1. **安装依赖：**
   ```bash
   pip install -r requirements-memory.txt
   ```

2. **安装语义模型：**
   ```bash
   python scripts/llmMemory/memoryModel.py install
   ```

   模型信息：
   - 仓库：Qdrant/bge-small-zh-v1.5
   - 维度：512
   - 大小：约 95 MiB（model + tokenizer）
   - 安装位置：`.cache/llmMemory/model/`

3. **验证安装：**
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

输出示例：

```
记忆检索状态
  模式：hybrid
  校准：不可用（calibrationStatusInvalid）
  记忆条目：启用 42 / 总计 50
  
Runtime 状态
  编码器：就绪
  向量缓存：35 / 42 覆盖（覆盖率 83.3%），12.5 MiB
  队列：query 0 queued, index 3 pending, oldest 120 ms
  累计计数：queries 156, admissions 42, semantic 38, lexical 35, degraded 2, timeout 0
  活跃任务：0 native jobs
  对账容量：正常（reconcileCapacitySaturated=false）
  最近运行时降级：-
  
  当前效果：hybrid 已配置，但检索会降级（calibrationStatusInvalid）
```

**关键字段解读：**

- **calibrationStatusInvalid：** 阈值未配置，会降级（只保留 pinned + lexical）
- **覆盖率 < 100%：** 部分记忆未编码（容量饱和 / 新增未追上）
- **reconcileCapacitySaturated=true：** 缓存已满（32 MiB），冷条目暂不编码
- **最近运行时降级：** `matrixTooLarge` / `queryTimeout` / `indexQueueFull` 等

### 配置 LLM Selector（可选）

修改 `llmConfig.json`：

```json
{
  "memoryRetrievalMode": "hybrid",
  "memoryHybridSelector": {
    "enabled": true,
    "protocol": "messages",
    "model": "claude-opus-4-8",
    "effort": "high"
  }
}
```

**注意：** Messages 协议需要 Claude 兼容模型（Opus/Sonnet）。

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

### 自动化回归

**必须通过：**

```bash
# 完整测试套件
python scripts/test.py

# Memory 专项测试
pytest tests/utils/llm/memory/
pytest tests/scripts/test_memoryModel.py
pytest tests/scripts/test_evaluateMemory.py
```

**覆盖范围：**
- 接口契约、边界、竞态、降级
- 编码器资源基准
- 不增加生成调用

**不能证明：** 目标机真实 RSS、模型实际质量、Telegram 网络链路

### 离线评测

**工具：** `scripts/llmMemory/evaluateMemory.py`

**输入：** 人工标注的脱敏 fixture（`tests/utils/llm/memory/fixtures/retrievalCases.json`）

**命令：**

```bash
# 只校验 fixture
python scripts/llmMemory/evaluateMemory.py validate --cases <path>

# 编码器资源基准
python scripts/llmMemory/evaluateMemory.py encoder --cases <path>

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

**启动 Bot：**

```bash
python bot.py
```

**检查项：**

1. **模型加载：** 启动日志无 ONNX 错误
2. **Runtime 就绪：** `/llm memory status` 显示 `encoderReady: true`
3. **索引追赶：** 覆盖率逐步接近 100%
4. **资源占用：** 峰值内存 < 256 MiB（可用 `ps`/`htop` 观测）
5. **关闭干净：** `Ctrl+C` 后无挂起线程

**覆盖范围：**
- 固定模型在目标 Python/CPU 上能启动
- 索引能追赶、资源和关闭行为可接受

**不能证明：** 人工回复是否真正符合产品预期

### Telegram 人工验收

**测试场景：**

1. **回指：** "还是那里吧" → 能找到"用户通常在萨莉亚约饭"
2. **话题切换：** 跨轮次讨论，历史辅助理解
3. **写入审核：** 模型申请记忆操作 → 审核 → 执行
4. **Prompt 质量：** 记忆注入后，回复是否符合预期
5. **Ops 操作：** `/llm memory add`、`/llm memory edit`、`/llm memory del`

**覆盖范围：**
- 端到端流程、Prompt 质量、Operator 操作闭环

**不能证明：** 大规模统计质量（人工场景不能替代 holdout）

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
   | `calibrationStatusInvalid` | 阈值未配置 | 运行 `calibrate` 并替换配置文件 |
   | `calibrationModelMismatch` | 模型 revision 不匹配 | 重新校准或回退模型 |
   | `calibrationEncodingMismatch` | encodingVersion 不匹配 | 重新校准 |
   | `calibrationDatasetMissing` | 校准数据集文件不存在 | 检查文件路径 |

3. **检查编码器：**
   - `encoderReady: false` → 模型未就绪，运行 `memoryModel.py install`
   - `runtimeStatus: null` → Runtime 未初始化，检查日志

### 问题：覆盖率 < 100%

**症状：** `/llm memory status` 显示 `向量缓存：35 / 42 覆盖（覆盖率 83.3%）`

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

**临时缓解：** 降级只影响语义通道，pinned 和 lexical 仍可用。

### 问题：LLM Selector 超时

**症状：** 选择阶段耗时 > 30 秒

**排查步骤：**

1. **检查网络：** curl 测试 API 端点
2. **检查模型：** 是否为支持的模型
3. **检查日志：** 搜索 `selectorTimeout` / `selectorTransportTimeout`

**临时缓解：** 关闭 selector（`memoryHybridSelector.enabled=false`），降级到本地阈值。

---

## 扩展阅读

- **[LLM Memory 主文档](llm-memory.md)** — 面向所有用户的使用指南
- **[Calibration Expansion](llm-memory-calibration-expansion.md)** — 校准扩充与检索研究
- **[Smoke Test](llm-memory-smoke-test.md)** — 完整验收方案
- **[Hybrid Research 归档](archive/llm-memory-hybrid-research-2026-09.md)** — 完整实验数据（4400+ 行）

---

**文档版本：** 2026-09-28  
**对应代码：** commit `8e524bb` (feat(memory): add hybrid selector and base/enhanced representations)