"""
语录数据层

IO 操作与业务逻辑：ensure/load/save、CRUD、随机获取
"""

import os
import json
import random
from typing import List , Optional

from config import QUOTES_PATH




def ensureQuoteFile():
    if QUOTES_PATH:
        os.makedirs(os.path.dirname(QUOTES_PATH) , exist_ok=True)
    if not os.path.exists(QUOTES_PATH):
        with open(QUOTES_PATH , "w" , encoding="utf-8") as f:
            json.dump([] , f , ensure_ascii=False , indent=2)


def loadQuoteFile() -> List[dict]:
    from utils.core.fileCache import getQuotesCache
    ensureQuoteFile()
    cache = getQuotesCache()
    data = cache.get()

    if isinstance(data , list):
        return data
    return []


def saveQuoteFile(quotes: List[dict]):
    from utils.core.fileCache import getQuotesCache
    ensureQuoteFile()
    cache = getQuotesCache()
    cache.set(quotes)




def getRandomQuote() -> list[str]:
    """
    从 json (缓存) 中根据权重随机挑出语录返回

    支持两种 weight 格式：
        1. 传统格式：weight: float（单条消息）
        2. 新格式：weight: [float, float, ...]（多条消息 + 条件概率）

    新格式说明：
        - text 用 "|||" 分隔多条消息
        - weight[0] = P(这条语录被选中)
        - weight[1] = P(发送第二条 | 已发送第一条)
        - weight[n] = P(发送第n+1条 | 已发送第n条)

    返回：
        - 消息列表 List[str]，可能是 1 条或多条
        - 如果语录为空，返回空列表
    """

    quotes = loadQuoteFile()
    if not quotes:
        return []

    # 提取基础权重用于选择语录
    # 如果 weight 是列表，取第一个元素作为基础权重
    baseWeights = []
    for q in quotes:
        w = q.get("weight", 1.0)
        if isinstance(w, list):
            baseWeights.append(float(w[0]) if w else 1.0)
        else:
            baseWeights.append(float(w))

    # 根据基础权重随机选择一条语录
    picked = random.choices(quotes, weights=baseWeights, k=1)[0]

    text = picked.get("text", "")
    weight = picked.get("weight", 1.0)

    # 检查是否是多条消息格式
    if "|||" in text:
        # 分割消息
        parts = text.split("|||")
        messages = []

        # 第一条消息总是发送（因为已经被选中了）
        messages.append(parts[0].replace("\\n", "\n"))

        # 处理后续消息的条件概率
        if isinstance(weight, list) and len(weight) > 1:
            chainWeights = weight[1:]  # 条件概率列表

            # 衰减因子：未指定权重时，每多一条消息概率乘以此值
            DECAY_FACTOR = 0.64

            for i, part in enumerate(parts[1:], start=0):
                if i < len(chainWeights):
                    # 使用指定的权重
                    prob = chainWeights[i]
                else:
                    # 指数衰减：以最后一个指定权重为基准，逐步衰减
                    lastWeight = chainWeights[-1] if chainWeights else 0.8
                    decaySteps = i - len(chainWeights) + 1
                    prob = lastWeight * (DECAY_FACTOR ** decaySteps)

                # 根据条件概率决定是否发送这条消息
                if random.random() < prob:
                    messages.append(part.replace("\\n", "\n"))
                else:
                    # 一旦某条消息没有发送，后续的也不发送
                    break

        else:
            # 如果没有指定条件概率，则全部发送
            for part in parts[1:]:
                messages.append(part.replace("\\n", "\n"))

        return messages

    else:
        # 传统单条消息格式
        return [text.replace("\\n", "\n")]




def userOperation(operation: str , index:Optional[int]=None , payload:Optional[dict]=None):

    quotes = loadQuoteFile()

    match operation:
        case "add":
            if not isinstance(payload , dict) or "text" not in payload:
                return False
            quotes.append({
                "text": payload["text"],
                "weight": float(payload.get("weight" , 1.0)),
            })
            saveQuoteFile(quotes)
            return True

        case "delete":
            if index is None or not (0 <= index < len(quotes)):
                return False
            quotes.pop(index)
            saveQuoteFile(quotes)
            return True

        case "set":
            if index is None or not (0<= index < len(quotes)):
                return False
            if payload is None or not isinstance(payload, dict):
                return False
            quotes[index].update(payload)
            saveQuoteFile(quotes)
            return True

        case "list":
            return quotes

    return False
