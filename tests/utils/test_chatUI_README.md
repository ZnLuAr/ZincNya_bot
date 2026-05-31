# Chat UI 模块测试文档
## 概述
## 测试目标
## 测试思路

### 1. _formatMessageLines() 懒加载机制

**设计**：
- 类变量 `_fmtLinesFn` 初始为 `None`
- 首次调用时从 `utils.command.send` 导入 `_formatMessageLines`
- 后续调用直接使用缓存的函数

**测试点**：
- 首次调用触发懒加载
- 后续调用使用缓存

### 2. _getTermWidth() 终端宽度获取

**逻辑**：
- 从 `_app.output.get_size().columns` 获取终端宽度
- 异常时返回默认值 80

**测试点**：
- 正常获取终端宽度
- 异常时返回默认值

### 3. _defaultStatus() 默认状态栏

**逻辑**：
- 返回包含快捷键提示和 chatID 的状态栏文本

**测试点**：
- 返回正确的状态栏文本

### 4. _updateStatus() 状态栏更新

**逻辑**：
- `_scrollOffset == 0` → 显示默认状态
- `_scrollOffset > 0` → 显示历史浏览状态（包含偏移量和新消息提示）

**测试点**：
- 滚动偏移为 0 时显示默认状态
- 滚动偏移 > 0 时显示历史浏览状态

### 5. _getWindowHeight() 窗口高度计算

**逻辑**：
- 优先从 `_transcriptWindow.render_info.window_height` 获取
- render_info 为 None 时从 `_app.output.get_size().rows - 7` 计算
- 异常时返回默认值 20

**测试点**：
- 从 render_info 获取高度
- render_info 为 None 时从 output 获取
- 异常时返回默认值

### 6. _clampOffset() 偏移量边界限制

**逻辑**：
- 最大偏移量 = `len(_allLines) - windowHeight`
- 偏移量不低于 0

**测试点**：
- 偏移量在有效范围内
- 偏移量超过最大值时被限制
- 空历史时偏移量为 0

### 7. _scrollUp() / _scrollDown() 滚动控制

**逻辑**：
- `_scrollUp(lines)` → `_scrollOffset += lines`
- `_scrollDown(lines)` → `_scrollOffset = max(0, _scrollOffset - lines)`
- 滚动后调用 `_clampOffset()`、`_updateStatus()`、`_refreshTranscript()`

**测试点**：
- 向上滚动增加偏移量
- 向下滚动减少偏移量（不低于 0）
- 滚动后调用相关方法

### 8. _refreshTranscript() 聊天记录刷新
