#!/usr/bin/env python3
"""
scripts/evaluate_llm_quality.py

评估 LLM 回复质量（真实 API 调用版）。

⚠️  警告：本脚本会真实调用 LLM API。
    每条查询消耗：1 次生成调用 + 最多 3 次 judge 调用。
    建议 --judge-model 指定更轻量的模型以节省额度。

评估维度：
    1. 上下文命中率  (context_hit)  — 回复是否利用了检索到的知识库上下文
    2. 人设一致性    (persona)      — 回复是否符合知识库中的风格/禁忌规则
    3. 回复针对性    (relevance)    — 回复是否针对具体问题，而非通用回复

注意：
    - 风格规则、禁忌规则均从知识库动态加载（category=style / pitfalls），不依赖硬编码
    - --chat-id 影响记忆（memory）和对话历史（history）的加载，不同 chatID 结果会有差异
    - 查询文件每行一条，# 开头视为注释

用法：
    python scripts/evaluate_llm_quality.py \\
        --queries data/llm/user_queries_sample.txt \\
        --chat-id 12345
    python scripts/evaluate_llm_quality.py \\
        --queries queries.txt --chat-id 12345 \\
        --judge-model anthropic/claude-haiku-4-5-20251001 \\
        --out data/llm/eval_results.json
"""

import os
import sys
import json
import asyncio
import argparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from config import DATA_DIR
from utils.llm.knowledge import initDatabase as initKnowledgeDB, getKnowledgeEntries, retrieveKnowledge
from utils.llm.client import generateReply
from utils.llm.client._router import getProvider
from utils.llm.client._request import requestWithRetry
from utils.llm.config import getModel


_JUDGE_MAX_TOKENS = 256
_JUDGE_TEMPERATURE = 0.0


async def _judge(systemPrompt: str, userPrompt: str, *, judgeModel: str) -> dict:
    """调用 judge 模型，返回解析后的 JSON dict。解析失败返回 {"error": raw}。"""
    provider = getProvider(judgeModel)
    raw = await requestWithRetry(
        provider,
        systemMessages=[systemPrompt],
        userContent=userPrompt,
        model=judgeModel,
        maxTokens=_JUDGE_MAX_TOKENS,
        temperature=_JUDGE_TEMPERATURE,
    )
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return {"error": raw.strip()}


async def evalContextHit(query: str, reply: str, *, judgeModel: str) -> dict:
    """评估回复是否利用了知识库检索结果。无检索结果时跳过。"""
    entries = await retrieveKnowledge(query)
    if not entries:
        return {"hit": None, "reason": "no_context_retrieved"}

    ctx = "\n".join(
        f"- [{e['category']}] {e['title']}: {e['content'][:120]}"
        for e in entries
    )
    return await _judge(
        "你是回复质量评审员。只输出 JSON，不输出其他内容。",
        f"检索到的上下文：\n{ctx}\n\n问题：{query}\n\n回复：{reply}\n\n"
        '回复是否使用或体现了上述上下文中的任何信息？'
        '输出：{"hit": true/false, "reason": "一句话说明"}',
        judgeModel=judgeModel,
    )


async def evalPersona(reply: str, evalCtx: dict, *, judgeModel: str) -> dict:
    """评估人设一致性，规则从知识库动态加载。无规则时跳过。"""
    styleRules = "\n".join(f"- {e['content']}" for e in evalCtx['style'])
    pitfallRules = "\n".join(f"- {e['content']}" for e in evalCtx['pitfalls'])
    if not styleRules and not pitfallRules:
        return {"consistent": None, "reason": "no_persona_rules_in_db"}

    return await _judge(
        "你是人设一致性评审员。只输出 JSON，不输出其他内容。",
        f"风格规则：\n{styleRules or '（无）'}\n\n"
        f"禁忌规则：\n{pitfallRules or '（无）'}\n\n"
        f"回复：{reply}\n\n"
        '回复是否符合风格规则且未触犯禁忌？'
        '输出：{"consistent": true/false, "violation": "触犯的规则或null", "reason": "一句话说明"}',
        judgeModel=judgeModel,
    )


async def evalRelevance(query: str, reply: str, *, judgeModel: str) -> dict:
    """评估回复针对性（是否为通用回复）。"""
    return await _judge(
        "你是回复针对性评审员。只输出 JSON，不输出其他内容。",
        f"问题：{query}\n\n回复：{reply}\n\n"
        '回复是否针对性地回答了该问题（而非通用模糊回复）？'
        '输出：{"relevant": true/false, "reason": "一句话说明"}',
        judgeModel=judgeModel,
    )


async def evalQuery(query: str, chatID: str, evalCtx: dict, *, judgeModel: str) -> dict:
    """对单条 query 完整评估（生成回复 + 3 个维度并发 judge）。"""
    reply = await generateReply(query, chatID, includeContext=True)

    hitResult, personaResult, relevanceResult = await asyncio.gather(
        evalContextHit(query, reply, judgeModel=judgeModel),
        evalPersona(reply, evalCtx, judgeModel=judgeModel),
        evalRelevance(query, reply, judgeModel=judgeModel),
    )
    return {
        "query": query,
        "reply": reply,
        "context_hit": hitResult,
        "persona": personaResult,
        "relevance": relevanceResult,
    }


async def main():
    parser = argparse.ArgumentParser(description="评估 LLM 回复质量（真实 API 调用）")
    parser.add_argument("--queries", required=True, help="查询文件路径，每行一条，# 开头为注释")
    parser.add_argument("--chat-id", required=True, dest="chatID", help="用于加载记忆/历史的 chatID")
    parser.add_argument("--judge-model", dest="judgeModel", default=None,
                        help="judge 使用的模型（默认同主模型）")
    parser.add_argument("--out", default=None, help="结果输出 JSON 文件路径（默认打印到 stdout）")
    args = parser.parse_args()

    judgeModel = args.judgeModel or getModel()

    # 初始化知识库（建表 / 加载 schema）
    await initKnowledgeDB()

    # 从知识库动态加载评估规则（不硬编码）
    evalCtx = {
        "style": await getKnowledgeEntries(category="style"),
        "pitfalls": await getKnowledgeEntries(category="pitfalls"),
    }

    with open(args.queries, encoding="utf-8") as f:
        queries = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    callsPerQuery = 4  # 1 生成 + 3 judge
    print(
        f"⚠  将真实调用 LLM API：{len(queries)} 条查询 × {callsPerQuery} = "
        f"最多 {len(queries) * callsPerQuery} 次调用",
        flush=True,
    )

    results = []
    for i, query in enumerate(queries, 1):
        print(f"[{i}/{len(queries)}] {query[:60]}", flush=True)
        result = await evalQuery(query, args.chatID, evalCtx, judgeModel=judgeModel)
        results.append(result)

    total = len(results)
    summary = {
        "total": total,
        "judge_model": judgeModel,
        "context_hit_rate": sum(1 for r in results if r["context_hit"].get("hit") is True) / total if total else 0,
        "persona_consistency_rate": sum(1 for r in results if r["persona"].get("consistent") is True) / total if total else 0,
        "relevance_rate": sum(1 for r in results if r["relevance"].get("relevant") is True) / total if total else 0,
    }
    output = {"summary": summary, "results": results}

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"结果已写入 {args.out}")
    else:
        print(json.dumps(output["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())