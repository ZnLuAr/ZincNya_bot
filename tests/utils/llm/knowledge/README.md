# LLM Knowledge 模块测试文档
## 概述
## test_tokenizer.py
### 测试目标

`utils/llm/knowledge/tokenizer.py` - 中英混合分词与 BM25 评分算法

### 测试思路

1. **分词正确性**：验证中英文混合文本的分词结果
   - 英文：按空格/标点切分，转小写
   - 中文：1-gram + 2-gram 组合（"你好" → ["你", "好", "你好"]）
   - 混合：分别处理后合并

2. **边界情况**：空字符串、纯空格、特殊符号、emoji
3. **去重与顺序**：使用 `dict.fromkeys()` 保证去重且保序
4. **BM25 算法**：TF 饱和度、长度归一化、多查询词累加

### 覆盖面

- **tokenize()**: 13 个测试
  - 英文分词（基本、标点、大小写）
  - 中文分词（单字、多字、2-gram）
  - 混合分词
  - 数字与字母数字混合
  - 边界（空、空格、特殊符号、emoji）
  - 去重与顺序

- **_bm25()**: 8 个测试
  - 完全匹配、部分匹配、无匹配
  - 空查询、空文档
  - 词频提升（TF）
  - 长度归一化
  - 多查询词累加

- **computeAvgDocLen()**: 3 个测试
  - 正常计算、空列表、单文档

### 测试目标

`utils/llm/knowledge/loader.py` - Markdown 文档解析与 hash 计算

### 测试思路

1. **Hash 确定性**：相同输入产生相同 hash，不同输入产生不同 hash
2. **Frontmatter 解析**：YAML 格式正确性、必填字段验证
3. **Tag 合并**：`tags` + `tags_expanded` 去重合并
4. **错误处理**：无效 YAML、缺失字段、空内容

### 覆盖面

- **_computeSourceHash()**: 5 个测试
  - 确定性（相同输入 → 相同 hash）
  - 不同内容/tags → 不同 hash
  - Tag 顺序不敏感（内部排序）

- **_parseMarkdownFile()**: 11 个测试
  - 有效解析（完整 frontmatter + content）
  - Tags 合并（tags + tags_expanded）
  - 重复 tag 去重
  - 错误处理（无 frontmatter、无效 YAML、缺失 title、空 content）
  - 默认值（category="unknown", tags=[], priority=0）
  - 多行内容、文件不存在、不完整 frontmatter

## 关键测试技术

### 1. 纯函数测试
- 无副作用，直接断言输入输出
- 无需 mock，测试速度快

### 2. 临时文件隔离
```python
@pytest.fixture
def tmp_path():  # pytest 内置
    # 自动创建临时目录，测试结束后清理

### 3. 边界值测试
- 空输入、单元素、大量元素
- 特殊字符、Unicode、emoji
- 数值边界（0、负数、极大值）
