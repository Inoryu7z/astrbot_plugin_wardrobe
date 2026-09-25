import asyncio
import uuid
from pathlib import Path

import aiofiles

from astrbot.api import logger

from .utils import detect_image_mime, mime_to_ext

# 供取图模型"看图"用的视觉缩略图：按【短边】定尺寸。
# 原因：图库以竖图（自拍）为主，若按长边定尺寸，9:16 竖图长边 768 时横向只剩 424px，
# 服装细节（图案、花纹、配饰）会糊掉——而区分度往往就在这些细节上。
_VISION_THUMB_SHORT_EDGE = 768     # 短边目标值（不放大，只缩小）
_VISION_THUMB_MAX_LONG_EDGE = 1536  # 长边兜底上限，避免超长图失控
_VISION_THUMB_QUALITY = 88


class ImageStore:
    def __init__(self, data_dir: Path):
        self.images_dir = data_dir / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnails_dir = data_dir / "thumbnails"
        self.thumbnails_dir.mkdir(parents=True, exist_ok=True)

    async def save_image(self, image_bytes: bytes) -> str:
        mime = detect_image_mime(image_bytes)
        ext = mime_to_ext(mime)
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = self.images_dir / filename
        async with aiofiles.open(filepath, "wb") as f:
            await f.write(image_bytes)
        logger.debug("[Wardrobe] 图片已保存: %s (format=%s)", filename, ext)
        await self.ensure_thumbnail(filename)
        return filename

    async def save_image_from_path(self, source_path: str) -> str:
        source = Path(source_path)
        ext = source.suffix.lstrip(".") or "jpg"
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = self.images_dir / filename
        await asyncio.to_thread(self._copy_file, str(source), str(filepath))
        logger.debug("[Wardrobe] 图片已从路径保存: %s -> %s", source_path, filename)
        await self.ensure_thumbnail(filename)
        return filename

    def get_image_path(self, filename: str) -> Path:
        return self.images_dir / filename

    async def delete_image(self, filename: str) -> bool:
        filepath = self.images_dir / filename
        deleted = False
        if filepath.exists():
            await asyncio.to_thread(filepath.unlink)
            logger.debug("[Wardrobe] 图片已删除: %s", filename)
            deleted = True
        thumb_path = self.get_thumbnail_path(filename)
        if thumb_path.exists():
            await asyncio.to_thread(thumb_path.unlink)
        return deleted

    async def read_image_bytes(self, filename: str) -> bytes | None:
        filepath = self.images_dir / filename
        if not filepath.exists():
            return None
        async with aiofiles.open(filepath, "rb") as f:
            return await f.read()

    def get_thumbnail_path(self, filename: str) -> Path:
        thumb_name = Path(filename).stem + ".jpg"
        return self.thumbnails_dir / thumb_name

    async def ensure_thumbnail(self, filename: str, max_long_edge: int = 400) -> Path:
        thumb_path = self.get_thumbnail_path(filename)
        if thumb_path.exists():
            return thumb_path
        orig_path = self.images_dir / filename
        if not orig_path.exists():
            return orig_path
        try:
            thumb_path = await asyncio.to_thread(
                self._generate_thumbnail, orig_path, thumb_path, max_long_edge
            )
            return thumb_path
        except Exception as e:
            logger.warning("[Wardrobe] 缩略图生成失败: %s error=%s", filename, e)
            return orig_path

    @staticmethod
    def _generate_thumbnail(orig_path: Path, thumb_path: Path, max_long_edge: int) -> Path:
        from PIL import Image
        img = Image.open(str(orig_path))
        img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_long_edge:
            ratio = max_long_edge / max(w, h)
            new_w = max(1, int(w * ratio))
            new_h = max(1, int(h * ratio))
            img = img.resize((new_w, new_h), Image.LANCZOS)
        img.save(str(thumb_path), "JPEG", quality=85)
        return thumb_path

    def get_vision_thumbnail_path(self, filename: str) -> Path:
        return self.thumbnails_dir / (
            Path(filename).stem + f"_v{_VISION_THUMB_SHORT_EDGE}.jpg"
        )

    async def ensure_vision_thumbnail(self, filename: str) -> Path:
        """生成/复用供取图模型看图用的缩略图（短边 768，独立于 WebUI 的 400px 缩略图）。

        与主缩略图分文件存放，避免覆盖已有缓存尺寸。失败时回退原图路径。
        """
        thumb_path = self.get_vision_thumbnail_path(filename)
        if thumb_path.exists():
            return thumb_path
        orig_path = self.images_dir / filename
        if not orig_path.exists():
            return orig_path
        try:
            return await asyncio.to_thread(
                self._generate_vision_thumbnail, orig_path, thumb_path
            )
        except Exception as e:
            logger.warning("[Wardrobe] 视觉缩略图生成失败: %s error=%s", filename, e)
            return orig_path

    @staticmethod
    def _vision_thumb_size(width: int, height: int) -> tuple[int, int]:
        """按短边缩放（不放大），再用长边上限兜底。"""
        short = min(width, height)
        scale = 1.0
        if short > _VISION_THUMB_SHORT_EDGE:
            scale = _VISION_THUMB_SHORT_EDGE / short
        w = max(1, int(width * scale))
        h = max(1, int(height * scale))
        if max(w, h) > _VISION_THUMB_MAX_LONG_EDGE:
            s2 = _VISION_THUMB_MAX_LONG_EDGE / max(w, h)
            w = max(1, int(w * s2))
            h = max(1, int(h * s2))
        return w, h

    @staticmethod
    def _generate_vision_thumbnail(orig_path: Path, thumb_path: Path) -> Path:
        from PIL import Image

        img = Image.open(str(orig_path)).convert("RGB")
        w, h = img.size
        new_w, new_h = ImageStore._vision_thumb_size(w, h)
        if (new_w, new_h) != (w, h):
            img = img.resize((new_w, new_h), Image.LANCZOS)
        img.save(str(thumb_path), "JPEG", quality=_VISION_THUMB_QUALITY, optimize=True)
        return thumb_path

    @staticmethod
    def _copy_file(src: str, dst: str):
        import shutil
        shutil.copy2(src, dst)
