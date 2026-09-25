"""测试本地 memory 编码器的校验、分片和 ONNX 输入契约。"""

import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from utils.llm.memory import encoder as encoderModule
from utils.llm.memory.encoder import (
    MemoryEncoder,
    MemoryEncoderError,
    MemoryEncoderUnavailable,
    inspectModelArtifacts,
    loadModelManifest,
    resolveModelArtifactPath,
)




class _FakeEncoding:
    def __init__(self, ids):
        self.ids = ids


class _FakeTokenizer:
    _SPECIAL_IDS = {"[PAD]": 0, "[CLS]": 101, "[SEP]": 102}

    def token_to_id(self, token):
        return self._SPECIAL_IDS.get(token)

    def encode(self, text, add_special_tokens=False):
        assert add_special_tokens is False
        return _FakeEncoding([ord(character) % 89 + 3 for character in text])


class _FakeInput:
    def __init__(self, name, inputType="tensor(int64)"):
        self.name = name
        self.type = inputType


class _FakeSession:
    def __init__(self, numpyModule, *, dimension=4, inputNames=None):
        self._numpy = numpyModule
        self._dimension = dimension
        self._inputNames = inputNames or ["input_ids", "attention_mask", "token_type_ids"]
        self.lastInputs = None

    def get_inputs(self):
        return [_FakeInput(name) for name in self._inputNames]

    def run(self, outputNames, modelInputs):
        assert outputNames is None
        self.lastInputs = modelInputs
        inputIDs = modelInputs["input_ids"]
        output = self._numpy.zeros(
            (inputIDs.shape[0], inputIDs.shape[1], self._dimension),
            dtype=self._numpy.float32,
        )
        output[:, 0, 0] = 3
        output[:, 0, 1] = 4
        return [output]


def _writeManifest(tmpPath: Path, *, maxTokens=8, dimension=4, modelFile=None):
    modelDir = tmpPath / "model"
    modelDir.mkdir()
    artifacts = []
    for fileName, content in (
        (modelFile or "model_optimized.onnx", b"model"),
        ("tokenizer.json", b"tokenizer"),
        ("ort_config.json", b"config"),
    ):
        filePath = modelDir / fileName
        filePath.parent.mkdir(parents=True, exist_ok=True)
        filePath.write_bytes(content)
        artifacts.append({
            "path": fileName,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })

    manifest = {
        "schemaVersion": 1,
        "repository": "example/model",
        "revision": "a" * 40,
        "encodingVersion": "test-v1",
        "queryPrefix": "p",
        "maxTokens": maxTokens,
        "embeddingDimension": dimension,
        "pooling": "cls",
        "normalization": "l2",
        "specialTokens": {"cls": "[CLS]", "sep": "[SEP]", "pad": "[PAD]"},
        "artifacts": artifacts,
    }
    if modelFile is not None:
        manifest["modelFile"] = modelFile
    manifestPath = tmpPath / "manifest.json"
    manifestPath.write_text(json.dumps(manifest), encoding="utf-8")
    return modelDir, manifestPath


def test_inspectModelArtifactsDetectsCorruption(tmp_path):
    modelDir, manifestPath = _writeManifest(tmp_path)

    assert all(item["valid"] for item in inspectModelArtifacts(modelDir, manifestPath))

    (modelDir / "tokenizer.json").write_bytes(b"broken")
    statuses = inspectModelArtifacts(modelDir, manifestPath)

    assert next(item for item in statuses if item["path"] == "tokenizer.json")["valid"] is False


def test_resolveModelArtifactPathRejectsTraversal(tmp_path):
    with pytest.raises(MemoryEncoderError, match="不安全"):
        resolveModelArtifactPath(tmp_path, "../outside.bin")


def test_loadModelManifestRejectsMalformedArtifactMetadata(tmp_path):
    _, manifestPath = _writeManifest(tmp_path)
    manifest = json.loads(manifestPath.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["sha256"] = "not-a-digest"
    manifestPath.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(MemoryEncoderError, match="SHA-256"):
        loadModelManifest(manifestPath)


@pytest.mark.parametrize("modelFile", [None, "", "   ", 1, True, [], {}])
def test_loadModelManifestRejectsInvalidModelFile(tmp_path, modelFile):
    """显式声明的 modelFile 必须是非空字符串，不能默默回退默认值。"""
    _, manifestPath = _writeManifest(tmp_path)
    manifest = json.loads(manifestPath.read_text(encoding="utf-8"))
    manifest["modelFile"] = modelFile
    manifestPath.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(MemoryEncoderError, match="modelFile"):
        loadModelManifest(manifestPath)


@pytest.mark.parametrize("modelFile", ["../outside.onnx", "onnx/../../outside.onnx", "absolute"])
def test_loadModelManifestRejectsUnsafeModelFile(tmp_path, modelFile):
    """模型选择与其他产物共用路径检查，拒绝目录穿越和绝对路径。"""
    _, manifestPath = _writeManifest(tmp_path)
    manifest = json.loads(manifestPath.read_text(encoding="utf-8"))
    manifest["modelFile"] = (
        str(tmp_path / "outside.onnx") if modelFile == "absolute" else modelFile
    )
    manifestPath.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(MemoryEncoderError, match="不安全"):
        loadModelManifest(manifestPath)


def test_loadModelManifestRejectsUnlistedModelFile(tmp_path):
    """即使文件存在，也不能加载未在 artifacts 中声明散列的模型。"""
    modelDir, manifestPath = _writeManifest(tmp_path)
    (modelDir / "unlisted.onnx").write_bytes(b"model")
    manifest = json.loads(manifestPath.read_text(encoding="utf-8"))
    manifest["modelFile"] = "unlisted.onnx"
    manifestPath.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(MemoryEncoderError, match="artifacts"):
        loadModelManifest(manifestPath)


@pytest.mark.parametrize("modelFile", [None, "model_optimized.onnx", "onnx/model_quantized.onnx"])
def test_encoderLoadsDeclaredOrDefaultModelFile(tmp_path, monkeypatch, modelFile):
    """真实构造路径选择兼容旧清单，并把合法嵌套产物交给 ONNX session。"""
    modelDir, manifestPath = _writeManifest(tmp_path, modelFile=modelFile)
    loadedPaths = []

    def createSession(modelPath, *, sess_options, providers):
        loadedPaths.append(modelPath)
        assert sess_options.intra_op_num_threads == 1
        assert providers == ["CPUExecutionProvider"]
        return _FakeSession(None)

    onnxruntime = SimpleNamespace(
        SessionOptions=SimpleNamespace,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        InferenceSession=createSession,
    )

    def importOptionalDependency(moduleName):
        assert moduleName == "onnxruntime"
        return onnxruntime

    monkeypatch.setattr(
        MemoryEncoder, "_importOptionalDependency", staticmethod(importOptionalDependency)
    )
    encoder = MemoryEncoder(
        modelDir=modelDir,
        manifestPath=manifestPath,
        numpyModule=object(),
        tokenizer=_FakeTokenizer(),
    )

    expectedPath = (modelDir / (modelFile or "model_optimized.onnx")).resolve()
    assert loadedPaths == [str(expectedPath)]
    encoder.close()


def test_encoderChecksSelectedModelIntegrityBeforeLoading(tmp_path, monkeypatch):
    """选择不同模型文件名不应绕过已有的 SHA-256 校验。"""
    modelDir, manifestPath = _writeManifest(tmp_path, modelFile="onnx/model_quantized.onnx")
    (modelDir / "onnx/model_quantized.onnx").write_bytes(b"other")
    monkeypatch.setattr(
        MemoryEncoder,
        "_importOptionalDependency",
        staticmethod(lambda moduleName: pytest.fail("不应加载未通过校验的模型")),
    )

    with pytest.raises(MemoryEncoderUnavailable, match="校验失败"):
        MemoryEncoder(modelDir=modelDir, manifestPath=manifestPath)


@pytest.mark.parametrize(
    ("fieldName", "value", "message"),
    [
        ("pooling", "mean", "pooling=cls"),
        ("normalization", "none", "normalization=l2"),
    ],
)
def test_loadModelManifestRejectsUnsupportedEncodingContract(
    tmp_path, fieldName, value, message,
):
    _, manifestPath = _writeManifest(tmp_path)
    manifest = json.loads(manifestPath.read_text(encoding="utf-8"))
    manifest[fieldName] = value
    manifestPath.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(MemoryEncoderError, match=message):
        loadModelManifest(manifestPath)


def test_encoderUsesHeadTailQueryBudgetAndNormalizes(tmp_path):
    numpy = pytest.importorskip("numpy")
    modelDir, manifestPath = _writeManifest(tmp_path)
    session = _FakeSession(numpy)
    encoder = MemoryEncoder(
        modelDir=modelDir,
        manifestPath=manifestPath,
        numpyModule=numpy,
        tokenizer=_FakeTokenizer(),
        session=session,
    )

    sequence = encoder._prepareQuery("abcdefghi")
    vectors = encoder.encodeQueries(["short", "another"])

    expectedContent = _FakeTokenizer().encode("abc", False).ids
    expectedContent += _FakeTokenizer().encode("hi", False).ids
    assert sequence == [101, *_FakeTokenizer().encode("p", False).ids, *expectedContent, 102]
    assert sequence[0] == 101 and sequence[-1] == 102
    assert len(sequence) == 8
    assert vectors.shape == (2, 4)
    assert vectors.dtype == numpy.float32
    assert numpy.allclose(numpy.linalg.norm(vectors, axis=1), [1.0, 1.0])
    assert set(session.lastInputs) == {"input_ids", "attention_mask", "token_type_ids"}


def test_encoderAllocatesAssistedQueryByCurrentReplyAndNewestHistory(tmp_path):
    numpy = pytest.importorskip("numpy")
    modelDir, manifestPath = _writeManifest(tmp_path, maxTokens=12)
    encoder = MemoryEncoder(
        modelDir=modelDir,
        manifestPath=manifestPath,
        numpyModule=numpy,
        tokenizer=_FakeTokenizer(),
        session=_FakeSession(numpy),
    )

    sequence = encoder._prepareQuery(
        "当前\n\n引用：\n引用\n\n近期对话：\n旧\n新"
    )

    # prefix=1、CLS/SEP=2，剩余 9 个 token：当前、引用和最新历史依次进入。
    tokenIDs = sequence[2:-1]
    assert tokenIDs[:2] == _FakeTokenizer().encode("当前", False).ids
    assert tokenIDs[2:4] == _FakeTokenizer().encode("引用", False).ids
    assert tokenIDs[-2:] == (
        _FakeTokenizer().encode("旧", False).ids
        + _FakeTokenizer().encode("新", False).ids
    )


def test_encoderRejectsBlankQueryButAllowsEmptyBatch(tmp_path):
    numpy = pytest.importorskip("numpy")
    modelDir, manifestPath = _writeManifest(tmp_path)
    encoder = MemoryEncoder(
        modelDir=modelDir,
        manifestPath=manifestPath,
        numpyModule=numpy,
        tokenizer=_FakeTokenizer(),
        session=_FakeSession(numpy),
    )

    with pytest.raises(MemoryEncoderError, match="不能为空"):
        encoder.encodeQueries(["有效查询", ""])

    vectors = encoder.encodeQueries([])
    assert vectors.shape == (0, 4)


def test_encoderChunksLongMemoryWithOverlap(tmp_path, monkeypatch):
    numpy = pytest.importorskip("numpy")
    modelDir, manifestPath = _writeManifest(tmp_path)
    monkeypatch.setattr(encoderModule, "LLM_MEMORY_CHUNK_OVERLAP", 2)
    encoder = MemoryEncoder(
        modelDir=modelDir,
        manifestPath=manifestPath,
        numpyModule=numpy,
        tokenizer=_FakeTokenizer(),
        session=_FakeSession(numpy),
    )

    chunks = encoder._prepareMemoryChunks({"content": "abcdefghijklmnop"})
    vectors = encoder.encodeMemory({"content": "abcdefghijklmnop"})

    assert len(chunks) > 1
    assert all(len(chunk) <= 8 for chunk in chunks)
    assert chunks[0][-3:-1] == chunks[1][1:3]
    assert vectors.shape[0] == len(chunks)


def test_memoryRepresentationsKeepHintOutOfBaseText(tmp_path):
    numpy = pytest.importorskip("numpy")
    modelDir, manifestPath = _writeManifest(tmp_path, maxTokens=64)
    encoder = MemoryEncoder(
        modelDir=modelDir,
        manifestPath=manifestPath,
        numpyModule=numpy,
        tokenizer=_FakeTokenizer(),
        session=_FakeSession(numpy),
    )
    memory = {
        "content": "喜欢安静的餐厅",
        "tags": ["餐厅"],
        "retrievalHint": "约会地点",
    }

    baseText = encoder._formatMemoryText(memory, includeHint=False)
    enhancedText = encoder._formatMemoryText(memory, includeHint=True)
    representations = encoder.encodeMemoryRepresentations(memory)

    assert "检索说明" not in baseText
    assert "约会地点" not in baseText
    assert "检索说明：约会地点" in enhancedText
    assert representations.base is not representations.enhanced


def test_memoryRepresentationsReuseBaseMatrixWithoutHint(tmp_path):
    numpy = pytest.importorskip("numpy")
    modelDir, manifestPath = _writeManifest(tmp_path)
    encoder = MemoryEncoder(
        modelDir=modelDir,
        manifestPath=manifestPath,
        numpyModule=numpy,
        tokenizer=_FakeTokenizer(),
        session=_FakeSession(numpy),
    )

    representations = encoder.encodeMemoryRepresentations({
        "content": "只包含正文",
        "tags": [],
        "retrievalHint": "   ",
    })

    assert representations.base is representations.enhanced


def test_encoderRejectsUnsupportedOnnxInput(tmp_path):
    numpy = pytest.importorskip("numpy")
    modelDir, manifestPath = _writeManifest(tmp_path)

    with pytest.raises(MemoryEncoderError, match="未支持的输入"):
        MemoryEncoder(
            modelDir=modelDir,
            manifestPath=manifestPath,
            numpyModule=numpy,
            tokenizer=_FakeTokenizer(),
            session=_FakeSession(numpy, inputNames=["input_ids", "attention_mask", "position_ids"]),
        )


def test_encoderReportsMissingOptionalDependency(monkeypatch):
    originalImport = __import__

    def _missingImport(name, *args, **kwargs):
        if name == "onnxruntime":
            raise ImportError("missing")
        return originalImport(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _missingImport)

    with pytest.raises(MemoryEncoderUnavailable, match="requirements-memory.txt"):
        MemoryEncoder._importOptionalDependency("onnxruntime")
