"""
utils/llm/review/

LLM 输出的人工审核。回复与记忆操作两类审核项，Telegram 按钮、chatScreen、
独立控制台三个审核入口共用这里的实现，各入口只负责自己的交互。

    - items：审核项的创建与存取（控制台队列、Telegram 的 bot_data）
    - cards：Telegram 审核卡的文字与键盘
    - console：控制台 / chatScreen 的展示文字，重试产生的记忆操作入队
    - actions：批准 / 重试 / 补充反馈重试 / 取消 / 编辑

按子模块导入（如 from utils.llm.review.actions import reviewSend）。本文件不汇总导出：
actions 依赖任务入口，任务入口经发言、记忆能力依赖 items 与 cards，在这里一并导入
会形成循环。
"""
