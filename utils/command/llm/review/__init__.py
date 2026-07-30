"""
utils/command/llm/review/

LLM 审核的两套交互入口：
    - consoleReview：/llm review 控制台审核（同步阻塞式）
    - chatScreenReview：chatScreen 内 :ra/:re/:rr/:rf/:rc/:rq（两阶段异步）
两者共享底层 utils.llm.review 原语，仅交互层不同。
"""
