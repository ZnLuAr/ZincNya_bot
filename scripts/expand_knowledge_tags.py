#!/usr/bin/env python3
"""
scripts/expand_knowledge_tags.py

用 LLM 为知识库条目扩展语义标签。

用法：
    python scripts/expand_knowledge_tags.py            # 增量（默认）
    python scripts/expand_knowledge_tags.py --force    # 全量重扩展
    python scripts/expand_knowledge_tags.py --dry-run  # 只显示将要处理的文件
    python scripts/expand_knowledge_tags.py --model anthropic/claude-haiku-4-5  # 指定模型
"""

import os
import sys
import json
import hashlib
import argparse
import asyncio

# 添加项目根目录到 sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import yaml
from config import LLM_KNOWLEDGE_DIR
from utils.llm.client._request import requestWithRetry
from utils.llm.client._router import getProvider
from utils.llm.config import getModel


EXPAND_SYSTEM_PROMPT = """
你是一个语义标签扩展器。给定一个知识条目（标题 + 内容 + 核心标签），返回 30-40 个与该条目语义相关的中英文关键词。

要求：
- 你的输出会被装入一个列表（list），所以请只输出标签内容，不要任何解释文字
- 输出必须在【一行内】，用英文逗号 + 空格分隔，例如：python, rust, 编程语言, 代码, async
- 不要换行，不要项目符号（如 - 或 *），不要编号
- 不要输出 JSON 数组的方括号 []，也不要给标签加引号（系统会自动处理）
- 包含同义词、相关词、上位词、下位词
- 中英文混合，优先常用词
- 去重，不要重复核心标签

示例输入：
标题：编程语言偏好
内容：熟悉 Python（异步编程、Telegram Bot）和 Rust（所有权系统、零成本抽象）
核心标签：["Python", "Rust", "编程"]

示例输出（一行）：
python, rust, 编程语言, 代码, 写代码, 脚本, 异步, async, bot, telegram, 所有权, ownership, 系统编程, 开发
"""


class Color:
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    DIM = "\033[90m"
    RESET = "\033[0m"


def _enable_win_vt() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def supports_color() -> bool:
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        _enable_win_vt()
        return True
    return True


_USE_COLOR = supports_color()


def printWithColor(color: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return (f"{color}{text}{Color.RESET}")




def computeSourceHash(title: str, content: str, tags: list[str]) -> str:
    """计算 source_hash（与 loader.py 保持一致）"""
    combined = f"{title}\n{content}\n{','.join(sorted(tags))}"
    return hashlib.sha256(combined.encode('utf-8')).hexdigest()[:16]




def parseMarkdownFile(filepath: str) -> dict | None:
    """解析 Markdown 文件（含 frontmatter）"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            raw = f.read()

        if not raw.startswith('---'):
            return None

        parts = raw.split('---', 2)
        if len(parts) < 3:
            return None

        frontmatter = yaml.safe_load(parts[1])
        content = parts[2].strip()

        if not isinstance(frontmatter, dict):
            return None

        title = frontmatter.get('title', '')
        tags = frontmatter.get('tags', [])
        tagsExpanded = frontmatter.get('tags_expanded', [])

        if not title or not content:
            return None

        sourceHash = computeSourceHash(title, content, tags)

        return {
            "frontmatter": frontmatter,
            "content": content,
            "title": title,
            "tags": tags,
            "tags_expanded": tagsExpanded,
            "source_hash": sourceHash,
            "raw_parts": parts,  # 保留原始分段用于重写
        }

    except Exception as e:
        print(printWithColor(Color.RED, f"解析失败 {filepath}: {e}"))
        return None




async def expandTags(title: str, content: str, coreTags: list[str], model: str | None = None) -> list[str] | None:
    """调用 LLM 扩展 tags"""
    userPrompt = f"""标题：{title}
内容：{content}
核心标签：{json.dumps(coreTags, ensure_ascii=False)}

请在一行内用逗号分隔输出扩展标签："""

    try:
        modelToUse = model or getModel()
        provider = getProvider(modelToUse)
        response = await requestWithRetry(
            provider,
            systemMessages=[EXPAND_SYSTEM_PROMPT],
            userContent=userPrompt,
            model=modelToUse,
            temperature=0.6,
            maxTokens=512,
        )

        text = response.strip()
        # 去除可能的 markdown 代码块
        if text.startswith('```'):
            lines = text.split('\n')
            text = '\n'.join(lines[1:-1]) if len(lines) > 2 else text

        text = text.strip()

        # 尝试 JSON 数组解析（兼容旧格式）
        if text.startswith('['):
            try:
                expanded = json.loads(text)
                if isinstance(expanded, list):
                    expanded = list(dict.fromkeys(
                        t.strip().lower() for t in expanded if isinstance(t, str) and t.strip()
                    ))
                    return expanded
            except json.JSONDecodeError:
                pass

        # 逗号分隔格式（主路径）：去掉可能残留的 [] 和引号，按逗号切分
        text = text.strip('[]')
        raw_tags = text.split(',')
        expanded = []
        seen = set()
        for tag in raw_tags:
            tag = tag.strip().strip('"\'').strip().lower()
            if tag and tag not in seen:
                seen.add(tag)
                expanded.append(tag)

        if not expanded:
            print(printWithColor(Color.YELLOW, f"LLM 返回无法解析: {response[:200]}"))
            return None

        return expanded
    except Exception as e:
        print(printWithColor(Color.RED, f"LLM 调用失败：{e}"))
        return None




def writeMarkdownFile(filepath: str, parsed: dict, newTagsExpanded: list[str]):
    """原子写入 Markdown 文件（更新 tags_expanded）"""
    frontmatter = parsed["frontmatter"].copy()
    frontmatter["tags_expanded"] = newTagsExpanded

    # 自定义 dumper：list 用 flow style（[a, b, c]），其它保持 block style
    class _FlowListDumper(yaml.SafeDumper):
        pass

    def _list_flow_representer(dumper, data):
        return dumper.represent_sequence('tag:yaml.org,2002:seq', data, flow_style=True)

    _FlowListDumper.add_representer(list, _list_flow_representer)

    # 重建文件
    newFrontmatter = yaml.dump(
        frontmatter,
        Dumper=_FlowListDumper,
        allow_unicode=True,
        sort_keys=False,
        width=4096,  # 防止长 list 被强行折行
    )
    newContent = f"---\n{newFrontmatter}---\n\n{parsed['content']}"

    # 原子写入
    tmpPath = filepath + ".tmp"
    try:
        with open(tmpPath, 'w', encoding='utf-8') as f:
            f.write(newContent)
        os.replace(tmpPath, filepath)
    except Exception as e:
        if os.path.exists(tmpPath):
            os.remove(tmpPath)
        raise e




async def processFile(filepath: str, force: bool, dryRun: bool, model: str | None) -> dict:
    """处理单个文件"""
    filename = os.path.basename(filepath)
    parsed = parseMarkdownFile(filepath)

    if not parsed:
        printWithColor(Color.RED, f"[跳过] {filename} - 解析失败")
        return {"status": "skip", "reason": "解析失败"}

    # 检查是否需要扩展
    if not force and parsed["tags_expanded"]:
        print(printWithColor(Color.DIM, f"[跳过] {filename} - 已有 tags_expanded（{len(parsed['tags_expanded'])} 个），用 --force 强制重扩展"))
        return {"status": "skip", "reason": "已有 tags_expanded"}

    if dryRun:
        print(printWithColor(Color.YELLOW, f"[dry-run] {filename} - {parsed['title']}（待扩展）\n"))
        return {"status": "dry_run", "title": parsed["title"]}

    print(printWithColor(Color.RESET, f"[处理] {filename} - {parsed['title']} ..."))

    expanded = await expandTags(parsed["title"], parsed["content"], parsed["tags"], model)
    if not expanded:
        print(printWithColor(Color.RED, f"[失败] {filename} - LLM 扩展失败\n"))
        return {"status": "fail", "reason": "LLM 扩展失败"}

    try:
        writeMarkdownFile(filepath, parsed, expanded)
    except Exception as e:
        print(printWithColor(Color.RED, f"[失败] {filename} - 写入失败：{e}\n"))
        return {"status": "fail", "reason": f"写入失败: {e}"}

    print(printWithColor(Color.GREEN, f"[成功] {filename} - 扩展 {len(expanded)} 个标签\n"))
    return {"status": "success", "count": len(expanded)}




async def main():
    parser = argparse.ArgumentParser(description="用 LLM 扩展知识库标签")
    parser.add_argument("--force", action="store_true", help="强制重扩展所有文件")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要处理的文件")
    parser.add_argument("--model", type=str, help="指定 LLM 模型（如 anthropic/claude-haiku-4-5）")
    args = parser.parse_args()

    if not os.path.exists(LLM_KNOWLEDGE_DIR):
        print(printWithColor(Color.RED, f"知识库目录不存在：{LLM_KNOWLEDGE_DIR}"))
        return

    mdFiles = [f for f in os.listdir(LLM_KNOWLEDGE_DIR) if f.endswith('.md')]
    if not mdFiles:
        print(printWithColor(Color.RED, "未找到 .md 文件"))
        return

    print(printWithColor(Color.RESET, f"扫描到 {len(mdFiles)} 个文件"))
    if args.dry_run:
        print("Dry-run 模式")
    if args.force:
        print("强制重扩展模式")
    if args.model:
        print(f"使用模型：{args.model}")

    stats = {"success": 0, "skip": 0, "fail": 0, "dry_run": 0}

    for filename in mdFiles:
        filepath = os.path.join(LLM_KNOWLEDGE_DIR, filename)
        result = await processFile(filepath, args.force, args.dry_run, args.model)
        stats[result["status"]] += 1

    print("\n" + "="*50)
    print(printWithColor(Color.GREEN, f"成功：{stats['success']}"))
    print(printWithColor(Color.YELLOW, f"跳过：{stats['skip']}"))
    print(printWithColor(Color.RED, f"失败：{stats['fail']}"))
    if args.dry_run:
        print(printWithColor(Color.YELLOW, f"Dry-run：{stats['dry_run']}"))


if __name__ == "__main__":
    asyncio.run(main())