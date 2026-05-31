# Shutdown Handler 测试

## 测试统计

- **模块**: `handlers/shutdown.py`
- **测试数**: 17 个
- **覆盖率**: 100%

---

## 测试思路

### 核心功能

**shutdown() / restart()**：
- 权限检查（有权限 → 执行，无权限 → 拒绝）
- 调用 `getStateManager().requestShutdown/Restart()`
- 记录系统事件 + 回复确认消息

**status()**：
- 权限检查
- 运行时长计算（天/小时/分钟/秒，只显示非零单位）
- 资源监控（内存 MB、CPU %，通过 `psutil.Process()`）

**_mentionDispatch()**：
- 触发条件：消息包含 `@bot_username` + 关键词/别名
- 关键词映射：`关机`/`去睡觉`/`睡觉`/`休眠` → shutdown；`重启`/`重新启动` → restart；`运行状态`/`状态` → status
- 匹配成功 → 调用 handler → 抛出 `ApplicationHandlerStop` 停止 handler 链

---

## 关键测试技术

### 1. Mock 权限检查

```python
with patch('handlers.shutdown.hasPermission') as mockPerm:
    mockPerm.return_value = True  # 或 False
    await shutdown(mockUpdate, mockContext)
```

### 2. Mock StateManager

```python
with patch('handlers.shutdown.getStateManager') as mockState:
    mockMgr = MagicMock()
    mockState.return_value = mockMgr
    await shutdown(mockUpdate, mockContext)
    mockMgr.requestShutdown.assert_called_once()
```

### 3. Mock psutil（资源监控）

```python
with patch('handlers.shutdown.psutil.Process') as mockProc:
    mockProc.return_value.memory_info.return_value.rss = 100 * 1024 * 1024  # 100 MB
    mockProc.return_value.cpu_percent.return_value = 25.5
    await status(mockUpdate, mockContext)
```

### 4. 验证 ApplicationHandlerStop 传播

```python
with patch('handlers.shutdown.shutdown', new_callable=AsyncMock) as mockShutdown:
    mockShutdown.side_effect = ApplicationHandlerStop
    with pytest.raises(ApplicationHandlerStop):
        await _mentionDispatch(mockUpdate, mockContext)
```

---

## 测试陷阱

- **ApplicationHandlerStop 必须重抛**：`@handleTelegramErrors` 装饰器现在会检查并重抛此异常（修复了控制流被错误捕获的 bug）
- **时长格式化**：只显示非零单位（如 `1天2小时`，不显示 `0分0秒`）
- **关键词大小写不敏感**：`@bot 关机` 和 `@bot 關機` 都应匹配
- **别名匹配**：`去睡觉` 应触发 shutdown，不是独立命令
- **CPU 占用率需异步**：`cpu_percent()` 阻塞，需用 `asyncio.to_thread`
