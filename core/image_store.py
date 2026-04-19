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

    async def save_image(self, image_bytes: bytes) -> str:
        mime = detect_image_mime(image_bytes)
        ext = mime_to_ext(mime)
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = self.images_dir / filename
        async with aiofiles.open(filepath, "wb") as f:
            await f.write(image_bytes)
        logger.info("[Wardrobe] 图片已保存: %s (format=%s)", filename, ext)
        return filename

    async def save_image_from_path(self, source_path: str) -> str:
        source = Path(source_path)
        ext = source.suffix.lstrip(".") or "jpg"
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = self.images_dir / filename
        await asyncio.to_thread(self._copy_file, str(source), str(filepath))
        logger.info("[Wardrobe] 图片已从路径保存: %s -> %s", source_path, filename)
        return filename

    def get_image_path(self, filename: str) -> Path:
        return self.images_dir / filename

    async def delete_image(self, filename: str) -> bool:
        filepath = self.images_dir / filename
        if filepath.exists():
            await asyncio.to_thread(filepath.unlink)
            logger.info("[Wardrobe] 图片已删除: %s", filename)
            return True
        return False

    async def read_image_bytes(self, filename: str) -> bytes | None:
        filepath = self.images_dir / filename
        if not filepath.exists():
            return None
        async with aiofiles.open(filepath, "rb") as f:
            return await f.read()

    @staticmethod
    def _copy_file(src: str, dst: str):
        import shutil
        shutil.copy2(src, dst)
