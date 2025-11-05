import os
import shutil
import asyncio
import magic
import uuid
import subprocess
from telegram.error import TelegramError , NetworkError

from utils.logger import logAction
from config import MAX_CONCURRENT_DOWNLOADS , MAX_DOWNLOADS_ATTEMPTS




# 下载并打包完整表情包，之后的发送消息交给 /handlers/stickers.py
# 表情包的下载分为 3 步……
async def createStickerZip(bot, stickerSet, setName, outputDir="data/"):
    os.makedirs(outputDir , exist_ok=True)
    
    # 生成uuid，防止生成时间相近的两个表情包发生冲突
    uniqueID = uuid.uuid4().hex[:8]
    tmpDir = os.path.join(outputDir , f"{setName}_{uniqueID}")
    os.makedirs(tmpDir , exist_ok=True)

    try:

        downloadTasks = []

        # Step 1：启动并发下载任务，让所有 Stickers 同时开始下载
        for i , s in enumerate(stickerSet.stickers , start=1):
            outPath = os.path.join(tmpDir , f"{i}.webp")
            task = asyncio.create_task(downLoadOne(bot , s.file_id , outPath))
            downloadTasks.append(task)

        # Step 2：单张 Sticker 开始并发下载
        await asyncio.gather(*downloadTasks)

        await logAction(None , "全部Stickers下载完毕" , "现在就要打包喵——" , "childWithChild")

        # Step 3：打包为.zip
        zipBase = os.path.join(outputDir , f"{setName}_{uniqueID}")
        zipPath = shutil.make_archive(zipBase , "zip" , tmpDir)
        await logAction(None , "打包完毕" , "打包完毕——现在就要发出来喵——" , "childWithChild")

        # 确保zip文件已经被写入盘中，而能被正确清除
        for _ in range(5):
            if os.path.exists(zipPath):
                break
            await asyncio.sleep(1)

        return zipPath
    
    except Exception as e:
        await logAction(None , "呜喵……？下载出现问题了……" , f"报错在这里哦：{e}" , "lastChildWithChild")
        raise

    finally:
        # 无论获取成败，都清理临时目录
        shutil.rmtree(tmpDir , ignore_errors=True)




# Step 2：开始下载各张 Sticker
async def downLoadOne(bot , fileID , outPath):
    async with asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS):
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
                    await logAction(None , f"检测到 {fileID} 为 WebM 动态贴纸喵" , f"重命名为 {os.path.basename(newPath)}" , "childWithChild")
                elif mimeType == "image/webp":
                    continue
                elif mimeType == "application/x-tgsticker" or mimeType == "application/json":
                    newPath = outPath.rsplit('.' , 1)[0] + ".tgs"
                    os.rename(outPath , newPath)
                    await logAction(None , f"检测到 {fileID} 为 TGS 矢量动画喵" , f"重命名为 {os.path.basename(newPath)}" , "childWithChild")
                else:
                    await logAction(None , f"是、是未知类型 {mimeType} 喵…… 😰" , f"还是保留原文件名 {fileID} 吧……" , "childWithChild")

                return True
            
            except (TelegramError , NetworkError , OSError) as e:
                await logAction(None , f"出错啦喵，现在就尝试第 {attempt + 1} / {MAX_DOWNLOADS_ATTEMPTS} 次下载……" , f"表情{fileID}，{e}" , "childWithChild")
                if attempt < MAX_DOWNLOADS_ATTEMPTS:
                    await asyncio.sleep(MAX_DOWNLOADS_ATTEMPTS * attempt)  # 指数级退避等待，防止 Tg API 限速
                else:
                    await logAction(None , "下载最终失败……" , f"下载错误 {MAX_DOWNLOADS_ATTEMPTS} 次了，锌酱放弃了喵——！" , "lastChildWithChild")
                    return False
            except Exception as e:
                # 其它不可预期错误，直接记录并退出下载
                await logAction(None , "下载出现未知错误了喵……" , f"报错在这里——{e}" , "lastChildWithChild")
                return False



# 在180秒后清除相关信息，防止刷屏
async def deleteLater(context, chatId, messageId, filePath, deleteDelay):
    await asyncio.sleep(deleteDelay)
    try:
       await context.bot.delete_message(chatId, message_id=messageId)
    except Exception:
        pass

    try:
        if os.path.exists(filePath):
            os.remove(filePath)
    except Exception:
        pass