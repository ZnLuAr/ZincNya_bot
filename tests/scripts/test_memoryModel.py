"""测试固定 memory 模型安装器。"""

import io
import json
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.llmMemory.memoryModel import (
    MemoryEncoderError,
    buildArtifactURL,
    installModel,
    verifyModel,
)




class _FakeResponse:
    def __init__(self, content):
        self._stream = io.BytesIO(content)

    def __enter__(self):
        return self

    def __exit__(self, excType, excValue, traceback):
        return False

    def read(self, size=-1):
        return self._stream.read(size)


def _writeManifest(tmpPath: Path, files: dict[str, bytes]):
    artifacts = [
        {
            "path": fileName,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for fileName, content in files.items()
    ]
    manifest = {
        "schemaVersion": 1,
        "repository": "Qdrant/bge-small-zh-v1.5",
        "revision": "4" * 40,
        "encodingVersion": "test",
        "queryPrefix": "query",
        "maxTokens": 8,
        "embeddingDimension": 4,
        "pooling": "cls",
        "normalization": "l2",
        "specialTokens": {"cls": "[CLS]", "sep": "[SEP]", "pad": "[PAD]"},
        "artifacts": artifacts,
    }
    manifestPath = tmpPath / "manifest.json"
    manifestPath.write_text(json.dumps(manifest), encoding="utf-8")
    return manifestPath, manifest


def test_verifyModelDoesNotAccessNetwork(tmp_path):
    manifestPath, _ = _writeManifest(tmp_path, {"model.bin": b"expected"})

    valid, statuses = verifyModel(tmp_path / "missing", manifestPath)

    assert valid is False
    assert statuses == [{
        "path": "model.bin",
        "exists": False,
        "sizeValid": False,
        "hashValid": False,
        "valid": False,
    }]


def test_buildArtifactURLPinsFullRevision(tmp_path):
    _, manifest = _writeManifest(tmp_path, {"model.bin": b"expected"})

    url = buildArtifactURL(manifest, "model.bin")

    assert manifest["revision"] in url
    assert "/resolve/main/" not in url


def test_buildArtifactURLAllowsExplicitHttpsMirror(tmp_path):
    _, manifest = _writeManifest(tmp_path, {"model.bin": b"expected"})

    url = buildArtifactURL(
        manifest,
        "model.bin",
        endpoint="https://hf-mirror.com/",
    )

    assert url.startswith("https://hf-mirror.com/")
    assert manifest["revision"] in url


def test_buildArtifactURLRejectsInsecureEndpoint(tmp_path):
    _, manifest = _writeManifest(tmp_path, {"model.bin": b"expected"})

    with pytest.raises(MemoryEncoderError, match="HTTPS URL"):
        buildArtifactURL(
            manifest,
            "model.bin",
            endpoint="http://example.com",
        )


def test_buildArtifactURLRejectsArtifactPathTraversal(tmp_path):
    _, manifest = _writeManifest(tmp_path, {"model.bin": b"expected"})

    with pytest.raises(MemoryEncoderError, match="路径不安全"):
        buildArtifactURL(manifest, "../outside.bin")


def test_installModelVerifiesAllFilesBeforePublishing(tmp_path):
    expectedFiles = {"model.bin": b"model", "tokenizer.json": b"tokenizer"}
    manifestPath, _ = _writeManifest(tmp_path, expectedFiles)
    modelDir = tmp_path / "model"
    modelDir.mkdir()
    existingPath = modelDir / "model.bin"
    existingPath.write_bytes(b"old")

    def _failingOpener(request, timeout):
        if request.full_url.endswith("model.bin"):
            return _FakeResponse(expectedFiles["model.bin"])
        raise OSError("network stopped")

    with pytest.raises(OSError, match="network stopped"):
        installModel(modelDir, manifestPath, opener=_failingOpener)

    assert existingPath.read_bytes() == b"old"
    assert not (modelDir / "tokenizer.json").exists()


def test_installModelPublishesVerifiedArtifacts(tmp_path):
    expectedFiles = {"model.bin": b"model", "tokenizer.json": b"tokenizer"}
    manifestPath, _ = _writeManifest(tmp_path, expectedFiles)
    modelDir = tmp_path / "model"
    modelDir.mkdir()
    (modelDir / "stale.bin").write_bytes(b"stale")

    def _opener(request, timeout):
        fileName = request.full_url.rsplit("/", 1)[-1]
        return _FakeResponse(expectedFiles[fileName])

    statuses = installModel(modelDir, manifestPath, opener=_opener)

    assert all(status["valid"] for status in statuses)
    assert (modelDir / "model.bin").read_bytes() == b"model"
    assert (modelDir / "tokenizer.json").read_bytes() == b"tokenizer"
    assert not (modelDir / "stale.bin").exists()


def test_installModelRollsBackWholeDirectoryWhenPublishFails(tmp_path):
    expectedFiles = {"model.bin": b"model", "tokenizer.json": b"tokenizer"}
    manifestPath, _ = _writeManifest(tmp_path, expectedFiles)
    modelDir = tmp_path / "model"
    modelDir.mkdir()
    (modelDir / "model.bin").write_bytes(b"old-model")
    (modelDir / "old-only.bin").write_bytes(b"old-only")

    def _opener(request, timeout):
        fileName = request.full_url.rsplit("/", 1)[-1]
        return _FakeResponse(expectedFiles[fileName])

    realReplace = os.replace
    replaceCalls = 0

    def _failStagingPublish(source, destination):
        nonlocal replaceCalls
        replaceCalls += 1
        if replaceCalls == 2:
            raise OSError("publish interrupted")
        return realReplace(source, destination)

    with (
        patch("scripts.llmMemory.memoryModel.os.replace", side_effect=_failStagingPublish),
        pytest.raises(OSError, match="publish interrupted"),
    ):
        installModel(modelDir, manifestPath, opener=_opener)

    assert (modelDir / "model.bin").read_bytes() == b"old-model"
    assert (modelDir / "old-only.bin").read_bytes() == b"old-only"
    assert not (modelDir / "tokenizer.json").exists()


def test_installModelKeepsOldDirectoryWhenBackupMoveFails(tmp_path):
    """备份 rename 未成功时，回滚不能误删仍在原位的旧安装。"""
    expectedFiles = {"model.bin": b"model", "tokenizer.json": b"tokenizer"}
    manifestPath, _ = _writeManifest(tmp_path, expectedFiles)
    modelDir = tmp_path / "model"
    modelDir.mkdir()
    (modelDir / "model.bin").write_bytes(b"old-model")
    (modelDir / "old-only.bin").write_bytes(b"old-only")

    def _opener(request, timeout):
        fileName = request.full_url.rsplit("/", 1)[-1]
        return _FakeResponse(expectedFiles[fileName])

    realReplace = os.replace

    def _failBackupMove(source, destination):
        if source == modelDir:
            raise OSError("backup interrupted")
        return realReplace(source, destination)

    with (
        patch("scripts.llmMemory.memoryModel.os.replace", side_effect=_failBackupMove),
        pytest.raises(OSError, match="backup interrupted"),
    ):
        installModel(modelDir, manifestPath, opener=_opener)

    assert (modelDir / "model.bin").read_bytes() == b"old-model"
    assert (modelDir / "old-only.bin").read_bytes() == b"old-only"
    assert not (modelDir / "tokenizer.json").exists()


def test_installModelPreservesBackupWhenRollbackFails(tmp_path):
    expectedFiles = {"model.bin": b"model", "tokenizer.json": b"tokenizer"}
    manifestPath, _ = _writeManifest(tmp_path, expectedFiles)
    modelDir = tmp_path / "model"
    modelDir.mkdir()
    (modelDir / "model.bin").write_bytes(b"old-model")
    (modelDir / "old-only.bin").write_bytes(b"old-only")

    def _opener(request, timeout):
        fileName = request.full_url.rsplit("/", 1)[-1]
        return _FakeResponse(expectedFiles[fileName])

    realReplace = os.replace
    replaceCalls = 0

    def _failPublishAndRollback(source, destination):
        nonlocal replaceCalls
        replaceCalls += 1
        if replaceCalls in (2, 3):
            raise OSError("replace interrupted")
        return realReplace(source, destination)

    with (
        patch("scripts.llmMemory.memoryModel.os.replace", side_effect=_failPublishAndRollback),
        pytest.raises(MemoryEncoderError, match="旧安装保留于"),
    ):
        installModel(modelDir, manifestPath, opener=_opener)

    backupPaths = list(tmp_path.glob(".model-backup-*"))
    assert len(backupPaths) == 1
    assert (backupPaths[0] / "model.bin").read_bytes() == b"old-model"
    assert (backupPaths[0] / "old-only.bin").read_bytes() == b"old-only"


def test_installModelRejectsBadDigest(tmp_path):
    manifestPath, _ = _writeManifest(tmp_path, {"model.bin": b"expected"})

    with pytest.raises(MemoryEncoderError, match="大小不匹配|SHA-256"):
        installModel(
            tmp_path / "model",
            manifestPath,
            opener=lambda request, timeout: _FakeResponse(b"tampered"),
        )
