# FFmpeg 配置说明

本文件夹用于存放 FFmpeg 可执行文件，Bot 需要使用 FFmpeg 来处理媒体文件。

## 为什么此文件夹是空的？

FFmpeg 的可执行文件体积很大（约 170MB），不适合提交到 Git 仓库。请按照下面的步骤手动下载和配置。

---

## 配置步骤

### Windows 用户

1. **下载 FFmpeg**
   - 访问官网：https://ffmpeg.org/download.html
   - 或直接下载：https://github.com/BtbN/FFmpeg-Builds/releases
   - 选择 `ffmpeg-master-latest-win64-gpl.zip`

2. **解压并复制**
   - 解压下载的文件
   - 将 `bin/ffmpeg.exe` 复制到本文件夹（`ffmpeg/ffmpeg.exe`）

3. **验证**
   ```cmd
   ffmpeg\ffmpeg.exe -version
   ```

### Linux 用户

1. **使用包管理器安装**（推荐）
   ```bash
   # Debian/Ubuntu
   sudo apt update
   sudo apt install ffmpeg

   # 验证
   ffmpeg -version
   ```

2. **或手动下载**
   - 访问官网：https://ffmpeg.org/download.html
   - 下载静态构建版本
   - 将 `ffmpeg` 可执行文件复制到本文件夹

---

## 预期文件结构

配置完成后，此文件夹应包含：

```
ffmpeg/
├── README.md         # 本文件
├── ffmpeg.exe        # Windows 可执行文件
└── ffmpeg            # Linux 可执行文件（如需要）
```

---

## 自动下载脚本（可选）

如果你希望自动化此过程，可以运行：

**Windows:**
```cmd
python scripts/setup_ffmpeg.py
```

**Linux:**
```bash
python3 scripts/setup_ffmpeg.py
```

---

## 常见问题

**Q: Bot 启动时提示找不到 ffmpeg？**
A: 请确保 `ffmpeg.exe`（Windows）或 `ffmpeg`（Linux）在本文件夹内，且有执行权限。

**Q: 我可以使用系统已安装的 ffmpeg 吗？**
A: 可以！如果你的系统 PATH 中已有 ffmpeg，Bot 会自动使用它。

---

**提示：** 本文件夹中的 `*.exe` 和可执行文件已在 `.gitignore` 中忽略，不会被提交到版本控制。
