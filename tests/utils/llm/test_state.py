"""
tests/utils/llm/test_state.py

测试 utils/llm/state.py 的防抖批次收集（collectDebouncedBatch）。
队列 item 契约（_formatReviewHint 等）的测试已随契约迁至 test_review.py。

注意：_pendingMessages / _contextOnce 是模块全局，用例用唯一 debounceKey，
结束后 pop 清理，避免污染其他用例。
"""

from utils.llm.state import (
    appendPendingMessage,
    collectDebouncedBatch,
    popPendingMessages,
    setContextOnce,
)




class TestCollectDebouncedBatch:
    def test_empty_buffer_returns_none(self):
        assert collectDebouncedBatch("test:none") is None

    def test_single_message_aggregation(self):
        key = "test:single"
        try:
            appendPendingMessage(key, "第一句", includeContext=False, images=[{"data": "x"}],
                                 urlIntentText="意图", urlCandidateText="候选")
            batch = collectDebouncedBatch(key)
            assert batch is not None
            assert batch.combinedText == "第一句"
            assert batch.includeContext is False
            assert batch.images == [{"data": "x"}]
            assert batch.urlIntentText == "意图"
            assert batch.urlCandidateText == "候选"
        finally:
            popPendingMessages(key)

    def test_multi_message_join_and_any_include_context(self):
        key = "test:multi"
        try:
            appendPendingMessage(key, "第一句", includeContext=False, images=[])
            appendPendingMessage(key, "第二句", includeContext=True, images=[{"data": "y"}])
            batch = collectDebouncedBatch(key)

            assert batch.combinedText == "第一句\n第二句"
            assert batch.includeContext is True          # 任一为 True 即 True
            assert batch.images == [{"data": "y"}]       # 跨消息拼接
        finally:
            popPendingMessages(key)

    def test_empty_strings_skipped_in_join(self):
        key = "test:empty"
        try:
            appendPendingMessage(key, "有内容", includeContext=False, images=[])
            appendPendingMessage(key, "", includeContext=False, images=[])
            batch = collectDebouncedBatch(key)

            assert batch.combinedText == "有内容"        # 空串不进 join
        finally:
            popPendingMessages(key)

    def test_context_once_consumed_or(self):
        """one-shot 标记参与或运算，且消费后即清除"""
        key = "test:once"
        try:
            appendPendingMessage(key, "内容", includeContext=False, images=[])
            setContextOnce()
            batch1 = collectDebouncedBatch(key)
            assert batch1.includeContext is True

            # 标记已被消费：再次收集（无标记消息）不再触发
            appendPendingMessage(key, "内容", includeContext=False, images=[])
            batch2 = collectDebouncedBatch(key)
            assert batch2.includeContext is False
        finally:
            popPendingMessages(key)

    def test_collect_pops_buffer(self):
        """收集即清空：第二次收集返回 None"""
        key = "test:pop"
        appendPendingMessage(key, "内容", includeContext=False, images=[])
        assert collectDebouncedBatch(key) is not None
        assert collectDebouncedBatch(key) is None


class TestCollectDebouncedBatchDisplayPairs:
    def test_pairs_aggregate_per_message(self):
        """displayPairs 逐条配对：有引用的消息紧邻一个引用块"""
        key = "test:dp1"
        try:
            appendPendingMessage(key, "第一句", replyLine="<@rep> 引用一", currentText="第一句", images=[])
            appendPendingMessage(key, "第二句", replyLine="", currentText="第二句", images=[])
            batch = collectDebouncedBatch(key)

            assert batch.displayPairs == [
                {"replyLine": "<@rep> 引用一", "currentText": "第一句"},
                {"replyLine": "", "currentText": "第二句"},
            ]
        finally:
            popPendingMessages(key)

    def test_pairs_empty_when_no_new_keys(self):
        """旧缓冲条目（无 replyLine/currentText 键）：displayPairs 为空"""
        key = "test:dp2"
        try:
            appendPendingMessage(key, "第一句", images=[])
            batch = collectDebouncedBatch(key)

            assert batch.displayPairs == []
        finally:
            popPendingMessages(key)
