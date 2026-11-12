"""
utils/llm/

LLM 集成模块：
    - config: 配置管理（开关、模式、模型、视觉模型、提示词、记忆自动批准）
    - state: 运行时状态容器（审核队列、速率限制、防抖消息缓冲、one-shot context）
    - client: 多模型 LLM 客户端（按模型前缀路由至各 provider，双调用视觉架构）
    - memory: structured memory 存储、检索与 LLM 自主操作
    - knowledge: 知识库（Markdown 文件管理、BM25 检索、上下文注入）
    - contextBuilder: 统一上下文组装（背景侧）
    - models: 消息、任务请求、收集结果和审核展示的共享数据对象
    - messagePrep: Telegram 正文、引用与身份提取
    - trigger: 群聊触发判断（私聊 / @entity / 关键词）
    - vision: 图片提取（photo/document/reply）与下载编码
    - operation: 操作循环（整批解析校验、按序处理、结果回传、续跑或结束）
    - capability: LLM 自带的能力（发言、记忆操作），说明、校验与送出
    - task: 任务入口，首次生成与审核重试共用
    - review: 人工审核（审核项、Telegram 审核卡、控制台展示、审核动作）
"""

from .config import (
    getLLMEnabled,
    setLLMEnabled,
    getAutoMode,
    setAutoMode,
    getModel,
    setModel,
    getVisionModel,
    setVisionModel,
    getForceFallbackPrompt,
    setForceFallbackPrompt,
    getGroupTriggerMode,
    setGroupTriggerMode,
    getGroupTriggerKeywords,
    setGroupTriggerKeywords,
    addGroupTriggerKeyword,
    removeGroupTriggerKeyword,
    getMemoryEnabled,
    setMemoryEnabled,
    getMemoryAutoApprove,
    setMemoryAutoApprove,
    getMemoryRetrievalMode,
    setMemoryRetrievalMode,
    getKnowledgeEnabled,
    setKnowledgeEnabled,
    getKnowledgeMaxResults,
    setKnowledgeMaxResults,
    getKnowledgeMinScore,
    setKnowledgeMinScore,
    loadPrompts,
)
from .state import (
    getReviewQueue,
    isRateLimited,
    addRateLimit,
    appendPendingMessage,
    popPendingMessages,
    getPendingTask,
    setPendingTask,
    clearPendingTask,
    setContextOnce,
    consumeContextOnce,
    isContextOnceSet,
    makeDebounceKey,
)
from .memory import (
    addMemory,
    getMemoryByID,
    getMemories,
    getMemoryCounts,
    getMemoryCandidates,
    getMemorySnapshots,
    getEnabledContextualMemoryPage,
    updateMemory,
    deleteMemory,
    retrieveMemories,
    buildMemoryContextBlock,
    summarizeRetrievedMemories,
    MEMORY_SCOPE_GLOBAL,
    MEMORY_SCOPE_CHAT,
    MEMORY_SCOPE_USER,
    MEMORY_SCOPE_SESSION,
    MEMORY_MODE_CONTEXTUAL,
    MEMORY_MODE_PINNED,
    MemoryTurn,
    MemoryQuery,
    MemoryRetrievalResult,
    MemoryWriteGuard,
    loadCalibratedThresholds,
    buildSemanticQueryPlan,
    retrieveMemoryContext,
)
from .contextBuilder import (
    buildConversationContext,
    buildStructuredMemoryContext,
    buildHistoryContext,
    buildKnowledgeContext,
)
from .knowledge import (
    initDatabase as initKnowledgeDB,
    retrieveKnowledge,
    getKnowledgeEntries,
    getKnowledgeStats,
    reindexKnowledgeBase,
)
from .client import requestReply
from .models import CollectedOutput, MessageInput, TaskRequest
from .task import collectOutput
from .promptSafety import neutralizePromptDelimiters
