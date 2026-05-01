import asyncio
import uuid
from pathlib import Path

import aiofiles

from astrbot.api import logger

from .utils import detect_image_mime, mime_to_ext


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
        logger.info("[Wardrobe] 图片已保存: %s (format=%s)", filename, ext)
        await self.ensure_thumbnail(filename)
        return filename

    async def save_image_from_path(self, source_path: str) -> str:
        source = Path(source_path)
        ext = source.suffix.lstrip(".") or "jpg"
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = self.images_dir / filename
        await asyncio.to_thread(self._copy_file, str(source), str(filepath))
        logger.info("[Wardrobe] 图片已从路径保存: %s -> %s", source_path, filename)
        await self.ensure_thumbnail(filename)
        return filename

    def get_image_path(self, filename: str) -> Path:
        return self.images_dir / filename

    async def delete_image(self, filename: str) -> bool:
        filepath = self.images_dir / filename
        deleted = False
        if filepath.exists():
            await asyncio.to_thread(filepath.unlink)
            logger.info("[Wardrobe] 图片已删除: %s", filename)
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

    @staticmethod
    def _copy_file(src: str, dst: str):
        import shutil
        shutil.copy2(src, dst)
