"""
utils/llm/vision.py

LLM 图片处理：
    - 从 Telegram 消息中提取图片引用（photo / document）
    - 从 reply_to_message 中提取图片引用
    - 按需下载并 base64 编码
    - 过大 / 不支持的图片生成文字说明供 LLM 参考
"""


import base64
import logging
from dataclasses import dataclass

from config import LLM_IMAGE_MAX_BYTES, LLM_IMAGE_SUPPORTED_MIMES

from utils.logger import logSystemEvent, LogLevel, LogChildType

_logger = logging.getLogger(__name__)




@dataclass
class ImageRef:
    """Telegram 图片引用（延迟下载）。"""
    fileID: str
    mimeType: str
    fileSize: int | None = None
    tooLarge: bool = False




_VISION_PREFERRED_MIN_WIDTH = 800


def _pickBestPhoto(photos: tuple) -> object:
    """选择适合视觉模型的分辨率—— 宽度 >= 800px 的最小尺寸，不够则取最大。"""
    for p in photos:
        if p.width >= _VISION_PREFERRED_MIN_WIDTH:
            return p
    return photos[-1]




def extractImageRefs(message) -> list[ImageRef]:
    """
    从单条 Telegram 消息中提取图片引用。

    支持 message.photo（压缩图）和 message.document（以文件发送的图片）。
    过大的图片标记为 tooLarge 而非直接丢弃，便于后续生成文字说明。
    """
    refs: list[ImageRef] = []

    if message.photo:
        # message.photo 是 tuple[PhotoSize]，按分辨率从小到大排列
        # 选择合适的分辨率：优先取宽度 >= 800px 的最小尺寸，减小请求体积
        # 视觉模型不需要最高分辨率，800px 应该足以让大模型识别文字和视觉元素
        photo = _pickBestPhoto(message.photo)
        tooLarge = bool(photo.file_size and photo.file_size > LLM_IMAGE_MAX_BYTES)
        refs.append(ImageRef(
            fileID=photo.file_id,
            mimeType="image/jpeg",
            fileSize=photo.file_size,
            tooLarge=tooLarge,
        ))
        _logger.info(
            "LLM 图片分辨率: 可用 %s, 选取 %dx%d",
            [(p.width, p.height) for p in message.photo],
            photo.width, photo.height,
        )

    elif message.document and message.document.mime_type:
        doc = message.document
        if doc.mime_type in LLM_IMAGE_SUPPORTED_MIMES:
            tooLarge = bool(doc.file_size and doc.file_size > LLM_IMAGE_MAX_BYTES)
            refs.append(ImageRef(
                fileID=doc.file_id,
                mimeType=doc.mime_type,
                fileSize=doc.file_size,
                tooLarge=tooLarge,
            ))

    return refs




def extractReplyImageRefs(message) -> list[ImageRef]:
    """从 message.reply_to_message 中提取图片引用。"""
    if not message.reply_to_message:
        return []
    return extractImageRefs(message.reply_to_message)


async def downloadImages(bot, refs: list[ImageRef]) -> tuple[list[dict], list[str]]:
    """
    下载图片引用列表并 base64 编码。

    返回:
        images: [{"data": b64_str, "mimeType": "image/jpeg"}, ...]
        notes:  人类可读的文字说明列表（过大 / 下载失败时生成）
    """
    images: list[dict] = []
    notes: list[str] = []

    for ref in refs:
        if ref.tooLarge:
            notes.append("[用户发送了一张图片，但文件过大无法处理]")
            continue
        try:
            file = await bot.get_file(ref.fileID)
            raw = await file.download_as_bytearray()
            await logSystemEvent(
                "LLM 图片下载完成",
                f"file_id={ref.fileID[:20]}..., mime={ref.mimeType}, size={len(raw)} bytes",
                LogLevel.INFO,
                LogChildType.WITH_ONE_CHILD,
            )
            images.append({
                "data": base64.b64encode(raw).decode("ascii"),
                "mimeType": ref.mimeType,
            })
        except Exception as e:
            notes.append("[有一张图片下载失败]")
            await logSystemEvent(
                "LLM 图片下载失败",
                f"file_id={ref.fileID[:20]}..., error={e}",
                LogLevel.WARNING,
                LogChildType.WITH_ONE_CHILD
            )

    return images, notes
