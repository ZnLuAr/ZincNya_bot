"""
tests/scripts/test_edit_data.py

测试离线数据编辑脚本 scripts/edit_data.py 的纯逻辑单元。

覆盖范围（跳过交互/编辑器/main 等 I/O 重的入口）：
    - 纯函数：calculate_sha256 / is_valid_table_name / format_size / classify_file / should_exclude
    - Validator：validate_json_format / validate_required_fields / validate_tags_json / validate_schema
    - BackupManager：create_backup / list_backups / restore_backup
    - DBEditor：export_to_json / import_from_json（含 dry_run、表名注入防护）
    - EncryptedDBEditor：load_key / 加密往返（export ↔ import）

scripts/ 不是 Python 包，通过 importlib 按文件路径加载。
"""

import importlib.util
import json
import sqlite3
from datetime import datetime as _real_datetime
from pathlib import Path

import pytest


# ===========================================================================
# 模块加载（scripts/ 非包，按路径加载）
# ===========================================================================

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_EDIT_DATA_PATH = _PROJECT_ROOT / "scripts" / "edit_data.py"


def _load_edit_data():
    spec = importlib.util.spec_from_file_location("edit_data_under_test", _EDIT_DATA_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


edit_data = _load_edit_data()


# ===========================================================================
# 纯函数
# ===========================================================================

def test_calculate_sha256_known_content(tmp_path):
    """已知内容的 SHA256 应与 hashlib 标准结果一致"""
    import hashlib

    f = tmp_path / "x.txt"
    content = b"hello zincnya"
    f.write_bytes(content)

    expected = hashlib.sha256(content).hexdigest()
    assert edit_data.calculate_sha256(f) == expected


def test_calculate_sha256_empty_file(tmp_path):
    """空文件的 SHA256 是固定值"""
    f = tmp_path / "empty.txt"
    f.write_bytes(b"")
    assert edit_data.calculate_sha256(f) == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


@pytest.mark.parametrize("name,valid", [
    ("memory_entries", True),
    ("todos", True),
    ("_private", True),
    ("Table1", True),
    ("messages; DROP TABLE x", False),
    ("table-name", False),
    ("1table", False),
    ("", False),
    ("a b", False),
])
def test_is_valid_table_name(name, valid):
    """表名校验：仅允许字母数字下划线，且不能数字开头（防 SQL 注入）"""
    assert edit_data.is_valid_table_name(name) is valid


@pytest.mark.parametrize("size,expected", [
    (0, "0.0 B"),
    (512, "512.0 B"),
    (1023, "1023.0 B"),
    (1024, "1.0 KB"),
    (1024 * 1024, "1.0 MB"),
    (1024 ** 3, "1.0 GB"),
    (1024 ** 4, "1.0 TB"),
])
def test_format_size(size, expected):
    """文件大小格式化的单位边界"""
    assert edit_data.format_size(size) == expected


@pytest.mark.parametrize("filename,expected", [
    ("llmConfig.json", "json"),
    ("doc.md", "markdown"),
    ("todos.db", "sqlite_db"),
    ("llmMemory.db", "sqlite_db"),
    ("chatHistory.db", "encrypted_db"),
    ("mystery.bin", "unknown"),
])
def test_classify_file(filename, expected):
    """文件分类：扩展名 + chatHistory 特判加密"""
    assert edit_data.classify_file(Path(filename)) == expected


@pytest.mark.parametrize("filename,excluded", [
    ("prompts.example.json", True),
    ("data.bak", True),
    ("notes~", True),
    ("llmConfig.json", False),
    ("memory.db", False),
])
def test_should_exclude(filename, excluded):
    """排除模式：*.example.json / *.bak / *~"""
    assert edit_data.should_exclude(Path(filename)) is excluded


# ===========================================================================
# Validator
# ===========================================================================

def test_validate_json_format_valid():
    ok, parsed, err = edit_data.Validator.validate_json_format('{"a": 1}')
    assert ok is True
    assert parsed == {"a": 1}
    assert err is None


def test_validate_json_format_invalid():
    ok, parsed, err = edit_data.Validator.validate_json_format('{not json}')
    assert ok is False
    assert parsed is None
    assert err is not None


def test_validate_required_fields_missing_table():
    ok, err = edit_data.Validator.validate_required_fields({"records": []})
    assert ok is False
    assert "table" in err


def test_validate_required_fields_records_not_list():
    ok, err = edit_data.Validator.validate_required_fields({"table": "todos", "records": {}})
    assert ok is False
    assert "records" in err


def test_validate_required_fields_memory_entries_ok():
    data = {
        "table": "memory_entries",
        "records": [
            {"content": "x", "scope_type": "global", "scope_id": "global"},
        ],
    }
    ok, err = edit_data.Validator.validate_required_fields(data)
    assert ok is True
    assert err is None


def test_validate_required_fields_memory_entries_missing_field():
    data = {
        "table": "memory_entries",
        "records": [
            {"content": "x", "scope_type": "global"},  # 缺 scope_id
        ],
    }
    ok, err = edit_data.Validator.validate_required_fields(data)
    assert ok is False
    assert "scope_id" in err


def test_validate_tags_json_valid():
    data = {"records": [{"tags_json": ["a", "b"]}]}
    ok, err = edit_data.Validator.validate_tags_json(data)
    assert ok is True
    assert err is None


def test_validate_tags_json_not_list():
    data = {"records": [{"tags_json": "notalist"}]}
    ok, err = edit_data.Validator.validate_tags_json(data)
    assert ok is False
    assert "数组" in err


def test_validate_tags_json_non_string_element():
    data = {"records": [{"tags_json": ["ok", 123]}]}
    ok, err = edit_data.Validator.validate_tags_json(data)
    assert ok is False
    assert "字符串" in err


def test_validate_schema_memory_entries_ok():
    """合法 memory_entries 通过 schema 校验（依赖 jsonschema）"""
    if not edit_data.HAS_JSONSCHEMA:
        pytest.skip("jsonschema 未安装")

    data = {
        "table": "memory_entries",
        "records": [
            {"scope_type": "global", "scope_id": "global", "content": "hi"},
        ],
    }
    ok, err = edit_data.Validator.validate_schema(data, "memory_entries")
    assert ok is True
    assert err is None


def test_validate_schema_memory_entries_bad_scope():
    """非法 scope_type 应被 schema 拒绝"""
    if not edit_data.HAS_JSONSCHEMA:
        pytest.skip("jsonschema 未安装")

    data = {
        "table": "memory_entries",
        "records": [
            {"scope_type": "invalid", "scope_id": "global", "content": "hi"},
        ],
    }
    ok, err = edit_data.Validator.validate_schema(data, "memory_entries")
    assert ok is False
    assert err is not None


def test_validate_schema_unknown_table_passes():
    """未定义 schema 的表名直接放行"""
    ok, err = edit_data.Validator.validate_schema({"table": "x"}, "no_such_table")
    assert ok is True


# ===========================================================================
# BackupManager
# ===========================================================================

@pytest.fixture
def backupEnv(tmp_path, monkeypatch):
    """构造一个隔离的项目根 + 备份目录，并 monkeypatch 模块级 PROJECT_ROOT。"""
    monkeypatch.setattr(edit_data, "PROJECT_ROOT", tmp_path)
    backup_dir = tmp_path / "backup"
    mgr = edit_data.BackupManager(backup_dir)
    return tmp_path, backup_dir, mgr


def test_backup_create_and_manifest(backupEnv):
    """create_backup 复制文件、写 manifest、记录 SHA256"""
    root, backup_dir, mgr = backupEnv
    original = root / "data.json"
    original.write_text('{"v": 1}', encoding="utf-8")

    backup_path = mgr.create_backup(original)

    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == '{"v": 1}'
    # manifest 持久化
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["backups"]) == 1
    entry = manifest["backups"][0]
    assert entry["original_path"] == "data.json"
    assert entry["sha256"] == edit_data.calculate_sha256(backup_path)


def test_backup_create_missing_file_raises(backupEnv):
    root, _, mgr = backupEnv
    with pytest.raises(FileNotFoundError):
        mgr.create_backup(root / "nope.json")


def test_backup_list_sorted_and_limited(backupEnv):
    """list_backups 按时间倒序并受 limit 截断"""
    _, _, mgr = backupEnv
    mgr.manifest["backups"] = [
        {"backup_file": "a", "timestamp": "2026-01-01T00:00:00"},
        {"backup_file": "b", "timestamp": "2026-03-01T00:00:00"},
        {"backup_file": "c", "timestamp": "2026-02-01T00:00:00"},
    ]
    result = mgr.list_backups(limit=2)
    assert [b["backup_file"] for b in result] == ["b", "c"]


def test_backup_restore_roundtrip(backupEnv):
    """restore_backup 能把原文件恢复为备份时的内容（BUG-1 已修复：文件名冲突时加序号去重）"""
    root, _, mgr = backupEnv
    original = root / "data.json"
    original.write_text("v1", encoding="utf-8")
    backup_path = mgr.create_backup(original)

    # 改动原文件后恢复
    original.write_text("v2-modified", encoding="utf-8")
    ok = mgr.restore_backup(backup_path.name)

    assert ok is True
    assert original.read_text(encoding="utf-8") == "v1"


class _FixedDatetime:
    """datetime 替身：now() 始终返回同一时刻，用于确定性复现时间戳冲突。"""

    _fixed = _real_datetime(2026, 6, 7, 0, 32, 25, 123456)

    @classmethod
    def now(cls):
        return cls._fixed


def test_backup_same_timestamp_no_overwrite(backupEnv, monkeypatch):
    """
    BUG-1 边界：同一时间戳下连续两次 create_backup。

    即便时间戳完全相同（精度无法区分），两次备份也必须落到不同文件、
    互不覆盖，且 manifest 各记录一条。验证文件名冲突时的序号去重逻辑。
    """
    root, backup_dir, mgr = backupEnv
    monkeypatch.setattr(edit_data, "datetime", _FixedDatetime)

    original = root / "data.json"

    original.write_text("first", encoding="utf-8")
    backup1 = mgr.create_backup(original)

    original.write_text("second", encoding="utf-8")
    backup2 = mgr.create_backup(original)

    # 两次备份是不同的物理文件
    assert backup1 != backup2
    assert backup1.exists() and backup2.exists()
    # 各自内容独立保留，未被覆盖
    assert backup1.read_text(encoding="utf-8") == "first"
    assert backup2.read_text(encoding="utf-8") == "second"
    # manifest 记录两条
    assert len(mgr.manifest["backups"]) == 2


def test_backup_restore_under_timestamp_collision(backupEnv, monkeypatch):
    """
    BUG-1 根因场景：时间戳冲突下的恢复。

    原缺陷是 restore 前的二次备份与首次备份同名互相覆盖，导致恢复出修改后内容。
    强制 now() 恒定后，恢复仍须还原到备份时刻的内容。
    """
    root, _, mgr = backupEnv
    monkeypatch.setattr(edit_data, "datetime", _FixedDatetime)

    original = root / "data.json"
    original.write_text("v1", encoding="utf-8")
    backup_path = mgr.create_backup(original)

    original.write_text("v2-modified", encoding="utf-8")
    ok = mgr.restore_backup(backup_path.name)

    assert ok is True
    assert original.read_text(encoding="utf-8") == "v1"


def test_backup_restore_unknown_entry(backupEnv):
    _, _, mgr = backupEnv
    assert mgr.restore_backup("does_not_exist.json") is False


# ===========================================================================
# DBEditor（明文单表数据库）
# ===========================================================================

def _make_single_table_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memory_entries ("
        "id INTEGER PRIMARY KEY, content TEXT, tags_json TEXT)"
    )
    conn.execute(
        "INSERT INTO memory_entries (id, content, tags_json) VALUES (1, 'first', ?)",
        (json.dumps(["t1", "t2"]),),
    )
    conn.commit()
    conn.close()


def test_dbeditor_export_parses_tags(tmp_path):
    """export_to_json 返回单表结构并把 tags_json 解析为 list"""
    db = tmp_path / "mem.db"
    _make_single_table_db(db)

    result = edit_data.DBEditor.export_to_json(db)

    assert result["table"] == "memory_entries"
    assert len(result["records"]) == 1
    assert result["records"][0]["tags_json"] == ["t1", "t2"]


def test_dbeditor_export_empty_db_raises(tmp_path):
    """无表数据库导出应报错"""
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    with pytest.raises(ValueError):
        edit_data.DBEditor.export_to_json(db)


def test_dbeditor_import_insert_update_delete(tmp_path):
    """import_from_json 正确执行新增/更新/删除"""
    db = tmp_path / "mem.db"
    _make_single_table_db(db)

    # id=1 改内容(update)，新增 id=null(insert)，原 id=1 保留 → 不删；这里删除靠"现有不在新列表"
    data = {
        "table": "memory_entries",
        "records": [
            {"id": 1, "content": "updated", "tags_json": ["x"]},
            {"id": None, "content": "brand new", "tags_json": []},
        ],
    }
    edit_data.DBEditor.import_from_json(db, data, dry_run=False)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {r["content"] for r in conn.execute("SELECT content FROM memory_entries").fetchall()}
    conn.close()
    assert "updated" in rows
    assert "brand new" in rows
    assert "first" not in rows  # 已被 update 覆盖


def test_dbeditor_import_delete_missing(tmp_path):
    """新 JSON 中不含的现有记录会被删除"""
    db = tmp_path / "mem.db"
    _make_single_table_db(db)

    data = {"table": "memory_entries", "records": []}  # 清空
    edit_data.DBEditor.import_from_json(db, data, dry_run=False)

    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
    conn.close()
    assert count == 0


def test_dbeditor_import_dry_run_no_write(tmp_path):
    """dry_run 不应改动数据库，但返回 SQL 列表"""
    db = tmp_path / "mem.db"
    _make_single_table_db(db)

    data = {"table": "memory_entries", "records": []}
    stmts = edit_data.DBEditor.import_from_json(db, data, dry_run=True)

    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
    conn.close()
    assert count == 1  # 未被删除
    assert len(stmts) == 1  # 有一条 DELETE 计划


def test_dbeditor_import_rejects_bad_table_name(tmp_path):
    """非法表名应被拒绝（SQL 注入防护）"""
    db = tmp_path / "mem.db"
    _make_single_table_db(db)
    data = {"table": "mem; DROP TABLE x", "records": []}
    with pytest.raises(ValueError):
        edit_data.DBEditor.import_from_json(db, data)


# ===========================================================================
# EncryptedDBEditor（加密聊天记录）
# ===========================================================================

def _make_messages_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY, chat_id TEXT, direction TEXT, "
        "sender TEXT, content BLOB, timestamp TEXT)"
    )
    conn.commit()
    conn.close()


def test_encrypted_load_key_bad_length(tmp_path):
    """密钥长度非 44 字节应报错"""
    key_path = tmp_path / ".chatKey"
    key_path.write_bytes(b"too short")
    with pytest.raises(ValueError):
        edit_data.EncryptedDBEditor.load_key(key_path)


def test_encrypted_load_key_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        edit_data.EncryptedDBEditor.load_key(tmp_path / "nope")


def test_encrypted_roundtrip(tmp_path):
    """加密导入后再解密导出，内容应一致"""
    from cryptography.fernet import Fernet

    key_path = tmp_path / ".chatKey"
    key_path.write_bytes(Fernet.generate_key())

    db = tmp_path / "chatHistory.db"
    _make_messages_db(db)

    data = {
        "table": "messages",
        "records": [
            {
                "id": None,
                "chat_id": "100",
                "direction": "in",
                "sender": "alice",
                "content": "你好喵\n第二行",
                "timestamp": "2026-06-06T12:00:00",
            },
        ],
    }
    edit_data.EncryptedDBEditor.import_from_json(db, key_path, data, dry_run=False)

    exported = edit_data.EncryptedDBEditor.export_to_json(db, key_path, limit=0)
    assert len(exported["records"]) == 1
    rec = exported["records"][0]
    assert rec["content"] == "你好喵\n第二行"
    assert rec["chat_id"] == "100"


def test_encrypted_export_chat_id_filter(tmp_path):
    """export 的 chat_id 过滤只返回指定聊天"""
    from cryptography.fernet import Fernet

    key_path = tmp_path / ".chatKey"
    key_path.write_bytes(Fernet.generate_key())
    db = tmp_path / "chatHistory.db"
    _make_messages_db(db)

    data = {
        "table": "messages",
        "records": [
            {"id": None, "chat_id": "100", "direction": "in", "sender": "a",
             "content": "m1", "timestamp": "2026-06-06T12:00:00"},
            {"id": None, "chat_id": "200", "direction": "in", "sender": "b",
             "content": "m2", "timestamp": "2026-06-06T12:01:00"},
        ],
    }
    edit_data.EncryptedDBEditor.import_from_json(db, key_path, data, dry_run=False)

    exported = edit_data.EncryptedDBEditor.export_to_json(db, key_path, chat_id="200", limit=0)
    assert len(exported["records"]) == 1
    assert exported["records"][0]["content"] == "m2"


def test_encrypted_import_dry_run_no_write(tmp_path):
    """加密导入 dry_run 不落库"""
    from cryptography.fernet import Fernet

    key_path = tmp_path / ".chatKey"
    key_path.write_bytes(Fernet.generate_key())
    db = tmp_path / "chatHistory.db"
    _make_messages_db(db)

    data = {
        "table": "messages",
        "records": [
            {"id": None, "chat_id": "1", "direction": "in", "sender": "a",
             "content": "x", "timestamp": "2026-06-06T12:00:00"},
        ],
    }
    edit_data.EncryptedDBEditor.import_from_json(db, key_path, data, dry_run=True)

    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    assert count == 0
