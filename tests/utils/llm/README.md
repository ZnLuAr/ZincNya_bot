# LLM 模块测试文档
## 概述
## test_config.py
### 测试目标

`utils/llm/config.py` - LLM 配置的加载、保存、验证与规范化

### 测试思路

1. **配置持久化**：JSON 文件的原子写（tmpfile + os.replace）
2. **默认值合并**：部分配置 + 默认值 → 完整配置
3. **取值校验**：记忆选择配置、autoMode、知识库参数的非法值拒绝
4. **字符串规范化**：group trigger keywords 的清洗
5. **集合操作**：add/remove/set 的去重与幂等性

### 覆盖面（36 个测试）

#### getMemorySelectorSettings() - 15 个测试
- 非法配置值抛出 ValueError（13 组参数）
- 默认值与显式 llm 后端
- messages 协议需显式选择，不修改传入的配置快照

#### loadLLMConfig() - 4 个测试
- 文件存在时加载
- 文件不存在返回默认值
- 无效 JSON 返回默认值
- 部分配置与默认值合并

#### saveLLMConfig() - 2 个测试
- 成功保存
- 原子写（tmpfile 被清理）

#### _normalizeGroupTriggerKeyword() - 5 个测试
- 基本规范化（trim + 转小写）
- 空关键词抛出异常
- 包含空白字符抛出异常
- 超长关键词（>32 字符）抛出异常
- 最大长度边界（32 字符）

#### setAutoMode() - 2 个测试
- 有效值（on/off/once）
- 无效值抛出异常

#### Group Trigger Keywords - 4 个测试
- setGroupTriggerKeywords 去重
- addGroupTriggerKeyword 添加
- addGroupTriggerKeyword 重复不添加
- removeGroupTriggerKeyword 移除

#### Knowledge 配置 - 4 个测试
- setKnowledgeMaxResults 有效值（1-10）
- setKnowledgeMaxResults 无效值抛出异常
- setKnowledgeMinScore 有效值（≥0）
- setKnowledgeMinScore 无效值抛出异常

### 测试目标

`utils/llm/contextBuilder.py` - LLM 对话上下文的组装与格式化

### 测试思路

1. **历史格式化**：时间戳 + 发送者 + 内容的统一格式
2. **块隔离**：UNTRUSTED_MEMORY / UNTRUSTED_HISTORY / TRUSTED_KNOWLEDGE 的安全标记
