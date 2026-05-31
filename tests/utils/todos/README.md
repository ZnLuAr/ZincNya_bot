# Todos 模块测试文档
## 概述
## test_database.py
### 测试目标

`utils/todos/database.py` - 待办事项的数据库持久化与查询

### 测试思路

1. **CRUD 完整性**：增删改查的基本操作
2. **数据隔离**：按 chatID 和 userID 隔离数据
3. **分页查询**：limit + offset 的正确性
4. **状态转换**：pending ↔ done 的双向转换与时间戳记录
5. **提醒查询**：按时间窗口筛选待提醒的待办
6. **聚合统计**：按用户统计 pending/done/overdue 数量
7. **SQL 注入防护**：参数化查询防止恶意输入

### 覆盖面（25 个测试）

#### _rowToTodoDict() - 2 个测试
- 将 sqlite3.Row 转换为字典
- 无提醒时间时 remind_time 为 None

#### addTodo() - 3 个测试
- 添加基本待办
- 添加带提醒时间的待办
- 添加失败时返回 None

#### getTodos() - 4 个测试
- 空列表
- 按状态筛选（pending/done/all）
- 分页查询（limit + offset）
- 按用户隔离

#### getTodoByID() - 2 个测试
- 根据 ID 获取待办
- ID 不存在返回 None

#### updateTodo() - 6 个测试
- 更新内容
- 更新优先级
- 标记为完成时记录 completed_at
- 重新打开时清除 completed_at
- 无更新内容时返回 True

#### deleteTodo() - 1 个测试
- 删除待办

#### markDone() / reopenTodo() - 2 个测试
- 标记为完成
- 重新打开待办（清除 reminded 标记）

#### getTodosCount() - 1 个测试
- 统计待办数量（pending/done/all）

#### getUsersTodosSummary() - 2 个测试
- 按用户聚合统计（pending/done/total/last_active）
- 统计过期待办（remind_time <= now）

#### getPendingReminders() - 2 个测试
- 获取需要提醒的待办（时间已到 + 未提醒 + pending）
- 提醒按时间升序排列

#### SQL 注入防护 - 1 个测试
- 恶意 SQL 被当作普通字符串存储

### 测试目标

`utils/todos/utils.py` - 时间解析、优先级解析与格式化工具

### 测试思路

1. **时间解析多样性**：英文简写（2h/30m/1d）+ 中文自然语言（明天/一小时后）
2. **优先级提取**：独立 token 识别（P0-P3/P_）
3. **格式化友好性**：今天/明天/具体日期的自动选择
4. **DoS 防护**：限制扫描前 100 字符，防止超长输入卡住

### 覆盖面（24 个测试）

#### parseTime() - 8 个测试
