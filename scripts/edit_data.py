#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZincNya 数据编辑器

离线编辑 data/ 目录下的 JSON 配置、SQLite 数据库、Markdown 知识库和加密聊天记录。
提供交互式菜单、自动备份、数据验证和安全恢复功能。

使用方法：
    python scripts/edit_data.py                          # 交互式菜单
    python scripts/edit_data.py data/llm/llmMemory.db    # 直接编辑指定文件
    python scripts/edit_data.py --list                   # 列出所有可编辑文件
    python scripts/edit_data.py --backups                # 查看备份列表
    python scripts/edit_data.py --restore                # 从备份恢复
"""

import os
import sys
import json
import sqlite3
import shutil
import hashlib
import tempfile
import subprocess
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any

# 可选依赖检测
try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

try:
    from rich.console import Console
    from rich.table import Table
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# 加密支持（必需依赖）
try:
    from cryptography.fernet import Fernet
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    print("[ERROR] cryptography 库未安装，无法处理加密聊天记录")
    print("       请运行: pip install cryptography")

# ============================================================================
# 配置常量
# ============================================================================

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
DATA_DIR = PROJECT_ROOT / "data"
BACKUP_DIR = DATA_DIR / "zincnya_backup" / "edit_data"
KEY_PATH = DATA_DIR / ".chatKey"

# 文件扫描配置
SCAN_PATTERNS = {
    "json": [
        DATA_DIR / "*.json",
        DATA_DIR / "llm" / "*.json",
    ],
    "db": [
        DATA_DIR / "*.db",
        DATA_DIR / "llm" / "*.db",
    ],
    "markdown": [
        DATA_DIR / "llm" / "knowledge" / "*.md",
    ],
}

# 排除的文件模式
EXCLUDE_PATTERNS = [
    "*.example.json",
    "*.bak",
    "*~",
]


# ============================================================================
# JSON Schema 定义
# ============================================================================

SCHEMAS = {
    "memory_entries": {
        "type": "object",
        "required": ["table", "records"],
        "properties": {
            "table": {"type": "string", "const": "memory_entries"},
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["content", "scope_type", "scope_id"],
                    "properties": {
                        "id": {"type": ["integer", "null"]},
                        "scope_type": {"enum": ["global", "chat", "user"]},
                        "scope_id": {"type": "string"},
                        "content": {"type": "string", "minLength": 1},
                        "tags_json": {"type": "array", "items": {"type": "string"}},
                        "enabled": {"type": "integer", "enum": [0, 1]},
                        "priority": {"type": "integer", "minimum": 1, "maximum": 3},
                        "source": {"type": "string"},
                        "created_at": {"type": "string"},
                        "updated_at": {"type": "string"},
                    }
                }
            }
        }
    },
    "knowledge_entries": {
        "type": "object",
        "required": ["table", "records"],
        "properties": {
            "table": {"type": "string", "const": "knowledge_entries"},
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["category", "title", "content"],
                    "properties": {
                        "id": {"type": ["integer", "null"]},
                        "category": {"type": "string"},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "tags_json": {"type": "array", "items": {"type": "string"}},
                        "source_file": {"type": ["string", "null"]},
                        "source_hash": {"type": ["string", "null"]},
                        "priority": {"type": "integer"},
                        "enabled": {"type": "integer", "enum": [0, 1]},
                        "created_at": {"type": "string"},
                        "updated_at": {"type": "string"},
                    }
                }
            }
        }
    },
    "todos": {
        "type": "object",
        "required": ["table", "records"],
        "properties": {
            "table": {"type": "string", "const": "todos"},
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["content"],
                    "properties": {
                        "id": {"type": ["integer", "null"]},
                        "chat_id": {"type": ["string", "null"]},
                        "user_id": {"type": ["integer", "null"]},
                        "content": {"type": "string"},
                        "remind_time": {"type": ["string", "null"]},
                        "priority": {"type": "integer"},
                        "status": {"type": "string"},
                        "reminded": {"type": "integer", "enum": [0, 1]},
                        "created_at": {"type": "string"},
                        "completed_at": {"type": ["string", "null"]},
                    }
                }
            }
        }
    }
}


# ============================================================================
# 工具函数
# ============================================================================

def calculate_sha256(file_path: Path) -> str:
    """计算文件的 SHA256 哈希值"""
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256.update(chunk)
    return sha256.hexdigest()


def is_valid_table_name(name: str) -> bool:
    """验证 SQLite 表名是否安全（仅包含字母、数字、下划线）"""
    import re
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def print_info(message: str):
    """打印信息消息"""
    if HAS_RICH:
        Console().print(f"[cyan][INFO][/cyan] {message}")
    else:
        print(f"[INFO] {message}")


def print_success(message: str):
    """打印成功消息"""
    if HAS_RICH:
        Console().print(f"[green]✓[/green] {message}")
    else:
        print(f"[✓] {message}")


def print_warning(message: str):
    """打印警告消息"""
    if HAS_RICH:
        Console().print(f"[yellow][WARN][/yellow] {message}")
    else:
        print(f"[WARN] {message}")


def print_error(message: str):
    """打印错误消息"""
    if HAS_RICH:
        Console().print(f"[red][ERROR][/red] {message}")
    else:
        print(f"[ERROR] {message}")


# ============================================================================
# 文件分类与扫描
# ============================================================================

def classify_file(file_path: Path) -> str:
    """
    根据文件路径和内容自动判断类型

    Returns:
        'json', 'markdown', 'sqlite_db', 'encrypted_db', 'unknown'
    """
    ext = file_path.suffix.lower()

    if ext == '.json':
        return 'json'
    elif ext == '.md':
        return 'markdown'
    elif ext == '.db':
        # 检查是否是加密聊天记录
        if 'chatHistory' in file_path.name:
            return 'encrypted_db'
        else:
            return 'sqlite_db'
    return 'unknown'


def should_exclude(file_path: Path) -> bool:
    """检查文件是否应该被排除"""
    for pattern in EXCLUDE_PATTERNS:
        if file_path.match(pattern):
            return True
    return False


def scan_editable_files() -> Dict[str, List[Path]]:
    """
    扫描所有可编辑的文件

    Returns:
        按类型分组的文件列表字典
    """
    files_by_type = {
        'json': [],
        'sqlite_db': [],
        'encrypted_db': [],
        'markdown': [],
    }

    # 扫描 JSON 文件
    for pattern in SCAN_PATTERNS['json']:
        for file_path in pattern.parent.glob(pattern.name):
            if file_path.is_file() and not should_exclude(file_path):
                files_by_type['json'].append(file_path)

    # 扫描数据库文件
    for pattern in SCAN_PATTERNS['db']:
        for file_path in pattern.parent.glob(pattern.name):
            if file_path.is_file() and not should_exclude(file_path):
                file_type = classify_file(file_path)
                if file_type in files_by_type:
                    files_by_type[file_type].append(file_path)

    # 扫描 Markdown 文件
    for pattern in SCAN_PATTERNS['markdown']:
        for file_path in pattern.parent.glob(pattern.name):
            if file_path.is_file() and not should_exclude(file_path):
                files_by_type['markdown'].append(file_path)

    # 去重并排序
    for file_type in files_by_type:
        files_by_type[file_type] = sorted(set(files_by_type[file_type]))

    return files_by_type


def get_db_record_count(db_path: Path) -> Optional[int]:
    """获取数据库的记录数（仅限单表数据库）"""
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()

        if len(tables) == 1:
            table_name = tables[0][0]
            # 表名虽来自 sqlite_master（非用户直接输入），但库文件本身可能被替换，
            # 仍按与其它查询一致的规则校验，避免恶意库名注入。
            if not is_valid_table_name(table_name):
                conn.close()
                return None
            count = cur.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            conn.close()
            return count
        conn.close()
    except Exception:
        pass
    return None


# ============================================================================
# 备份管理
# ============================================================================

class BackupManager:
    """备份管理器"""

    def __init__(self, backup_dir: Path):
        self.backup_dir = backup_dir
        self.manifest_path = backup_dir / "manifest.json"
        self._ensure_backup_dir()
        self._load_manifest()

    def _ensure_backup_dir(self):
        """确保备份目录存在"""
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _load_manifest(self):
        """加载备份清单"""
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, 'r', encoding='utf-8') as f:
                    self.manifest = json.load(f)
                    if 'backups' not in self.manifest:
                        self.manifest = {'backups': []}
            except Exception as e:
                print_warning(f"manifest.json 损坏，将重建: {e}")
                self.manifest = {'backups': []}
        else:
            self.manifest = {'backups': []}

    def _save_manifest(self):
        """保存备份清单"""
        try:
            with open(self.manifest_path, 'w', encoding='utf-8') as f:
                json.dump(self.manifest, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print_error(f"无法保存 manifest.json: {e}")

    def create_backup(self, original_path: Path) -> Path:
        """
        创建文件备份

        Args:
            original_path: 原始文件路径

        Returns:
            备份文件路径
        """
        if not original_path.exists():
            raise FileNotFoundError(f"原始文件不存在: {original_path}")

        # 生成备份文件名（包含到毫秒的时间戳，避免同秒内多次备份文件名冲突）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        backup_name = f"{original_path.stem}_{timestamp}{original_path.suffix}"
        backup_path = self.backup_dir / backup_name

        # 兜底：即使时间戳相同（同毫秒内多次备份），也追加序号确保文件名唯一，
        # 防止新备份覆盖已有备份（restore 前的二次备份尤其容易撞上首次备份）
        if backup_path.exists():
            seq = 1
            while True:
                backup_name = f"{original_path.stem}_{timestamp}_{seq}{original_path.suffix}"
                backup_path = self.backup_dir / backup_name
                if not backup_path.exists():
                    break
                seq += 1

        # 复制文件
        shutil.copy2(original_path, backup_path)

        # 计算 SHA256
        sha256 = calculate_sha256(backup_path)

        # 更新清单
        backup_entry = {
            "backup_file": backup_name,
            "original_path": str(original_path.relative_to(PROJECT_ROOT)),
            "timestamp": datetime.now().isoformat(),
            "sha256": sha256,
            "size": backup_path.stat().st_size
        }
        self.manifest['backups'].append(backup_entry)
        self._save_manifest()

        print_info(f"已备份至: {backup_path.relative_to(PROJECT_ROOT)}")
        return backup_path

    def list_backups(self, limit: int = 20) -> List[Dict]:
        """
        列出备份（按时间倒序）

        Args:
            limit: 最多返回的备份数量

        Returns:
            备份条目列表
        """
        backups = sorted(
            self.manifest['backups'],
            key=lambda x: x['timestamp'],
            reverse=True
        )
        return backups[:limit]

    def restore_backup(self, backup_file: str) -> bool:
        """
        从备份恢复文件

        Args:
            backup_file: 备份文件名

        Returns:
            是否成功
        """
        # 查找备份条目
        backup_entry = next(
            (b for b in self.manifest['backups'] if b['backup_file'] == backup_file),
            None
        )

        if not backup_entry:
            print_error(f"未找到备份条目: {backup_file}")
            return False

        backup_path = self.backup_dir / backup_file
        if not backup_path.exists():
            print_error(f"备份文件不存在: {backup_path}")
            return False

        # manifest.json 是 data/ 下的明文文件，可能被篡改。original_path 取自其中，
        # 必须 resolve 后限制在项目目录内，否则被篡改的绝对路径或 .. 遍历会让
        # 下方的 replace() 把备份内容写到项目外的任意可写位置（如 /etc/cron.d）。
        original_path = (PROJECT_ROOT / backup_entry['original_path']).resolve()
        if original_path != PROJECT_ROOT and PROJECT_ROOT not in original_path.parents:
            print_error(f"备份记录的原始路径越界，拒绝恢复: {backup_entry['original_path']}")
            return False

        # 验证 SHA256
        current_sha256 = calculate_sha256(backup_path)
        if current_sha256 != backup_entry['sha256']:
            print_warning(f"备份文件 SHA256 不匹配，可能已损坏")
            response = input("是否继续恢复? (y/n): ").strip().lower()
            if response != 'y':
                return False

        # 恢复前先备份当前文件
        if original_path.exists():
            print_info("恢复前备份当前文件...")
            self.create_backup(original_path)

        # 使用原子操作恢复备份（防止 TOCTOU 竞争）
        # 先复制到临时文件，再原子替换原文件
        temp_restore = original_path.with_suffix(original_path.suffix + '.tmp_restore')
        try:
            shutil.copy2(backup_path, temp_restore)
            # Path.replace() 在 Windows/Unix 都是原子操作
            temp_restore.replace(original_path)
            print_success(f"已恢复: {original_path.relative_to(PROJECT_ROOT)}")
            return True
        except Exception as e:
            print_error(f"恢复失败: {e}")
            if temp_restore.exists():
                temp_restore.unlink()
            return False


# ============================================================================
# 编辑器选择
# ============================================================================

class EditorSelector:
    """编辑器检测与选择"""

    @staticmethod
    def find_available_editor() -> Optional[Tuple[str, List[str]]]:
        """
        查找可用的编辑器

        Returns:
            (编辑器名称, 命令参数列表) 或 None
        """
        # Windows: 直接使用记事本
        if sys.platform == 'win32':
            return ('Notepad', ['notepad.exe'])

        # Unix: 按优先级检测编辑器
        code_path = shutil.which('code')
        if code_path and os.path.exists(code_path):
            return ('VS Code', [code_path, '--wait'])

        if shutil.which('nano'):
            return ('nano', ['nano'])

        if shutil.which('vim'):
            return ('vim', ['vim'])

        return None

    @staticmethod
    def open_file(file_path: Path, editor_override: Optional[str] = None) -> bool:
        """
        打开文件进行编辑

        Args:
            file_path: 文件路径
            editor_override: 强制使用的编辑器命令

        Returns:
            是否成功打开
        """
        if editor_override:
            # 使用指定的编辑器
            if not shutil.which(editor_override):
                print_error(f"指定的编辑器不可用: {editor_override}")
                return False
            cmd = [editor_override, str(file_path)]
            editor_name = editor_override
        else:
            # 自动检测编辑器
            editor = EditorSelector.find_available_editor()
            if editor:
                editor_name, cmd = editor
                cmd = cmd + [str(file_path)]
            else:
                # 回退模式：打印路径让用户手动编辑
                print_warning("未检测到可用的编辑器（VS Code, nano, vim, notepad）")
                print_info(f"请手动编辑以下文件:")
                print(f"  {file_path}")
                input("\n编辑完成后按 Enter 继续...")
                return True

        # 打开编辑器
        print_info(f"使用 {editor_name} 打开...")
        try:
            result = subprocess.run(cmd, check=True)
            return result.returncode == 0
        except subprocess.CalledProcessError as e:
            print_error(f"编辑器退出异常: {e}")
            return False
        except Exception as e:
            print_error(f"无法启动编辑器: {e}")
            return False


# ============================================================================
# 数据库编辑器
# ============================================================================

class DBEditor:
    """数据库 ↔ JSON 转换编辑器"""

    @staticmethod
    def export_to_json(db_path: Path) -> Dict:
        """
        导出数据库为 JSON

        Args:
            db_path: 数据库路径

        Returns:
            JSON 数据字典
        """
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 检测所有表（排除 SQLite 系统表）
        tables = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()

        if len(tables) == 0:
            raise ValueError("数据库中没有表")
        elif len(tables) == 1:
            # 单表数据库（llmMemory, todos, knowledge）
            table_name = tables[0][0]

            # 验证表名安全性（防止 SQL 注入）
            if not is_valid_table_name(table_name):
                raise ValueError(f"不安全的表名: {table_name}")

            rows = cur.execute(f"SELECT * FROM {table_name}").fetchall()
            records = [dict(row) for row in rows]

            # 解析 JSON 字段（tags_json）
            for rec in records:
                if 'tags_json' in rec and rec['tags_json']:
                    try:
                        rec['tags_json'] = json.loads(rec['tags_json'])
                    except json.JSONDecodeError:
                        rec['tags_json'] = []

            conn.close()
            return {"table": table_name, "records": records}
        else:
            # 多表数据库（未来扩展）
            result = {"tables": {}}
            for (table_name,) in tables:
                # 验证表名安全性
                if not is_valid_table_name(table_name):
                    print_warning(f"跳过不安全的表名: {table_name}")
                    continue

                rows = cur.execute(f"SELECT * FROM {table_name}").fetchall()
                result["tables"][table_name] = [dict(row) for row in rows]
            conn.close()
            return result

    @staticmethod
    def import_from_json(db_path: Path, data: Dict, dry_run: bool = False) -> List[str]:
        """
        从 JSON 导入数据到数据库

        Args:
            db_path: 数据库路径
            data: JSON 数据
            dry_run: 是否为干跑模式（仅显示 SQL 不执行）

        Returns:
            执行的 SQL 语句列表
        """
        if "table" not in data or "records" not in data:
            raise ValueError("JSON 格式错误：缺少 'table' 或 'records' 字段")

        table_name = data["table"]
        new_records = data["records"]

        # 验证表名安全性（防止 SQL 注入）
        if not is_valid_table_name(table_name):
            raise ValueError(f"不安全的表名: {table_name}")

        # 连接数据库。用 try/finally 确保任何异常路径都回滚并关闭连接，
        # 否则中途抛错会泄露连接（依赖 GC 隐式回滚），在 Windows 上还会锁住 DB 文件。
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        try:
            # 取出表的真实列名作为白名单。列名来自被编辑/恢复的 JSON（不可信），
            # 会被直接拼进 INSERT/UPDATE 语句，因此必须按真实 schema 校验，
            # 防止恶意 key（如 "x); DROP TABLE ..--"）注入 SQL。
            valid_columns = {row[1] for row in cur.execute(f"PRAGMA table_info({table_name})").fetchall()}

            def _check_columns(rec_keys):
                for k in rec_keys:
                    if k not in valid_columns:
                        raise ValueError(f"不安全或未知的列名: {k}")

            # 读取现有记录
            existing_rows = cur.execute(f"SELECT * FROM {table_name}").fetchall()
            existing_records = {row["id"]: dict(row) for row in existing_rows if "id" in row.keys()}

            # 对比生成 SQL
            sql_statements = []
            stats = {"insert": 0, "update": 0, "delete": 0}

            # 处理新记录（INSERT 或 UPDATE）
            for rec in new_records:
                rec_id = rec.get("id")

                # 校验本条记录的所有列名都属于真实 schema（防 SQL 注入）
                _check_columns(rec.keys())

                # 序列化 JSON 字段
                if 'tags_json' in rec and isinstance(rec['tags_json'], list):
                    rec['tags_json'] = json.dumps(rec['tags_json'], ensure_ascii=False)

                if rec_id is None or rec_id not in existing_records:
                    # INSERT（新记录或 id 为 null）
                    columns = [k for k in rec.keys() if k != 'id' or rec['id'] is not None]
                    placeholders = ', '.join(['?'] * len(columns))
                    col_names = ', '.join(columns)
                    values = [rec[k] for k in columns]

                    sql = f"INSERT INTO {table_name} ({col_names}) VALUES ({placeholders})"
                    sql_statements.append((sql, values))
                    stats["insert"] += 1

                    if not dry_run:
                        cur.execute(sql, values)
                else:
                    # UPDATE（更新现有记录）
                    set_clause = ', '.join([f"{k} = ?" for k in rec.keys() if k != 'id'])
                    values = [rec[k] for k in rec.keys() if k != 'id'] + [rec_id]

                    sql = f"UPDATE {table_name} SET {set_clause} WHERE id = ?"
                    sql_statements.append((sql, values))
                    stats["update"] += 1

                    if not dry_run:
                        cur.execute(sql, values)

            # 处理删除（现有记录在新 JSON 中不存在）
            new_ids = {rec.get("id") for rec in new_records if rec.get("id") is not None}
            for existing_id in existing_records:
                if existing_id not in new_ids:
                    sql = f"DELETE FROM {table_name} WHERE id = ?"
                    sql_statements.append((sql, [existing_id]))
                    stats["delete"] += 1

                    if not dry_run:
                        cur.execute(sql, [existing_id])

            # 提交或回滚
            if not dry_run:
                conn.commit()
                print_success(f"事务提交成功")
                print_info(f"插入 {stats['insert']} 条, 更新 {stats['update']} 条, 删除 {stats['delete']} 条")
            else:
                print_info(f"[DRY-RUN] 将执行:")
                print_info(f"  插入 {stats['insert']} 条, 更新 {stats['update']} 条, 删除 {stats['delete']} 条")

            return sql_statements
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class EncryptedDBEditor:
    """加密数据库编辑器（chatHistory.db）"""

    @staticmethod
    def load_key(key_path: Path) -> Fernet:
        """加载加密密钥"""
        if not key_path.exists():
            raise FileNotFoundError(f"加密密钥不存在: {key_path}")

        with open(key_path, 'rb') as f:
            key = f.read()

        if len(key) != 44:  # Base64 编码的 32 字节密钥 = 44 字节
            raise ValueError(f"密钥格式错误（应为 44 字节 Base64）: {len(key)} 字节")

        return Fernet(key)

    @staticmethod
    def export_to_json(db_path: Path, key_path: Path, chat_id: Optional[str] = None, limit: int = 100) -> Dict:
        """
        解密并导出聊天记录为 JSON

        Args:
            db_path: chatHistory.db 路径
            key_path: .chatKey 路径
            chat_id: 可选，仅导出指定聊天的消息
            limit: 限制导出条数（默认 100，0 表示无限制）

        Returns:
            JSON 数据字典
        """
        fernet = EncryptedDBEditor.load_key(key_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # 构建查询
        query = "SELECT * FROM messages"
        params = []

        if chat_id:
            query += " WHERE chat_id = ?"
            params.append(chat_id)

        query += " ORDER BY timestamp DESC"

        if limit > 0:
            query += f" LIMIT {limit}"

        rows = conn.execute(query, params).fetchall()

        # 解密
        decrypted = []
        for row in rows:
            try:
                content = fernet.decrypt(row["content"]).decode('utf-8')
            except Exception as e:
                content = f"[解密失败: {e}]"

            decrypted.append({
                "id": row["id"],
                "chat_id": row["chat_id"],
                "direction": row["direction"],
                "sender": row["sender"],
                "content": content,
                "timestamp": row["timestamp"]
            })

        conn.close()
        return {"table": "messages", "records": decrypted}

    @staticmethod
    def import_from_json(db_path: Path, key_path: Path, data: Dict, dry_run: bool = False) -> List[str]:
        """
        加密并导入聊天记录到数据库

        Args:
            db_path: chatHistory.db 路径
            key_path: .chatKey 路径
            data: JSON 数据
            dry_run: 是否为干跑模式

        Returns:
            执行的 SQL 语句列表
        """
        fernet = EncryptedDBEditor.load_key(key_path)

        if "table" not in data or "records" not in data:
            raise ValueError("JSON 格式错误：缺少 'table' 或 'records' 字段")

        new_records = data["records"]

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # 异常时显式回滚并关闭连接，避免连接泄漏与 Windows 下 DB 文件被锁。
        try:
            cur = conn.cursor()

            # 读取现有记录
            existing_rows = cur.execute("SELECT * FROM messages").fetchall()
            existing_records = {row["id"]: dict(row) for row in existing_rows}

            sql_statements = []
            stats = {"insert": 0, "update": 0, "delete": 0}

            # 处理新记录
            for rec in new_records:
                rec_id = rec.get("id")

                # 加密 content 字段
                if 'content' in rec:
                    encrypted_content = fernet.encrypt(rec['content'].encode('utf-8'))
                    rec['content'] = encrypted_content

                if rec_id is None or rec_id not in existing_records:
                    # INSERT
                    columns = ['chat_id', 'direction', 'sender', 'content', 'timestamp']
                    placeholders = ', '.join(['?'] * len(columns))
                    col_names = ', '.join(columns)
                    values = [rec.get(k) for k in columns]

                    sql = f"INSERT INTO messages ({col_names}) VALUES ({placeholders})"
                    sql_statements.append((sql, values))
                    stats["insert"] += 1

                    if not dry_run:
                        cur.execute(sql, values)
                else:
                    # UPDATE
                    set_clause = 'chat_id = ?, direction = ?, sender = ?, content = ?, timestamp = ?'
                    values = [rec['chat_id'], rec['direction'], rec['sender'], rec['content'], rec['timestamp'], rec_id]

                    sql = f"UPDATE messages SET {set_clause} WHERE id = ?"
                    sql_statements.append((sql, values))
                    stats["update"] += 1

                    if not dry_run:
                        cur.execute(sql, values)

            # 处理删除
            new_ids = {rec.get("id") for rec in new_records if rec.get("id") is not None}
            for existing_id in existing_records:
                if existing_id not in new_ids:
                    sql = f"DELETE FROM messages WHERE id = ?"
                    sql_statements.append((sql, [existing_id]))
                    stats["delete"] += 1

                    if not dry_run:
                        cur.execute(sql, [existing_id])

            if not dry_run:
                conn.commit()
                print_success(f"事务提交成功")
                print_info(f"插入 {stats['insert']} 条, 更新 {stats['update']} 条, 删除 {stats['delete']} 条")
            else:
                print_info(f"[DRY-RUN] 将执行:")
                print_info(f"  插入 {stats['insert']} 条, 更新 {stats['update']} 条, 删除 {stats['delete']} 条")

            return sql_statements
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ============================================================================
# 验证器
# ============================================================================

class Validator:
    """数据验证器"""

    @staticmethod
    def validate_json_format(data: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        验证 JSON 格式

        Returns:
            (是否有效, 解析后的数据, 错误信息)
        """
        try:
            parsed = json.loads(data)
            return (True, parsed, None)
        except json.JSONDecodeError as e:
            return (False, None, str(e))

    @staticmethod
    def validate_schema(data: Dict, table_name: str) -> Tuple[bool, Optional[str]]:
        """
        验证 JSON Schema

        Returns:
            (是否有效, 错误信息)
        """
        if not HAS_JSONSCHEMA:
            print_warning("jsonschema 未安装，跳过 Schema 验证")
            return (True, None)

        if table_name not in SCHEMAS:
            print_warning(f"未定义 Schema: {table_name}")
            return (True, None)

        schema = SCHEMAS[table_name]

        try:
            jsonschema.validate(data, schema)
            return (True, None)
        except jsonschema.ValidationError as e:
            return (False, str(e))

    @staticmethod
    def validate_required_fields(data: Dict) -> Tuple[bool, Optional[str]]:
        """
        验证必需字段（基础检查，不依赖 jsonschema）

        Returns:
            (是否有效, 错误信息)
        """
        if "table" not in data:
            return (False, "缺少 'table' 字段")

        if "records" not in data:
            return (False, "缺少 'records' 字段")

        if not isinstance(data["records"], list):
            return (False, "'records' 必须是数组")

        table_name = data["table"]

        # 根据表名检查必需字段
        required_fields_map = {
            "memory_entries": ["content", "scope_type", "scope_id"],
            "knowledge_entries": ["category", "title", "content"],
            "todos": ["content"],
            "messages": ["chat_id", "direction", "content"],
        }

        if table_name in required_fields_map:
            required_fields = required_fields_map[table_name]
            for i, rec in enumerate(data["records"]):
                for field in required_fields:
                    if field not in rec or not rec[field]:
                        return (False, f"记录 {i+1} 缺少必需字段: {field}")

        return (True, None)

    @staticmethod
    def validate_tags_json(data: Dict) -> Tuple[bool, Optional[str]]:
        """
        验证 tags_json 字段格式

        Returns:
            (是否有效, 错误信息)
        """
        for i, rec in enumerate(data.get("records", [])):
            if "tags_json" in rec:
                if not isinstance(rec["tags_json"], list):
                    return (False, f"记录 {i+1} 的 tags_json 必须是数组，而不是 {type(rec['tags_json']).__name__}")
                if not all(isinstance(tag, str) for tag in rec["tags_json"]):
                    return (False, f"记录 {i+1} 的 tags_json 必须包含字符串")

        return (True, None)


# ============================================================================
# 主程序逻辑
# ============================================================================

def edit_file(file_path: Path, backup_mgr: BackupManager, editor_override: Optional[str] = None, dry_run: bool = False) -> bool:
    """
    编辑单个文件

    Args:
        file_path: 文件路径
        backup_mgr: 备份管理器
        editor_override: 强制使用的编辑器
        dry_run: 是否为干跑模式

    Returns:
        是否成功
    """
    file_type = classify_file(file_path)

    print_info(f"选择: {file_path.relative_to(PROJECT_ROOT)} ({file_type})")

    # 创建备份
    try:
        backup_mgr.create_backup(file_path)
    except Exception as e:
        print_error(f"备份失败: {e}")
        return False

    # 根据文件类型处理
    if file_type == 'json' or file_type == 'markdown':
        # 直接编辑
        original_sha256 = calculate_sha256(file_path)

        if not EditorSelector.open_file(file_path, editor_override):
            print_error("编辑器打开失败")
            return False

        # 检查是否修改
        new_sha256 = calculate_sha256(file_path)
        if original_sha256 == new_sha256:
            print_info("文件未修改，跳过")
            return True

        print_success("文件已保存")
        return True

    elif file_type == 'sqlite_db':
        # 数据库 → JSON 编辑
        return edit_database(file_path, backup_mgr, editor_override, dry_run)

    elif file_type == 'encrypted_db':
        # 加密数据库特殊处理
        return edit_encrypted_database(file_path, backup_mgr, editor_override, dry_run)

    else:
        print_error(f"不支持的文件类型: {file_type}")
        return False


def edit_database(db_path: Path, backup_mgr: BackupManager, editor_override: Optional[str] = None, dry_run: bool = False) -> bool:
    """编辑普通数据库"""
    try:
        # 导出为 JSON
        print_info("导出数据库为 JSON...")
        data = DBEditor.export_to_json(db_path)

        record_count = len(data.get("records", []))
        table_name = data.get("table", "unknown")
        print_info(f"检测到 {record_count} 条记录 (表: {table_name})")

        # 写入临时文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            tmp_path = Path(tmp.name)

        # 设置临时文件权限为仅所有者可读写（防止其他用户访问敏感数据）
        if sys.platform != 'win32':
            os.chmod(tmp_path, 0o600)

        print_info(f"临时文件: {tmp_path}")
        original_sha256 = calculate_sha256(tmp_path)

        # 打开编辑器
        if not EditorSelector.open_file(tmp_path, editor_override):
            print_error("编辑器打开失败")
            tmp_path.unlink()
            return False

        # 检查是否修改
        new_sha256 = calculate_sha256(tmp_path)
        if original_sha256 == new_sha256:
            print_info("文件未修改，跳过写入")
            tmp_path.unlink()
            return True

        print_info("检测到文件已修改")

        # 读取修改后的 JSON
        with open(tmp_path, 'r', encoding='utf-8') as f:
            modified_data = f.read()

        # 验证 JSON 格式
        valid, parsed_data, error = Validator.validate_json_format(modified_data)
        if not valid:
            print_error(f"JSON 格式错误: {error}")
            print_warning(f"临时文件保留在: {tmp_path}")
            print_info("请修复后重新运行脚本")
            return False

        # 验证必需字段
        valid, error = Validator.validate_required_fields(parsed_data)
        if not valid:
            print_error(f"必需字段验证失败: {error}")
            print_warning(f"临时文件保留在: {tmp_path}")
            return False

        # 验证 tags_json 字段
        valid, error = Validator.validate_tags_json(parsed_data)
        if not valid:
            print_error(f"tags_json 验证失败: {error}")
            print_warning(f"临时文件保留在: {tmp_path}")
            return False

        # 验证 Schema
        if HAS_JSONSCHEMA:
            valid, error = Validator.validate_schema(parsed_data, table_name)
            if not valid:
                print_error(f"Schema 验证失败: {error}")
                print_warning(f"临时文件保留在: {tmp_path}")
                return False
            print_success("Schema 验证通过")

        # 显示将要执行的更改
        print_info("[DIFF] 准备写入数据库...")

        # 确认写入
        if not dry_run:
            response = input("确认写入? (y/n/d=dry-run): ").strip().lower()
            if response == 'd':
                dry_run = True
            elif response != 'y':
                print_info("已取消")
                tmp_path.unlink()
                return False

        # 导入到数据库
        try:
            sql_statements = DBEditor.import_from_json(db_path, parsed_data, dry_run)

            if not dry_run:
                print_success("写入完成")
            else:
                print_info(f"[DRY-RUN] 共 {len(sql_statements)} 条 SQL 语句")
                for sql, values in sql_statements[:5]:  # 只显示前 5 条
                    print(f"  {sql}")
                if len(sql_statements) > 5:
                    print(f"  ... 还有 {len(sql_statements) - 5} 条")

        except Exception as e:
            print_error(f"数据库写入失败: {e}")
            print_warning(f"临时文件保留在: {tmp_path}")
            return False

        # 清理临时文件
        tmp_path.unlink()
        print_info("临时文件已清理")
        return True

    except Exception as e:
        print_error(f"编辑数据库时出错: {e}")
        import traceback
        traceback.print_exc()
        return False


def edit_encrypted_database(db_path: Path, backup_mgr: BackupManager, editor_override: Optional[str] = None, dry_run: bool = False, chat_id: Optional[str] = None, limit: int = 100) -> bool:
    """编辑加密数据库"""
    if not HAS_CRYPTO:
        print_error("cryptography 库未安装，无法处理加密数据库")
        return False

    if not KEY_PATH.exists():
        print_error(f"加密密钥不存在: {KEY_PATH}")
        print_info("请确保 data/.chatKey 文件存在")
        return False

    print_warning("警告：您正在编辑加密的聊天记录，涉及隐私数据")
    print_info(f"将导出最近 {limit} 条消息")
    if chat_id:
        print_info(f"仅导出聊天 ID: {chat_id}")

    response = input("确认继续? (y/n): ").strip().lower()
    if response != 'y':
        print_info("已取消")
        return False

    tmp_path = None
    try:
        # 导出
        print_info("解密并导出...")
        data = EncryptedDBEditor.export_to_json(db_path, KEY_PATH, chat_id, limit)

        record_count = len(data.get("records", []))
        print_info(f"已导出 {record_count} 条消息")

        # 写入临时文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            tmp_path = Path(tmp.name)

        # 设置临时文件权限为仅所有者可读写（防止其他用户访问敏感数据）
        if sys.platform != 'win32':
            os.chmod(tmp_path, 0o600)

        original_sha256 = calculate_sha256(tmp_path)

        # 打开编辑器
        if not EditorSelector.open_file(tmp_path, editor_override):
            print_error("编辑器打开失败")
            return False

        # 检查是否修改
        new_sha256 = calculate_sha256(tmp_path)
        if original_sha256 == new_sha256:
            print_info("文件未修改，跳过写入")
            return True

        print_info("检测到文件已修改")

        # 读取并验证
        with open(tmp_path, 'r', encoding='utf-8') as f:
            modified_data = f.read()

        valid, parsed_data, error = Validator.validate_json_format(modified_data)
        if not valid:
            print_error(f"JSON 格式错误: {error}")
            return False

        valid, error = Validator.validate_required_fields(parsed_data)
        if not valid:
            print_error(f"必需字段验证失败: {error}")
            return False

        # 确认写入
        if not dry_run:
            response = input("确认加密并写回数据库? (y/n/d=dry-run): ").strip().lower()
            if response == 'd':
                dry_run = True
            elif response != 'y':
                print_info("已取消")
                return False

        # 导入
        try:
            sql_statements = EncryptedDBEditor.import_from_json(db_path, KEY_PATH, parsed_data, dry_run)

            if not dry_run:
                print_success("写入完成")
            else:
                print_info(f"[DRY-RUN] 共 {len(sql_statements)} 条 SQL 语句")

        except Exception as e:
            print_error(f"数据库写入失败: {e}")
            return False

        return True

    except Exception as e:
        print_error(f"编辑加密数据库时出错: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        # 安全：临时文件含解密后的明文聊天记录，无论成功/失败/取消/异常，
        # 都必须删除，绝不保留在系统临时目录（多用户环境下可被其它用户读取）。
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
            else:
                print_info("临时文件已清理")


def show_main_menu(files_by_type: Dict[str, List[Path]]) -> Optional[Path]:
    """
    显示主菜单并获取用户选择

    Returns:
        选择的文件路径或 None
    """
    # 构建文件列表
    all_files = []

    print("\n" + "="*60)
    print("  ZincNya 数据编辑器 v1.0")
    print(f"  工作目录: {PROJECT_ROOT}")
    print("="*60 + "\n")

    index = 1

    # JSON 配置
    if files_by_type['json']:
        print(f"  JSON 配置 ({len(files_by_type['json'])}个)")
        for f in files_by_type['json']:
            size = format_size(f.stat().st_size)
            rel_path = f.relative_to(DATA_DIR)
            print(f"   {index}. {rel_path} ({size})")
            all_files.append(f)
            index += 1
        print()

    # SQLite 数据库
    if files_by_type['sqlite_db']:
        print(f"  SQLite 数据库 ({len(files_by_type['sqlite_db'])}个)")
        for f in files_by_type['sqlite_db']:
            size = format_size(f.stat().st_size)
            count = get_db_record_count(f)
            count_str = f", {count} 条" if count else ""
            rel_path = f.relative_to(DATA_DIR)
            print(f"   {index}. {rel_path} ({size}{count_str})")
            all_files.append(f)
            index += 1
        print()

    # 加密数据库
    if files_by_type['encrypted_db']:
        print(f"  加密数据库 ({len(files_by_type['encrypted_db'])}个)")
        for f in files_by_type['encrypted_db']:
            size = format_size(f.stat().st_size)
            rel_path = f.relative_to(DATA_DIR)
            print(f"   {index}. {rel_path} ({size}, 需密钥)")
            all_files.append(f)
            index += 1
        print()

    # Markdown 知识库
    if files_by_type['markdown']:
        print(f"  知识库 Markdown ({len(files_by_type['markdown'])}个)")
        for i, f in enumerate(files_by_type['markdown'][:3]):  # 只显示前 3 个
            rel_path = f.relative_to(DATA_DIR)
            print(f"   {index}. {rel_path}")
            all_files.append(f)
            index += 1

        if len(files_by_type['markdown']) > 3:
            print(f"   [输入 'm' 查看完整列表]")
            for f in files_by_type['markdown'][3:]:
                all_files.append(f)
                index += 1
        print()

    print("  特殊命令:")
    print("   b - 查看备份列表    r - 从备份恢复    0 - 退出")
    print()

    # 获取用户输入
    while True:
        user_input = input("请输入序号 (1-{}): ".format(len(all_files))).strip()

        if not user_input:
            continue

        # 特殊命令
        if user_input == '0':
            return None
        elif user_input == 'b':
            return 'SHOW_BACKUPS'
        elif user_input == 'r':
            return 'RESTORE_BACKUP'
        elif user_input == 'm':
            # 展开 Markdown 列表
            print("\n  完整 Markdown 列表:")
            start_idx = len(files_by_type['json']) + len(files_by_type['sqlite_db']) + len(files_by_type['encrypted_db']) + 1
            for i, f in enumerate(files_by_type['markdown']):
                rel_path = f.relative_to(DATA_DIR)
                print(f"   {start_idx + i}. {rel_path}")
            print()
            continue

        # 检查多个数字或非法字符
        if ' ' in user_input or ',' in user_input:
            print_error("仅支持编辑单个文件")
            continue

        # 尝试解析为数字
        try:
            choice = int(user_input)
            if 1 <= choice <= len(all_files):
                return all_files[choice - 1]
            else:
                print_error(f"序号越界 (1-{len(all_files)})")
        except ValueError:
            print_error("无效输入")


def show_backup_menu(backup_mgr: BackupManager, interactive: bool = True):
    """显示备份列表菜单"""
    backups = backup_mgr.list_backups(limit=20)

    if not backups:
        print_info("没有备份记录")
        return

    print("\n" + "="*60)
    print("  备份列表 (最近 20 个)")
    print("="*60 + "\n")

    for i, backup in enumerate(backups):
        print(f"  {i+1}. {Path(backup['original_path']).name}")
        print(f"     时间: {backup['timestamp'][:19]}")
        print(f"     文件: {backup['backup_file']} ({format_size(backup['size'])})")
        print()

    if not interactive:
        return

    print("  输入序号恢复, 0 返回")

    user_input = input("  选择: ").strip()

    if user_input == '0' or not user_input:
        return

    try:
        choice = int(user_input)
        if 1 <= choice <= len(backups):
            backup = backups[choice - 1]
            print_info(f"恢复: {backup['original_path']}")
            if backup_mgr.restore_backup(backup['backup_file']):
                input("\n按 Enter 返回...")
        else:
            print_error(f"序号越界 (1-{len(backups)})")
            input("\n按 Enter 返回...")
    except ValueError:
        print_error("无效输入")
        input("\n按 Enter 返回...")




def main():
    """主程序入口"""
    parser = argparse.ArgumentParser(description="ZincNya 数据编辑器")
    parser.add_argument('file', nargs='?', help='直接编辑指定文件')
    parser.add_argument('--list', action='store_true', help='列出所有可编辑文件')
    parser.add_argument('--backups', action='store_true', help='查看备份列表')
    parser.add_argument('--restore', nargs='?', const=True, help='从备份恢复')
    parser.add_argument('--editor', help='强制使用指定编辑器')
    parser.add_argument('--dry-run', action='store_true', help='预览模式（不实际写入）')
    parser.add_argument('--chat-id', help='加密数据库：仅导出指定聊天ID')
    parser.add_argument('--limit', type=int, default=100, help='加密数据库：限制导出条数（默认100）')

    args = parser.parse_args()

    # 初始化备份管理器
    backup_mgr = BackupManager(BACKUP_DIR)

    # 扫描文件
    files_by_type = scan_editable_files()

    # 处理命令行参数
    if args.list:
        print("可编辑文件列表:\n")
        for file_type, files in files_by_type.items():
            if files:
                print(f"{file_type}: {len(files)} 个")
                for f in files:
                    size = format_size(f.stat().st_size)
                    rel_path = f.relative_to(PROJECT_ROOT)
                    extra = ""
                    if file_type == 'sqlite_db':
                        count = get_db_record_count(f)
                        if count is not None:
                            extra = f", {count} 条"
                    elif file_type == 'encrypted_db':
                        extra = ", 需密钥"
                    print(f"  {rel_path} ({size}{extra})")
                print()
        return

    if args.backups:
        show_backup_menu(backup_mgr, interactive=False)
        return

    if args.restore:
        if isinstance(args.restore, str):
            # 直接恢复指定备份
            backup_path = Path(args.restore)
            if backup_mgr.restore_backup(backup_path.name):
                print_success("恢复成功")
        else:
            # 交互式选择
            show_backup_menu(backup_mgr, interactive=False)
        return

    if args.file:
        # 直接编辑指定文件
        file_path = Path(args.file)

        # 统一先解析为真实绝对路径（消解 .. 与符号链接），再做项目目录限制。
        # 注意：绝对路径同样必须 resolve，否则 D:\proj\..\..\secret 这类路径
        # 能在词法层通过 relative_to 检查，却在 OS 层逃逸到项目外（路径遍历）。
        if not file_path.is_absolute():
            file_path = PROJECT_ROOT / file_path
        file_path = file_path.resolve()

        # 安全检查：验证路径在项目目录下（防止路径遍历攻击）
        if file_path != PROJECT_ROOT and PROJECT_ROOT not in file_path.parents:
            print_error(f"文件路径必须在项目目录下: {PROJECT_ROOT}")
            return

        if not file_path.exists():
            print_error(f"文件不存在: {file_path}")
            return

        edit_file(file_path, backup_mgr, args.editor, args.dry_run)
        return

    # 交互式菜单
    while True:
        selected = show_main_menu(files_by_type)

        if selected is None:
            print_info("退出")
            break
        elif selected == 'SHOW_BACKUPS':
            show_backup_menu(backup_mgr)
        elif selected == 'RESTORE_BACKUP':
            show_backup_menu(backup_mgr)
        else:
            edit_file(selected, backup_mgr, args.editor, args.dry_run)
            input("\n按 Enter 返回菜单...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已中断")
        sys.exit(0)
    except Exception as e:
        print_error(f"程序异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
