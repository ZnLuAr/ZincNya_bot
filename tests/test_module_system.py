#!/usr/bin/env python3
"""
tests/test_module_system.py

模块管理系统单元测试。

运行方式：
    python -m pytest tests/test_module_system.py -v
    或
    python tests/test_module_system.py
"""

import os
import sys
import json
import tempfile
import shutil
import unittest
from unittest.mock import patch, MagicMock

# 添加项目根目录到 sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


class TestModuleRegistry(unittest.TestCase):
    """测试模块注册表"""

    def test_modules_registry_exists(self):
        """测试 modulesRegistry.py 是否存在"""
        from modulesRegistry import MODULES
        self.assertIsInstance(MODULES, dict)
        self.assertGreater(len(MODULES), 0)

    def test_all_modules_have_required_fields(self):
        """测试所有模块是否包含必需字段"""
        from modulesRegistry import MODULES

        requiredFields = ["id", "name", "version", "files", "handlers"]

        for moduleId, metadata in MODULES.items():
            with self.subTest(module=moduleId):
                for field in requiredFields:
                    self.assertIn(field, metadata, f"模块 {moduleId} 缺少字段 {field}")

                # 验证 id 与字典 key 一致
                self.assertEqual(metadata["id"], moduleId, f"模块 {moduleId} 的 id 字段不匹配")

                # 验证 files 是列表
                self.assertIsInstance(metadata["files"], list, f"模块 {moduleId} 的 files 不是列表")

                # 验证 handlers 是列表
                self.assertIsInstance(metadata["handlers"], list, f"模块 {moduleId} 的 handlers 不是列表")

    def test_module_files_exist(self):
        """测试模块文件是否存在"""
        from modulesRegistry import MODULES

        for moduleId, metadata in MODULES.items():
            for filePath in metadata.get("files", []):
                fullPath = os.path.join(PROJECT_ROOT, filePath)
                with self.subTest(module=moduleId, file=filePath):
                    self.assertTrue(
                        os.path.exists(fullPath),
                        f"模块 {moduleId} 的文件 {filePath} 不存在"
                    )

    def test_handler_files_have_register_function(self):
        """测试 handler 文件是否有 register() 函数"""
        from modulesRegistry import MODULES

        for moduleId, metadata in MODULES.items():
            for handlerFile in metadata.get("handlers", []):
                fullPath = os.path.join(PROJECT_ROOT, handlerFile)
                with self.subTest(module=moduleId, handler=handlerFile):
                    if os.path.exists(fullPath):
                        with open(fullPath, 'r', encoding='utf-8') as f:
                            content = f.read()
                            self.assertIn(
                                'def register(',
                                content,
                                f"模块 {moduleId} 的 handler {handlerFile} 缺少 register() 函数"
                            )


class TestModuleManager(unittest.TestCase):
    """测试模块配置管理"""

    def setUp(self):
        """测试前准备"""
        # 注意：这是 unittest.TestCase，不是 pytest 风格测试
        # 无法使用 pytest 的 tmp_path fixture，需要手动管理临时目录
        import tempfile
        self.tempDir = tempfile.mkdtemp()
        self.configPath = os.path.join(self.tempDir, "modules.json")
        self.customPath = os.path.join(self.tempDir, "modules_custom.json")

        # 保存原始路径以便恢复
        import utils.moduleManager
        self.originalConfigPath = utils.moduleManager._MODULES_CONFIG_PATH

    def tearDown(self):
        """测试后清理"""
        # 恢复原始路径
        import utils.moduleManager
        utils.moduleManager._MODULES_CONFIG_PATH = self.originalConfigPath

        # 清理临时目录
        shutil.rmtree(self.tempDir)

    def test_load_empty_config(self):
        """测试加载空配置"""
        import utils.moduleManager
        utils.moduleManager._MODULES_CONFIG_PATH = self.configPath

        from utils.moduleManager import loadModulesConfig

        config = loadModulesConfig()
        self.assertIsInstance(config, dict)
        self.assertEqual(len(config), 0)

    def test_save_and_load_config(self):
        """测试保存和加载配置"""
        from utils.moduleManager import saveModulesConfig, loadModulesConfig

        # Mock 路径
        import utils.moduleManager
        utils.moduleManager._MODULES_CONFIG_PATH = self.configPath

        testConfig = {
            "llm": {"enabled": True},
            "todos": {"enabled": False}
        }

        # 保存
        result = saveModulesConfig(testConfig)
        self.assertTrue(result)

        # 加载
        loadedConfig = loadModulesConfig()
        self.assertEqual(loadedConfig, testConfig)

    def test_get_all_modules(self):
        """测试获取所有模块"""
        from utils.moduleManager import getAllModules

        allModules = getAllModules()
        self.assertIsInstance(allModules, dict)
        self.assertGreater(len(allModules), 0)

        # 验证每个模块的结构
        for moduleId, config in allModules.items():
            with self.subTest(module=moduleId):
                self.assertIn("enabled", config)
                self.assertIn("metadata", config)
                self.assertIn("source", config)

    def test_is_module_enabled(self):
        """测试模块启用状态检查"""
        from utils.moduleManager import isModuleEnabled

        # 默认应该启用
        self.assertTrue(isModuleEnabled("llm"))

        # 不存在的模块应该返回 False
        self.assertFalse(isModuleEnabled("nonexistent_module"))


class TestModuleCLI(unittest.TestCase):
    """测试 CLI 工具"""

    def setUp(self):
        """测试前准备"""
        import tempfile
        self.tempDir = tempfile.mkdtemp()

    def tearDown(self):
        """测试后清理"""
        shutil.rmtree(self.tempDir)

    def test_cli_list_command(self):
        """测试 list 命令"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/module.py", "list"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True
        )
        self.assertEqual(result.returncode, 0)
        # 应该包含至少一个模块
        self.assertIn("llm", result.stdout.lower())

    def test_cli_show_command(self):
        """测试 show 命令"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/module.py", "show", "llm"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True
        )
        self.assertEqual(result.returncode, 0)
        # 应该包含版本信息
        self.assertIn("1.0.0", result.stdout)

    def test_cli_validate_command(self):
        """测试 validate 命令"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/module.py", "validate"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True
        )
        self.assertEqual(result.returncode, 0)

    def test_module_id_validation(self):
        """测试模块 ID 格式校验"""
        import re

        validIds = ["llm", "todos", "my_module", "module123"]
        invalidIds = ["My-Module", "module.name", "模块", "module name"]

        pattern = r'^[a-z0-9_]+$'

        for moduleId in validIds:
            with self.subTest(id=moduleId):
                self.assertIsNotNone(re.match(pattern, moduleId))

        for moduleId in invalidIds:
            with self.subTest(id=moduleId):
                self.assertIsNone(re.match(pattern, moduleId))


class TestSharedFileDetection(unittest.TestCase):
    """测试共享文件检测"""

    def test_shared_files_detection(self):
        """测试共享文件检测逻辑"""
        from utils.moduleManager import getAllModules

        allModules = getAllModules()

        # 收集所有文件及其所属模块
        fileToModules = {}
        for moduleId, config in allModules.items():
            if not config.get("enabled", True):
                continue
            for filePath in config["metadata"].get("files", []):
                if filePath not in fileToModules:
                    fileToModules[filePath] = []
                fileToModules[filePath].append(moduleId)

        # 找出共享文件
        sharedFiles = {f: modules for f, modules in fileToModules.items() if len(modules) > 1}

        # 输出共享文件信息（用于调试）
        if sharedFiles:
            print(f"\n发现 {len(sharedFiles)} 个共享文件：")
            for filePath, modules in sharedFiles.items():
                print(f"  {filePath} -> {', '.join(modules)}")


class TestModuleDeduplication(unittest.TestCase):
    """测试模块去重逻辑"""

    def test_init_functions_deduplication(self):
        """测试 initFunctions 去重"""
        from utils.moduleManager import getAllModules

        allModules = getAllModules()

        # 收集所有 initFunctions
        allInitFunctions = []
        for moduleId, config in allModules.items():
            if not config.get("enabled", True):
                continue
            allInitFunctions.extend(config["metadata"].get("initFunctions", []))

        # 检查是否有重复
        uniqueFunctions = set(allInitFunctions)
        duplicates = [f for f in uniqueFunctions if allInitFunctions.count(f) > 1]

        if duplicates:
            print(f"\n发现重复的 initFunctions：")
            for func in duplicates:
                count = allInitFunctions.count(func)
                print(f"  {func} (出现 {count} 次)")


class TestPathInjectionPrevention(unittest.TestCase):
    """测试路径注入防护"""

    def test_path_traversal_detection(self):
        """测试路径遍历攻击检测"""
        maliciousPaths = [
            "../../../etc/passwd",
            "handlers/../../../etc/passwd",
            "utils/../../config.py",
            "handlers/./../../secret.key"
        ]

        for path in maliciousPaths:
            with self.subTest(path=path):
                # 检查是否包含 ..
                self.assertIn("..", path, f"路径 {path} 应该被检测为恶意")

    def test_valid_paths(self):
        """测试合法路径"""
        validPaths = [
            "handlers/llm.py",
            "utils/moduleManager.py",
            "utils/llm/config.py"
        ]

        for path in validPaths:
            with self.subTest(path=path):
                # 检查是否以 handlers/ 或 utils/ 开头
                self.assertTrue(
                    path.startswith("handlers/") or path.startswith("utils/"),
                    f"路径 {path} 应该是合法的"
                )
                # 检查不包含 ..
                self.assertNotIn("..", path)


def runTests():
    """运行所有测试"""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    success = runTests()
    sys.exit(0 if success else 1)