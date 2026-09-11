"""测试 memory 合并工具的新旧 schema 与隐私字段语义。"""

import json
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet
import pytest

from scripts.merge_data import (
    DiffResult,
    MergeError,
    ScriptContext,
    _merge_memory,
    analyze_memory_diff,
    plan_llm_memory,
    show_record_diff,
)


def _createMemoryDB(path: Path, fernet: Fernet, rows, *, modern=False):
    conn = sqlite3.connect(path)
    columns = (
        "id INTEGER PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL, "
        "content BLOB NOT NULL, tags_json TEXT NOT NULL, enabled INTEGER NOT NULL, "
        "priority INTEGER NOT NULL, source TEXT NOT NULL, "
        + (
            "mode TEXT NOT NULL DEFAULT 'contextual', retrieval_hint BLOB, "
            if modern else ""
        )
        + "created_at TEXT, updated_at TEXT"
    )
    conn.execute(f"CREATE TABLE memory_entries ({columns})")
    for row in rows:
        values = {
            "id": row.get("id"),
            "scope_type": row.get("scope_type", "global"),
            "scope_id": row.get("scope_id", "global"),
            "content": fernet.encrypt(row["content"].encode("utf-8")),
            "tags_json": json.dumps(row.get("tags", []), ensure_ascii=False),
            "enabled": row.get("enabled", 1),
            "priority": row.get("priority", 0),
            "source": row.get("source", "manual"),
            "created_at": row.get("created_at", "2026-09-01 00:00:00"),
            "updated_at": row.get("updated_at", "2026-09-01 00:00:00"),
        }
        if modern:
            values["mode"] = row.get("mode", "contextual")
            hint = row.get("retrieval_hint")
            values["retrieval_hint"] = (
                fernet.encrypt(hint.encode("utf-8")) if hint is not None else None
            )
            conn.execute(
                "INSERT INTO memory_entries "
                "(id, scope_type, scope_id, content, tags_json, enabled, priority, "
                "source, mode, retrieval_hint, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(values[key] for key in (
                    "id", "scope_type", "scope_id", "content", "tags_json",
                    "enabled", "priority", "source", "mode", "retrieval_hint",
                    "created_at", "updated_at",
                )),
            )
        else:
            conn.execute(
                "INSERT INTO memory_entries "
                "(id, scope_type, scope_id, content, tags_json, enabled, priority, "
                "source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(values[key] for key in (
                    "id", "scope_type", "scope_id", "content", "tags_json",
                    "enabled", "priority", "source", "created_at", "updated_at",
                )),
            )
    conn.commit()
    conn.close()


def _context(sourceData: Path, targetData: Path) -> ScriptContext:
    return ScriptContext(
        source_data=sourceData,
        target_data=targetData,
        selected_sections={"llm-memory"},
        apply=True,
        max_preview=50,
        max_json_diff_lines=300,
        show_full_content=False,
        no_content=False,
        backup_dir=targetData / "backup",
        allow_wal=True,
    )


def _fernetFiles(sourceData: Path, targetData: Path):
    key = Fernet.generate_key()
    sourceData.mkdir(parents=True, exist_ok=True)
    targetData.mkdir(parents=True, exist_ok=True)
    (sourceData / ".chatKey").write_bytes(key)
    (targetData / ".chatKey").write_bytes(key)
    return Fernet(key)


def test_old_schema_is_read_with_defaults_and_upgraded(tmp_path):
    sourceData = tmp_path / "source"
    targetData = tmp_path / "target"
    fernet = _fernetFiles(sourceData, targetData)
    sourceDB = sourceData / "llmMemory.db"
    targetDB = targetData / "llmMemory.db"
    _createMemoryDB(sourceDB, fernet, [{"id": 1, "content": "旧格式"}])
    _createMemoryDB(targetDB, fernet, [])

    plan = plan_llm_memory(_context(sourceData, targetData))

    assert len(plan.inserts) == 1
    values = plan.inserts[0].values
    assert values["mode"] == "contextual"
    assert values["retrieval_hint"] is None
    assert all("retrieval_hint" not in line for line in plan.preview_lines)

    from scripts.merge_data import apply_llm_memory
    assert apply_llm_memory(plan) == 1

    conn = sqlite3.connect(targetDB)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_entries)")}
    content, mode, hint = conn.execute(
        "SELECT content, mode, retrieval_hint FROM memory_entries"
    ).fetchone()
    conn.close()
    assert {"mode", "retrieval_hint"} <= columns
    assert fernet.decrypt(content).decode("utf-8") == "旧格式"
    assert mode == "contextual"
    assert hint is None


def test_same_fact_mode_and_hint_conflict_is_reported_without_leaking_hint(tmp_path):
    sourceData = tmp_path / "source"
    targetData = tmp_path / "target"
    fernet = _fernetFiles(sourceData, targetData)
    _createMemoryDB(
        sourceData / "llmMemory.db",
        fernet,
        [{"id": 1, "content": "同一事实", "mode": "pinned", "retrieval_hint": "私密说明"}],
        modern=True,
    )
    _createMemoryDB(
        targetData / "llmMemory.db",
        fernet,
        [{"id": 2, "content": "同一事实", "mode": "contextual", "retrieval_hint": "另一说明"}],
        modern=True,
    )

    plan = plan_llm_memory(_context(sourceData, targetData))

    assert plan.stats["new_rows"] == 0
    assert plan.stats["metadata_differences"] == 1
    preview = "\n".join(plan.preview_lines)
    assert "retrieval_hint differs" in preview
    assert "私密说明" not in preview
    assert "另一说明" not in preview


def test_interactive_merge_reencrypts_remote_hint_with_matching_key(tmp_path):
    localData = tmp_path / "local"
    remoteData = tmp_path / "remote"
    sharedKey = Fernet.generate_key()
    localData.mkdir()
    remoteData.mkdir()
    (localData / ".chatKey").write_bytes(sharedKey)
    (remoteData / ".chatKey").write_bytes(sharedKey)
    localFernet = Fernet(sharedKey)
    remoteFernet = Fernet(sharedKey)
    _createMemoryDB(localData / "llmMemory.db", localFernet, [], modern=True)
    _createMemoryDB(
        remoteData / "llmMemory.db",
        remoteFernet,
        [{"id": 8, "content": "远端事实", "retrieval_hint": "远端说明", "mode": "contextual"}],
        modern=True,
    )
    conn = sqlite3.connect(remoteData / "llmMemory.db")
    remoteContent, remoteHint = conn.execute(
        "SELECT content, retrieval_hint FROM memory_entries"
    ).fetchone()
    conn.close()

    diff = analyze_memory_diff(
        localData / "llmMemory.db",
        remoteData / "llmMemory.db",
    )
    assert len(diff.remote_only) == 1
    assert diff.remote_only[0]["retrieval_hint"] == "远端说明"
    assert _merge_memory(diff, localData / "llmMemory.db") is True

    conn = sqlite3.connect(localData / "llmMemory.db")
    content, hint = conn.execute(
        "SELECT content, retrieval_hint FROM memory_entries"
    ).fetchone()
    conn.close()
    assert localFernet.decrypt(content).decode("utf-8") == "远端事实"
    assert localFernet.decrypt(hint).decode("utf-8") == "远端说明"
    assert content != remoteContent
    assert hint != remoteHint


def test_interactive_memory_merge_rejects_different_keys(tmp_path):
    localData = tmp_path / "local"
    remoteData = tmp_path / "remote"
    localKey = Fernet.generate_key()
    remoteKey = Fernet.generate_key()
    localData.mkdir()
    remoteData.mkdir()
    (localData / ".chatKey").write_bytes(localKey)
    (remoteData / ".chatKey").write_bytes(remoteKey)
    _createMemoryDB(localData / "llmMemory.db", Fernet(localKey), [], modern=True)
    _createMemoryDB(remoteData / "llmMemory.db", Fernet(remoteKey), [], modern=True)

    with pytest.raises(MergeError, match=r"\.chatKey differ"):
        analyze_memory_diff(
            localData / "llmMemory.db",
            remoteData / "llmMemory.db",
        )


@pytest.mark.parametrize("missingSide", ["local", "remote"])
def test_interactive_memory_merge_rejects_missing_key(tmp_path, missingSide):
    localData = tmp_path / "local"
    remoteData = tmp_path / "remote"
    key = Fernet.generate_key()
    localData.mkdir()
    remoteData.mkdir()
    (localData / ".chatKey").write_bytes(key)
    (remoteData / ".chatKey").write_bytes(key)
    _createMemoryDB(
        localData / "llmMemory.db",
        Fernet(key),
        [{"id": 1, "content": "本地事实"}],
        modern=True,
    )
    _createMemoryDB(
        remoteData / "llmMemory.db",
        Fernet(key),
        [{"id": 2, "content": "远端事实"}],
        modern=True,
    )
    ((localData if missingSide == "local" else remoteData) / ".chatKey").unlink()

    with pytest.raises(MergeError, match=rf"{missingSide} \.chatKey missing"):
        analyze_memory_diff(
            localData / "llmMemory.db",
            remoteData / "llmMemory.db",
        )


def test_batch_memory_merge_skips_when_target_key_is_missing(tmp_path):
    sourceData = tmp_path / "source"
    targetData = tmp_path / "target"
    fernet = _fernetFiles(sourceData, targetData)
    _createMemoryDB(
        sourceData / "llmMemory.db",
        fernet,
        [{"id": 1, "content": "源事实"}],
        modern=True,
    )
    _createMemoryDB(targetData / "llmMemory.db", fernet, [], modern=True)
    (targetData / ".chatKey").unlink()

    plan = plan_llm_memory(_context(sourceData, targetData))

    assert plan.apply_capable is False
    assert plan.inserts == []
    assert any("target .chatKey not found" in warning for warning in plan.warnings)


def test_batch_memory_merge_rejects_ciphertext_not_matching_shared_key(tmp_path):
    sourceData = tmp_path / "source"
    targetData = tmp_path / "target"
    sourceData.mkdir()
    targetData.mkdir()
    sharedKey = Fernet.generate_key()
    wrongKey = Fernet.generate_key()
    (sourceData / ".chatKey").write_bytes(sharedKey)
    (targetData / ".chatKey").write_bytes(sharedKey)
    _createMemoryDB(
        sourceData / "llmMemory.db",
        Fernet(wrongKey),
        [{"id": 1, "content": "不可解密事实"}],
        modern=True,
    )
    _createMemoryDB(targetData / "llmMemory.db", Fernet(sharedKey), [], modern=True)

    plan = plan_llm_memory(_context(sourceData, targetData))

    assert plan.apply_capable is False
    assert plan.inserts == []
    assert any("could not be decrypted" in error for error in plan.errors)


def test_invalid_integer_metadata_is_skipped_in_batch_plan(tmp_path):
    sourceData = tmp_path / "source"
    targetData = tmp_path / "target"
    fernet = _fernetFiles(sourceData, targetData)
    _createMemoryDB(
        sourceData / "llmMemory.db",
        fernet,
        [{"id": 1, "content": "坏元数据", "priority": "high"}],
        modern=True,
    )
    _createMemoryDB(targetData / "llmMemory.db", fernet, [], modern=True)

    plan = plan_llm_memory(_context(sourceData, targetData))

    assert plan.stats["skipped_source"] == 1
    assert plan.inserts == []
    assert any("priority must be an integer" in item for item in plan.warnings)


def test_interactive_preview_does_not_print_hint(tmp_path, capsys):
    diff = DiffResult(
        db_name="llmMemory.db",
        local_count=0,
        remote_count=1,
        local_only=[],
        remote_only=[{
            "scope_type": "global",
            "scope_id": "global",
            "content": "事实",
            "retrieval_hint": "不应展示",
        }],
    )
    settings = type("Settings", (), {
        "max_preview": 10,
        "show_full_content": False,
        "no_content": False,
    })()

    show_record_diff(diff, settings)

    assert "不应展示" not in capsys.readouterr().out
