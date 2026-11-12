"""
tests/utils/llm/operation/test_parser.py

测试操作块的整批解析：parseBatch 接受什么、拒绝什么，以及拒绝时给模型的原因。
"""

import json

import pytest

from config import LLM_BATCH_MAX_OPERATIONS

from utils.llm.operation.parser import BatchFormatError, ParsedBatch, parseBatch




_OPEN = "<OPERATIONS>"
_CLOSE = "</OPERATIONS>"

# 嵌套层数远超解释器的递归上限（3.11 约 1000 层）
_DEEP_NESTING = 100_000




def _wrap(body: str) -> str:
    """把块内文本包进一对 <OPERATIONS> 标记。"""
    return f"{_OPEN}\n{body}\n{_CLOSE}"


def _body(operations, end=True) -> str:
    """块内的 JSON 文本。"""
    return json.dumps({"operations": operations, "end": end}, ensure_ascii=False)


def _batch(operations, end=True) -> str:
    """一次格式合法的模型输出。"""
    return _wrap(_body(operations, end))


def _messages(count: int) -> list[dict]:
    return [{"op": "message", "text": f"第 {index} 条"} for index in range(1, count + 1)]


def _rejectReason(output, **kwargs) -> str:
    """断言 parseBatch 拒绝这次输出，返回给模型看的原因。"""
    with pytest.raises(BatchFormatError) as excInfo:
        parseBatch(output, **kwargs)
    return str(excInfo.value)




# ===========================================================================
# 合法的操作块
# ===========================================================================

def test_parseBatchReturnsOperationsInOrder():
    output = _batch(
        [
            {"op": "tool", "name": "weather", "city": "上海"},
            {"op": "message", "text": "查到了"},
        ],
        end=False,
    )

    batch = parseBatch(output)

    # op 只用来定位能力，不留在参数里
    assert batch == ParsedBatch(
        operations=(
            ("tool", {"name": "weather", "city": "上海"}),
            ("message", {"text": "查到了"}),
        ),
        end=False,
    )


def test_parseBatchStripsWhitespaceAroundOpName():
    batch = parseBatch(_batch([{"op": "  message\n", "text": "你好"}]))

    assert batch.operations == (("message", {"text": "你好"}),)


def test_parseBatchAllowsOperationWithoutArgs():
    batch = parseBatch(_batch([{"op": "ping"}]))

    assert batch.operations == (("ping", {}),)


def test_parseBatchIgnoresTextOutsideBlock():
    output = f"先想一下怎么回答。\n{_batch([{'op': 'message', 'text': '你好'}])}\n以上是我的操作。"

    batch = parseBatch(output)

    # 块外的文字既不报错，也不变成发言
    assert batch == ParsedBatch(operations=(("message", {"text": "你好"}),), end=True)


def test_parseBatchIgnoresExtraTopLevelKeys():
    body = json.dumps({
        "thought": "先打个招呼",
        "operations": [{"op": "message", "text": "你好"}],
        "end": True,
        "version": 2,
    }, ensure_ascii=False)

    batch = parseBatch(_wrap(body))

    assert batch == ParsedBatch(operations=(("message", {"text": "你好"}),), end=True)


@pytest.mark.parametrize("body", [
    '{"operations": [], "end": true}',
    '{"end": true}',
])
def test_parseBatchAcceptsEmptyBatchThatEnds(body):
    """不做任何事并结束是合法输出；operations 缺失按空数组处理。"""
    assert parseBatch(_wrap(body)) == ParsedBatch(operations=(), end=True)


def test_parseBatchTreatsMissingEndAsFalse():
    batch = parseBatch(_wrap('{"operations": [{"op": "message", "text": "你好"}]}'))

    assert batch.end is False
    assert len(batch.operations) == 1




# ===========================================================================
# 代码围栏
# ===========================================================================

@pytest.mark.parametrize("template", [
    "```json\nBODY\n```",
    "```\nBODY\n```",
    "```JSON BODY```",
    "\n\n```json\nBODY\n```\n\n",
])
def test_parseBatchAcceptsCodeFencedJson(template):
    fenced = template.replace("BODY", _body([{"op": "message", "text": "你好"}]))

    batch = parseBatch(f"{_OPEN}{fenced}{_CLOSE}")

    assert batch == ParsedBatch(operations=(("message", {"text": "你好"}),), end=True)


def test_parseBatchAcceptsFenceAroundWholeBlock():
    """围栏包在标记外面时属于块外文字，直接忽略。"""
    output = f"```\n{_batch([{'op': 'message', 'text': '你好'}])}\n```"

    assert parseBatch(output).operations == (("message", {"text": "你好"}),)


def test_parseBatchStripsOnlyOutermostFence():
    body = _body([{"op": "message", "text": "你好"}])

    reason = _rejectReason(_wrap(f"```json\n```json\n{body}\n```\n```"))

    assert "不是合法 JSON" in reason


def test_parseBatchRejectsUnclosedFence():
    body = _body([{"op": "message", "text": "你好"}])

    reason = _rejectReason(_wrap(f"```json\n{body}"))

    assert "不是合法 JSON" in reason




# ===========================================================================
# 标记缺失、重复或错位
# ===========================================================================

@pytest.mark.parametrize("output", [
    "",
    None,
    "今天天气不错喵",
    '{"operations": [], "end": true}',
    '<operations>{"operations": [], "end": true}</operations>',
])
def test_parseBatchRejectsMissingBlock(output):
    """没有标记的输出一律拒绝：裸 JSON 不算，标记也区分大小写。"""
    assert _rejectReason(output) == f"没有找到 {_OPEN} 块"


def test_parseBatchRejectsMultipleBlocks():
    output = _batch([{"op": "message", "text": "第一块"}]) + "\n" + _batch([], end=True)

    reason = _rejectReason(output)

    assert "必须恰好出现一次" in reason
    assert "起始标记 2 个，结束标记 2 个" in reason


@pytest.mark.parametrize("output, counts", [
    (f'{_OPEN}\n{{"operations": [{{"op": "message", "text": "说到一半', "起始标记 1 个，结束标记 0 个"),
    (f'{{"operations": [], "end": true}}\n{_CLOSE}', "起始标记 0 个，结束标记 1 个"),
    (f'{_OPEN}{_OPEN}{{"operations": [], "end": true}}{_CLOSE}', "起始标记 2 个，结束标记 1 个"),
])
def test_parseBatchRejectsUnbalancedTags(output, counts):
    reason = _rejectReason(output)

    assert counts in reason
    assert "输出可能被截断" in reason


def test_parseBatchRejectsCloseTagBeforeOpenTag():
    reason = _rejectReason(f'{_CLOSE}{{"operations": [], "end": true}}{_OPEN}')

    assert reason == f"{_CLOSE} 出现在 {_OPEN} 之前"


@pytest.mark.parametrize("tag, counts", [
    (_OPEN, "起始标记 2 个，结束标记 1 个"),
    (_CLOSE, "起始标记 1 个，结束标记 2 个"),
])
def test_parseBatchRejectsTagLiteralInsideMessageText(tag, counts):
    """标记按纯文本计数：发言正文里出现标记字面量，整批会被当作标记重复而拒绝。"""
    reason = _rejectReason(_batch([{"op": "message", "text": f"标记写作 {tag}"}]))

    assert counts in reason




# ===========================================================================
# 块内 JSON 的结构
# ===========================================================================

@pytest.mark.parametrize("body", [
    "",
    '{"operations": [], "end": true,}',
    "{'operations': [], 'end': true}",
    '{"operations": [',
    "operations: [], end: true",
    # 字符串里直接出现换行符（没有写成 \n 转义）
    '{"operations": [{"op": "message", "text": "第一行\n第二行"}], "end": true}',
])
def test_parseBatchRejectsInvalidJson(body):
    reason = _rejectReason(_wrap(body))

    assert reason.startswith(f"{_OPEN} 块内不是合法 JSON：")


def test_parseBatchKeepsJsonErrorAsCause():
    with pytest.raises(BatchFormatError) as excInfo:
        parseBatch(_wrap('{"operations": [], "end": true,}'))

    assert isinstance(excInfo.value.__cause__, json.JSONDecodeError)
    # 解析器给出的位置信息要带给模型
    assert str(excInfo.value.__cause__) in str(excInfo.value)


@pytest.mark.parametrize("body", ["[]", '"message"', "123", "null", "true"])
def test_parseBatchRejectsNonObjectTopLevel(body):
    assert _rejectReason(_wrap(body)) == f"{_OPEN} 块内必须是一个 JSON 对象"


@pytest.mark.parametrize("end", ["true", "false", 1, 0, None, []])
def test_parseBatchRejectsNonBooleanEnd(end):
    reason = _rejectReason(_batch([{"op": "message", "text": "你好"}], end=end))

    assert reason == "end 必须是 true 或 false"


@pytest.mark.parametrize("operations", [
    {"op": "message", "text": "你好"},
    "message",
    None,
    3,
])
def test_parseBatchRejectsNonListOperations(operations):
    assert _rejectReason(_batch(operations)) == "operations 必须是数组"


@pytest.mark.parametrize("body", [
    '{"operations": [], "end": false}',
    '{"operations": []}',
    "{}",
])
def test_parseBatchRejectsEmptyBatchThatDoesNotEnd(body):
    """既没有操作也不结束的输出没有意义，退回模型重写。"""
    reason = _rejectReason(_wrap(body))

    assert "operations 为空且 end 不是 true" in reason


def test_batchFormatErrorIsValueError():
    assert issubclass(BatchFormatError, ValueError)


def test_parseBatchRejectsDeeplyNestedJson():
    """嵌套过深时 json.loads 抛的是 RecursionError，同样按格式不符退回。"""
    nested = "[" * _DEEP_NESTING + "]" * _DEEP_NESTING
    body = '{"operations": [{"op": "message", "text": ' + nested + '}], "end": true}'

    reason = _rejectReason(_wrap(body))

    assert "嵌套层数过多" in reason




# ===========================================================================
# 操作个数上限
# ===========================================================================

def test_parseBatchAcceptsExactlyMaxOperations():
    batch = parseBatch(_batch(_messages(LLM_BATCH_MAX_OPERATIONS)))

    assert len(batch.operations) == LLM_BATCH_MAX_OPERATIONS


def test_parseBatchRejectsMoreThanMaxOperations():
    count = LLM_BATCH_MAX_OPERATIONS + 1

    reason = _rejectReason(_batch(_messages(count)))

    assert reason == f"一次最多 {LLM_BATCH_MAX_OPERATIONS} 个操作，这次有 {count} 个"


def test_parseBatchHonorsCustomMaxOperations():
    assert len(parseBatch(_batch(_messages(2)), maxOperations=2).operations) == 2

    reason = _rejectReason(_batch(_messages(3)), maxOperations=2)

    assert reason == "一次最多 2 个操作，这次有 3 个"


def test_parseBatchChecksCountBeforeEachOperation():
    """超过上限时不再逐个检查操作，原因只说个数。"""
    operations = ["不是对象"] * (LLM_BATCH_MAX_OPERATIONS + 1)

    assert "一次最多" in _rejectReason(_batch(operations))




# ===========================================================================
# 单个操作的格式
# ===========================================================================

@pytest.mark.parametrize("item", ["message", ["message", "你好"], None, 7])
def test_parseBatchRejectsNonObjectOperation(item):
    operations = [{"op": "message", "text": "你好"}, item]

    assert _rejectReason(_batch(operations)) == "第 2 个操作必须是 JSON 对象"


def test_parseBatchRejectsOperationWithoutOp():
    operations = [{"op": "message", "text": "你好"}, {"text": "忘了写 op"}]

    assert _rejectReason(_batch(operations)) == "第 2 个操作的 op 字段必须是非空字符串"


@pytest.mark.parametrize("name", ["", "   ", "\n"])
def test_parseBatchRejectsBlankOp(name):
    assert _rejectReason(_batch([{"op": name, "text": "你好"}])) == "第 1 个操作的 op 字段必须是非空字符串"


@pytest.mark.parametrize("name", [123, None, True, ["message"], {"name": "message"}])
def test_parseBatchRejectsNonStringOp(name):
    reason = _rejectReason(_batch([{"op": name, "text": "你好"}]))

    # 只要求指出是第几个操作的 op 有问题，不绑定具体措辞
    assert "第 1 个操作" in reason
    assert "op" in reason


def test_parseBatchRejectsWholeBatchOnOneBadOperation():
    """前面的操作合法也不返回部分结果。"""
    operations = [*_messages(2), {"text": "缺 op"}]

    assert _rejectReason(_batch(operations)).startswith("第 3 个操作")
