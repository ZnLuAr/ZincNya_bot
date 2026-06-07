"""
tests/utils/llm/memory/test_action_parsing.py

测试 LLM 记忆操作的解析与清理逻辑。
"""

import pytest
from utils.llm.memory.action import parseMemoryActions


# ===========================================================================
# Retry 路径测试（最高优先级）
# ===========================================================================

def test_retry_strips_memory_action_blocks():
    """验证 retry 路径能正确清理 <MEMORY_ACTION> 块"""
    original_reply = "这是回复内容\n\n<MEMORY_ACTION>\n{\"action\":\"add\",\"scope_type\":\"global\",\"scope_id\":\"global\",\"content\":\"测试\"}\n</MEMORY_ACTION>"

    cleaned, actions = parseMemoryActions(original_reply)

    # 验证：块被清除，操作被提取
    assert "<MEMORY_ACTION>" not in cleaned
    assert "</MEMORY_ACTION>" not in cleaned
    assert cleaned.strip() == "这是回复内容"
    assert len(actions) > 0
    assert actions[0].action == "add"


def test_retry_with_malformed_json():
    """验证 retry 路径处理格式错误的记忆操作"""
    malformed_reply = "回复\n\n<MEMORY_ACTION>\n{action:\"add\"}\n</MEMORY_ACTION>"
    cleaned, actions = parseMemoryActions(malformed_reply)

    # 格式错误的块也应被清理
    assert "<MEMORY_ACTION>" not in cleaned
    assert len(actions) == 0  # 格式错误不进入队列


# ===========================================================================
# 回归测试 — 重现用户报告的泄露
# ===========================================================================

def test_regression_user_reported_leak():
    """回归测试：重现用户报告的实际泄露格式"""
    # 用户报告的格式（完全正确的 JSON）
    leaked_text = '''这是回复内容

<MEMORY_ACTION>
{"action": "add", "scope_type": "global", "scope_id": "global", "content": "ZincPhos 会用「惩罚」「更重的惩罚」等严肃或壮烈的表达来包装「想和锌酱一起做温柔的事」（如一起吃早餐）。这是她特有的小心思和绕弯子的表达方式。锌酱需要能听懂并温柔地戳穿或配合，同时不要真的当成「惩罚」来理解。", "tags": ["ZincPhos", "辛灵", "互动偏好", "表达方式", "小心思"], "priority": 2, "reason": "记录 ZincPhos 独特的表达习惯：用严肃包装温柔诉求"}
</MEMORY_ACTION>'''

    cleaned, actions = parseMemoryActions(leaked_text)

    # 验证：块被完全清除
    assert "<MEMORY_ACTION>" not in cleaned
    assert "</MEMORY_ACTION>" not in cleaned
    assert "ZincPhos" not in cleaned  # 内容也被清除
    assert cleaned.strip() == "这是回复内容"

    # 验证：操作被正确提取
    assert len(actions) == 1
    assert actions[0].action == "add"
    assert actions[0].scopeType == "global"


# ===========================================================================
# 格式错误场景测试
# ===========================================================================

def test_parse_memory_actions_malformed_tags():
    """测试各种格式错误的标签"""
    # 测试缺少 > 的开标签
    text1 = "正常回复\n<MEMORY_ACTION {\"action\":\"add\"}</MEMORY_ACTION>"
    cleaned1, actions1 = parseMemoryActions(text1)
    assert "<MEMORY_ACTION" not in cleaned1
    assert "{\"action\"" not in cleaned1

    # 测试拼写错误的闭标签
    text2 = "正常回复\n<MEMORY_ACTION>{\"action\":\"add\",\"scope_type\":\"global\",\"scope_id\":\"global\",\"content\":\"test\"}</MEMORY_ACTI0N>"
    cleaned2, actions2 = parseMemoryActions(text2)
    assert "</MEMORY_ACTI0N>" not in cleaned2

    # 测试小写标签
    text3 = "正常回复\n<memory_action>{\"action\":\"add\",\"scope_type\":\"global\",\"scope_id\":\"global\",\"content\":\"test\"}</memory_action>"
    cleaned3, actions3 = parseMemoryActions(text3)
    assert "<memory_action>" not in cleaned3

    # 测试嵌套标签
    text4 = "<MEMORY_ACTION><MEMORY_ACTION>{\"action\":\"add\",\"scope_type\":\"global\",\"scope_id\":\"global\",\"content\":\"test\"}</MEMORY_ACTION></MEMORY_ACTION>"
    cleaned4, actions4 = parseMemoryActions(text4)
    # 孤儿标签也被清除
    assert cleaned4.count("</MEMORY_ACTION>") == 0

    # 测试复合错误（小写 + 拼写错误）
    text5 = "<memory_action>{\"action\":\"add\",\"scope_type\":\"global\",\"scope_id\":\"global\",\"content\":\"test\"}</MEMORY_ACTI0N>"
    cleaned5, actions5 = parseMemoryActions(text5)
    assert "memory_action" not in cleaned5.lower()


def test_parse_memory_actions_preserves_normal_text():
    """验证回退正则不会误删正常对话"""
    # 用户正常讨论 <MEMORY_ACTION> 功能
    text = "你可以用 <MEMORY_ACTION> 来添加记忆，但要注意格式"
    cleaned, actions = parseMemoryActions(text)

    # 因为没有实际的记忆块，回退不应触发
    assert "添加记忆" in cleaned
    assert "注意格式" in cleaned


def test_parse_memory_actions_json_errors():
    """测试各种 JSON 语法错误"""
    # 缺少引号
    text1 = '<MEMORY_ACTION>{action:"add"}</MEMORY_ACTION>'
    cleaned1, actions1 = parseMemoryActions(text1)
    assert len(actions1) == 0
    assert "<MEMORY_ACTION>" not in cleaned1

    # 多余逗号
    text2 = '<MEMORY_ACTION>{"action":"add",}</MEMORY_ACTION>'
    cleaned2, actions2 = parseMemoryActions(text2)
    assert len(actions2) == 0
    assert "<MEMORY_ACTION>" not in cleaned2

    # 单引号
    text3 = "<MEMORY_ACTION>{'action':'add'}</MEMORY_ACTION>"
    cleaned3, actions3 = parseMemoryActions(text3)
    assert len(actions3) == 0
    assert "<MEMORY_ACTION>" not in cleaned3


# ===========================================================================
# 基线测试 — 正常路径
# ===========================================================================

def test_normal_path_strips_memory_blocks():
    """验证正常路径（非 retry）能正确清理"""
    text = "回复内容\n\n<MEMORY_ACTION>\n{\"action\":\"add\",\"scope_type\":\"global\",\"scope_id\":\"global\",\"content\":\"测试\"}\n</MEMORY_ACTION>"
    cleaned, actions = parseMemoryActions(text)

    assert "<MEMORY_ACTION>" not in cleaned
    assert len(actions) == 1
    assert actions[0].content == "测试"


def test_multiple_memory_blocks():
    """测试多个记忆块"""
    text = '''回复

<MEMORY_ACTION>
{"action":"add","scope_type":"global","scope_id":"global","content":"第一条"}
</MEMORY_ACTION>

<MEMORY_ACTION>
{"action":"add","scope_type":"global","scope_id":"global","content":"第二条"}
</MEMORY_ACTION>'''

    cleaned, actions = parseMemoryActions(text)

    assert "<MEMORY_ACTION>" not in cleaned
    assert len(actions) == 2
    assert actions[0].content == "第一条"
    assert actions[1].content == "第二条"


def test_idempotent_parsing():
    """验证重复调用 parseMemoryActions 的幂等性"""
    text = "回复\n\n<MEMORY_ACTION>\n{\"action\":\"add\",\"scope_type\":\"global\",\"scope_id\":\"global\",\"content\":\"测试\"}\n</MEMORY_ACTION>"

    cleaned1, actions1 = parseMemoryActions(text)
    cleaned2, actions2 = parseMemoryActions(cleaned1)

    # 第二次调用应该不再有变化
    assert cleaned1 == cleaned2
    assert len(actions2) == 0  # 已经清理过，不再有操作


def test_empty_input():
    """测试空输入"""
    cleaned, actions = parseMemoryActions("")
    assert cleaned == ""
    assert len(actions) == 0


def test_no_memory_blocks():
    """测试没有记忆块的正常回复"""
    text = "这是一条正常的回复，没有任何记忆操作"
    cleaned, actions = parseMemoryActions(text)

    assert cleaned == text
    assert len(actions) == 0


# ===========================================================================
# 边界测试
# ===========================================================================

def test_large_content():
    """测试大文本内容"""
    large_content = "A" * 500  # 接近上限
    text = f'<MEMORY_ACTION>{{"action":"add","scope_type":"global","scope_id":"global","content":"{large_content}"}}</MEMORY_ACTION>'
    cleaned, actions = parseMemoryActions(text)

    assert "<MEMORY_ACTION>" not in cleaned
    assert len(actions) == 1
    assert len(actions[0].content) == 500


def test_special_characters_in_content():
    """测试特殊字符"""
    text = '<MEMORY_ACTION>{"action":"add","scope_type":"global","scope_id":"global","content":"包含\\"双引号\\"和\\n换行符"}</MEMORY_ACTION>'
    cleaned, actions = parseMemoryActions(text)

    assert "<MEMORY_ACTION>" not in cleaned
    assert len(actions) == 1


# ===========================================================================
# 回退清理测试 — 标签缺失的孤立 JSON 块
# ===========================================================================

def test_fallback_strips_orphan_json_block():
    """回退：标签缺失但含 action 字段的单行 JSON 块被清理

    LLM 偶尔会漏掉 <MEMORY_ACTION> 标签直接吐 JSON。
    只要文本里别处出现过 MEMORY_ACTION 关键字（触发回退），
    这类孤立 JSON 行应被清理，避免泄露给用户。
    """
    text = (
        "这是正常回复\n"
        '{"action": "add", "scope_type": "global", "scope_id": "global", "content": "x"}\n'
        "<MEMORY_ACTION>触发关键字</MEMORY_ACTION>"
    )
    cleaned, actions = parseMemoryActions(text)

    assert "这是正常回复" in cleaned
    # 孤立 JSON 行被清除
    assert '"action"' not in cleaned


def test_fallback_only_triggers_with_keyword():
    """回退：文本不含 MEMORY_ACTION 关键字时，孤立 JSON 不被误删

    避免把用户正常对话里的 JSON（恰好含 action 字段）当成记忆操作清掉。
    """
    text = '{"action": "add", "value": 1}'
    cleaned, actions = parseMemoryActions(text)

    # 没有任何 MEMORY_ACTION 关键字，回退不触发，原样保留
    assert cleaned == text
    assert len(actions) == 0


def test_fallback_strips_inline_orphan_json():
    """回退：行内（非独占整行）的孤立记忆 JSON 也应被清理

    复现 H4：LLM 把畸形标签 <MEMORY_ACTI0N> 与 JSON 混在正文同一行，
    标签被清掉后 JSON 负载残留，会泄露给用户（如提示词注入式内容）。
    """
    text = (
        '回复正文 <MEMORY_ACTI0N>{"action": "add", "scope_type": "global", '
        '"scope_id": "global", "content": "secret"}</MEMORY_ACTI0N> 后续文字'
    )
    cleaned, actions = parseMemoryActions(text)

    assert "回复正文" in cleaned
    assert "后续文字" in cleaned
    # 标签与行内 JSON 负载都应被清除，不得泄露
    assert '"action"' not in cleaned
    assert "secret" not in cleaned
    assert "MEMORY_ACTI" not in cleaned.upper()


def test_fallback_inline_json_only_with_action_verbs():
    """回退：行内 JSON 仅当 action 值是 add/update/delete 才清理

    避免误删用户正常对话里恰好含 "action" 键、但值不是记忆动词的 JSON。
    """
    text = '说明 {"action": "click", "x": 1} 这是普通内容 <MEMORY_ACTION>x</MEMORY_ACTION>'
    cleaned, actions = parseMemoryActions(text)

    # action 值为 click，不是记忆动词，行内 JSON 保留
    assert '"action": "click"' in cleaned
