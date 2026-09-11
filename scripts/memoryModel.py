#!/usr/bin/env python3
"""
scripts/memoryModel.py

安装或校验固定版本的本地 memory 语义模型。

用法：`python scripts/memoryModel.py install` / `verify`。
模型身份（仓库 / 完整 commit SHA / 逐产物 SHA-256）以
modelManifest.json 为准，从 Hugging Face 按 revision 下载——不取
latest、不接受部分成功：install 在同级临时目录完整下载并校验后才
原子发布到 .cache/llmMemory/model，任一产物失败不污染现有安装。
"""

import os
import re
import sys
import json
import hashlib
import argparse
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import LLM_MEMORY_MODEL_DIR, LLM_MEMORY_MODEL_MANIFEST_PATH

from utils.llm.memory.encoder import (
    MemoryEncoderError,
    inspectModelArtifacts,
    loadModelManifest,
    resolveModelArtifactPath,
)


_DOWNLOAD_TIMEOUT_SECONDS = 60
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
# repository 形如 owner/repo；revision 必须是完整 commit SHA——
# 拒绝 tag/branch，防止「同一 manifest 两次安装拉到不同模型」。
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")




def buildArtifactURL(manifest: dict, artifactPath: str) -> str:
    """为固定 repository 与完整 commit SHA 构造 Hugging Face 产物 URL。"""
    repository = str(manifest.get("repository", ""))
    revision = str(manifest.get("revision", ""))
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise MemoryEncoderError("模型清单中的 repository 无效")
    if not _REVISION_PATTERN.fullmatch(revision):
        raise MemoryEncoderError("模型清单中的 revision 必须是完整 commit SHA")

    encodedRepository = quote(repository, safe="/")
    encodedRevision = quote(revision, safe="")
    encodedPath = quote(str(artifactPath), safe="/")
    return (
        f"https://huggingface.co/{encodedRepository}/resolve/"
        f"{encodedRevision}/{encodedPath}"
    )


def verifyModel(
    modelDir: str | Path = LLM_MEMORY_MODEL_DIR,
    manifestPath: str | Path = LLM_MEMORY_MODEL_MANIFEST_PATH,
) -> tuple[bool, list[dict]]:
    """校验本地全部模型产物，并同时返回逐文件状态。"""
    statuses = inspectModelArtifacts(modelDir, manifestPath)
    return all(status["valid"] for status in statuses), statuses


def _downloadArtifact(url: str, destination: Path, artifact: dict, opener) -> None:
    """流式下载单个产物，并在落盘过程中强制大小和 SHA-256 契约。"""
    expectedSize = int(artifact["size"])
    expectedHash = str(artifact["sha256"]).lower()
    request = urllib.request.Request(url, headers={"User-Agent": "ZincNya-memory-model"})
    digest = hashlib.sha256()
    downloadedSize = 0

    with opener(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
        with destination.open("wb") as outputFile:
            while chunk := response.read(_DOWNLOAD_CHUNK_BYTES):
                downloadedSize += len(chunk)
                if downloadedSize > expectedSize:
                    raise MemoryEncoderError(
                        f"下载文件超过清单大小: {artifact['path']}"
                    )
                digest.update(chunk)
                outputFile.write(chunk)

    if downloadedSize != expectedSize:
        raise MemoryEncoderError(
            f"下载文件大小不匹配: {artifact['path']} "
            f"({downloadedSize} != {expectedSize})"
        )
    if digest.hexdigest() != expectedHash:
        raise MemoryEncoderError(f"下载文件 SHA-256 不匹配: {artifact['path']}")


def installModel(
    modelDir: str | Path = LLM_MEMORY_MODEL_DIR,
    manifestPath: str | Path = LLM_MEMORY_MODEL_MANIFEST_PATH,
    *,
    opener=urllib.request.urlopen,
) -> list[dict]:
    """在同级临时目录完整下载并校验后，原子发布固定模型产物。

    任一产物失败都不会把未完整校验的文件发布到正式模型目录；已通过
    校验的现有安装会直接复用，不访问网络。staging 放在目标目录同级
    是为了让 os.replace 落在同一文件系统上（跨盘 rename 会失败）。
    """
    manifest = loadModelManifest(manifestPath)
    modelPath = Path(modelDir).resolve()
    modelPath.parent.mkdir(parents=True, exist_ok=True)

    alreadyValid, statuses = verifyModel(modelPath, manifestPath)
    if alreadyValid:
        return statuses

    with tempfile.TemporaryDirectory(
        prefix=".memory-model-",
        dir=modelPath.parent,
    ) as stagingName:
        stagingPath = Path(stagingName)
        for artifact in manifest["artifacts"]:
            relativePath = str(artifact.get("path", ""))
            stagedArtifact = resolveModelArtifactPath(stagingPath, relativePath)
            stagedArtifact.parent.mkdir(parents=True, exist_ok=True)
            _downloadArtifact(
                buildArtifactURL(manifest, relativePath),
                stagedArtifact,
                artifact,
                opener,
            )

        stagedValid, stagedStatuses = verifyModel(stagingPath, manifestPath)
        if not stagedValid:
            invalidPaths = [
                status["path"] for status in stagedStatuses if not status["valid"]
            ]
            raise MemoryEncoderError(
                f"暂存模型校验失败: {', '.join(invalidPaths)}"
            )

        modelPath.mkdir(parents=True, exist_ok=True)
        for artifact in manifest["artifacts"]:
            relativePath = str(artifact["path"])
            stagedArtifact = resolveModelArtifactPath(stagingPath, relativePath)
            targetArtifact = resolveModelArtifactPath(modelPath, relativePath)
            targetArtifact.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stagedArtifact, targetArtifact)

    installed, installedStatuses = verifyModel(modelPath, manifestPath)
    if not installed:
        raise MemoryEncoderError("模型发布后校验失败")
    return installedStatuses


def _printStatuses(statuses: list[dict]) -> None:
    """把逐产物校验状态渲染为稳定的 CLI 文本。"""
    for status in statuses:
        if status["valid"]:
            state = "OK"
        elif not status["exists"]:
            state = "MISSING"
        elif not status["sizeValid"]:
            state = "BAD_SIZE"
        else:
            state = "BAD_SHA256"
        print(f"{state:10} {status['path']}")


def main() -> int:
    """解析 ``verify/install`` 命令并返回适合作为进程退出码的状态。"""
    parser = argparse.ArgumentParser(
        description="安装或校验固定版本的 memory 语义模型"
    )
    parser.add_argument("action", choices=("verify", "install"))
    parser.add_argument("--model-dir", default=LLM_MEMORY_MODEL_DIR)
    parser.add_argument("--manifest", default=LLM_MEMORY_MODEL_MANIFEST_PATH)
    args = parser.parse_args()

    try:
        if args.action == "install":
            statuses = installModel(args.model_dir, args.manifest)
            print("memory 模型安装完成")
            _printStatuses(statuses)
            return 0

        valid, statuses = verifyModel(args.model_dir, args.manifest)
        _printStatuses(statuses)
        return 0 if valid else 1
    except (OSError, ValueError, json.JSONDecodeError, MemoryEncoderError) as exc:
        print(f"memory 模型操作失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
