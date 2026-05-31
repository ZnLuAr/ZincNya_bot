# Stickers Downloader 模块测试文档
## 概述
## 测试目标
## 测试思路

### 1. 纯函数测试（无副作用）

**_sanitizeSetName(name: str) -> str**
- 验证合法字符保留（字母、数字、下划线、连字符）
- 验证非法字符替换为下划线
- 验证空字符串返回 'sticker'
- 验证 Unicode 字符（中文）正确处理

**getActiveGifJobs() -> int**
- 验证返回当前 GIF 任务计数

**_ensureFFmpeg()**
- FFMPEG 为 None 时抛出 RuntimeError
- FFMPEG 可用时不抛出

### 2. 异步函数测试（涉及 subprocess、文件系统、Telegram API）

**convertToGif(rawInputPath: str) -> str**
- **格式拒绝**：.tgs/不支持的格式抛出 ValueError
- **依赖检查**：FFmpeg 不可用抛出 RuntimeError
- **静态/动态检测**：通过 probe 帧数判断
- **抖动算法选择**：小尺寸用 bayer，大尺寸用 sierra2_4a
- **subprocess 失败处理**：palettegen/paletteuse 失败抛出 RuntimeError

**downloadEachOne(bot, fileID, outPath, stickerSuffix)**
- **MIME 类型检测**：WebP/WebM/TGS 各自的处理路径
- **文件重命名**：根据 MIME 类型修正扩展名
- **重试机制**：TelegramError 触发指数退避重试
- **达到最大重试**：返回 `{"ok": False, "error": ...}`
- **不可预期异常**：直接返回错误（不重试）

**createStickerZip(bot, stickerSet, setName, stickerSuffix, outputDir)**
- **正常流程**：下载 → 打包 → 清理临时目录
- **GIF 计数**：stickerSuffix=gif 时计数增加，结束后释放
- **异常时仍清理**：finally 块保证临时目录清理

**deleteLater(context, chatId, messageId, filePath, delay)**
- **正常流程**：延迟后删除消息和文件
- **filePath=None**：跳过文件删除
- **文件不存在**：不抛出异常
- **delete_message 失败**：异常被吞掉

**deleteMessageLater(context, chatId, messageId, delay)**
- **正常流程**：延迟后删除消息
- **失败静默**：异常被吞掉

---

## 关键测试技术

### 1. Mock subprocess 序列

通过 `iter()` + `side_effect` 让多次调用返回不同 mock：

```python
procs = [probe_proc, proc1, proc2]
proc_iter = iter(procs)

async def mock_create_subprocess(*args, **kwargs):
    return next(proc_iter)
```

### 2. Mock TemporaryDirectory

模拟上下文管理器：

```python
with patch('utils.downloader.tempfile.TemporaryDirectory') as mock_tmp:
    mock_tmp.return_value.__enter__ = MagicMock(return_value="/tmp/fake")
    mock_tmp.return_value.__exit__ = MagicMock(return_value=None)
```

### 3. AsyncMock 异步函数

```python
