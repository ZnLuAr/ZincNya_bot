# News API 模块测试文档
## 概述
## 测试目标
## 测试思路

### 1. isAlreadyPushed() 纯函数

**逻辑**：
- 检查 URL 是否在 `record["pushed_urls"]` 列表中
- 记录为空时返回 False

**测试点**：
- URL 在记录中返回 True
- URL 不在记录中返回 False
- 空记录返回 False

### 2. markAsPushed() 记录管理

**逻辑**：
- 将 URL 添加到 `record["pushed_urls"]`
- 重复添加不重复记录
- 超过 1000 条时截断（保留最新 1000 条）
- 记录中无 `pushed_urls` 时初始化

**测试点**：
- 标记新 URL
- 重复标记不重复添加
- 超过 1000 条时截断
- 记录中无 `pushed_urls` 时初始化

### 3. loadPushedRecord() 文件读取

**逻辑**：
- 从 `NEWS_DATA_FILE` 读取 JSON
- 文件不存在返回默认值 `{"pushed_urls": [], "last_check": None}`
- JSON 解析错误返回默认值

**测试点**：
- 正常加载 JSON 文件
- 文件不存在返回默认值
- JSON 解析错误返回默认值

### 4. savePushedRecord() 文件写入

**逻辑**：
- 添加 `last_check` 时间戳（ISO 8601 格式）
- 写入 `NEWS_DATA_FILE`（JSON 格式，缩进 2 空格）

**测试点**：
- 保存记录并添加时间戳
- 验证 JSON 格式

### 5. _getSession() 单例模式

**逻辑**：
- 全局变量 `_session` 初始为 None
- 首次调用创建 `aiohttp.ClientSession`
- 后续调用返回同一 Session
- Session 关闭后重新创建

**测试点**：
- 首次调用创建 Session
- 后续调用返回同一 Session
- Session 关闭后重新创建

### 6. _closeSession() 资源清理

**逻辑**：
- 调用 `_session.close()`
- 设置 `_session = None`
- Session 为 None 时不抛出异常

**测试点**：
- 关闭已打开的 Session
- Session 为 None 时不抛出异常

### 7. fetchLatestNews() HTML 解析

**逻辑**：
- 从 `NEWS_SOURCE_URL` 获取 HTML
