"""
tests/utils/command/test_helpRender.py

子命令速查表渲染（utils/command/_helpRender.py）——列宽自适应与
尾部单换行约定（print 补行兜底空行）。
"""

from utils.command._helpRender import renderSubcommands



class TestRenderSubcommands:
    def test_header_then_two_column_rows(self):
        out = renderSubcommands("/x 可用的子命令有", {"add": "添加", "del <id>": "删除"})
        lines = out.split("\n")
        assert lines[0] == "/x 可用的子命令有："
        assert lines[1] == "  add       添加"
        assert lines[2] == "  del <id>  删除"     # 列宽按最长键自适应

    def test_trailing_single_newline(self):
        out = renderSubcommands("/x", {"a": "b"})
        assert out.endswith("\n") and not out.endswith("\n\n")

    def test_column_width_aligns_to_longest_key(self):
        out = renderSubcommands("/x", {"on": "开", "autoapprove": "自动批准"})
        first = out.split("\n")[1]
        second = out.split("\n")[2]
        # 两行描述起始列相同（按最长键 "autoapprove" 对齐）
        assert first.index("开") == second.index("自")
