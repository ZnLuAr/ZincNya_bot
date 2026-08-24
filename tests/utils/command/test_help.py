"""
tests/utils/command/test_help.py

/help 列表渲染的契约守卫：
    - 所有控制台命令（单文件 + 包命令）的 getHelp() 必须返回 dict 且含
      name / description 键——help.py 的排序与渲染直接下标访问这两个键，
      任一命令返回纯字符串会让整个 /help 列表崩溃
      （2026-08 的 killsticker 纯字符串返回即此事故）
    - 包命令（utils/command/llm/）也能被收集——漏扫会让 /help 缺条目
"""

import importlib
import os

import pytest

from config import COMMAND_DIR, COMMAND_MODULE



def _collectCommandModules():
    """镜像 help.py 的收集逻辑：单文件命令 + 包命令。"""
    modules = []
    for entry in os.listdir(COMMAND_DIR):
        if entry.startswith("__") or entry.endswith(".pyc"):
            continue
        if entry.endswith(".py"):
            name = entry[:-3]
        elif os.path.isdir(os.path.join(COMMAND_DIR, entry)):
            name = entry
        else:
            continue
        mod = importlib.import_module(f"{COMMAND_MODULE}.{name}")
        if hasattr(mod, "getHelp"):
            modules.append((name, mod))
    return modules



@pytest.fixture(scope="module")
def collectedHelpInfos():
    """收集全部命令的 getHelp() 返回（含包命令），列表渲染的前置数据。"""
    infos = [(name, mod.getHelp()) for name, mod in _collectCommandModules()]
    assert len(infos) >= 10, f"命令收集过少（{len(infos)}），扫描逻辑可能漏了包命令"
    return infos



class TestGetHelpContract:
    def test_all_return_dict(self, collectedHelpInfos):
        """所有 getHelp() 必须返回 dict——纯字符串会让 x["name"] 崩掉整个列表"""
        for name, info in collectedHelpInfos:
            assert isinstance(info, dict), f"{name}.getHelp() 返回了 {type(info).__name__}，应为 dict"

    def test_name_and_description_present(self, collectedHelpInfos):
        """渲染访问的 name / description 键必须存在"""
        for name, info in collectedHelpInfos:
            assert "name" in info, f"{name}.getHelp() 缺 name 键"
            assert "description" in info, f"{name}.getHelp() 缺 description 键"
            assert isinstance(info["name"], str)

    def test_names_sortable(self, collectedHelpInfos):
        """help.py 按 name 排序——name 需可比较"""
        names = [info["name"] for _, info in collectedHelpInfos]
        sorted(names)  # 不抛异常即可

    def test_package_command_collected(self, collectedHelpInfos):
        """包命令 llm/ 必须被收集（漏扫会让 /help 缺 /llm 条目）"""
        names = [info["name"] for _, info in collectedHelpInfos]
        assert any("llm" in n for n in names), f"/llm 未被收集，实际条目：{names}"

    def test_killsticker_is_dict(self):
        """事故回归：killsticker 曾返回纯字符串导致 /help 崩溃"""
        from utils.command import killsticker

        assert isinstance(killsticker.getHelp(), dict)
        assert killsticker.getHelp()["name"] == "/killsticker"