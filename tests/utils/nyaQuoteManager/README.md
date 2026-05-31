# Nya Quote Manager 模块测试文档
## 概述
## test_data.py
### 测试目标

`utils/nyaQuoteManager/data.py` - 语录数据的加载、保存、CRUD 操作和权重随机选择

### 测试思路

1. **权重随机选择算法**：验证单条/多条消息的权重选择逻辑
   - 基础权重提取（float/list 格式兼容）
   - 多消息格式（"|||" 分隔）+ 条件概率链
   - 衰减因子计算（DECAY_FACTOR = 0.64）
   - 消息中的 "\\n" 转义处理

2. **CRUD 操作**：add/delete/set/list 的正确性
   - 权重格式转换（float/list）
   - 无效索引/payload 边界处理
   - 未知操作类型返回 False

3. **边界情况**：空语录库、空权重列表、无效参数

### 覆盖面（21 个测试）

#### getRandomQuote() - 11 个测试
- 空语录库返回空列表
- 单条语录选择
- 多条语录的权重选择
- 多消息格式（无条件概率）— 全部发送
- 多消息格式（带条件概率）— 根据概率决定
- 条件概率链中断（一旦某条不发送，后续也不发送）
- 衰减因子（未指定权重时的指数衰减）
- "\\n" 转义处理（\\n → \n）
- list 格式权重提取（用于 random.choices）
- 空 weight 列表边界（使用默认权重 1.0）

#### userOperation() - 10 个测试
- add 操作（基本/list 权重/默认权重）
- add 操作无效 payload（缺少 text/非 dict）
- delete 操作（基本/无效索引/缺少索引）
- set 操作（完整更新/部分更新/无效参数）
- list 操作（返回所有语录）
- 未知操作返回 False

### 测试目标

`utils/nyaQuoteManager/ui.py` - ViewModel 构建、编辑器交互和权重序列化

### 测试思路

1. **权重提取**：_extractBaseWeight() 兼容 float/list 格式
2. **ViewModel 构建**：collectQuoteViewModel() 的排序、截断、边界处理
   - 空语录库返回仅含 (+) 行的列表
   - 多条语录按权重升序排序
   - preview 截断（15 字符）
   - selectedIndex 边界钳制
3. **编辑器交互**：editQuoteViaEditor() 的序列化/反序列化
   - weight 序列化为逗号分隔字符串
   - weight 反序列化为 float/list
   - "\\n" 转义处理（\n → \\n）
   - 用户取消编辑返回 None
   - 临时文件清理

### 覆盖面（19 个测试）

#### _extractBaseWeight() - 3 个测试
- float 类型直接返回
- list 类型返回第一个元素
- 空 list 返回 1.0

#### collectQuoteViewModel() - 7 个测试
- 空语录库返回仅含 (+) 行
- 单条语录
- 多条语录按权重升序排序
- preview 截断（15 字符 + "..."）
- preview 不截断（短文本）
- selectedIndex 边界处理（超出范围钳制/负数钳制/None 默认）
- list 格式权重提取

#### editQuoteViaEditor() - 9 个测试
