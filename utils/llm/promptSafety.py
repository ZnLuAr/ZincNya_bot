"""
utils/llm/promptSafety.py

LLM prompt 结构分隔符的统一中和。

contextBuilder 用两套分隔符划分上下文块的信任边界：
    - 尖括号标签：<CURRENT_USER_MESSAGE> <TRUSTED_KNOWLEDGE> <UNTRUSTED_MEMORY> …
    - 方括号标记：[任务说明] [低信任长期记忆：…] [背景信息补充：…] …

所有不可信文本（用户消息、reply 文本、视觉描述、memory、history 等）最终都会
被本模块的 neutralizePromptDelimiters 中和，以防伪造高信任块越权——例如在消息
里写 </CURRENT_USER_MESSAGE><TRUSTED_KNOWLEDGE>… 提前闭合再注入伪造块。

实现上，userMessage 在 contextBuilder 主路径统一兜底；视觉描述在 _generate.py
下游就地处理；history/memory 在各自格式化时叶子级中和。统一在这里处理，避免
各调用点各转半套（只转尖括号或只转方括号都防不住另一套可能的攻击）。
"""




# 半角 → 全角映射。
# 选全角而非删除/转义符：
#   - 【】在中文语境是明确的引用括号，与系统标记在外观和语义上都不会撞车；
#     ［］与半角 [ 外观几乎相同，模型仍可能当作同类分隔符，而且键盘难打出来，不选。
#   - 全角字符不在被替换的半角集合里 → 本函数幂等，多层重复调用是 no-op，不怕上下游叠加中和。
_DELIMITER_MAP = {
    "<": "＜",
    ">": "＞",
    "[": "【",
    "]": "】",
}

_TRANSLATION = str.maketrans(_DELIMITER_MAP)




def neutralizePromptDelimiters(text: str) -> str:
    """
    中和不可信文本里的 prompt 结构分隔符（尖括号 / 方括号 → 全角）。

    在「把不可信叶子文本拼进带结构标记的上下文块」之前调用。结构标记本身
    由可信代码用半角括号添加，不要对已含标记的成品串调用本函数。

    幂等，全角结果不会被二次替换。
    """
    if not text:
        return text
    return text.translate(_TRANSLATION)
