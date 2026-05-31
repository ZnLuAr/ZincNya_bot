# Reaction Handler 测试

## 测试统计

- **模块**: `handlers/reaction.py`
- **测试数**: 12 个
- **覆盖率**: 100%

---

## 测试思路

### handleReaction() 核心逻辑

**边界条件**：
- 无 reaction 对象 / 无 user → 直接返回

**Emoji 差异计算**（集合差运算）：
- 添加新 emoji → 保存到 chatHistory
- 只移除 emoji → 不保存（addedEmojis 为空）
- 同时添加和移除 → 只保存新增的
- 多个新增 emoji → 循环调用 saveMessage

**用户名优先级**：
- username > first_name > str(user.id)

**None 处理**：
- old_reaction / new_reaction 为 None → 视为空集合

---

## 关键测试技术

### 1. Mock Telegram 对象层级

```python
mock_update.message_reaction.user.username
mock_update.message_reaction.chat.id
mock_update.message_reaction.old_reaction[0].emoji
```

### 2. AsyncMock saveMessage

```python
with patch('handlers.reaction.saveMessage', new_callable=AsyncMock) as mockSave:
    await handleReaction(mockUpdate, mockContext)
    mockSave.assert_called_once_with("123", "reaction", "alice", "回应了 ❤")
```

### 3. 验证多次调用

```python
assert mockSave.call_count == 2
calls = [c[0][3] for c in mockSave.call_args_list]  # 提取第 4 个参数
assert "回应了 ❤" in calls
assert "回应了 😂" in calls
```

---

## 测试陷阱

- **集合无序**：多个新增 emoji 的保存顺序不确定，需用 `in` 检查而非精确匹配
- **None 处理**：old_reaction/new_reaction 可能为 None，需转为空集合
- **用户名回退**：测试三种情况（有 username / 只有 first_name / 都无）
