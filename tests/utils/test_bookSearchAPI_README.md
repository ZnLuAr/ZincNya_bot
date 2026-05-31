# Book Search API 模块测试文档
## 概述
## 测试目标
## 测试思路

### 1. 纯函数测试（无副作用）

**_languageCodeToName(code: str) -> str**
- 验证已知语言代码的映射（ISO 639-2/3 → 可读名称）
- 验证未知代码的降级处理（返回大写形式）
- 验证空输入的边界处理
- 验证大小写不敏感

**getCoverUrl(coverId: Optional[int], size: str = "M") -> Optional[str]**
- 验证 URL 生成格式（`https://covers.openlibrary.org/b/id/{coverId}-{size}.jpg`）
- 验证不同尺寸参数（S/M/L）
- 验证空输入边界（None/0）

### 2. 异步 API 函数测试（网络交互）

**searchBooks(query: str, page: int = 1, limit: int = 5) -> dict**
- **正常流程**：验证 API 200 响应的数据解析
  - Work ID 提取（"/works/OL123W" → "OL123W"）
  - 语言代码转换（"eng" → "English"）
  - 分页计算（totalPages = (total + limit - 1) // limit）
- **错误处理**：验证各类异常的降级处理
  - 空查询返回空结果
  - API 422 状态码（查询太短）
  - API 500 状态码（服务器错误）
  - 网络超时（asyncio.TimeoutError）
  - 网络错误（aiohttp.ClientError）
- **边界情况**：验证参数校验和数据清洗
  - limit 上限钳制（BOOK_MAX_ITEMS_PER_PAGE）
  - page 下限钳制（最小为 1）
  - 缺少 key 字段的文档跳过
  - total=0 时 totalPages=0

**getBookDetail(workId: str) -> Optional[dict]**
- **正常流程**：验证详情数据的解析和格式化
  - description 字段类型处理（string/dict）
  - description 截断（超过 BOOK_DESCRIPTION_MAX_LENGTH）
  - covers 列表提取第一个元素
  - subjects 限制为前 5 个
- **错误处理**：验证异常时返回 None
  - 空 workId
  - API 404 状态码
  - 网络异常
- **边界情况**：验证缺失字段的默认值
  - description 为空时返回 "暂无简介"
  - covers 为空时 coverId 为 None
  - 所有字段缺失时使用默认值

---

## 关键测试技术

### 1. Mock aiohttp ClientSession

```python
mock_session = MagicMock()
mock_session.get = MagicMock(return_value=mock_response)

with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
    result = await searchBooks("test")
```

### 2. Mock async context manager

```python
mock_response = MagicMock()
mock_response.status = 200
mock_response.json = AsyncMock(return_value={"data": "..."})
mock_response.__aenter__ = AsyncMock(return_value=mock_response)
mock_response.__aexit__ = AsyncMock(return_value=None)
```

### 3. Mock 异常

```python
mock_session.get = MagicMock(side_effect=asyncio.TimeoutError())
