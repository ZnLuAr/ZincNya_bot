# FFmpeg 配置说明喵

这个文件夹是用来放 FFmpeg 可执行文件的喵，Bot 需要用它来处理媒体文件喵～

## 为什么这个文件夹是空的喵？

FFmpeg 的可执行文件体积太大了（约 170MB），不适合提交到 Git 仓库喵。所以请按照下面的步骤手动下载和配置——

---

## 配置步骤

### Windows 用户

1. **下载 FFmpeg **
   - 访问官网：https://ffmpeg.org/download.html
   - 或者直接下载：https://github.com/BtbN/FFmpeg-Builds/releases
   - 选择 `ffmpeg-master-latest-win64-gpl.zip` 这个文件喵

2. **解压并复制**
   - 解压下载的文件
   - 把 `bin/ffmpeg.exe` 复制到这个文件夹里（`ffmpeg/ffmpeg.exe`）

3. **验证**
   ```cmd
   ffmpeg\ffmpeg.exe -version
   ```
   能看到版本信息就说明配置成功了喵！

### Linux 用户

1. **使用包管理器安装**（推荐）
   ```bash
   # Debian/Ubuntu
   sudo apt update
   sudo apt install ffmpeg

   # 验证
   ffmpeg -version
   ```
   包管理器安装最方便了喵～

2. **或者手动下载**
   - 访问官网：https://ffmpeg.org/download.html
   - 下载静态构建版本
   - 把 `ffmpeg` 可执行文件复制到这个文件夹

---

## 预期文件结构喵

配置完成后，这个文件夹应该是这样的喵：

```
ffmpeg/
├── README.md         # 就是这个文件喵
├── ffmpeg.exe        # Windows 可执行文件
└── ffmpeg            # Linux 可执行文件（如果需要的话）
```

---

## 自动下载脚本（可选）

如果你想让脚本帮你自动搞定的话，可以运行：

**Windows:**
```cmd
python scripts/setup_ffmpeg.py
```

**Linux:**
```bash
python3 scripts/setup_ffmpeg.py
```

脚本会帮你自动下载并配置好一切喵～

---

## 常见问题

**Q: Bot 启动时提示找不到 ffmpeg ？**
A: 请确保 `ffmpeg.exe`（Windows）或 `ffmpeg`（Linux）在这个文件夹里，而且有执行权限喵！

**Q: 我可以使用系统已经安装的 ffmpeg 吗？**
A: 当然可以喵！如果你的系统 PATH 中已经有 ffmpeg 了，Bot 会自动使用它的喵～

**Q: 下载太慢了怎么办喵？**
A: 可以试试用国内的镜像源，或者手动下载后放到这个文件夹里喵～

---

**提示喵：** 这个文件夹中的 `*.exe` 和可执行文件已经在 `.gitignore` 中忽略了，不会被提交到版本控制，

所以，就放心大胆地把 FFmpeg 放进来喵！
