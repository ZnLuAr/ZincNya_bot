#!/usr/bin/env python3
"""
Offline data merge utility for ZincNya Bot.

Dry-run by default. Use --apply to write merge-capable changes.
This script intentionally avoids importing config.py or bot runtime modules.
"""

import argparse
import difflib
import hashlib
import json
import shutil
import sqlite3
import sys
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
    parser.add_argument("--source", required=True, type=Path, help="Source data directory or project root containing data/.")
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




def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
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
            plan.preview_lines.append("# skipped: source llmMemory.db not found")
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
            f"# target rows: {len(target_rows)}",
            f"# source rows: {len(source_rows)}",
            f"# new rows: {len(new_rows)}",
            f"# duplicates: {duplicates}",
            f"# metadata differences: {metadata_differences}",
            f"# skipped invalid source rows: {skipped_source}",
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
            plan.preview_lines.append(f"# ... {hidden} more memory rows not shown; increase --max-preview to display more")
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
            plan.preview_lines.append(f"# ... {remaining} more metadata differences not shown")

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
        plan.preview_lines.append("# skipped: source chatHistory.db not found")
        return plan
    if not source_key_path.exists():
        plan.apply_capable = False
        plan.warnings.append("source .chatKey not found; skipping chat-history")
        plan.preview_lines.append("# skipped: source .chatKey not found")
        return plan
    if not target_key_path.exists():
        plan.apply_capable = False
        plan.warnings.append("target .chatKey not found; skipping chat-history")
        plan.preview_lines.append("# skipped: target .chatKey not found")
        return plan

    source_key = source_key_path.read_bytes()
    target_key = target_key_path.read_bytes()
    if source_key != target_key:
        plan.apply_capable = False
        plan.warnings.append("source and target .chatKey differ; skipping chat-history")
        plan.preview_lines.extend([
            "# skipped: source and target .chatKey differ",
            "# reason: v1 does not re-encrypt chat history; copying source ciphertext would make rows unreadable",
        ])
        return plan

    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        plan.apply_capable = False
        plan.errors.append("cryptography is not installed; cannot decrypt chatHistory.db")
        plan.preview_lines.append("# error: cryptography is not installed; cannot decrypt chatHistory.db")
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
            f"# target rows: {len(target_rows)}",
            f"# source rows: {len(source_rows)}",
            f"# new rows: {len(new_rows)}",
            f"# duplicates: {duplicates}",
            f"# skipped undecryptable source rows: {skipped_source}",
            f"# skipped undecryptable target rows while matching: {skipped_target}",
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
            plan.preview_lines.append(f"# ... {hidden} more chat rows not shown; increase --max-preview to display more")
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
        lines.append(f"# ... {hidden} more diff lines not shown; increase --max-json-diff-lines to display more")
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
            "# report-only: this file is not auto-merged by --apply",
        ])
        if source_text is None:
            plan.preview_lines.append("# skipped: source file missing; target will not be deleted")
            plan.stats["source_missing"] = 1
            plans.append(plan)
            continue
        if target_text is None:
            target_text = ""
            plan.preview_lines.append("# target file missing; report-only addition preview")

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
        "# report-only: todos are not auto-merged in v1",
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
            plan.preview_lines.append("# skipped: source todos.db not found")
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
            f"# target rows: {len(target_rows)}",
            f"# source rows: {len(source_rows)}",
            f"# likely source-only rows: {source_only}",
            f"# likely duplicates: {duplicates}",
            f"# ambiguous/conflicting rows: {ambiguous}",
        ]
        hidden = source_only - min(source_only, ctx.max_preview)
        if hidden > 0:
            plan.preview_lines.append(f"# ... {hidden} more todo rows not shown; increase --max-preview to display more")
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


if __name__ == "__main__":
    sys.exit(main())
