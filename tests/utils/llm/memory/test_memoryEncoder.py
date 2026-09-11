"""测试本地 memory 编码器的校验、分片和 ONNX 输入契约。"""

import json
import hashlib
from pathlib import Path

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


def _writeManifest(tmpPath: Path, *, maxTokens=8, dimension=4):
    modelDir = tmpPath / "model"
    modelDir.mkdir()
    artifacts = []
    for fileName, content in (
        ("model_optimized.onnx", b"model"),
        ("tokenizer.json", b"tokenizer"),
        ("ort_config.json", b"config"),
    ):
        (modelDir / fileName).write_bytes(content)
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
