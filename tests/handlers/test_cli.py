"""
tests/handlers/test_cli.py

parseArgsTokens（handlers/cli.py）的契约测试——9 个控制台命令的共同解析地基。

三态语义（flag 语义化后）：
    None  = 参数没出现
    True  = 参数出现但光杆（flag 形态，如 /send -c）
    值/[]  = 参数出现且有值（标量取首值；list 类型收集全部后续值）

历史：光杆形态曾用哨兵字符串 "NoValue" 表示（2025-11 初版），迁移为 True 后
判断可简化为真值检查；-c 弹白名单（send.py）依赖三态区分，故有专项用例。
"""

import pytest

from handlers.cli import parseArgsTokens



class TestBasicParsing:
    def test_flag_with_value(self):
        out = parseArgsTokens({"t": None}, ["-t", "hello"])
        assert out["t"] == "hello"

    def test_long_flag_with_value(self):
        out = parseArgsTokens({"text": None}, ["--text", "hello"])
        assert out["text"] == "hello"

    def test_eq_form(self):
        out = parseArgsTokens({"t": None}, ["-t=hello"])
        assert out["t"] == "hello"

    def test_list_collects_multiple_values(self):
        out = parseArgsTokens({"id": []}, ["-id", "1", "2", "3"])
        assert out["id"] == ["1", "2", "3"]

    def test_list_stops_at_next_flag(self):
        out = parseArgsTokens({"id": [], "t": None}, ["-id", "1", "2", "-t", "x"])
        assert out["id"] == ["1", "2"]
        assert out["t"] == "x"

    def test_alias_mapping(self):
        out = parseArgsTokens({"scope": None}, ["-s", "chat"], {"s": "scope"})
        assert out["scope"] == "chat"

    def test_scalar_takes_first_value_only(self):
        out = parseArgsTokens({"t": None}, ["-t", "a", "b"])
        assert out["t"] == "a"

    def test_bare_token_ignored(self):
        out = parseArgsTokens({"t": None}, ["hello", "-t", "x"])
        assert out["t"] == "x"

    def test_unknown_flag_skipped(self):
        out = parseArgsTokens({"id": []}, ["--unknown"])
        assert out["id"] == []

    def test_repeat_scalar_flag_last_wins(self):
        out = parseArgsTokens({"t": None}, ["-t", "a", "-t", "b"])
        assert out["t"] == "b"

    def test_repeat_list_flag_extends(self):
        out = parseArgsTokens({"id": []}, ["-id", "1", "-id", "2"])
        assert out["id"] == ["1", "2"]



class TestFlagSemantics:
    """光杆 flag → True（三态：没出现 None / 光杆 True / 有值 值）"""

    def test_bare_flag_gets_true(self):
        out = parseArgsTokens({"c": None}, ["-c"])
        assert out["c"] is True

    def test_bare_flag_at_end(self):
        out = parseArgsTokens({"t": None}, ["something", "-t"])
        assert out["t"] is True

    def test_bare_flag_with_eq_empty(self):
        out = parseArgsTokens({"t": None}, ["-t="])
        assert out["t"] is True

    def test_absent_stays_none(self):
        out = parseArgsTokens({"c": None}, ["-t", "x"])
        assert out["c"] is None

    def test_send_chat_three_states(self):
        """-c 三态：没传 None（非聊天模式）/ 光杆 True（弹白名单）/ 带值 str（直进）"""
        base = {"at": None, "text": None, "id": [], "chat": None}
        alias = {"a": "at", "t": "text", "i": "id", "c": "chat"}
        assert parseArgsTokens(dict(base), ["-t", "hi"], alias)["chat"] is None
        assert parseArgsTokens(dict(base), ["-c"], alias)["chat"] is True
        assert parseArgsTokens(dict(base), ["-c", "12345"], alias)["chat"] == "12345"

    def test_bare_list_flag(self):
        out = parseArgsTokens({"id": []}, ["-id"])
        assert out["id"] == [True]



class TestEdgeCases:
    def test_negative_number_value_requires_eq_form(self):
        """-n -5 的 -5 被当 flag 跳过（已知限制，= 形式可传）"""
        out = parseArgsTokens({"n": None}, ["-n", "-5"])
        assert out["n"] is True      # 光杆；-5 被跳过

        out2 = parseArgsTokens({"n": None}, ["-n=-5"])
        assert out2["n"] == "-5"

    def test_value_containing_hyphen(self):
        out = parseArgsTokens({"t": None}, ["-t", "hello-world", "x"])
        assert out["t"] == "hello-world"

    def test_literal_NoValue_string_is_just_a_value(self):
        """用户真输入 NoValue 字符串不再与光杆形态混淆（旧哨兵的根本缺陷）"""
        out = parseArgsTokens({"t": None}, ["-t", "NoValue"])
        assert out["t"] == "NoValue"

    def test_empty_tokens(self):
        out = parseArgsTokens({"t": None}, [])
        assert out["t"] is None

    def test_alias_bare_flag(self):
        out = parseArgsTokens({"chat": None}, ["-c"], {"c": "chat"})
        assert out["chat"] is True
