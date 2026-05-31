# Whitelist Manager 模块测试文档
## 概述
## test_data.py
### 测试目标

`utils/whitelistManager/data.py` - 白名单数据的加载、保存、权限检查、CRUD 操作和 /start 命令处理

### 测试思路

1. **权限检查**：验证 allowed/suspended 状态的正确判断
   - 用户在 allowed 且不在 suspended 返回 True
   - 用户不在 allowed 或在 suspended 返回 False

2. **CRUD 操作**：add/delete/suspend/unsuspend/list/setComment 的正确性
   - 状态转换（allowed ↔ suspended）
   - 备注管理（allowed/suspended 用户均可设置）
   - 无效操作边界处理
   - 未知操作抛出 ValueError

3. **/start 鉴权与通知**：未授权用户触发时的通知机制
   - 冷却期检查（10 分钟内不重复通知）
   - 过期条目清理（语义上等同于"从未记录"）
   - 缓存上限兜底（_MAX_NOTIFY_CACHE = 4096）
   - 发送消息失败时的异常隔离

### 覆盖面（22 个测试）

#### whetherAuthorizedUser() - 3 个测试
- 用户在 allowed 且不在 suspended 返回 True
- 用户不在 allowed 返回 False
- 用户在 suspended 返回 False

#### userOperation() - 13 个测试
- addUser（基本/已存在）
- deleteUser（基本/不存在）
- suspendUser（基本/已暂停）
- unsuspendUser（基本/未暂停）
- listUsers（返回完整数据）
- setComment（allowed 用户/suspended 用户/不存在用户）
- 未知操作抛出 ValueError

#### handleStart() - 6 个测试
- 已授权用户（回复欢迎消息）
- 未授权用户（首次触发 → 通知 operator）
- 未授权用户（冷却期内 → 不通知）
- 未授权用户（清理过期条目）
- 未授权用户（缓存上限兜底）
- 未授权用户（发送消息失败 → 异常隔离）

### 测试目标

`utils/whitelistManager/ui.py` - 用户可用性检查和 ViewModel 构建

### 测试思路

1. **用户可用性检查**：checkChatAvailable() 的错误分类
   - 使用 send_chat_action 而非 get_chat（更准确）
   - Forbidden → 用户屏蔽了 Bot
   - BadRequest → 用户不存在
   - 其他异常 → Error

2. **ViewModel 构建**：collectWhitelistViewModel() 的状态映射
   - 并发检查用户可用性（asyncio.gather）
   - 状态映射（Allowed/Suspended/Forbidden/NotFound/Error）
   - 颜色编码（wheat1 = 可用，grey70 = 不可用）
   - selectedIndex 边界钳制

### 覆盖面（14 个测试）

#### checkChatAvailable() - 4 个测试
- 用户可用返回 True
- Forbidden 返回 ("Forbidden", exception)
- BadRequest 返回 ("NotFound", exception)
- 其他异常返回 ("Error", exception)

#### collectWhitelistViewModel() - 10 个测试
- 空白名单
- 包含 (+) 行（includeAddRow=True）
- Allowed 用户（状态映射 + 颜色）
- Suspended 用户（displayStatus = "Suspended"）
