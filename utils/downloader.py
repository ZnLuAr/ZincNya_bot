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
                    downLoadEachOne(bot , s.file_id , outPath , stickerSuffix)
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
            f"成功 {converted if stickerSuffix == "gif" else ok} 张，失败 {failed} 张",
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
async def downLoadEachOne(bot , fileID , outPath , stickerSuffix="webp"):

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
            raise RuntimeError(
                "GIF 转换失败喵：",
                err = err2.decode(errors="ignore")
            )

    return outputPath




# 在180秒后清除相关信息，防止刷屏
async def deleteLater(context, chatId, messageId, filePath, deleteDelay):
    await asyncio.sleep(deleteDelay)
    try:
       await context.bot.delete_message(chatId, message_id=messageId)
    except Exception as e:
        pass

    try:
        if os.path.exists(filePath):
            os.remove(filePath)
    except Exception:
        pass