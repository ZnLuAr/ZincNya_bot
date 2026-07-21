"""
tests/scripts/test_afc_tools_paths.py

测试 afc_tools 的路径安全校验（_assertSafeToolPath）与
module.py 的模块路径校验（_assertSafeModulePath）。

这两个函数是 install/uninstall 防路径穿越的核心：
- 绝对路径会被 os.path.join 吞并 PROJECT_ROOT，必须拒绝
- ".." 段会经 normpath 穿越出工具目录，必须拒绝
- startswith 子串不可靠，要用 realpath + commonpath 判定
"""

import os
import sys

import pytest

# scripts/ 不在包路径里，手动加入以便 import
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import afc_tools  # noqa: E402
import module as module_script  # noqa: E402


class TestAssertSafeToolPath:
    """afc_tools._assertSafeToolPath：限定在 utils/afc/tools/<id>/ 内"""

    def test_legit_tool_file_passes(self):
        afc_tools._assertSafeToolPath(
            "timezone", "utils/afc/tools/timezone/__init__.py", afc_tools._TOOLS_DIR_REL
        )

    def test_dotdot_traversal_rejected(self):
        with pytest.raises(ValueError):
            afc_tools._assertSafeToolPath(
                "timezone", "utils/afc/tools/timezone/../../core/crypto.py", afc_tools._TOOLS_DIR_REL
            )

    def test_absolute_path_rejected(self):
        with pytest.raises(ValueError):
            afc_tools._assertSafeToolPath(
                "timezone", "C:/Windows/System32/evil", afc_tools._TOOLS_DIR_REL
            )

    def test_posix_absolute_rejected(self):
        with pytest.raises(ValueError):
            afc_tools._assertSafeToolPath(
                "timezone", "/etc/cron.d/evil", afc_tools._TOOLS_DIR_REL
            )

    def test_escapes_to_other_tool_rejected(self):
        """路径逃逸到别的工具目录（weather）也应拒绝"""
        with pytest.raises(ValueError):
            afc_tools._assertSafeToolPath(
                "timezone", "utils/afc/tools/weather/__init__.py", afc_tools._TOOLS_DIR_REL
            )

    def test_escapes_tool_subtree_rejected(self):
        """在 tools 下但不在本工具 id 子树内 → 拒绝"""
        with pytest.raises(ValueError):
            afc_tools._assertSafeToolPath(
                "timezone", "utils/afc/tools/evil.py", afc_tools._TOOLS_DIR_REL
            )

    def test_testfile_base(self):
        """testFile 走 tests/utils/afc/tools 基准，扁平 test_<id>.py，不约束 <id>/ 子目录"""
        afc_tools._assertSafeToolPath(
            "timezone", "tests/utils/afc/tools/test_timezone.py",
            afc_tools._TESTS_TOOLS_DIR_REL, requireSubdir=False,
        )
        # testFile 逃逸出 tests/ 目录 → 拒绝
        with pytest.raises(ValueError):
            afc_tools._assertSafeToolPath(
                "timezone", "tests/utils/afc/tools/../../../utils/afc/tools/weather/x.py",
                afc_tools._TESTS_TOOLS_DIR_REL, requireSubdir=False,
            )
        # testFile 含绝对路径 → 拒绝
        with pytest.raises(ValueError):
            afc_tools._assertSafeToolPath(
                "timezone", "C:/evil/test_x.py",
                afc_tools._TESTS_TOOLS_DIR_REL, requireSubdir=False,
            )


class TestAssertSafeModulePath:
    """module._assertSafeModulePath：保留 handlers//utils/ 前缀，叠加安全校验"""

    def test_legit_handlers_passes(self):
        module_script._assertSafeModulePath("handlers/llm.py")

    def test_legit_utils_passes(self):
        module_script._assertSafeModulePath("utils/llm/client.py")

    def test_dotdot_rejected(self):
        with pytest.raises(ValueError):
            module_script._assertSafeModulePath("utils/../config.py")

    def test_absolute_rejected(self):
        with pytest.raises(ValueError):
            module_script._assertSafeModulePath("C:/Windows/evil")
        with pytest.raises(ValueError):
            module_script._assertSafeModulePath("/etc/passwd")

    def test_no_prefix_rejected(self):
        with pytest.raises(ValueError):
            module_script._assertSafeModulePath("config.py")

    def test_fake_prefix_rejected(self):
        """utilsx/ 不是合法的 utils/ 前缀"""
        with pytest.raises(ValueError):
            module_script._assertSafeModulePath("utilsx/sneak.py")

    def test_nested_dotdot_rejected(self):
        with pytest.raises(ValueError):
            module_script._assertSafeModulePath("handlers/../../etc/passwd")
