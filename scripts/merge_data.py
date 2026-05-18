#!/usr/bin/env python3
"""
ZincNya Bot 离线数据合并工具。

默认为 dry-run 模式，使用 --apply 写入变更。
若未指定 --source，则进入交互模式（通过 SSH 从远端拉取 DB，
左右分栏展示差异，按文件选择"保留本端 / 保留远端 / 合并"）。
本脚本不导入 config.py 或 bot 运行时模块。
"""

import argparse
import difflib
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_TARGET_DATA = PROJECT_ROOT / "data"
DEFAULT_BACKUP_ROOT = PROJECT_ROOT.parent / "data" / "zincnya_merge_backup"

SECTIONS = {"llm-memory", "chat-history", "json", "todos"}
DEFAULT_SECTIONS = {"llm-memory", "chat-history", "json", "todos"}

LLM_MEMORY_DB = "llmMemory.db"
CHAT_HISTORY_DB = "chatHistory.db"
CHAT_KEY = ".chatKey"
TODOS_DB = "todos.db"

JSON_REPORT_FILES = [
    "whitelist.json",
    "operators.json",
    "llmConfig.json",
    "prompts.json",
    "ZincNyaQuotes.json",
    "pushedNews.json",
]

MEMORY_REQUIRED_COLUMNS = {
    "id",
    "scope_type",
    "scope_id",
    "content",
    "tags_json",
    "enabled",
    "priority",
    "source",
    "created_at",
    "updated_at",
}
CHAT_REQUIRED_COLUMNS = {
    "id",
    "chat_id",
    "direction",
    "sender",
    "content",
    "timestamp",
}
TODOS_REQUIRED_COLUMNS = {
    "id",
    "chat_id",
    "user_id",
    "content",
    "remind_time",
    "priority",
    "status",
    "reminded",
    "created_at",
    "completed_at",
}

VALID_MEMORY_SCOPE_TYPES = {"global", "chat", "user", "session"}
VALID_MEMORY_SOURCES = {"manual", "inferred"}
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
TIMESTAMP_SHORT_FORMAT = "%Y%m%d%H%M%S"

MEMORY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    content TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_entries(scope_type, scope_id);
CREATE INDEX IF NOT EXISTS idx_memory_enabled ON memory_entries(enabled);
CREATE INDEX IF NOT EXISTS idx_memory_priority ON memory_entries(priority DESC);
CREATE INDEX IF NOT EXISTS idx_memory_updated_at ON memory_entries(updated_at DESC);
"""

CHAT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    sender TEXT,
    content BLOB NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chat_id ON messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp);
"""




class MergeError(Exception):
    pass




@dataclass
class PlannedInsert:
    source_id: int | None
    values: dict[str, Any]
    preview_lines: list[str]




@dataclass
class SectionPlan:
    name: str
    merge_capable: bool
    apply_capable: bool
    source_path: Path | None = None
    target_path: Path | None = None
    inserts: list[PlannedInsert] = field(default_factory=list)
    preview_lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    report_only: bool = False


    def has_output(self) -> bool:
        return bool(self.preview_lines or self.warnings or self.errors)




@dataclass
class ScriptContext:
    source_data: Path
    target_data: Path
    selected_sections: set[str]
    apply: bool
    max_preview: int
    max_json_diff_lines: int
    show_full_content: bool
    no_content: bool
    backup_dir: Path
    allow_wal: bool




def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline merge utility for ZincNya Bot data directories.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source", type=Path, help="Source data directory or project root containing data/. If omitted, enters interactive mode.")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET_DATA, help="Target data directory.")
    parser.add_argument("--apply", action="store_true", help="Apply merge-capable inserts. Default is dry-run.")
    parser.add_argument("--only", help="Comma-separated sections: llm-memory,chat-history,json,todos.")
    parser.add_argument("--skip", help="Comma-separated sections to skip.")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_ROOT, help="Backup root used in --apply mode.")
    parser.add_argument("--max-preview", type=int, default=50, help="Maximum DB insert preview rows per section.")
    parser.add_argument("--max-json-diff-lines", type=int, default=300, help="Maximum unified diff lines per JSON file.")
    parser.add_argument("--no-content", action="store_true", help="Hide chat plaintext in previews; show hash and length instead.")
    parser.add_argument("--show-full-content", action="store_true", help="Show full memory/chat content instead of snippets.")
    parser.add_argument("--allow-wal", action="store_true", help="Allow --apply when non-empty SQLite WAL sidecars are present.")
    return parser.parse_args()




def parse_section_list(value: str | None, *, option_name: str) -> set[str] | None:
    if value is None:
        return None
    result = {item.strip() for item in value.split(",") if item.strip()}
    unknown = result - SECTIONS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise MergeError(f"unknown section in {option_name}: {names}")
    return result




def resolve_data_dir(raw_path: Path) -> Path:
    path = raw_path.expanduser().resolve()
    nested = path / "data"
    if nested.is_dir():
        return nested
    return path




def build_context(args: argparse.Namespace) -> ScriptContext:
    if args.no_content and args.show_full_content:
        raise MergeError("--no-content and --show-full-content cannot be used together")
    if args.max_preview < 0:
        raise MergeError("--max-preview must be >= 0")
    if args.max_json_diff_lines < 0:
        raise MergeError("--max-json-diff-lines must be >= 0")

    only = parse_section_list(args.only, option_name="--only")
    skip = parse_section_list(args.skip, option_name="--skip") or set()
    selected = set(only if only is not None else DEFAULT_SECTIONS)
    selected -= skip
    if not selected:
        raise MergeError("no sections selected")

    source_data = resolve_data_dir(args.source)
    target_data = resolve_data_dir(args.target)
    if not source_data.is_dir():
        raise MergeError(f"source data directory does not exist: {source_data}")
    if not target_data.is_dir():
        raise MergeError(f"target data directory does not exist: {target_data}")
    if source_data.resolve() == target_data.resolve():
        raise MergeError("source and target data directories resolve to the same path")

    return ScriptContext(
        source_data=source_data,
        target_data=target_data,
        selected_sections=selected,
        apply=args.apply,
        max_preview=args.max_preview,
        max_json_diff_lines=args.max_json_diff_lines,
        show_full_content=args.show_full_content,
        no_content=args.no_content,
        backup_dir=args.backup_dir.expanduser().resolve(),
        allow_wal=args.allow_wal,
    )




def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn




def connect_writable(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn




def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None




_VALID_TABLE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _VALID_TABLE_NAME_RE.match(table):
        raise MergeError(f"invalid table name: {table}")
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}




def require_columns(conn: sqlite3.Connection, table: str, required: set[str]) -> None:
    columns = table_columns(conn, table)
    missing = required - columns
    if missing:
        names = ", ".join(sorted(missing))
        raise MergeError(f"table {table} is missing required columns: {names}")




def fetch_rows(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql).fetchall()]




def sqlite_sidecars(db_path: Path) -> list[Path]:
    return [Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]




def selected_db_names(sections: set[str]) -> list[str]:
    names = []
    if "llm-memory" in sections:
        names.append(LLM_MEMORY_DB)
    if "chat-history" in sections:
        names.append(CHAT_HISTORY_DB)
    if "todos" in sections:
        names.append(TODOS_DB)
    return names




def wal_preflight(ctx: ScriptContext) -> tuple[list[str], list[str]]:
    warnings = []
    errors = []
    for label, data_dir in (("source", ctx.source_data), ("target", ctx.target_data)):
        for db_name in selected_db_names(ctx.selected_sections):
            db_path = data_dir / db_name
            for sidecar in sqlite_sidecars(db_path):
                if sidecar.exists() and sidecar.stat().st_size > 0:
                    message = (
                        f"{label} {sidecar.name} is non-empty. Stop the bot before applying, "
                        "or ensure the DB/WAL snapshot is complete."
                    )
                    warnings.append(message)
                    if ctx.apply and not ctx.allow_wal:
                        errors.append(message)
    return warnings, errors




def ensure_memory_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(MEMORY_SCHEMA_SQL)




def ensure_chat_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CHAT_SCHEMA_SQL)




def read_table_rows(
    db_path: Path,
    *,
    table: str,
    required_columns: set[str],
    sql: str,
) -> tuple[list[dict[str, Any]], str | None]:
    if not db_path.exists():
        return [], "missing"
    conn = None
    try:
        conn = connect_readonly(db_path)
        if not table_exists(conn, table):
            raise MergeError(f"table {table} does not exist")
        require_columns(conn, table, required_columns)
        return fetch_rows(conn, sql), None
    except sqlite3.Error as exc:
        raise MergeError(f"failed to read {db_path.name}: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()




def normalize_tags(value: Any) -> tuple[list[str], tuple[str, ...]]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise MergeError(f"invalid tags_json: {exc}") from exc
    if not isinstance(parsed, list):
        raise MergeError("tags_json is not a JSON list")

    result = []
    seen = set()
    for tag in parsed:
        text = str(tag).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result, tuple(sorted(result))




def normalize_memory_row(row: dict[str, Any]) -> dict[str, Any]:
    scope_type = str(row.get("scope_type") or "").strip().lower()
    if scope_type not in VALID_MEMORY_SCOPE_TYPES:
        raise MergeError(f"invalid scope_type: {scope_type}")

    if scope_type == "global":
        scope_id = "global"
    else:
        scope_id = str(row.get("scope_id") or "").strip()
        if not scope_id:
            raise MergeError(f"scope_id is empty for scope_type={scope_type}")

    content = str(row.get("content") or "").strip()
    if not content:
        raise MergeError("content is empty")

    source = str(row.get("source") or "manual").strip().lower()
    if source not in VALID_MEMORY_SOURCES:
        raise MergeError(f"invalid source: {source}")

    tags, tags_key = normalize_tags(row.get("tags_json"))
    enabled = int(row.get("enabled") if row.get("enabled") is not None else 1)
    priority = int(row.get("priority") if row.get("priority") is not None else 0)
    key = (scope_type, scope_id, content, source, tags_key)

    return {
        "id": row.get("id"),
        "key": key,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "content": content,
        "tags": tags,
        "tags_json": json.dumps(tags, ensure_ascii=False),
        "enabled": enabled,
        "priority": priority,
        "source": source,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }




def preview_text(text: str, *, full: bool, limit: int = 100) -> str:
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    if full or len(text) <= limit:
        return text
    return text[:limit] + "..."




def format_memory_insert(row: dict[str, Any], *, full: bool) -> list[str]:
    source_id = row.get("id")
    tags = json.dumps(row["tags"], ensure_ascii=False)
    return [
        (
            f"+ memory source_id={source_id} scope={row['scope_type']}:{row['scope_id']} "
            f"source={row['source']} enabled={row['enabled']} priority={row['priority']} tags={tags}"
        ),
        f"+   content: {preview_text(row['content'], full=full)}",
    ]




def plan_llm_memory(ctx: ScriptContext) -> SectionPlan:
    source_path = ctx.source_data / LLM_MEMORY_DB
    target_path = ctx.target_data / LLM_MEMORY_DB
    plan = SectionPlan(
        name="llm-memory",
        merge_capable=True,
        apply_capable=True,
        source_path=source_path,
        target_path=target_path,
    )
    plan.preview_lines.extend([
        f"diff --zincnya-data a/data/{LLM_MEMORY_DB} b/source-data/{LLM_MEMORY_DB}",
        "@@ memory_entries @@",
    ])

    sql = (
        "SELECT id, scope_type, scope_id, content, tags_json, enabled, priority, "
        "source, created_at, updated_at FROM memory_entries "
        "ORDER BY scope_type, scope_id, source, content, id"
    )

    try:
        source_rows, source_state = read_table_rows(
            source_path,
            table="memory_entries",
            required_columns=MEMORY_REQUIRED_COLUMNS,
            sql=sql,
        )
        if source_state == "missing":
            plan.apply_capable = False
            plan.warnings.append("source llmMemory.db not found; skipping llm-memory")
            plan.preview_lines.append("# 跳过：源 llmMemory.db 不存在")
            return plan

        target_rows, target_state = read_table_rows(
            target_path,
            table="memory_entries",
            required_columns=MEMORY_REQUIRED_COLUMNS,
            sql=sql,
        )
        if target_state == "missing":
            plan.warnings.append("target llmMemory.db not found; --apply will create it")

        target_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
        skipped_target = 0
        for row in target_rows:
            try:
                normalized = normalize_memory_row(row)
                target_by_key.setdefault(normalized["key"], normalized)
            except MergeError as exc:
                skipped_target += 1
                plan.warnings.append(f"target memory id={row.get('id')} skipped while matching: {exc}")

        new_rows = []
        duplicates = 0
        metadata_differences = 0
        skipped_source = 0
        metadata_preview = []
        for row in source_rows:
            try:
                normalized = normalize_memory_row(row)
            except MergeError as exc:
                skipped_source += 1
                plan.warnings.append(f"source memory id={row.get('id')} skipped: {exc}")
                continue

            target_match = target_by_key.get(normalized["key"])
            if target_match is None:
                new_rows.append(normalized)
                continue

            duplicates += 1
            changed = []
            for field_name in ("enabled", "priority", "created_at", "updated_at"):
                if str(target_match.get(field_name)) != str(normalized.get(field_name)):
                    changed.append(f"{field_name} target={target_match.get(field_name)!r} source={normalized.get(field_name)!r}")
            if changed:
                metadata_differences += 1
                if len(metadata_preview) < ctx.max_preview:
                    metadata_preview.append(
                        f"~ memory source_id={normalized.get('id')} target_id={target_match.get('id')} metadata differs: "
                        + "; ".join(changed)
                    )

        plan.stats.update({
            "target_rows": len(target_rows),
            "source_rows": len(source_rows),
            "new_rows": len(new_rows),
            "duplicates": duplicates,
            "metadata_differences": metadata_differences,
            "skipped_source": skipped_source,
            "skipped_target": skipped_target,
        })
        plan.preview_lines.extend([
            f"# 目标行数: {len(target_rows)}",
            f"# 源行数: {len(source_rows)}",
            f"# 新增行数: {len(new_rows)}",
            f"# 重复行数: {duplicates}",
            f"# 元数据差异: {metadata_differences}",
            f"# 跳过无效源行数: {skipped_source}",
        ])

        for normalized in new_rows[:ctx.max_preview]:
            values = {
                "scope_type": normalized["scope_type"],
                "scope_id": normalized["scope_id"],
                "content": normalized["content"],
                "tags_json": normalized["tags_json"],
                "enabled": normalized["enabled"],
                "priority": normalized["priority"],
                "source": normalized["source"],
                "created_at": normalized["created_at"],
                "updated_at": normalized["updated_at"],
            }
            preview_lines = format_memory_insert(normalized, full=ctx.show_full_content)
            plan.inserts.append(PlannedInsert(normalized.get("id"), values, preview_lines))
            plan.preview_lines.extend(preview_lines)

        hidden = len(new_rows) - min(len(new_rows), ctx.max_preview)
        if hidden > 0:
            plan.preview_lines.append(f"# ... {hidden} 条记忆记录未显示，增大 --max-preview 可查看更多")
            for normalized in new_rows[ctx.max_preview:]:
                values = {
                    "scope_type": normalized["scope_type"],
                    "scope_id": normalized["scope_id"],
                    "content": normalized["content"],
                    "tags_json": normalized["tags_json"],
                    "enabled": normalized["enabled"],
                    "priority": normalized["priority"],
                    "source": normalized["source"],
                    "created_at": normalized["created_at"],
                    "updated_at": normalized["updated_at"],
                }
                plan.inserts.append(PlannedInsert(normalized.get("id"), values, []))

        plan.preview_lines.extend(metadata_preview)
        if metadata_differences > len(metadata_preview):
            remaining = metadata_differences - len(metadata_preview)
            plan.preview_lines.append(f"# ... {remaining} 条元数据差异未显示")

    except MergeError as exc:
        plan.apply_capable = False
        plan.errors.append(str(exc))
        plan.preview_lines.append(f"# error: {exc}")
    return plan




def canonical_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).strftime(TIMESTAMP_FORMAT)
    except ValueError:
        return text


def short_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "?"
    try:
        return datetime.fromisoformat(text).strftime(TIMESTAMP_SHORT_FORMAT)
    except ValueError:
        return text




def decrypt_chat_content(fernet: Any, row: dict[str, Any]) -> str:
    content = row.get("content")
    if isinstance(content, memoryview):
        content = content.tobytes()
    elif isinstance(content, str):
        content = content.encode("utf-8")
    else:
        content = bytes(content)
    return fernet.decrypt(content).decode("utf-8")




def chat_key_for(row: dict[str, Any], plaintext: str) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("chat_id") or ""),
        str(row.get("direction") or ""),
        str(row.get("sender") or ""),
        canonical_timestamp(row.get("timestamp")),
        plaintext,
    )




def format_chat_insert(row: dict[str, Any], plaintext: str, *, ctx: ScriptContext) -> list[str]:
    timestamp = canonical_timestamp(row.get("timestamp")) or "?"
    direction = str(row.get("direction") or "?")
    sender = str(row.get("sender") or "")
    first = f"+ {timestamp} chat={row.get('chat_id')} {direction} {sender}".rstrip()
    if ctx.no_content:
        digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()[:12]
        second = f"+   content_sha256={digest} chars={len(plaintext)}"
    else:
        second = f"+   content: {preview_text(plaintext, full=ctx.show_full_content)}"
    return [first, second]




def plan_chat_history(ctx: ScriptContext) -> SectionPlan:
    source_path = ctx.source_data / CHAT_HISTORY_DB
    target_path = ctx.target_data / CHAT_HISTORY_DB
    source_key_path = ctx.source_data / CHAT_KEY
    target_key_path = ctx.target_data / CHAT_KEY
    plan = SectionPlan(
        name="chat-history",
        merge_capable=True,
        apply_capable=True,
        source_path=source_path,
        target_path=target_path,
    )
    plan.preview_lines.extend([
        f"diff --zincnya-data a/data/{CHAT_HISTORY_DB} b/source-data/{CHAT_HISTORY_DB}",
        "@@ messages @@",
    ])

    if not source_path.exists():
        plan.apply_capable = False
        plan.warnings.append("source chatHistory.db not found; skipping chat-history")
        plan.preview_lines.append("# 跳过：源 chatHistory.db 不存在")
        return plan
    if not source_key_path.exists():
        plan.apply_capable = False
        plan.warnings.append("source .chatKey not found; skipping chat-history")
        plan.preview_lines.append("# 跳过：源 .chatKey 不存在")
        return plan
    if not target_key_path.exists():
        plan.apply_capable = False
        plan.warnings.append("target .chatKey not found; skipping chat-history")
        plan.preview_lines.append("# 跳过：目标 .chatKey 不存在")
        return plan

    source_key = source_key_path.read_bytes()
    target_key = target_key_path.read_bytes()
    if source_key != target_key:
        plan.apply_capable = False
        plan.warnings.append("source and target .chatKey differ; skipping chat-history")
        plan.preview_lines.extend([
            "# 跳过：源与目标 .chatKey 不一致",
            "# 原因：v1 不重新加密聊天记录，直接复制密文会导致行不可读",
        ])
        return plan

    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        plan.apply_capable = False
        plan.errors.append("cryptography is not installed; cannot decrypt chatHistory.db")
        plan.preview_lines.append("# 错误：未安装 cryptography 库，无法解密 chatHistory.db")
        return plan

    sql = "SELECT id, chat_id, direction, sender, content, timestamp FROM messages ORDER BY timestamp, id"

    try:
        fernet = Fernet(target_key)
        source_rows, _ = read_table_rows(
            source_path,
            table="messages",
            required_columns=CHAT_REQUIRED_COLUMNS,
            sql=sql,
        )
        target_rows, target_state = read_table_rows(
            target_path,
            table="messages",
            required_columns=CHAT_REQUIRED_COLUMNS,
            sql=sql,
        )
        if target_state == "missing":
            plan.warnings.append("target chatHistory.db not found; --apply will create it")

        target_keys = set()
        skipped_target = 0
        for row in target_rows:
            try:
                plaintext = decrypt_chat_content(fernet, row)
                target_keys.add(chat_key_for(row, plaintext))
            except Exception as exc:
                skipped_target += 1
                plan.warnings.append(f"target chat row id={row.get('id')} could not be decrypted: {exc}")

        new_rows = []
        duplicates = 0
        skipped_source = 0
        for row in source_rows:
            try:
                plaintext = decrypt_chat_content(fernet, row)
            except Exception as exc:
                skipped_source += 1
                plan.warnings.append(f"source chat row id={row.get('id')} skipped: {exc}")
                continue
            key = chat_key_for(row, plaintext)
            if key in target_keys:
                duplicates += 1
                continue
            new_rows.append((row, plaintext))

        plan.stats.update({
            "target_rows": len(target_rows),
            "source_rows": len(source_rows),
            "new_rows": len(new_rows),
            "duplicates": duplicates,
            "skipped_source": skipped_source,
            "skipped_target": skipped_target,
        })
        plan.preview_lines.extend([
            f"# 目标行数: {len(target_rows)}",
            f"# 源行数: {len(source_rows)}",
            f"# 新增行数: {len(new_rows)}",
            f"# 重复行数: {duplicates}",
            f"# 跳过无法解密的源行数: {skipped_source}",
            f"# 匹配时跳过无法解密的目标行数: {skipped_target}",
        ])

        for row, plaintext in new_rows[:ctx.max_preview]:
            content = row.get("content")
            if isinstance(content, memoryview):
                content = content.tobytes()
            else:
                content = bytes(content)
            values = {
                "chat_id": str(row.get("chat_id") or ""),
                "direction": str(row.get("direction") or ""),
                "sender": row.get("sender"),
                "content": content,
                "timestamp": row.get("timestamp"),
            }
            preview_lines = format_chat_insert(row, plaintext, ctx=ctx)
            plan.inserts.append(PlannedInsert(row.get("id"), values, preview_lines))
            plan.preview_lines.extend(preview_lines)

        hidden = len(new_rows) - min(len(new_rows), ctx.max_preview)
        if hidden > 0:
            plan.preview_lines.append(f"# ... {hidden} 条聊天记录未显示，增大 --max-preview 可查看更多")
            for row, _plaintext in new_rows[ctx.max_preview:]:
                content = row.get("content")
                if isinstance(content, memoryview):
                    content = content.tobytes()
                else:
                    content = bytes(content)
                values = {
                    "chat_id": str(row.get("chat_id") or ""),
                    "direction": str(row.get("direction") or ""),
                    "sender": row.get("sender"),
                    "content": content,
                    "timestamp": row.get("timestamp"),
                }
                plan.inserts.append(PlannedInsert(row.get("id"), values, []))

    except MergeError as exc:
        plan.apply_capable = False
        plan.errors.append(str(exc))
        plan.preview_lines.append(f"# error: {exc}")
    return plan


def read_text_if_exists(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def canonical_json_text(text: str) -> str:
    parsed = json.loads(text)
    return json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def diff_text_lines(
    target_text: str,
    source_text: str,
    *,
    target_label: str,
    source_label: str,
    max_lines: int,
) -> list[str]:
    lines = list(difflib.unified_diff(
        target_text.splitlines(),
        source_text.splitlines(),
        fromfile=target_label,
        tofile=source_label,
        lineterm="",
    ))
    if max_lines and len(lines) > max_lines:
        hidden = len(lines) - max_lines
        lines = lines[:max_lines]
        lines.append(f"# ... {hidden} 行差异未显示，增大 --max-json-diff-lines 可查看更多")
    return lines


def plan_json_reports(ctx: ScriptContext) -> list[SectionPlan]:
    plans = []
    for filename in JSON_REPORT_FILES:
        source_path = ctx.source_data / filename
        target_path = ctx.target_data / filename
        plan = SectionPlan(
            name=f"json:{filename}",
            merge_capable=False,
            apply_capable=False,
            source_path=source_path,
            target_path=target_path,
            report_only=True,
        )

        source_text = read_text_if_exists(source_path)
        target_text = read_text_if_exists(target_path)
        if source_text is None and target_text is None:
            plan.stats["identical"] = 1
            plans.append(plan)
            continue

        plan.preview_lines.extend([
            f"diff --git a/data/{filename} b/source-data/{filename}",
            "# 仅报告：此文件不会被 --apply 自动合并",
        ])
        if source_text is None:
            plan.preview_lines.append("# 跳过：源文件不存在，目标文件不会被删除")
            plan.stats["source_missing"] = 1
            plans.append(plan)
            continue
        if target_text is None:
            target_text = ""
            plan.preview_lines.append("# 目标文件不存在，仅预览新增内容")

        try:
            source_diff_text = canonical_json_text(source_text)
            target_diff_text = canonical_json_text(target_text) if target_text else ""
        except json.JSONDecodeError as exc:
            plan.warnings.append(f"{filename}: invalid JSON; using raw text diff: {exc}")
            source_diff_text = source_text
            target_diff_text = target_text

        diff_lines = diff_text_lines(
            target_diff_text,
            source_diff_text,
            target_label=f"a/data/{filename}",
            source_label=f"b/source-data/{filename}",
            max_lines=ctx.max_json_diff_lines,
        )
        if diff_lines:
            plan.preview_lines.extend(diff_lines)
            plan.stats["different"] = 1
        else:
            plan.preview_lines = []
            plan.stats["identical"] = 1
        plans.append(plan)
    return plans


def todo_key(row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(row.get("chat_id") or ""),
        str(row.get("user_id") or ""),
        str(row.get("content") or "").strip(),
        str(row.get("remind_time") or ""),
        str(row.get("priority") or "P_"),
        str(row.get("created_at") or ""),
    )


def todo_status_tuple(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("status") or ""),
        str(row.get("reminded") or ""),
        str(row.get("completed_at") or ""),
    )


def plan_todos_report(ctx: ScriptContext) -> SectionPlan:
    source_path = ctx.source_data / TODOS_DB
    target_path = ctx.target_data / TODOS_DB
    plan = SectionPlan(
        name="todos",
        merge_capable=False,
        apply_capable=False,
        source_path=source_path,
        target_path=target_path,
        report_only=True,
    )
    plan.preview_lines.extend([
        f"diff --zincnya-data a/data/{TODOS_DB} b/source-data/{TODOS_DB}",
        "@@ todos @@",
        "# 仅报告：v1 不自动合并待办记录",
    ])

    sql = (
        "SELECT id, chat_id, user_id, content, remind_time, priority, status, "
        "reminded, created_at, completed_at FROM todos ORDER BY created_at, id"
    )

    try:
        source_rows, source_state = read_table_rows(
            source_path,
            table="todos",
            required_columns=TODOS_REQUIRED_COLUMNS,
            sql=sql,
        )
        if source_state == "missing":
            plan.preview_lines.append("# 跳过：源 todos.db 不存在")
            plan.warnings.append("source todos.db not found; skipping todos report")
            return plan

        target_rows, target_state = read_table_rows(
            target_path,
            table="todos",
            required_columns=TODOS_REQUIRED_COLUMNS,
            sql=sql,
        )
        if target_state == "missing":
            target_rows = []
            plan.warnings.append("target todos.db not found; reporting source rows as source-only")

        target_by_key = {todo_key(row): row for row in target_rows}
        source_only = 0
        duplicates = 0
        ambiguous = 0
        preview_count = 0
        for row in source_rows:
            key = todo_key(row)
            target_row = target_by_key.get(key)
            if target_row is None:
                source_only += 1
                if preview_count < ctx.max_preview:
                    timestamp = row.get("created_at") or "?"
                    preview = preview_text(str(row.get("content") or ""), full=ctx.show_full_content)
                    plan.preview_lines.append(f"+ todo source_id={row.get('id')} user={row.get('user_id')} chat={row.get('chat_id')} created_at={timestamp}")
                    plan.preview_lines.append(f"+   content: {preview}")
                    preview_count += 1
                continue
            duplicates += 1
            if todo_status_tuple(row) != todo_status_tuple(target_row):
                ambiguous += 1

        plan.stats.update({
            "target_rows": len(target_rows),
            "source_rows": len(source_rows),
            "source_only": source_only,
            "duplicates": duplicates,
            "ambiguous": ambiguous,
        })
        plan.preview_lines[2:2] = [
            f"# 目标行数: {len(target_rows)}",
            f"# 源行数: {len(source_rows)}",
            f"# 疑似仅源端行数: {source_only}",
            f"# 疑似重复行数: {duplicates}",
            f"# 模糊/冲突行数: {ambiguous}",
        ]
        hidden = source_only - min(source_only, ctx.max_preview)
        if hidden > 0:
            plan.preview_lines.append(f"# ... {hidden} 条待办记录未显示，增大 --max-preview 可查看更多")
    except MergeError as exc:
        plan.errors.append(str(exc))
        plan.preview_lines.append(f"# error: {exc}")
    return plan


def create_backup_dir(root: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = root / f"merge_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    return backup_dir


def files_to_backup(ctx: ScriptContext, plans: list[SectionPlan]) -> list[Path]:
    result = []
    names_with_inserts = {plan.name for plan in plans if plan.apply_capable and plan.inserts}
    if "llm-memory" in names_with_inserts:
        db_path = ctx.target_data / LLM_MEMORY_DB
        result.append(db_path)
        result.extend(sqlite_sidecars(db_path))
    if "chat-history" in names_with_inserts:
        db_path = ctx.target_data / CHAT_HISTORY_DB
        result.append(db_path)
        result.append(ctx.target_data / CHAT_KEY)
        result.extend(sqlite_sidecars(db_path))
    unique = []
    seen = set()
    for path in result:
        if path in seen:
            continue
        seen.add(path)
        if path.exists():
            unique.append(path)
    return unique


def backup_target_files(files: list[Path], backup_dir: Path) -> list[str]:
    copied = []
    for path in files:
        dest = backup_dir / path.name
        shutil.copy2(path, dest)
        copied.append(path.name)
    return copied


def write_manifest(
    backup_dir: Path,
    ctx: ScriptContext,
    plans: list[SectionPlan],
    applied_counts: dict[str, int],
    backed_up_files: list[str],
) -> None:
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_data": str(ctx.source_data),
        "target_data": str(ctx.target_data),
        "mode": "apply",
        "backed_up_files": backed_up_files,
        "sections": {},
    }
    for plan in plans:
        if not plan.merge_capable:
            continue
        manifest["sections"][plan.name] = {
            "planned_inserts": len(plan.inserts),
            "applied_inserts": applied_counts.get(plan.name, 0),
            "stats": plan.stats,
        }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_llm_memory(plan: SectionPlan) -> int:
    if not plan.inserts or plan.target_path is None:
        return 0
    conn = connect_writable(plan.target_path)
    try:
        ensure_memory_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            """
            INSERT INTO memory_entries (
                scope_type, scope_id, content, tags_json, enabled, priority, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.values["scope_type"],
                    item.values["scope_id"],
                    item.values["content"],
                    item.values["tags_json"],
                    item.values["enabled"],
                    item.values["priority"],
                    item.values["source"],
                    item.values["created_at"],
                    item.values["updated_at"],
                )
                for item in plan.inserts
            ],
        )
        conn.commit()
        return len(plan.inserts)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_chat_history(plan: SectionPlan) -> int:
    if not plan.inserts or plan.target_path is None:
        return 0
    conn = connect_writable(plan.target_path)
    try:
        ensure_chat_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            """
            INSERT INTO messages (chat_id, direction, sender, content, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    item.values["chat_id"],
                    item.values["direction"],
                    item.values["sender"],
                    item.values["content"],
                    item.values["timestamp"],
                )
                for item in plan.inserts
            ],
        )
        conn.commit()
        return len(plan.inserts)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def build_plans(ctx: ScriptContext) -> list[SectionPlan]:
    plans = []
    if "llm-memory" in ctx.selected_sections:
        plans.append(plan_llm_memory(ctx))
    if "chat-history" in ctx.selected_sections:
        plans.append(plan_chat_history(ctx))
    if "todos" in ctx.selected_sections:
        plans.append(plan_todos_report(ctx))
    if "json" in ctx.selected_sections:
        plans.extend(plan_json_reports(ctx))
    return plans


def print_preview(ctx: ScriptContext, plans: list[SectionPlan], global_warnings: list[str], global_errors: list[str]) -> None:
    print("ZincNya offline data merge")
    print()
    print(f"Source data: {ctx.source_data}")
    print(f"Target data: {ctx.target_data}")
    print(f"Mode: {'apply' if ctx.apply else 'dry-run'}")
    if ctx.apply:
        print(f"Backup root: {ctx.backup_dir}")
    print()

    for warning in global_warnings:
        print(f"warning: {warning}")
    for error in global_errors:
        print(f"error: {error}")
    if global_warnings or global_errors:
        print()

    for plan in plans:
        if not plan.has_output():
            continue
        for warning in plan.warnings:
            print(f"warning: {warning}")
        for error in plan.errors:
            print(f"error: {error}")
        for line in plan.preview_lines:
            print(line)
        print()

    print_summary(plans)


def print_summary(plans: list[SectionPlan]) -> None:
    print("Summary:")
    print("  apply-capable inserts:")
    any_apply = False
    for plan in plans:
        if not plan.merge_capable:
            continue
        count = len(plan.inserts) if plan.apply_capable else 0
        print(f"    {plan.name}: {count}")
        any_apply = any_apply or count > 0
    if not any_apply:
        print("    none")

    report_lines = []
    for plan in plans:
        if plan.report_only and (plan.stats.get("different") or plan.stats.get("source_only") or plan.errors or plan.warnings):
            report_lines.append(plan.name)
    if report_lines:
        print("  report-only differences or warnings:")
        for name in report_lines:
            print(f"    {name}")


def blocking_apply_errors(plans: list[SectionPlan], global_errors: list[str]) -> list[str]:
    errors = list(global_errors)
    for plan in plans:
        if plan.merge_capable and plan.errors:
            errors.extend(f"{plan.name}: {error}" for error in plan.errors)
    return errors


def has_applyable_inserts(plans: list[SectionPlan]) -> bool:
    return any(plan.apply_capable and plan.inserts for plan in plans)


def apply_plans(plans: list[SectionPlan]) -> dict[str, int]:
    applied = {}
    for plan in plans:
        if not plan.apply_capable or not plan.inserts:
            continue
        if plan.name == "llm-memory":
            applied[plan.name] = apply_llm_memory(plan)
        elif plan.name == "chat-history":
            applied[plan.name] = apply_chat_history(plan)
    return applied


def main() -> int:
    try:
        args = parse_args()
        if args.source is None:
            return interactive_main()
        ctx = build_context(args)
        global_warnings, global_errors = wal_preflight(ctx)
        plans = build_plans(ctx)
        print_preview(ctx, plans, global_warnings, global_errors)

        if not ctx.apply:
            print("Dry-run only. No files changed.")
            print("Run again with --apply to merge apply-capable database rows.")
            return 0

        errors = blocking_apply_errors(plans, global_errors)
        if errors:
            print("Apply aborted due to blocking errors:")
            for error in errors:
                print(f"  - {error}")
            return 1

        if not has_applyable_inserts(plans):
            print("Nothing to apply. No files changed.")
            return 0

        backup_dir = create_backup_dir(ctx.backup_dir)
        backed_up_files = backup_target_files(files_to_backup(ctx, plans), backup_dir)
        applied_counts = apply_plans(plans)
        write_manifest(backup_dir, ctx, plans, applied_counts, backed_up_files)

        print("Applied:")
        for name, count in applied_counts.items():
            print(f"  {name}: inserted {count} rows")
        print(f"Backup created at: {backup_dir}")
        print("Backup contains sensitive bot data. Keep it private.")
        print("To rollback manually, stop the bot and copy the backed-up files back into the target data directory.")
        report_only = [plan.name for plan in plans if plan.report_only and plan.has_output()]
        if report_only:
            print("Report-only sections were not modified:")
            for name in report_only:
                print(f"  {name}")
        return 0

    except MergeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 1


# ============================================================================
# 交互模式 — SSH 基础设施
# ============================================================================

ENV_FILE = PROJECT_ROOT / ".env"

DB_FILES = [LLM_MEMORY_DB, CHAT_HISTORY_DB, TODOS_DB]


@dataclass
class RemoteConfig:
    host: str
    port: str
    path: str


@dataclass
class InteractiveSettings:
    target_data: Path = DEFAULT_TARGET_DATA
    backup_dir: Path = DEFAULT_BACKUP_ROOT
    max_preview: int = 50
    no_content: bool = False
    show_full_content: bool = False
    allow_wal: bool = False


def load_env_config() -> RemoteConfig | None:
    if not ENV_FILE.exists():
        return None
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    host = values.get("REMOTE_HOST")
    port = values.get("REMOTE_PORT")
    path = values.get("REMOTE_PATH")
    if not host or not port or not path:
        return None
    if not path.endswith("/"):
        path += "/"
    return RemoteConfig(host=host, port=port, path=path)


def ssh_test(cfg: RemoteConfig) -> bool:
    try:
        result = subprocess.run(
            ["ssh", "-p", cfg.port, "-o", "ConnectTimeout=10", cfg.host, "echo ok"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def ssh_list_files(cfg: RemoteConfig) -> list[tuple[str, int]]:
    try:
        result = subprocess.run(
            ["ssh", "-p", cfg.port, cfg.host,
             f"find {cfg.path} -maxdepth 1 -type f -printf '%f\\t%s\\n'"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if result.returncode != 0:
            return []
        files = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                files.append((parts[0], int(parts[1])))
        return sorted(files, key=lambda x: x[0])
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return []


def ssh_download(cfg: RemoteConfig, name: str, dest: Path) -> bool:
    remote = f"{cfg.host}:{cfg.path}{name}"
    try:
        result = subprocess.run(
            ["scp", "-P", cfg.port, remote, str(dest)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ============================================================================
# 交互模式 — 终端工具
# ============================================================================

class Color:
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    DIM = "\033[90m"
    RESET = "\033[0m"


def _enable_win_vt() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def supports_color() -> bool:
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        _enable_win_vt()
        return True
    return True


_USE_COLOR = supports_color()


def c(text: str, color: str) -> str:
    if not _USE_COLOR:
        return text
    return f"{color}{text}{Color.RESET}"


def format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def select_parse(raw: str, max_idx: int) -> list[int]:
    trimmed = raw.strip()
    if not trimmed or trimmed == "0":
        return []
    if trimmed == "*":
        return list(range(1, max_idx + 1))
    result = []
    for tok in trimmed.replace(",", " ").split():
        try:
            num = int(tok)
            if 1 <= num <= max_idx:
                if num not in result:
                    result.append(num)
            else:
                print(c(f"  序号 {num} 超出范围 (1-{max_idx})，已跳过", Color.YELLOW))
        except ValueError:
            print(c(f"  无法识别 '{tok}'，已跳过", Color.YELLOW))
    return result


def prompt_confirm(message: str) -> bool:
    try:
        answer = input(f"{message} (y/n): ").strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def show_file_comparison(
    local_files: list[tuple[str, int]],
    remote_files: list[tuple[str, int]],
    diff_stats: dict[str, tuple[int, int]] | None = None,
) -> dict[int, str]:
    local_map = {name: size for name, size in local_files}
    remote_map = {name: size for name, size in remote_files}
    all_names = sorted(set(local_map.keys()) | set(remote_map.keys()))

    if not all_names:
        print(c("  两侧都没有文件", Color.DIM))
        return {}

    name_width = max(max(len(n) for n in all_names), 10)
    name_width = min(name_width, 28)
    size_width = 9
    num_width = 5
    diff_width = 14 if diff_stats else 0

    print()
    left_header = f"  {'':>{num_width}}{'本端':<{name_width}}  {'大小':>{size_width}}"
    right_header = f"  {'远端':<{name_width}}  {'大小':>{size_width}}"
    diff_header = f"  {'差异':^{diff_width}}" if diff_stats else ""
    print(c(left_header, Color.CYAN), c("|", Color.DIM), c(right_header, Color.CYAN), end="")
    if diff_stats:
        print(c(" |", Color.DIM), c(diff_header, Color.CYAN))
    else:
        print()
    div_len = num_width + name_width + 2 + size_width + 2
    right_div = name_width + size_width + 6
    div_line = f"  {'─' * div_len}┼{'─' * right_div}"
    if diff_stats:
        div_line += f"┼{'─' * (diff_width + 3)}"
    print(c(div_line, Color.DIM))

    index_map: dict[int, str] = {}
    idx = 1

    for name in all_names:
        local_size = local_map.get(name)
        remote_size = remote_map.get(name)

        index_map[idx] = name
        prefix = f"[{idx}]"
        idx += 1

        if local_size is not None:
            left = f"  {prefix:<{num_width}}{name:<{name_width}}  {format_size(local_size):>{size_width}}"
            print(left, end="")
        else:
            left = f"  {prefix:<{num_width}}{'(--)':^{name_width}}  {'':>{size_width}}"
            print(c(left, Color.DIM), end="")

        print(c(" | ", Color.DIM), end="")

        if remote_size is not None:
            right = f"  {name:<{name_width}}  {format_size(remote_size):>{size_width}}"
            print(right, end="")
        else:
            right = f"  {'(--)':^{name_width}}  {'':>{size_width}}"
            print(c(right, Color.DIM), end="")

        if diff_stats and name in diff_stats:
            local_only_n, remote_only_n = diff_stats[name]
            parts = []
            if remote_only_n:
                parts.append(c(f"+{remote_only_n}", Color.GREEN))
            if local_only_n:
                parts.append(c(f"-{local_only_n}", Color.RED))
            if not parts:
                parts.append(c("=", Color.DIM))
            print(c(" | ", Color.DIM), " ".join(parts), end="")
        elif diff_stats:
            print(c(" | ", Color.DIM), c("?", Color.DIM), end="")

        print()

    print()
    return index_map


# ============================================================================
# 交互模式 — 差异分析
# ============================================================================

@dataclass
class DiffResult:
    db_name: str
    local_count: int
    remote_count: int
    local_only: list[dict[str, Any]]
    remote_only: list[dict[str, Any]]
    local_only_plain: list[str] = field(default_factory=list)
    remote_only_plain: list[str] = field(default_factory=list)


def analyze_memory_diff(local_path: Path, remote_path: Path) -> DiffResult:
    sql = (
        "SELECT id, scope_type, scope_id, content, tags_json, enabled, priority, "
        "source, created_at, updated_at FROM memory_entries "
        "ORDER BY scope_type, scope_id, source, content, id"
    )
    local_rows, local_state = read_table_rows(
        local_path, table="memory_entries",
        required_columns=MEMORY_REQUIRED_COLUMNS, sql=sql,
    )
    remote_rows, remote_state = read_table_rows(
        remote_path, table="memory_entries",
        required_columns=MEMORY_REQUIRED_COLUMNS, sql=sql,
    )
    if local_state == "missing":
        local_rows = []
    if remote_state == "missing":
        remote_rows = []

    local_by_key: dict[tuple, dict] = {}
    for row in local_rows:
        try:
            n = normalize_memory_row(row)
            local_by_key.setdefault(n["key"], row)
        except MergeError:
            pass

    remote_by_key: dict[tuple, dict] = {}
    for row in remote_rows:
        try:
            n = normalize_memory_row(row)
            remote_by_key.setdefault(n["key"], row)
        except MergeError:
            pass

    local_only = [row for key, row in local_by_key.items() if key not in remote_by_key]
    remote_only = [row for key, row in remote_by_key.items() if key not in local_by_key]

    return DiffResult(
        db_name=LLM_MEMORY_DB,
        local_count=len(local_rows),
        remote_count=len(remote_rows),
        local_only=local_only,
        remote_only=remote_only,
    )


def analyze_chat_diff(local_path: Path, remote_path: Path, key_path: Path) -> DiffResult | None:
    if not key_path.exists():
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None

    fernet = Fernet(key_path.read_bytes())
    sql = "SELECT id, chat_id, direction, sender, content, timestamp FROM messages ORDER BY timestamp, id"

    local_rows, local_state = read_table_rows(
        local_path, table="messages",
        required_columns=CHAT_REQUIRED_COLUMNS, sql=sql,
    )
    remote_rows, remote_state = read_table_rows(
        remote_path, table="messages",
        required_columns=CHAT_REQUIRED_COLUMNS, sql=sql,
    )
    if local_state == "missing":
        local_rows = []
    if remote_state == "missing":
        remote_rows = []

    local_keys: dict[tuple, tuple[dict, str]] = {}
    for row in local_rows:
        try:
            plain = decrypt_chat_content(fernet, row)
            key = chat_key_for(row, plain)
            local_keys.setdefault(key, (row, plain))
        except Exception:
            pass

    remote_keys: dict[tuple, tuple[dict, str]] = {}
    for row in remote_rows:
        try:
            plain = decrypt_chat_content(fernet, row)
            key = chat_key_for(row, plain)
            remote_keys.setdefault(key, (row, plain))
        except Exception:
            pass

    local_only = [row for key, (row, _) in local_keys.items() if key not in remote_keys]
    remote_only = [row for key, (row, _) in remote_keys.items() if key not in local_keys]
    local_only_plain = [plain for key, (_, plain) in local_keys.items() if key not in remote_keys]
    remote_only_plain = [plain for key, (_, plain) in remote_keys.items() if key not in local_keys]

    return DiffResult(
        db_name=CHAT_HISTORY_DB,
        local_count=len(local_rows),
        remote_count=len(remote_rows),
        local_only=local_only,
        remote_only=remote_only,
        local_only_plain=local_only_plain,
        remote_only_plain=remote_only_plain,
    )


def analyze_todos_diff(local_path: Path, remote_path: Path) -> DiffResult:
    sql = (
        "SELECT id, chat_id, user_id, content, remind_time, priority, status, "
        "reminded, created_at, completed_at FROM todos ORDER BY created_at, id"
    )
    local_rows, local_state = read_table_rows(
        local_path, table="todos",
        required_columns=TODOS_REQUIRED_COLUMNS, sql=sql,
    )
    remote_rows, remote_state = read_table_rows(
        remote_path, table="todos",
        required_columns=TODOS_REQUIRED_COLUMNS, sql=sql,
    )
    if local_state == "missing":
        local_rows = []
    if remote_state == "missing":
        remote_rows = []

    local_by_key = {todo_key(row): row for row in local_rows}
    remote_by_key = {todo_key(row): row for row in remote_rows}

    local_only = [row for key, row in local_by_key.items() if key not in remote_by_key]
    remote_only = [row for key, row in remote_by_key.items() if key not in local_by_key]

    return DiffResult(
        db_name=TODOS_DB,
        local_count=len(local_rows),
        remote_count=len(remote_rows),
        local_only=local_only,
        remote_only=remote_only,
    )


def show_diff_summary(diff: DiffResult) -> None:
    print(f"  {c(diff.db_name, Color.CYAN)}:")
    print(f"    本端: {diff.local_count} 条    远端: {diff.remote_count} 条")
    local_only_count = len(diff.local_only)
    remote_only_count = len(diff.remote_only)
    local_str = c(f"{local_only_count} 条", Color.RED) if local_only_count else "0 条"
    remote_str = c(f"{remote_only_count} 条", Color.GREEN) if remote_only_count else "0 条"
    print(f"    本端独有: {local_str}   远端独有: {remote_str}")


def show_record_diff(diff: DiffResult, settings: InteractiveSettings) -> None:
    max_show = settings.max_preview
    print()
    print(f"  {c(diff.db_name, Color.CYAN)} 记录级差异:")
    print()

    if diff.db_name == CHAT_HISTORY_DB:
        _show_chat_records(diff, settings, max_show)
    elif diff.db_name == LLM_MEMORY_DB:
        _show_memory_records(diff, max_show)
    elif diff.db_name == TODOS_DB:
        _show_todo_records(diff, max_show)


def _show_chat_records(diff: DiffResult, settings: InteractiveSettings, max_show: int) -> None:
    shown = 0
    if diff.remote_only_plain:
        print(c("  远端独有:", Color.GREEN))
        for i, (row, plain) in enumerate(zip(diff.remote_only, diff.remote_only_plain)):
            if i >= max_show:
                remaining = len(diff.remote_only) - max_show
                print(c(f"    ... 还有 {remaining} 条未显示", Color.DIM))
                break
            ts = canonical_timestamp(row.get("timestamp")) or "?"
            direction = row.get("direction", "?")
            sender = row.get("sender") or ""
            if settings.no_content:
                digest = hashlib.sha256(plain.encode()).hexdigest()[:12]
                content_str = f"sha256={digest} ({len(plain)} chars)"
            else:
                content_str = preview_text(plain, full=settings.show_full_content)
            print(c(f"    + {ts} [{direction}] {sender}: {content_str}", Color.GREEN))
            shown += 1

    if diff.local_only_plain:
        if shown:
            print()
        print(c("  本端独有:", Color.RED))
        for i, (row, plain) in enumerate(zip(diff.local_only, diff.local_only_plain)):
            if i >= max_show:
                remaining = len(diff.local_only) - max_show
                print(c(f"    ... 还有 {remaining} 条未显示", Color.DIM))
                break
            ts = canonical_timestamp(row.get("timestamp")) or "?"
            direction = row.get("direction", "?")
            sender = row.get("sender") or ""
            if settings.no_content:
                digest = hashlib.sha256(plain.encode()).hexdigest()[:12]
                content_str = f"sha256={digest} ({len(plain)} chars)"
            else:
                content_str = preview_text(plain, full=settings.show_full_content)
            print(c(f"    - {ts} [{direction}] {sender}: {content_str}", Color.RED))

    if not diff.remote_only_plain and not diff.local_only_plain:
        print(c("    无差异", Color.DIM))


def _show_memory_records(diff: DiffResult, max_show: int) -> None:
    shown = 0
    if diff.remote_only:
        print(c("  远端独有:", Color.GREEN))
        for i, row in enumerate(diff.remote_only):
            if i >= max_show:
                remaining = len(diff.remote_only) - max_show
                print(c(f"    ... 还有 {remaining} 条未显示", Color.DIM))
                break
            scope = f"{row.get('scope_type')}:{row.get('scope_id')}"
            content = preview_text(str(row.get("content") or ""), full=False)
            print(c(f"    + [{scope}] {content}", Color.GREEN))
            shown += 1

    if diff.local_only:
        if shown:
            print()
        print(c("  本端独有:", Color.RED))
        for i, row in enumerate(diff.local_only):
            if i >= max_show:
                remaining = len(diff.local_only) - max_show
                print(c(f"    ... 还有 {remaining} 条未显示", Color.DIM))
                break
            scope = f"{row.get('scope_type')}:{row.get('scope_id')}"
            content = preview_text(str(row.get("content") or ""), full=False)
            print(c(f"    - [{scope}] {content}", Color.RED))

    if not diff.remote_only and not diff.local_only:
        print(c("    无差异", Color.DIM))


def _show_todo_records(diff: DiffResult, max_show: int) -> None:
    shown = 0
    if diff.remote_only:
        print(c("  远端独有:", Color.GREEN))
        for i, row in enumerate(diff.remote_only):
            if i >= max_show:
                remaining = len(diff.remote_only) - max_show
                print(c(f"    ... 还有 {remaining} 条未显示", Color.DIM))
                break
            content = preview_text(str(row.get("content") or ""), full=False)
            ts = row.get("created_at") or "?"
            print(c(f"    + [{ts}] user={row.get('user_id')} {content}", Color.GREEN))
            shown += 1

    if diff.local_only:
        if shown:
            print()
        print(c("  本端独有:", Color.RED))
        for i, row in enumerate(diff.local_only):
            if i >= max_show:
                remaining = len(diff.local_only) - max_show
                print(c(f"    ... 还有 {remaining} 条未显示", Color.DIM))
                break
            content = preview_text(str(row.get("content") or ""), full=False)
            ts = row.get("created_at") or "?"
            print(c(f"    - [{ts}] user={row.get('user_id')} {content}", Color.RED))

    if not diff.remote_only and not diff.local_only:
        print(c("    无差异", Color.DIM))


# ============================================================================
# 交互模式 — 操作执行
# ============================================================================

def backup_file(file_path: Path, backup_dir: Path) -> bool:
    if not file_path.exists():
        return True
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"{file_path.stem}_{timestamp}{file_path.suffix}"
    try:
        shutil.copy2(file_path, dest)
        print(f"    备份: {dest.name}")
        return True
    except OSError as exc:
        print(c(f"    备份失败: {exc}", Color.RED))
        return False


def action_keep_remote(
    cfg: RemoteConfig, db_name: str, settings: InteractiveSettings,
) -> bool:
    target_path = settings.target_data / db_name
    if not backup_file(target_path, settings.backup_dir):
        return False
    print(f"    下载 {db_name} ...")
    if ssh_download(cfg, db_name, target_path):
        print(c(f"    已覆盖本端 {db_name}", Color.GREEN))
        return True
    else:
        print(c(f"    下载失败", Color.RED))
        return False


def action_merge(
    diff: DiffResult, remote_db_path: Path, settings: InteractiveSettings,
) -> bool:
    target_path = settings.target_data / diff.db_name
    if not diff.remote_only:
        print(c("    远端无独有记录，无需合并", Color.DIM))
        return True

    if not backup_file(target_path, settings.backup_dir):
        return False

    try:
        if diff.db_name == LLM_MEMORY_DB:
            return _merge_memory(diff, target_path)
        elif diff.db_name == CHAT_HISTORY_DB:
            return _merge_chat(diff, remote_db_path, target_path)
        elif diff.db_name == TODOS_DB:
            return _merge_todos(diff, target_path)
    except Exception as exc:
        print(c(f"    合并失败: {exc}", Color.RED))
        return False
    return False


def _merge_memory(diff: DiffResult, target_path: Path) -> bool:
    conn = connect_writable(target_path)
    try:
        ensure_memory_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        for row in diff.remote_only:
            try:
                n = normalize_memory_row(row)
            except MergeError:
                continue
            conn.execute(
                "INSERT INTO memory_entries "
                "(scope_type, scope_id, content, tags_json, enabled, priority, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (n["scope_type"], n["scope_id"], n["content"], n["tags_json"],
                 n["enabled"], n["priority"], n["source"], n["created_at"], n["updated_at"]),
            )
        conn.commit()
        print(c(f"    已合并 {len(diff.remote_only)} 条记忆记录", Color.GREEN))
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _merge_chat(
    diff: DiffResult, remote_db_path: Path, target_path: Path,
) -> bool:
    conn = connect_writable(target_path)
    try:
        ensure_chat_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        remote_conn = connect_readonly(remote_db_path)
        try:
            rows_to_insert = []
            for row in diff.remote_only:
                row_id = row.get("id")
                fetched = remote_conn.execute(
                    "SELECT chat_id, direction, sender, content, timestamp FROM messages WHERE id = ?",
                    (row_id,),
                ).fetchone()
                if fetched:
                    rows_to_insert.append(dict(fetched))
        finally:
            remote_conn.close()

        for r in rows_to_insert:
            conn.execute(
                "INSERT INTO messages (chat_id, direction, sender, content, timestamp) VALUES (?, ?, ?, ?, ?)",
                (r["chat_id"], r["direction"], r["sender"], r["content"], r["timestamp"]),
            )
        conn.commit()
        print(c(f"    已合并 {len(rows_to_insert)} 条聊天记录", Color.GREEN))
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _merge_todos(diff: DiffResult, target_path: Path) -> bool:
    conn = connect_writable(target_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for row in diff.remote_only:
            conn.execute(
                "INSERT INTO todos "
                "(chat_id, user_id, content, remind_time, priority, status, reminded, created_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row.get("chat_id"), row.get("user_id"), row.get("content"),
                 row.get("remind_time"), row.get("priority"), row.get("status"),
                 row.get("reminded"), row.get("created_at"), row.get("completed_at")),
            )
        conn.commit()
        print(c(f"    已合并 {len(diff.remote_only)} 条待办记录", Color.GREEN))
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================================
# 交互模式 — 主菜单
# ============================================================================

def show_side_by_side_diff(diff: DiffResult, settings: InteractiveSettings) -> None:
    print()
    print(c(f"{'═' * 60}", Color.CYAN))
    print(c(f"  {diff.db_name}", Color.CYAN))
    print(c(f"  本端: {diff.local_count} 条  |  远端: {diff.remote_count} 条", Color.CYAN))
    local_n = len(diff.local_only)
    remote_n = len(diff.remote_only)
    print(c(f"  本端独有: {local_n} 条  |  远端独有: {remote_n} 条", Color.CYAN))
    print(c(f"{'═' * 60}", Color.CYAN))

    if not local_n and not remote_n:
        print(c("  无差异", Color.DIM))
        return

    max_show = settings.max_preview

    if diff.db_name == CHAT_HISTORY_DB:
        _side_by_side_chat(diff, settings)
    elif diff.db_name == LLM_MEMORY_DB:
        _side_by_side_memory(diff)
    elif diff.db_name == TODOS_DB:
        _side_by_side_todos(diff)


def _render_side_by_side(
    local_items: list[tuple[str, str]],
    remote_items: list[tuple[str, str]],
) -> None:
    """左右分栏渲染差异。每项为 (排序键, 显示文本)。"""
    try:
        import shutil as _shutil
        term_width = _shutil.get_terminal_size().columns
    except Exception:
        term_width = 120
    col_width = max((term_width - 5) // 2, 30)

    print(c(f"  {'本端 (-)':^{col_width}}", Color.RED), end="")
    print(c(" │ ", Color.DIM), end="")
    print(c(f"{'远端 (+)':^{col_width}}", Color.GREEN))
    print(c(f"  {'─' * col_width}┼{'─' * (col_width + 1)}", Color.DIM))

    merged: list[tuple[str, str | None, str | None]] = []
    for sort_key, text in local_items:
        merged.append((sort_key, text, None))
    for sort_key, text in remote_items:
        merged.append((sort_key, None, text))
    merged.sort(key=lambda x: x[0])

    for _key, local_text, remote_text in merged:
        left = _truncate(local_text or "", col_width - 2)
        right = _truncate(remote_text or "", col_width - 2)

        if local_text:
            padded_left = _pad_to_width(left, col_width - 2)
            print(c(f"  - {padded_left}", Color.RED), end="")
        else:
            print(f"  {' ' * col_width}", end="")

        print(c(" │ ", Color.DIM), end="")

        if remote_text:
            print(c(f"+ {right}", Color.GREEN))
        else:
            print()


def _display_width(text: str) -> int:
    w = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def _truncate(text: str, width: int) -> str:
    w = 0
    for i, ch in enumerate(text):
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width - 1:
            return text[:i] + "…"
        w += cw
    return text


def _pad_to_width(text: str, width: int) -> str:
    dw = _display_width(text)
    if dw >= width:
        return text
    return text + " " * (width - dw)


def _short_direction(d: str) -> str:
    if d == "incoming":
        return "in"
    if d == "outgoing":
        return "out"
    return d[:3] if len(d) > 3 else d


def _side_by_side_chat(diff: DiffResult, settings: InteractiveSettings) -> None:
    local_items: list[tuple[str, str]] = []
    for row, plain in zip(diff.local_only, diff.local_only_plain):
        ts = short_timestamp(row.get("timestamp"))
        d = _short_direction(row.get("direction", "?"))
        sender = row.get("sender") or ""
        if settings.no_content:
            digest = hashlib.sha256(plain.encode()).hexdigest()[:12]
            text = f"{ts} {d} {sender}: sha={digest}"
        else:
            text = f"{ts} {d} {sender}: {preview_text(plain, full=False, limit=500)}"
        local_items.append((ts, text))

    remote_items: list[tuple[str, str]] = []
    for row, plain in zip(diff.remote_only, diff.remote_only_plain):
        ts = short_timestamp(row.get("timestamp"))
        d = _short_direction(row.get("direction", "?"))
        sender = row.get("sender") or ""
        if settings.no_content:
            digest = hashlib.sha256(plain.encode()).hexdigest()[:12]
            text = f"{ts} {d} {sender}: sha={digest}"
        else:
            text = f"{ts} {d} {sender}: {preview_text(plain, full=False, limit=500)}"
        remote_items.append((ts, text))

    _render_side_by_side(local_items, remote_items)


def _side_by_side_memory(diff: DiffResult) -> None:
    local_items: list[tuple[str, str]] = []
    for row in diff.local_only:
        scope = f"{row.get('scope_type')}:{row.get('scope_id')}"
        content = preview_text(str(row.get("content") or ""), full=False, limit=500)
        ts = str(row.get("updated_at") or row.get("created_at") or "")
        local_items.append((ts, f"[{scope}] {content}"))

    remote_items: list[tuple[str, str]] = []
    for row in diff.remote_only:
        scope = f"{row.get('scope_type')}:{row.get('scope_id')}"
        content = preview_text(str(row.get("content") or ""), full=False, limit=500)
        ts = str(row.get("updated_at") or row.get("created_at") or "")
        remote_items.append((ts, f"[{scope}] {content}"))

    _render_side_by_side(local_items, remote_items)


def _side_by_side_todos(diff: DiffResult) -> None:
    local_items: list[tuple[str, str]] = []
    for row in diff.local_only:
        content = preview_text(str(row.get("content") or ""), full=False, limit=500)
        ts = str(row.get("created_at") or "")
        local_items.append((ts, f"[{ts}] u={row.get('user_id')} {content}"))

    remote_items: list[tuple[str, str]] = []
    for row in diff.remote_only:
        content = preview_text(str(row.get("content") or ""), full=False, limit=500)
        ts = str(row.get("created_at") or "")
        remote_items.append((ts, f"[{ts}] u={row.get('user_id')} {content}"))

    _render_side_by_side(local_items, remote_items)


def interactive_merge(cfg: RemoteConfig, settings: InteractiveSettings) -> None:
    print()
    print(c("正在获取文件列表...", Color.YELLOW))

    target_data = settings.target_data
    local_files = []
    for name in sorted(target_data.iterdir()):
        if name.is_file() and name.suffix == ".db":
            local_files.append((name.name, name.stat().st_size))

    remote_files = ssh_list_files(cfg)
    remote_db_files = [(n, s) for n, s in remote_files if n.endswith(".db")]

    if not local_files and not remote_db_files:
        print(c("两侧都没有 DB 文件", Color.DIM))
        return

    all_db_names = sorted(set(n for n, _ in local_files) | set(n for n, _ in remote_db_files))

    tmp_dir = Path(tempfile.mkdtemp(prefix="zincnya_merge_"))
    try:
        print(c("正在下载远端文件并分析差异...", Color.YELLOW))
        downloaded: dict[str, Path] = {}
        for name in all_db_names:
            if any(n == name for n, _ in remote_db_files):
                dest = tmp_dir / name
                if ssh_download(cfg, name, dest):
                    downloaded[name] = dest

        key_dest = None
        if CHAT_HISTORY_DB in downloaded:
            key_dest = tmp_dir / CHAT_KEY
            if not ssh_download(cfg, CHAT_KEY, key_dest):
                key_dest = None

        diffs: dict[str, DiffResult] = {}
        diff_stats: dict[str, tuple[int, int]] = {}
        for name in all_db_names:
            remote_path = downloaded.get(name)
            local_path = target_data / name
            if not remote_path:
                continue
            try:
                if name == LLM_MEMORY_DB:
                    diff = analyze_memory_diff(local_path, remote_path)
                elif name == CHAT_HISTORY_DB:
                    local_key = target_data / CHAT_KEY
                    if key_dest and key_dest.exists():
                        remote_key_bytes = key_dest.read_bytes()
                        local_key_bytes = local_key.read_bytes() if local_key.exists() else b""
                        if remote_key_bytes != local_key_bytes:
                            continue
                    diff = analyze_chat_diff(local_path, remote_path, local_key)
                    if diff is None:
                        continue
                elif name == TODOS_DB:
                    diff = analyze_todos_diff(local_path, remote_path)
                else:
                    continue
                diffs[name] = diff
                diff_stats[name] = (len(diff.local_only), len(diff.remote_only))
            except MergeError:
                pass

        index_map = show_file_comparison(local_files, remote_db_files, diff_stats)
        max_idx = max(index_map.keys()) if index_map else 0

        sel = input("选择要处理的文件 (空格/逗号分隔, * = 全部, 0 = 取消): ")
        indices = select_parse(sel, max_idx)
        if not indices:
            print(c("已取消", Color.DIM))
            return

        selected_names = [index_map[i] for i in indices]

        for name in selected_names:
            diff = diffs.get(name)
            if diff is None:
                print(c(f"\n  {name}: 无法分析，跳过", Color.YELLOW))
                continue

            remote_path = downloaded.get(name)
            show_side_by_side_diff(diff, settings)

            has_diff = diff.local_only or diff.remote_only
            if not has_diff:
                continue

            print()
            print(c("─" * 60, Color.DIM))
            print(f"  {c('1', Color.CYAN)}. 保留本端 (不动)")
            print(f"  {c('2', Color.CYAN)}. 保留远端 (覆盖本端)")
            print(f"  {c('3', Color.CYAN)}. 合并 (远端独有记录写入本端)")
            print(f"  {c('0', Color.CYAN)}. 跳过")
            print()

            action = input(f"  对 {name} 选择操作: ").strip()
            if action == "1" or action == "0":
                print(c("    跳过", Color.DIM))
            elif action == "2":
                action_keep_remote(cfg, name, settings)
            elif action == "3":
                if remote_path:
                    action_merge(diff, remote_path, settings)
                else:
                    print(c("    远端文件不可用", Color.RED))
            else:
                print(c("    无效选项，跳过", Color.DIM))

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def interactive_settings(settings: InteractiveSettings) -> None:
    while True:
        print()
        print(c("═" * 40, Color.CYAN))
        print(c("  设置", Color.CYAN))
        print(c("═" * 40, Color.CYAN))
        print()
        print(f"  1. max-preview        = {settings.max_preview}")
        print(f"  2. no-content         = {settings.no_content}")
        print(f"  3. show-full-content  = {settings.show_full_content}")
        print(f"  4. allow-wal          = {settings.allow_wal}")
        print(f"  5. backup-dir         = {settings.backup_dir}")
        print(f"  6. target-data        = {settings.target_data}")
        print(f"  0. 返回")
        print()

        choice = input("请选择: ").strip()
        if choice == "0":
            break
        elif choice == "1":
            val = input(f"  max-preview [{settings.max_preview}]: ").strip()
            if val:
                try:
                    settings.max_preview = max(1, int(val))
                except ValueError:
                    print(c("  无效数字", Color.RED))
        elif choice == "2":
            settings.no_content = not settings.no_content
            if settings.no_content:
                settings.show_full_content = False
            print(f"  no-content = {settings.no_content}")
        elif choice == "3":
            settings.show_full_content = not settings.show_full_content
            if settings.show_full_content:
                settings.no_content = False
            print(f"  show-full-content = {settings.show_full_content}")
        elif choice == "4":
            settings.allow_wal = not settings.allow_wal
            print(f"  allow-wal = {settings.allow_wal}")
        elif choice == "5":
            val = input(f"  backup-dir [{settings.backup_dir}]: ").strip()
            if val:
                settings.backup_dir = Path(val).expanduser().resolve()
        elif choice == "6":
            val = input(f"  target-data [{settings.target_data}]: ").strip()
            if val:
                p = Path(val).expanduser().resolve()
                if p.is_dir():
                    settings.target_data = p
                else:
                    print(c(f"  目录不存在: {p}", Color.RED))


def interactive_main() -> int:
    cfg = load_env_config()
    if cfg is None:
        print(c("错误: 要先在 .env 文件中配置 REMOTE_HOST, REMOTE_PORT, REMOTE_PATH", Color.RED))
        return 1

    settings = InteractiveSettings()

    print()
    print(c("正在连接 {}:{} ...".format(cfg.host, cfg.port), Color.YELLOW))
    if not ssh_test(cfg):
        print(c("SSH 连接失败", Color.RED))
        print(f"  主机: {cfg.host}")
        print(f"  端口: {cfg.port}")
        print("请检查 .env 中的 REMOTE_HOST / REMOTE_PORT 配置")
        return 1
    print(c("已连接", Color.GREEN))

    while True:
        print()
        print(c("═" * 40, Color.CYAN))
        print(c("  ZincNya_bot 离线数据合并", Color.CYAN))
        print(f"  本端: {settings.target_data}")
        print(f"  远端: {cfg.host}:{cfg.path}")
        print(c("═" * 40, Color.CYAN))
        print()
        print(f"  {c('1', Color.YELLOW)}. 执行合并")
        print(f"  {c('2', Color.YELLOW)}. 修改设置")
        print(f"  {c('0', Color.YELLOW)}. 退出")
        print()

        choice = input("请选择操作: ").strip()
        if choice == "1":
            try:
                interactive_merge(cfg, settings)
            except MergeError as exc:
                print(c(f"错误: {exc}", Color.RED))
            except KeyboardInterrupt:
                print()
        elif choice == "2":
            interactive_settings(settings)
        elif choice == "0":
            break
        else:
            print(c("无效的选项", Color.RED))

        if choice != "0":
            print()
            input("按 Enter 继续...")

    return 0


if __name__ == "__main__":
    sys.exit(main())
