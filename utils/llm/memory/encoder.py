"""
utils/llm/memory/encoder.py

把文本变成向量的本地模型封装：加载 ONNX 模型、切词、跑推理、输出向量。

用哪个模型钉死在 modelManifest.json（哪个仓库、哪个 commit、每个文件
的 SHA-256）——下载和加载都按这份清单核对，不存在「装了个别的版本」。
numpy / onnxruntime / tokenizers 这三个依赖平时不装（普通 bot 用不到
语义检索），到真正创建 MemoryEncoder 时才 import；缺了就抛
MemoryEncoderUnavailable，不影响 bot 其他功能。

注意所有方法是同步阻塞的（跑一次推理就是占着线程跑完），runtime
会把它放进专门的工作线程执行——别在 asyncio 事件循环里直接调。
"""

import json
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from config import (
    LLM_MEMORY_CHUNK_OVERLAP,
    LLM_MEMORY_ENCODING_MAX_TOKENS,
    LLM_MEMORY_MODEL_DIR,
    LLM_MEMORY_MODEL_MANIFEST_PATH,
)




class MemoryEncoderError(RuntimeError):
    """模型清单、编码契约或 ONNX 输出不符合预期。"""


class MemoryEncoderUnavailable(MemoryEncoderError):
    """可选依赖或固定模型产物缺失，当前无法创建编码器。"""




@dataclass(frozen=True)
class MemoryVectorRepresentations:
    """一条记忆的两种 chunk 向量表示。

    base 只来自正文与标签，是语义通道唯一允许用于准入的表示；enhanced
    额外包含 retrievalHint，只能给已经通过 base 阈值的记忆调整名次。
    没有有效 hint 时两个字段会引用同一矩阵，避免重复编码和缓存占用。
    """

    base: object
    enhanced: object




def loadModelManifest(manifestPath: str | Path = LLM_MEMORY_MODEL_MANIFEST_PATH) -> dict:
    """读取并完整校验固定模型清单，不接受不安全的产物路径。

    模块内多处调用（runtime、脚本、calibration 绑定），是模型身份的
    唯一权威来源；清单问题在此处拦截，不遗留到加载 ONNX 时才暴露。
    可选 modelFile 只能指向清单内的安全相对路径；省略时沿用旧文件名。
    """
    path = Path(manifestPath)
    with path.open("r", encoding="utf-8") as manifestFile:
        manifest = json.load(manifestFile)

    if not isinstance(manifest, dict):
        raise MemoryEncoderError("模型清单必须是 JSON 对象")

    requiredKeys = {
        "schemaVersion",
        "repository",
        "revision",
        "encodingVersion",
        "queryPrefix",
        "maxTokens",
        "embeddingDimension",
        "pooling",
        "normalization",
        "specialTokens",
        "artifacts",
    }
    missingKeys = requiredKeys.difference(manifest)
    if missingKeys:
        raise MemoryEncoderError(
            f"模型清单缺少字段: {', '.join(sorted(missingKeys))}"
        )
    if manifest["schemaVersion"] != 1:
        raise MemoryEncoderError("不支持的模型清单版本")

    for key in ("repository", "revision", "encodingVersion", "queryPrefix"):
        if not isinstance(manifest[key], str) or not manifest[key].strip():
            raise MemoryEncoderError(f"模型清单字段无效: {key}")

    for key in ("maxTokens", "embeddingDimension"):
        value = manifest[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MemoryEncoderError(f"模型清单字段无效: {key}")

    if manifest["pooling"] != "cls":
        raise MemoryEncoderError("当前编码器只支持 pooling=cls")
    if manifest["normalization"] != "l2":
        raise MemoryEncoderError("当前编码器只支持 normalization=l2")

    specialTokens = manifest["specialTokens"]
    if not isinstance(specialTokens, dict):
        raise MemoryEncoderError("模型清单字段无效: specialTokens")
    for key in ("cls", "sep", "pad"):
        if not isinstance(specialTokens.get(key), str) or not specialTokens[key].strip():
            raise MemoryEncoderError(f"模型清单特殊 token 无效: {key}")

    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise MemoryEncoderError("模型清单没有产物")

    seenPaths = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise MemoryEncoderError("模型清单产物必须是 JSON 对象")
        artifactPath = artifact.get("path")
        if not isinstance(artifactPath, str) or not artifactPath.strip():
            raise MemoryEncoderError("模型清单产物路径不能为空")
        try:
            normalizedPath = resolveModelArtifactPath(".", artifactPath).as_posix()
        except MemoryEncoderError:
            raise MemoryEncoderError(f"模型产物路径不安全: {artifactPath}")
        if normalizedPath in seenPaths:
            raise MemoryEncoderError(f"模型清单产物路径重复: {artifactPath}")
        seenPaths.add(normalizedPath)

        size = artifact.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise MemoryEncoderError(f"模型清单产物大小无效: {artifactPath}")
        digest = artifact.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MemoryEncoderError(f"模型清单产物 SHA-256 无效: {artifactPath}")

    if "modelFile" in manifest:
        modelFile = manifest["modelFile"]
        if not isinstance(modelFile, str) or not modelFile.strip():
            raise MemoryEncoderError("模型清单字段无效: modelFile")
        # 允许选择嵌套的模型产物，但不能绕过清单既有的大小和散列校验。
        modelPath = resolveModelArtifactPath(".", modelFile).as_posix()
        if modelPath not in seenPaths:
            raise MemoryEncoderError("modelFile 必须属于模型清单 artifacts")
    return manifest


def resolveModelArtifactPath(modelDir: str | Path, artifactPath: str) -> Path:
    """将清单相对路径解析到模型目录内，并拒绝绝对路径与目录穿越。"""
    relativePath = Path(str(artifactPath))
    if relativePath.is_absolute() or relativePath.drive or ".." in relativePath.parts:
        raise MemoryEncoderError(f"模型产物路径不安全: {artifactPath}")

    rootPath = Path(modelDir).resolve()
    resolvedPath = (rootPath / relativePath).resolve()
    if resolvedPath != rootPath and rootPath not in resolvedPath.parents:
        raise MemoryEncoderError(f"模型产物路径越界: {artifactPath}")
    return resolvedPath


def calculateFileSha256(filePath: str | Path) -> str:
    """流式计算模型产物的 SHA-256，避免一次性读取大文件。"""
    digest = hashlib.sha256()
    with Path(filePath).open("rb") as artifactFile:
        while chunk := artifactFile.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def inspectModelArtifacts(
    modelDir: str | Path = LLM_MEMORY_MODEL_DIR,
    manifestPath: str | Path = LLM_MEMORY_MODEL_MANIFEST_PATH,
) -> list[dict[str, Any]]:
    """逐项报告固定模型产物是否存在，以及大小和散列是否匹配。"""
    manifest = loadModelManifest(manifestPath)
    statuses = []
    for artifact in manifest["artifacts"]:
        artifactPath = resolveModelArtifactPath(modelDir, artifact.get("path", ""))
        expectedSize = int(artifact.get("size", -1))
        expectedHash = str(artifact.get("sha256", "")).lower()
        status = {
            "path": str(artifact.get("path", "")),
            "exists": artifactPath.is_file(),
            "sizeValid": False,
            "hashValid": False,
        }
        if status["exists"]:
            status["sizeValid"] = artifactPath.stat().st_size == expectedSize
            if status["sizeValid"]:
                status["hashValid"] = calculateFileSha256(artifactPath) == expectedHash
        status["valid"] = status["exists"] and status["sizeValid"] and status["hashValid"]
        statuses.append(status)
    return statuses


def requireModelArtifacts(
    modelDir: str | Path = LLM_MEMORY_MODEL_DIR,
    manifestPath: str | Path = LLM_MEMORY_MODEL_MANIFEST_PATH,
) -> None:
    """要求全部模型产物校验通过，否则抛出不可用错误。"""
    invalidPaths = [
        status["path"]
        for status in inspectModelArtifacts(modelDir, manifestPath)
        if not status["valid"]
    ]
    if invalidPaths:
        raise MemoryEncoderUnavailable(
            f"本地 memory 模型缺失或校验失败: {', '.join(invalidPaths)}"
        )




class MemoryEncoder:
    """语义编码器：输入文本，输出归一化向量；全进程仅一份。

    ONNX 配置有意偏向「低内存、不抢 CPU」（单线程、串行执行、关闭
    内存池/内存模式）：编码速度只影响后台索引，而默认的多线程配置
    会使常驻内存超出 256MiB 目标并与事件循环争抢 CPU。
    """

    def __init__(
        self,
        *,
        modelDir: str | Path = LLM_MEMORY_MODEL_DIR,
        manifestPath: str | Path = LLM_MEMORY_MODEL_MANIFEST_PATH,
        numpyModule=None,
        tokenizer=None,
        session=None,
    ):
        """校验模型产物，按可选 modelFile 或旧默认路径创建单线程 session。"""
        self._modelDir = Path(modelDir)
        self._manifestPath = Path(manifestPath)
        self._manifest = loadModelManifest(self._manifestPath)
        requireModelArtifacts(self._modelDir, self._manifestPath)

        if numpyModule is None:
            numpyModule = self._importOptionalDependency("numpy")
        self._numpy = numpyModule

        if tokenizer is None:
            tokenizers = self._importOptionalDependency("tokenizers")
            tokenizerPath = resolveModelArtifactPath(self._modelDir, "tokenizer.json")
            tokenizer = tokenizers.Tokenizer.from_file(str(tokenizerPath))
        self._tokenizer = tokenizer

        if session is None:
            onnxruntime = self._importOptionalDependency("onnxruntime")
            sessionOptions = onnxruntime.SessionOptions()
            sessionOptions.intra_op_num_threads = 1
            sessionOptions.inter_op_num_threads = 1
            sessionOptions.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
            sessionOptions.enable_cpu_mem_arena = False
            sessionOptions.enable_mem_pattern = False
            modelPath = resolveModelArtifactPath(
                self._modelDir,
                self._manifest.get("modelFile", "model_optimized.onnx"),
            )
            session = onnxruntime.InferenceSession(
                str(modelPath),
                sess_options=sessionOptions,
                providers=["CPUExecutionProvider"],
            )
        self._session = session

        # 编码窗口取清单声明与 config 上限的较小者：清单可能描述更大模型，
        # 但线上预算按 config 收紧时不允许被清单放宽。
        self._maxTokens = min(
            int(self._manifest["maxTokens"]),
            LLM_MEMORY_ENCODING_MAX_TOKENS,
        )
        self._embeddingDimension = int(self._manifest["embeddingDimension"])
        self._queryPrefix = str(self._manifest["queryPrefix"])
        self._specialTokenIDs = self._loadSpecialTokenIDs()
        self._inputSpecs = self._loadInputSpecs()


    @staticmethod
    def _importOptionalDependency(moduleName: str):
        """延迟导入 memory 专用依赖，使普通 bot 安装无需携带模型栈。"""
        try:
            return __import__(moduleName)
        except ImportError as exc:
            raise MemoryEncoderUnavailable(
                f"缺少可选依赖 {moduleName}，请安装 requirements-memory.txt"
            ) from exc


    def _loadSpecialTokenIDs(self) -> dict[str, int]:
        """把 manifest 声明的特殊 token 转为 tokenizer ID。"""
        result = {}
        for key in ("cls", "sep", "pad"):
            token = self._manifest["specialTokens"].get(key)
            tokenID = self._tokenizer.token_to_id(token) if token else None
            if tokenID is None:
                raise MemoryEncoderError(f"tokenizer 缺少特殊 token: {key}")
            result[key] = int(tokenID)
        return result


    def _loadInputSpecs(self) -> dict[str, Any]:
        """校验 ONNX 输入名，只保留当前编码实现支持的张量。"""
        supportedNames = {"input_ids", "attention_mask", "token_type_ids"}
        specs = {inputSpec.name: inputSpec for inputSpec in self._session.get_inputs()}
        unsupportedNames = set(specs).difference(supportedNames)
        if unsupportedNames:
            raise MemoryEncoderError(
                f"ONNX 模型存在未支持的输入: {', '.join(sorted(unsupportedNames))}"
            )
        if "input_ids" not in specs or "attention_mask" not in specs:
            raise MemoryEncoderError("ONNX 模型缺少 input_ids 或 attention_mask")
        return specs


    def _tokenize(self, text: str) -> list[int]:
        """执行不含特殊 token 的原始分词。"""
        encoding = self._tokenizer.encode(str(text), add_special_tokens=False)
        return [int(tokenID) for tokenID in encoding.ids]


    def _withSpecialTokens(self, tokenIDs: Sequence[int]) -> list[int]:
        """为单个序列添加 manifest 指定的 CLS/SEP。"""
        return [
            self._specialTokenIDs["cls"],
            *tokenIDs,
            self._specialTokenIDs["sep"],
        ]


    def _takeHeadTail(self, tokenIDs: list[int], budget: int) -> list[int]:
        """超预算时同时保留序列头尾，避免只保留开场或结尾。"""
        if len(tokenIDs) <= budget:
            return tokenIDs
        headLength = (budget + 1) // 2
        tailLength = budget - headLength
        result = tokenIDs[:headLength]
        if tailLength:
            result += tokenIDs[-tailLength:]
        return result


    def _splitAssistedQuery(self, text: str) -> tuple[str, str, list[str]] | None:
        """拆出 buildQueryTexts 生成的当前、引用和历史段落。

        拆分标记与 retrieval.buildQueryTexts 的拼接格式是一对契约，
        改一处必须同步另一处。返回 None 表示没有引用和历史（普通查询）。
        """
        historyMarker = "\n\n近期对话：\n"
        replyMarker = "\n\n引用：\n"
        if historyMarker in text:
            mainText, historyText = text.split(historyMarker, 1)
        else:
            mainText, historyText = text, ""

        if replyMarker in mainText:
            currentText, replyText = mainText.split(replyMarker, 1)
        else:
            currentText, replyText = mainText, ""

        if not replyText and not historyText:
            return None
        historyLines = [line for line in historyText.splitlines() if line.strip()]
        return currentText, replyText, historyLines


    def _prepareAssistedQuery(self, text: str, contentBudget: int) -> list[int] | None:
        """辅助查询超出 token 预算时，按优先级截取各部分。

        优先级：本轮消息 > 引用的消息 > 聊天记录（由新到旧）。高层
        优先占用预算，剩余额度向下分配；单段超长时保留头尾各半
        （_takeHeadTail），避免只剩开头或只剩结尾。
        """
        parts = self._splitAssistedQuery(text)
        if parts is None:
            return None

        currentText, replyText, historyLines = parts
        currentIDs = self._tokenize(currentText)
        if len(currentIDs) >= contentBudget:
            return self._takeHeadTail(currentIDs, contentBudget)

        result = list(currentIDs)
        remaining = contentBudget - len(result)

        replyIDs = self._tokenize(replyText)
        replyTake = min(len(replyIDs), remaining)
        if replyTake:
            result.extend(self._takeHeadTail(replyIDs, replyTake))
            remaining -= replyTake

        # 历史由新到旧分配预算，最后反转回时间顺序，防止旧消息占满额度。
        selectedHistory = []
        for line in reversed(historyLines):
            if remaining <= 0:
                break
            lineIDs = self._tokenize(line)
            if not lineIDs:
                continue
            take = min(len(lineIDs), remaining)
            selectedHistory.append(self._takeHeadTail(lineIDs, take))
            remaining -= take
        for lineIDs in reversed(selectedHistory):
            result.extend(lineIDs)
        return result


    def _prepareQuery(self, text: str) -> list[int]:
        """加入模型 query prefix，并在固定编码窗口内准备查询 token。"""
        prefixIDs = self._tokenize(self._queryPrefix)
        contentBudget = self._maxTokens - len(prefixIDs) - 2
        if contentBudget < 1:
            raise MemoryEncoderError("query prefix 超过编码窗口")

        contentIDs = self._prepareAssistedQuery(text, contentBudget)
        if contentIDs is None:
            contentIDs = self._takeHeadTail(
                self._tokenize(text),
                contentBudget,
            )
        return self._withSpecialTokens([*prefixIDs, *contentIDs])


    def _formatMemoryText(self, memory: dict, *, includeHint: bool) -> str:
        """把一条记忆拼成 base 或 enhanced 编码文本。

        enhanced 文本形如：

            事实：用户在备考研究生
            标签：学业，考试
            检索说明：研究生备考、复习安排、学习压力

        base 永远排除「检索说明」，防止 hint 独自把无关记忆送过准入阈值；
        enhanced 才按 includeHint 加入它。这些文本都只用于计算向量，绝不
        进入最终 prompt，否则扩展词可能被模型当成已记录的事实复述。
        """
        content = str(memory.get("content", "")).strip()
        if not content:
            raise MemoryEncoderError("memory content 不能为空")

        sections = [f"事实：{content}"]
        tags = [str(tag).strip() for tag in memory.get("tags") or [] if str(tag).strip()]
        if tags:
            sections.append(f"标签：{'，'.join(tags)}")
        retrievalHint = memory.get("retrievalHint")
        if includeHint and retrievalHint and str(retrievalHint).strip():
            sections.append(f"检索说明：{str(retrievalHint).strip()}")
        return "\n".join(sections)


    def _prepareMemoryChunks(
        self,
        memory: dict,
        *,
        includeHint: bool = False,
    ) -> list[list[int]]:
        """将指定表示切成有重叠的编码窗口，并为每块加特殊 token。"""
        documentIDs = self._tokenize(
            self._formatMemoryText(memory, includeHint=includeHint)
        )
        contentBudget = self._maxTokens - 2
        if not documentIDs:
            raise MemoryEncoderError("memory 文本未产生可编码 token")
        if len(documentIDs) <= contentBudget:
            return [self._withSpecialTokens(documentIDs)]

        overlap = min(LLM_MEMORY_CHUNK_OVERLAP, contentBudget - 1)
        step = contentBudget - overlap
        chunks = []
        for startIndex in range(0, len(documentIDs), step):
            chunkIDs = documentIDs[startIndex:startIndex + contentBudget]
            chunks.append(self._withSpecialTokens(chunkIDs))
            if startIndex + contentBudget >= len(documentIDs):
                break
        return chunks


    def _numpyIntegerType(self, inputSpec):
        """将 ONNX 输入声明映射为对应的 NumPy 整数类型。"""
        inputType = str(getattr(inputSpec, "type", "tensor(int64)"))
        if inputType == "tensor(int32)":
            return self._numpy.int32
        if inputType == "tensor(int64)":
            return self._numpy.int64
        raise MemoryEncoderError(f"ONNX 输入类型不受支持: {inputType}")




    def _runSequences(self, sequences: list[list[int]]):
        """
        跑一次 ONNX 推理：一批 token 序列进，一批单位向量出。

        向量取自每个序列开头 CLS token 的输出（BERT 类模型的惯例），
        再除以自身长度归一——归一化后两个向量点积就是余弦相似度，
        后面打分和阈值都建立在这上面。输出逐项检查（维度对不对、
        有没有 NaN、有没有全零），不合格直接抛错：坏向量进缓存
        会污染之后所有用它打分的检索。
        """
        if not sequences:
            return self._numpy.empty(
                (0, self._embeddingDimension),
                dtype=self._numpy.float32,
            )

        sequenceLength = max(len(sequence) for sequence in sequences)
        batchSize = len(sequences)
        inputIDs = self._numpy.full(
            (batchSize, sequenceLength),
            self._specialTokenIDs["pad"],
            dtype=self._numpy.int64,
        )
        attentionMask = self._numpy.zeros(
            (batchSize, sequenceLength),
            dtype=self._numpy.int64,
        )
        tokenTypeIDs = self._numpy.zeros(
            (batchSize, sequenceLength),
            dtype=self._numpy.int64,
        )
        for rowIndex, sequence in enumerate(sequences):
            inputIDs[rowIndex, :len(sequence)] = sequence
            attentionMask[rowIndex, :len(sequence)] = 1

        sourceArrays = {
            "input_ids": inputIDs,
            "attention_mask": attentionMask,
            "token_type_ids": tokenTypeIDs,
        }
        modelInputs = {
            name: sourceArrays[name].astype(self._numpyIntegerType(inputSpec), copy=False)
            for name, inputSpec in self._inputSpecs.items()
        }
        outputs = self._session.run(None, modelInputs)
        if not outputs:
            raise MemoryEncoderError("ONNX 模型没有输出")

        hiddenState = self._numpy.asarray(outputs[0])
        if hiddenState.ndim != 3 or hiddenState.shape[0] != batchSize:
            raise MemoryEncoderError(f"ONNX 输出形状不受支持: {hiddenState.shape}")
        vectors = hiddenState[:, 0, :].astype(self._numpy.float32, copy=False)
        if vectors.shape[1] != self._embeddingDimension:
            raise MemoryEncoderError(
                f"ONNX 向量维度错误: {vectors.shape[1]} != {self._embeddingDimension}"
            )
        if not self._numpy.isfinite(vectors).all():
            raise MemoryEncoderError("ONNX 输出包含非有限值")

        norms = self._numpy.linalg.norm(vectors, axis=1, keepdims=True)
        if (norms <= 0).any() or not self._numpy.isfinite(norms).all():
            raise MemoryEncoderError("ONNX 输出包含零范数向量")
        return vectors / norms




    def encodeQueries(self, queryTexts: Sequence[str]):
        """
        编码非空查询列表，每个查询产出一个归一化向量。

        走 `_prepareQuery`：加 BGE 专用的检索 query prefix（manifest
        声明），辅助视图按 当前 > 引用 > 历史 的优先级瓜分预算。
        """
        queryTexts = list(queryTexts)
        if any(not str(text).strip() for text in queryTexts):
            raise MemoryEncoderError("query 文本不能为空")
        sequences = [self._prepareQuery(str(text)) for text in queryTexts]
        return self._runSequences(sequences)


    def encodeMemory(self, memory: dict, *, includeHint: bool = False):
        """编码一种 memory 表示；默认返回不含 hint 的安全 base 矩阵。"""
        return self._runSequences(
            self._prepareMemoryChunks(memory, includeHint=includeHint)
        )




    def encodeMemoryRepresentations(self, memory: dict) -> MemoryVectorRepresentations:
        """
        一次构造准入用 base 与排序用 enhanced 表示。

        有 hint 时把两组 chunks 合成一个 ONNX batch，减少 native 调用次数；
        分割后的矩阵仍分别对应各自的 chunk 集。无 hint 时只编码 base，并
        让 enhanced 复用同一对象，运行时据此避免第二次比较和重复计费。
        """
        baseSequences = self._prepareMemoryChunks(memory, includeHint=False)
        retrievalHint = str(memory.get("retrievalHint") or "").strip()
        if not retrievalHint:
            baseMatrix = self._runSequences(baseSequences)
            return MemoryVectorRepresentations(
                base=baseMatrix,
                enhanced=baseMatrix,
            )

        enhancedSequences = self._prepareMemoryChunks(memory, includeHint=True)
        baseCount = len(baseSequences)
        vectors = self._runSequences([*baseSequences, *enhancedSequences])
        return MemoryVectorRepresentations(
            base=vectors[:baseCount],
            enhanced=vectors[baseCount:],
        )




    def close(self):
        """释放 ONNX session 引用；调用前须确保没有在途 native 任务。"""
        self._session = None
