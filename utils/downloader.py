"""
utils/downloader.py

Telegram 表情包下载与格式转换模块。

该模块负责从 Telegram 下载完整的表情包（Sticker Sets），并提供格式转换功能，
支持将 WebP/WebM 格式的贴纸转换为 GIF 格式。


================================================================================
核心功能

1. createStickerZip(bot, stickerSet, setName, stickerSuffix, outputDir)
    - 下载并打包完整的表情包
    - 支持并发下载多个贴纸（通过 Semaphore 控制并发数）
    - 自动生成 UUID 避免文件名冲突
    - 打包为 .zip 文件
    - 下载完成后自动清理临时目录

2. downloadEachOne(bot, fileID, outPath, stickerSuffix)
    - 下载单个贴纸
    - 自动检测文件的真实格式（通过 python-magic）
    - 支持 WebP、WebM、TGS 三种格式
    - 可选 GIF 转换
    - 内置重试机制（指数退避）

3. convertToGif(rawInputPath)
    - 将 WebP/WebM 格式转换为 GIF
    - 使用 FFmpeg 进行高质量转换
    - 自动检测静态/动态贴纸，调整帧率
    - 根据贴纸尺寸选择最佳抖动算法
    - 生成优化的调色板

4. deleteLater(context, chatId, messageId, filePath, deleteDelay)
    - 延迟删除 Telegram 消息和本地文件
    - 用于定时清理下载的表情包，防止占用空间


================================================================================
并发控制

使用全局单例 Semaphore 控制并发下载数：
    - _downloadSemaphore: 全局信号量
    - _getSemaphore(): 延迟初始化，确保在正确的事件循环中创建
    - 最大并发数由 config.MAX_CONCURRENT_DOWNLOADS 控制


================================================================================
格式支持

支持的输入格式：
    - image/webp        静态或动态 WebP 图片
    - video/webm        WebM 视频格式
    - application/x-tgsticker    Telegram Lottie 动画（.tgs）

支持的输出格式：
    - WebP/WebM        保持原格式
    - GIF              转换为 GIF（仅支持 WebP 和 WebM）

注意：
    - .tgs 格式是 Lottie 动画，FFmpeg 无法直接转换
    - 转换为 GIF 时会有质量损失和体积增大


================================================================================
GIF 转换流程

1. 检测静态/动态
    - 使用 FFmpeg probe 检测帧数
    - 静态图片：fps = 1
    - 动态图片：fps = MAX_GIF_FPS（默认 24）

2. 获取尺寸并选择抖动算法
    - 小尺寸 (≤256x256)：bayer:bayer_scale=3（更激进）
    - 大尺寸：sierra2_4a（更平滑）

3. 生成调色板
    - 使用 Lanczos 缩放到 512px 宽度
    - palettegen 生成优化的 256 色调色板

4. 应用调色板转换
    - 使用 paletteuse 滤镜
    - 应用选定的抖动算法
    - 设置 alpha_threshold=128 处理透明度


================================================================================
错误处理

下载重试机制：
    - 最大尝试次数：MAX_DOWNLOADS_ATTEMPTS
    - 指数退避：每次重试延迟时间翻倍
    - 捕获的异常：TelegramError、NetworkError、OSError

转换错误：
    - 不支持的格式会抛出 ValueError
    - FFmpeg 执行失败会抛出 RuntimeError


================================================================================
使用示例

# 下载并打包表情包为 WebP
zipPath = await createStickerZip(
    bot=context.bot,
    stickerSet=stickerSet,
    setName="cute_cats",
    stickerSuffix="webp"
)

# 下载并打包表情包为 GIF
zipPath = await createStickerZip(
    bot=context.bot,
    stickerSet=stickerSet,
    setName="cute_cats",
    stickerSuffix="gif"
)

# 延迟删除文件和消息
asyncio.create_task(
    deleteLater(context, chatId, messageId, zipPath, 180)
)


================================================================================
依赖项

外部依赖：
    - python-magic: MIME 类型检测
    - FFmpeg: 视频/图像转换（需要二进制文件）

FFmpeg 路径：
    - Windows: ffmpeg/ffmpeg.exe
    - Linux/macOS: ffmpeg/ffmpeg
"""

import os
import sys
import uuid
import magic
import shutil
import asyncio
import tempfile
from telegram.error import TelegramError , NetworkError

from utils.logger import logAction
from config import (
    MAX_CONCURRENT_DOWNLOADS,       # 最大并发下载数量
    MAX_DOWNLOADS_ATTEMPTS,         # 最大下载尝试次数
    MAX_GIF_FPS                     # 最大 GIF 帧数
)



if sys.platform == "win32":
    FFMPEG = os.path.abspath("ffmpeg/ffmpeg.exe")
else:
    FFMPEG = os.path.abspath("ffmpeg/ffmpeg")

if not os.path.isfile(FFMPEG):
    raise RuntimeError(f"缺失 ffmpeg 喵：{FFMPEG}")


_downloadSemaphore: asyncio.Semaphore|None = None
def _getSemaphore():
    global _downloadSemaphore
    if _downloadSemaphore is None:
        _downloadSemaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    return _downloadSemaphore



# 下载并打包完整表情包，之后的发送消息交给 /handlers/stickers.py
# 这是 utils/downloader.py 的主入口，下载并打包整个表情包
async def createStickerZip(
        bot,
        stickerSet,
        setName,
        stickerSuffix,
        outputDir="download/"
    ):

    os.makedirs(outputDir , exist_ok=True)
    
    # 生成uuid，防止生成时间相近的两个相同表情包发生冲突
    uniqueID = uuid.uuid4().hex[:8]
    tmpDir = os.path.join(outputDir , f"{setName}_{uniqueID}")
    os.makedirs(tmpDir , exist_ok=True)

    try:
        downloadTasks = []
        # 启动并发下载任务，让若干个 Stickers 同时开始下载
        for i , s in enumerate(stickerSet.stickers , start=1):
            outPath = os.path.join(tmpDir , f"{i}.webp")
            downloadTasks.append(
                asyncio.create_task(
                    downloadEachOne(bot , s.file_id , outPath , stickerSuffix)
                )
            )

        results = await asyncio.gather(*downloadTasks)

        # 统计结果
        ok = sum(1 for r in results if r["ok"])
        failed = sum(1 for r in results if not r["ok"])
        converted = sum(1 for r in results if r.get("converted"))

        await logAction(
            None,
            "下载完成喵 (as GIF)" if stickerSuffix == "gif" else "下载完成喵 (as WebP/WebM)",
            f"成功 {converted if stickerSuffix == 'gif' else ok} 张，失败 {failed} 张",
            "childWithChild"
        )

        # 打包为.zip
        zipBase = os.path.join(outputDir , f"{setName}_{uniqueID}")
        zipPath = shutil.make_archive(zipBase , "zip" , tmpDir)
        await logAction(None , "打包完毕" , "现在就要发出来喵——" , "childWithChild")

        return zipPath

    finally:
        # 无论获取成败，都清理临时目录
        shutil.rmtree(tmpDir , ignore_errors=True)




# 单张 Sticker 的下载 + 转换 (可选)
async def downloadEachOne(bot , fileID , outPath , stickerSuffix="webp"):

    async with _getSemaphore():
        for attempt in range(1 , MAX_DOWNLOADS_ATTEMPTS + 1):
            try:
                file = await bot.get_file(fileID)
                await file.download_to_drive(outPath)

                # 使用 magic 检测真实文件类型
                mimeType = magic.from_file(outPath , mime=True)
                # 根据 mime 类型修正扩展名
                newPath = outPath

                if mimeType == "video/webm":
                    newPath = outPath.rsplit('.' , 1)[0] + ".webm"
                    os.rename(outPath , newPath)

                elif mimeType == "application/x-tgsticker" or mimeType == "application/json":
                    newPath = outPath.rsplit('.' , 1)[0] + ".tgs"
                    os.rename(outPath , newPath)

                elif mimeType == "image/webp":
                    pass

                # 若用户选择 gif 格式，则进行转换
                converted = False
                if stickerSuffix == "gif":
                    gifPath = await convertToGif(newPath)
                    os.remove(newPath)
                    newPath = gifPath
                    converted = True

                return {
                    "ok": True,
                    "converted": converted,
                }

            except (TelegramError , NetworkError , OSError) as e:
                if attempt < MAX_DOWNLOADS_ATTEMPTS:
                    # 指数级退避等待，防止 Tg API 限速
                    await asyncio.sleep(MAX_DOWNLOADS_ATTEMPTS * attempt)
                else:
                    return {"ok": False , "error": str(e)}

            except Exception as e:
                # 其它不可预期错误，直接记录并退出下载
                return {"ok": False , "error": str(e)}




async def convertToGif(rawInputPath: str) -> str:

    inputPath = os.path.abspath(rawInputPath)
    ext = os.path.splitext(inputPath)[1].lower()

    # .tgs 是 Telegram Lottie 动画格式，FFmpeg 不直接支持
    # 暂时抛出友好的错误提示
    if ext == ".tgs":
        raise ValueError(f"暂不支持将 .tgs 格式转换为 GIF 喵……\n这是 Lottie 动画格式，需要特殊处理……")

    if ext not in [".webp" , ".webm"]:
        raise ValueError(f"不支持的格式喵：{ext}")

    outputPath = inputPath.rsplit("." , 1)[0] + ".gif"

    fps = MAX_GIF_FPS

    # 判断 Sticker 是否为静态 WebP，若是，则将帧率设为 1
    if ext == ".webp":
        probe = await asyncio.create_subprocess_exec(
            FFMPEG,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "frame=pkt_pts_time",
            "-of", "csv=p=0",
            inputPath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out , _ = await probe.communicate()
        if len(out.splitlines()) <= 1:
            fps = 1

    # 获取 Sticker 尺寸，决定抖动策略
    # 若为小尺寸 Sticker，则采用更激进的抖动策略，提升观感
    probe = await asyncio.create_subprocess_exec(
        FFMPEG,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x",
        inputPath,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    sizeOut , _ = await probe.communicate()
    try:
        w , h = map(int , sizeOut.decode().strip().split("x"))
    except Exception:
        w = h = 512

    dither = "bayer:bayer_scale=3" if w <= 256 and h <= 256 else "sierra2_4a"

    # 开始转换 GIF
    with tempfile.TemporaryDirectory() as tmpdir:
        palettePath = os.path.join(tmpdir , "palette.png")

        # 生成调色板 (%temp%/palette.png) 用的滤镜
        vfPalette = (
            "scale=512:-1:flags=lanczos,"
            "palettegen=stats_mode=full"
        )

        # 把 webp / webm 转成 gif 用的 filter_complex
        filterGraph = (
            "scale=512:-1:flags=lanczos[x];"
            f"[x][1:v]paletteuse="
            f"dither={dither}:alpha_threshold=128"
        )

        # 在转换 GIF 前，获取临时调色盘
        proc1 = await asyncio.create_subprocess_exec(
            FFMPEG,
            "-y",
            "-i", inputPath,
            "-vf", vfPalette,
            palettePath,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        _ , err1 = await proc1.communicate()

        if proc1.returncode != 0:
            raise RuntimeError(err1.decode(errors="ignore"))
        
        proc2 = await asyncio.create_subprocess_exec(
            FFMPEG,
            "-y",
            "-i", inputPath,
            "-i", palettePath,
            "-r", str(fps),
            "-filter_complex", filterGraph,
            outputPath,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        _ , err2 = await proc2.communicate()
        if proc2.returncode != 0:
            raise RuntimeError(f"GIF 转换失败喵：{err2.decode(errors='ignore')}")

    return outputPath




# 在180秒后清除相关信息，防止刷屏
async def deleteLater(context, chatId, messageId, filePath, deleteDelay):
    await asyncio.sleep(deleteDelay)
    try:
       await context.bot.delete_message(chatId, message_id=messageId)
    except Exception as e:
        pass

    if filePath:  # 添加 None 检查
        try:
            if os.path.exists(filePath):
                os.remove(filePath)
        except Exception:
            pass