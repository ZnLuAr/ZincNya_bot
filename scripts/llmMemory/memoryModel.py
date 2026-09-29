#!/usr/bin/env python3
"""
scripts/llmMemory/memoryModel.py

安装或校验固定版本的本地 memory 语义模型。

用法：`python scripts/llmMemory/memoryModel.py install` / `verify`。
模型身份（仓库 / 完整 commit SHA / 逐产物 SHA-256）以
modelManifest.json 为准，从 Hugging Face 按 revision 下载——不取
latest、不接受部分成功：install 在同级临时目录完整下载并校验后，
以目录级事务发布到 .cache/llmMemory/model，任一失败都会恢复现有安装。
"""

import os
import re
import sys
import json
import hashlib
import argparse
import shutil
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[2]
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
_DEFAULT_DOWNLOAD_ENDPOINT = "https://huggingface.co"
# repository 形如 owner/repo；revision 必须是完整 commit SHA——
# 拒绝 tag/branch，防止「同一 manifest 两次安装拉到不同模型」。
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")




def buildArtifactURL(
    manifest: dict,
    artifactPath: str,
    *,
    endpoint: str = _DEFAULT_DOWNLOAD_ENDPOINT,
) -> str:
    """为固定 repository 与 revision 构造 HTTPS 模型产物 URL。

    endpoint 只决定从哪个 Hugging Face 兼容端点取字节；产物身份仍由
    manifest 的完整 commit SHA、文件大小和 SHA-256 决定。
    """
    repository = str(manifest.get("repository", ""))
    revision = str(manifest.get("revision", ""))
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise MemoryEncoderError("模型清单中的 repository 无效")
    if not _REVISION_PATTERN.fullmatch(revision):
        raise MemoryEncoderError("模型清单中的 revision 必须是完整 commit SHA")

    endpoint = str(endpoint).strip().rstrip("/")
    parsedEndpoint = urlsplit(endpoint)
    if (
        parsedEndpoint.scheme != "https"
        or not parsedEndpoint.hostname
        or parsedEndpoint.username is not None
        or parsedEndpoint.password is not None
        or parsedEndpoint.query
        or parsedEndpoint.fragment
    ):
        raise MemoryEncoderError("模型下载 endpoint 必须是无凭据、无查询参数的 HTTPS URL")

    # manifest 校验会保护正常安装路径，但这个公开 helper 也可能被单独
    # 调用；先用同一解析器拒绝绝对路径、.. 和模型目录外的目标，不能
    # 只依赖 URL quote 把不安全路径编码掉。
    rootPath = Path(".").resolve()
    try:
        resolvedArtifactPath = resolveModelArtifactPath(rootPath, artifactPath)
    except MemoryEncoderError:
        raise MemoryEncoderError(f"模型产物路径不安全: {artifactPath}")
    if resolvedArtifactPath == rootPath:
        raise MemoryEncoderError(f"模型产物路径不安全: {artifactPath}")

    encodedRepository = quote(repository, safe="/")
    encodedRevision = quote(revision, safe="")
    encodedPath = quote(
        resolvedArtifactPath.relative_to(rootPath).as_posix(),
        safe="/",
    )
    return (
        f"{endpoint}/{encodedRepository}/resolve/"
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
    endpoint: str = _DEFAULT_DOWNLOAD_ENDPOINT,
) -> list[dict]:
    """在同级临时目录完整下载并校验后，事务发布固定模型产物。

    任一产物失败都不会把未完整校验的文件发布到正式模型目录；已通过
    校验的现有安装会直接复用，不访问网络。staging 放在目标目录同级
    是为了让目录 rename 落在同一文件系统上（跨盘 rename 会失败）。
    发布时先把旧目录移到唯一 backup，再把 staging 整体换入；换入或
    发布后校验失败时恢复 backup，不会留下新旧 artifact 混合目录。
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
    ) as transactionName:
        transactionPath = Path(transactionName)
        stagingPath = transactionPath / "staging"
        stagingPath.mkdir()
        for artifact in manifest["artifacts"]:
            relativePath = str(artifact.get("path", ""))
            stagedArtifact = resolveModelArtifactPath(stagingPath, relativePath)
            stagedArtifact.parent.mkdir(parents=True, exist_ok=True)
            _downloadArtifact(
                buildArtifactURL(manifest, relativePath, endpoint=endpoint),
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

        backupPath = Path(tempfile.mkdtemp(
            prefix=f".{modelPath.name}-backup-",
            dir=modelPath.parent,
        ))
        backupPath.rmdir()
        backupActive = False
        preserveBackup = False
        published = False
        try:
            if modelPath.exists():
                try:
                    os.replace(modelPath, backupPath)
                finally:
                    # os.replace 失败时通常不会改变文件系统，但不能把这个
                    # 假设当成回滚依据；若 backup 已出现，说明旧安装确实
                    # 已移走，仍必须尝试恢复它。
                    backupActive = backupPath.exists()
            os.replace(stagingPath, modelPath)
            published = True

            installed, installedStatuses = verifyModel(modelPath, manifestPath)
            if not installed:
                raise MemoryEncoderError("模型发布后校验失败")
        except BaseException as publishError:
            try:
                # 只有确认旧目录已移走，或确认 staging 已整体换入，才能
                # 清理目标路径。第一次备份 rename 失败且旧目录仍在时，
                # 绝不能把它当成“新目录”删除。
                backupActive = backupActive or backupPath.exists()
                if published or backupActive:
                    if modelPath.is_dir():
                        shutil.rmtree(modelPath)
                    elif modelPath.exists():
                        modelPath.unlink()
                if backupActive:
                    os.replace(backupPath, modelPath)
                    backupActive = False
            except Exception as rollbackError:
                # 回滚失败后 backup 是人工恢复旧安装的最后副本，不能再自动清理。
                preserveBackup = backupActive and backupPath.exists()
                raise MemoryEncoderError(
                    f"模型发布失败且自动回滚失败；旧安装保留于 {backupPath}"
                ) from rollbackError
            raise publishError
        finally:
            if backupActive and not preserveBackup and backupPath.exists():
                shutil.rmtree(backupPath)

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
    parser.add_argument(
        "--endpoint",
        default=_DEFAULT_DOWNLOAD_ENDPOINT,
        help="显式指定 Hugging Face 兼容 HTTPS 下载端点",
    )
    args = parser.parse_args()

    try:
        if args.action == "install":
            statuses = installModel(
                args.model_dir,
                args.manifest,
                endpoint=args.endpoint,
            )
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
