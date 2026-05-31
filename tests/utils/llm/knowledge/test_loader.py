"""
tests/utils/llm/knowledge/test_loader.py

测试 utils/llm/knowledge/loader.py
"""

import pytest
from pathlib import Path
from utils.llm.knowledge.loader import _computeSourceHash, _parseMarkdownFile


# ============================================================================
# _computeSourceHash() 测试
# ============================================================================

def test_compute_source_hash():
    """计算 source_hash（SHA256 前 16 位）"""
    hash1 = _computeSourceHash("Title", "Content", ["tag1", "tag2"])
    assert len(hash1) == 16
    assert isinstance(hash1, str)


def test_compute_source_hash_deterministic():
    """相同输入产生相同 hash"""
    hash1 = _computeSourceHash("Title", "Content", ["tag1", "tag2"])
    hash2 = _computeSourceHash("Title", "Content", ["tag1", "tag2"])
    assert hash1 == hash2


def test_compute_source_hash_different_content():
    """不同内容产生不同 hash"""
    hash1 = _computeSourceHash("Title", "Content1", ["tag1"])
    hash2 = _computeSourceHash("Title", "Content2", ["tag1"])
    assert hash1 != hash2


def test_compute_source_hash_different_tags():
    """不同 tags 产生不同 hash"""
    hash1 = _computeSourceHash("Title", "Content", ["tag1"])
    hash2 = _computeSourceHash("Title", "Content", ["tag2"])
    assert hash1 != hash2


def test_compute_source_hash_tag_order_insensitive():
    """tags 顺序不影响 hash（内部排序）"""
    hash1 = _computeSourceHash("Title", "Content", ["tag1", "tag2"])
    hash2 = _computeSourceHash("Title", "Content", ["tag2", "tag1"])
    assert hash1 == hash2


# ============================================================================
# _parseMarkdownFile() 测试
# ============================================================================

def test_parse_markdown_valid(tmp_path):
    """解析有效的 Markdown 文件"""
    md_file = tmp_path / "test.md"
    md_file.write_text(
        """---
category: test
title: Test Entry
tags:
  - tag1
  - tag2
priority: 5
---

This is the content.
""",
        encoding="utf-8"
    )

    result = _parseMarkdownFile(str(md_file))
    assert result is not None
    assert result["category"] == "test"
    assert result["title"] == "Test Entry"
    assert result["content"] == "This is the content."
    assert result["tags"] == ["tag1", "tag2"]
    assert result["priority"] == 5
    assert len(result["source_hash"]) == 16


def test_parse_markdown_with_tags_expanded(tmp_path):
    """解析包含 tags_expanded 的文件"""
    md_file = tmp_path / "test.md"
    md_file.write_text(
        """---
category: test
title: Test Entry
tags:
  - tag1
tags_expanded:
  - tag2
  - tag3
---

Content here.
""",
        encoding="utf-8"
    )

    result = _parseMarkdownFile(str(md_file))
    assert result is not None
    # tags + tags_expanded 合并去重
    assert result["tags"] == ["tag1", "tag2", "tag3"]


def test_parse_markdown_duplicate_tags(tmp_path):
    """tags 和 tags_expanded 有重复时去重"""
    md_file = tmp_path / "test.md"
    md_file.write_text(
        """---
category: test
title: Test Entry
tags:
  - tag1
  - tag2
tags_expanded:
  - tag2
  - tag3
---

Content.
""",
        encoding="utf-8"
    )

    result = _parseMarkdownFile(str(md_file))
    assert result is not None
    # 去重保序：tag1, tag2, tag3
    assert result["tags"] == ["tag1", "tag2", "tag3"]


def test_parse_markdown_no_frontmatter(tmp_path):
    """没有 frontmatter 返回 None"""
    md_file = tmp_path / "test.md"
    md_file.write_text("Just plain text without frontmatter.", encoding="utf-8")

    result = _parseMarkdownFile(str(md_file))
    assert result is None


def test_parse_markdown_invalid_frontmatter(tmp_path):
    """无效的 YAML frontmatter 返回 None"""
    md_file = tmp_path / "test.md"
    md_file.write_text(
        """---
invalid: yaml: syntax: error
---

Content.
""",
        encoding="utf-8"
    )

    result = _parseMarkdownFile(str(md_file))
    assert result is None


def test_parse_markdown_missing_title(tmp_path):
    """缺少 title 返回 None"""
    md_file = tmp_path / "test.md"
    md_file.write_text(
        """---
category: test
---

Content without title.
""",
        encoding="utf-8"
    )

    result = _parseMarkdownFile(str(md_file))
    assert result is None


def test_parse_markdown_empty_content(tmp_path):
    """空 content 返回 None"""
    md_file = tmp_path / "test.md"
    md_file.write_text(
        """---
category: test
title: Test Entry
---

""",
        encoding="utf-8"
    )

    result = _parseMarkdownFile(str(md_file))
    assert result is None


def test_parse_markdown_default_values(tmp_path):
    """缺少可选字段时使用默认值"""
    md_file = tmp_path / "test.md"
    md_file.write_text(
        """---
title: Minimal Entry
---

Minimal content.
""",
        encoding="utf-8"
    )

    result = _parseMarkdownFile(str(md_file))
    assert result is not None
    assert result["category"] == "unknown"
    assert result["tags"] == []
    assert result["priority"] == 0


def test_parse_markdown_multiline_content(tmp_path):
    """多行内容正确解析"""
    md_file = tmp_path / "test.md"
    md_file.write_text(
        """---
title: Multiline Entry
---

Line 1
Line 2
Line 3
""",
        encoding="utf-8"
    )

    result = _parseMarkdownFile(str(md_file))
    assert result is not None
    assert result["content"] == "Line 1\nLine 2\nLine 3"


def test_parse_markdown_file_not_found():
    """文件不存在返回 None"""
    result = _parseMarkdownFile("/nonexistent/file.md")
    assert result is None


def test_parse_markdown_incomplete_frontmatter(tmp_path):
    """不完整的 frontmatter（只有一个 ---）返回 None"""
    md_file = tmp_path / "test.md"
    md_file.write_text(
        """---
title: Incomplete
Content without closing delimiter.
""",
        encoding="utf-8"
    )

    result = _parseMarkdownFile(str(md_file))
    assert result is None