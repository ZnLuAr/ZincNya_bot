# Core 基础设施模块测试文档

## 测试统计

| 模块 | 测试数量 | 覆盖率 | 说明 |
|------|---------|--------|------|
| logger.py | 34 | 94% | 树状日志格式化、文件写入、UI/CLI 双通道 |
| errorHandler.py | 33 | 89% | 5 种错误源捕获、聚合计数、secret 脱敏 |
| database.py | 16 | 100% | WAL 模式、事务语义、并发写保护 |
| resourceManager.py | 19 | 100% | 单例模式、优先级清理、异常隔离 |
| **总计** | **102** | **94%** | **Phase 12 完成** |

---

## 1. logger.py 测试

### 测试思路
- 树状日志格式化（7 种 `LogChildType`）
- 日志注入防护（ANSI 转义、控制字符、换行符）
- 延迟写入启动信息（空启动不污染日志）
- UI/CLI 双通道输出（callback vs print）
- ERROR 级别双写到 errorHandler

### 关键技术
- 参数化测试覆盖 7 种 `LogChildType`
- Mock `getStateManager` 验证 UI/CLI 分支
- Mock `logError` 验证错误双写
- 文件 I/O 使用 `tmp_path` fixture

### 测试陷阱
- `_formatConsoleText` 返回带 ANSI 的字符串，需用正则匹配
- `_writeStartupHeader` 幂等性依赖 `_startupWritten` 标志
- ERROR 级别必须同时验证日志写入和 `logError` 调用

---

## 2. errorHandler.py 测试

### 测试思路
- 5 种错误源捕获（`sys.excepthook` / `sys.unraisablehook` / Telegram / asyncio / logging）
- 重复错误聚合计数（`errorType:message[:50]` 作为 key）
- 每日重置（`resetDailyCountsIfNeeded`）
- Secret redaction（traceback 中的 token 自动替换为 `[REDACTED]`）
- logging 拦截器（httpx / telegram 错误分类）

### 关键技术
- Mock 环境变量测试 `_buildSecretPattern`
- 使用真实异常对象验证 traceback 处理
- Mock `safePrint` 验证终端输出
- `monkeypatch.setenv` 注入测试 token

### 测试陷阱
- `_buildSecretPattern` 需在测试中重新构建（模块级变量）
- logging 拦截器需 `isinstance` 判断具体异常类型
- `getErrorKey` 截取前 50 字符，需验证边界

---

## 3. database.py 测试

### 测试思路
- 每次 `run()` 创建新连接（不做池化）
- WAL 模式 + busy_timeout=5000 + Row factory
- `initSchema` 幂等（`_initialized` 标志）
- 事务通过 `with conn:` 自动 commit/rollback
- `initSchema` 后自动注册 ResourceManager 清理回调（WAL checkpoint）

### 关键技术
- 使用真实 SQLite 文件（`tmp_path`）验证 WAL 模式
- 端到端 CRUD 操作验证事务语义
- Mock `getResourceManager` 验证清理回调注册
- PRAGMA 查询验证配置生效

### 测试陷阱
- WAL 模式需查询 `PRAGMA journal_mode` 验证
- 事务 rollback 需在异常后查询数据验证未提交
- 清理回调注册时机：`initSchema` 首次调用后

---

## 4. resourceManager.py 测试

### 测试思路
- 单例 double-checked locking（线程安全）
- `_resources` 是 `(priority, name, cleanupFunc)` 元组列表
- 清理按 priority 降序（数值越大越先）
- 异常静默吞掉（safePrint 输出，但继续清理其他资源）
- 支持 `getRegisteredResources()` 查询

### 关键技术
- 多线程测试验证单例线程安全（10 个线程并发创建）
- 使用 `AsyncMock` 验证清理顺序
- Mock `safePrint` 验证异常处理
- 记录 `call_order` 列表验证优先级

### 测试陷阱
- 清理顺序：priority 降序，相同 priority 按注册顺序
- 异常不中断清理流程，需验证所有清理函数都被调用
- 重复注册同名资源不去重（允许）
- 多次清理不移除资源（会重复清理）