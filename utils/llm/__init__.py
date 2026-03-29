"""
utils/llm/

LLM 集成模块：
    - config: 配置管理（开关、模式、提示词）
    - state: 运行时状态（审核队列、速率限制、防抖缓冲、one-shot context）
    - client: Anthropic API 封装
    - memory: structured memory 存储与检索
    - contextBuilder: 统一上下文组装
"""

from .config import (
    getLLMEnabled,
    setLLMEnabled,
    getAutoMode,
    setAutoMode,
    getModel,
    setModel,
    getMemoryEnabled,
    setMemoryEnabled,
    loadPrompts,
)
from .state import (
    getReviewQueue,
    addReviewItem,
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
    updateMemory,
    deleteMemory,
    retrieveMemories,
    buildMemoryContextBlock,
    summarizeRetrievedMemories,
    MEMORY_SCOPE_GLOBAL,
    MEMORY_SCOPE_CHAT,
    MEMORY_SCOPE_USER,
    MEMORY_SCOPE_SESSION,
)
from .contextBuilder import (
    buildConversationContext,
    buildStructuredMemoryContext,
    buildHistoryContext,
)
from .client import generateReply, requestReply
