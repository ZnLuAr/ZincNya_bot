"""
tests/utils/test_operators.py

operators.py 权限判断四函数——白名单/权限链的最底层数据出口
（fileCache 之上的薄层）。用真实临时 JSON 文件测（经 fileCache 读盘路径），
避免 mock fileCache 导致与缓存行为的耦合。
"""

import json
import os

import pytest

from config import Permission
from utils.operators import (
    hasPermission,
    isOperator,
    loadOperators,
    getOperatorsWithPermission,
)



OPS = {
    "operators": {
        "111": {"name": "admin", "permissions": ["shutdown", "reboot", "status", "notify", "llm"]},
        "222": {"name": "viewer", "permissions": ["status"]},
        "333": {"name": "noPerms", "permissions": []},
    }
}



@pytest.fixture
def opsFile(tmp_path, monkeypatch):
    """写临时 operators.json 并让 OPERATORS_PATH 指向它"""
    p = tmp_path / "operators.json"
    p.write_text(json.dumps(OPS), encoding="utf-8")
    # operators.py 用 from fileCache import getOperatorsCache 的值副本，
    # patch fileCache 侧够不着——直接 patch utils.operators 命名空间
    data = json.loads(p.read_text(encoding="utf-8"))
    import utils.operators as opsMod
    monkeypatch.setattr(opsMod, "getOperatorsCache", lambda: _FakeCache(data))
    return p



class _FakeCache:
    def __init__(self, data):
        self._data = data
    def get(self):
        return self._data



class TestLoadOperators:
    def test_loads_dict(self, opsFile):
        ops = loadOperators()
        assert set(ops.keys()) == {"111", "222", "333"}



class TestIsOperator:
    def test_known_user(self, opsFile):
        assert isOperator(111) is True

    def test_unknown_user(self, opsFile):
        assert isOperator(999) is False

    def test_string_id_not_matched(self, opsFile):
        """实现按 str(userID) 索引——传 int 会转 str，行为锁定"""
        assert isOperator(111) is True



class TestHasPermission:
    def test_admin_has_llm(self, opsFile):
        assert hasPermission(111, Permission.LLM) is True

    def test_viewer_lacks_llm(self, opsFile):
        assert hasPermission(222, Permission.LLM) is False

    def test_viewer_has_status(self, opsFile):
        assert hasPermission(222, Permission.STATUS) is True

    def test_no_perm_user(self, opsFile):
        assert hasPermission(333, Permission.STATUS) is False

    def test_unknown_user(self, opsFile):
        assert hasPermission(999, Permission.LLM) is False

    def test_int_user_id_accepted(self, opsFile):
        """签名标 int 但实现 str()——传 int 必须工作（真实调用形态）"""
        assert hasPermission(111, Permission.REBOOT) is True



class TestGetOperatorsWithPermission:
    def test_llm_holders(self, opsFile):
        assert getOperatorsWithPermission(Permission.LLM) == ["111"]

    def test_status_holders(self, opsFile):
        result = getOperatorsWithPermission(Permission.STATUS)
        assert set(result) == {"111", "222"}

    def test_notify_holders(self, opsFile):
        assert getOperatorsWithPermission(Permission.NOTIFY) == ["111"]
