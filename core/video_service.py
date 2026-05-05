import asyncio
import hashlib
import os
import uuid
from pathlib import Path

import aiofiles
import httpx

from astrbot.api import logger

_DEFAULT_SYSTEM_PROMPT = """# 图片转视频提示词生成器

## 统一规则
1. **只写增量，不写存量**：不重复图片已有信息，只写从静止变成什么
2. **1-2 个核心动态**：最有表现力的展开，其余留白
3. **动态有明确方向**：写清变化（如"从坐着变成站起"）
4. **可视觉化**：每句话必须是镜头能直接拍到的东西
5. **物理可行**：动作自然流畅
6. **纯视觉化中文**：连贯句子，不编号，不分析，不写主观感受
7. **禁止**：抽象氛围词、模糊程度词、技术术语、主观表情（眼神迷离等）

## 正常档
中性动词：走、坐、站、看、转身、微笑、整理、拿起、放下、挥手
核心：日常自然动作。

## 轻荤档
[如果要使用，请联系作者]

## 重荤档
[如果要使用，请联系作者]

## 输出
以「参考图中身材傲人的少女，她」开头，输出一段连贯中文提示词。不输出其他内容。"""

_VIDEO_TIMEOUT = 600
_DOWNLOAD_TIMEOUT = 300


class VideoService:
    def __init__(self, plugin):
        self.plugin = plugin
        self.videos_dir: Path | None = None

    def _ensure_dirs(self):
        if self.videos_dir is None:
            self.videos_dir = self.plugin.data_dir / "videos"
        self.videos_dir.mkdir(parents=True, exist_ok=True)

    def get_system_prompt_path(self) -> Path:
        return self.plugin.data_dir / "video_system_prompt.txt"

    async def load_system_prompt(self) -> str:
        path = self.get_system_prompt_path()
        if path.exists():
            try:
                async with aiofiles.open(path, "r", encoding="utf-8") as f:
                    content = await f.read()
                    if content.strip():
                        return content
            except Exception:
                pass
        return _DEFAULT_SYSTEM_PROMPT

    async def save_system_prompt(self, text: str) -> None:
        self.plugin.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.get_system_prompt_path()
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(text)

    async def generate_video(
        self,
        image_id: str,
        tier: str,
        user_thoughts: str = "",
        backend_override: str = "",
    ) -> str:
        self._ensure_dirs()
        await self.plugin._ensure_db()

        tier_labels = {"normal": "正常", "light_spicy": "轻荤", "heavy_spicy": "重荤"}
        tier_label = tier_labels.get(tier, tier)

        image = await self.plugin.db.get_image(image_id)
        if not image:
            raise ValueError(f"未找到图片: {image_id}")

        image_path_str = image.get("image_path", "")
        if not image_path_str:
            raise ValueError("图片路径为空")

        image_path = self.plugin.store.get_image_path(image_path_str)
        if not image_path.exists():
            raise ValueError(f"图片文件不存在: {image_path}")

        persona = image.get("persona", "")

        video_id = await self.plugin.db.add_video(
            source_image_id=image_id,
            video_path="",
            provider_id="",
            tier=tier,
            user_thoughts=user_thoughts,
            persona=persona,
            status="generating",
        )

        asyncio.create_task(self._process_video(
            video_id, image_id, image_path, tier, tier_label,
            user_thoughts, backend_override, persona,
        ))

        return video_id

    async def _process_video(
        self,
        video_id: str,
        image_id: str,
        image_path: Path,
        tier: str,
        tier_label: str,
        user_thoughts: str,
        backend_override: str,
        persona: str,
    ):
        try:
            async with aiofiles.open(image_path, "rb") as f:
                image_bytes = await f.read()

            provider_id = backend_override.strip() or self._get_tier_backend(tier)
            if not provider_id:
                raise ValueError(f"未配置「{tier_label}」档的视频后端")

            prompt_provider_id = str(self.plugin._cfg("video_prompt_provider_id", "") or "").strip()
            if not prompt_provider_id:
                raise ValueError("未配置视频提示词生成模型")

            logger.info("[VideoService] 开始生成视频提示词 video_id=%s tier=%s", video_id, tier_label)

            generated_prompt = await self._generate_prompt(
                prompt_provider_id, image_bytes, tier, tier_label, user_thoughts
            )

            logger.info("[VideoService] 提示词生成完成 video_id=%s len=%d", video_id, len(generated_prompt))

            await self.plugin.db.update_video(video_id, generated_prompt=generated_prompt)

            logger.info("[VideoService] 开始调用视频后端 video_id=%s backend=%s", video_id, provider_id)

            video_url = await self._call_video_backend(provider_id, generated_prompt, image_bytes)

            logger.info("[VideoService] 视频生成完成 video_id=%s url=%s", video_id, video_url[:80])

            video_filename = f"{video_id}.mp4"
            video_path = self.videos_dir / video_filename
            await self._download_video(video_url, video_path)

            await self.plugin.db.update_video(
                video_id,
                video_path=video_filename,
                provider_id=provider_id,
                status="done",
            )

            logger.info("[VideoService] 视频处理完成 video_id=%s", video_id)

        except Exception as e:
            error_msg = str(e)
            logger.error("[VideoService] 视频生成失败 video_id=%s error=%s", video_id, error_msg, exc_info=True)
            try:
                await self.plugin.db.update_video(
                    video_id, status="failed", error_message=error_msg[:500]
                )
            except Exception:
                pass

    async def _generate_prompt(
        self,
        prompt_provider_id: str,
        image_bytes: bytes,
        tier: str,
        tier_label: str,
        user_thoughts: str,
    ) -> str:
        system_prompt = await self.load_system_prompt()

        user_prompt = f"【必须遵守以下档位】{tier_label}档\n"
        user_prompt += f"请严格使用 system prompt 中\"### {tier_label}档\"段落的规则生成视频提示词。提示词必须符合该档位标准。\n"
        user_prompt += f"禁止使用其他档位的任何动词和规则。\n"
        if user_thoughts.strip():
            user_prompt += f"用户附加看法：{user_thoughts.strip()}\n"
        user_prompt += "请根据图片生成视频提示词。"

        provider = self.plugin.context.get_provider_by_id(prompt_provider_id)
        if not provider:
            raise ValueError(f"未找到提示词生成模型: {prompt_provider_id}")

        mime = "image/jpeg"
        try:
            from ..core.utils import detect_image_mime
            mime = detect_image_mime(image_bytes)
        except Exception:
            pass

        image_url = f"data:{mime};base64,{_b64(image_bytes)}"

        try:
            resp = await provider.text_chat(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                image_urls=[image_url],
                temperature=0.8,
            )
        except TypeError:
            resp = await provider.text_chat(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                image_urls=[image_url],
            )

        prompt_text = self._extract_completion(resp)
        if not prompt_text.strip():
            raise ValueError("提示词生成模型返回了空结果")

        return prompt_text.strip()

    async def _call_video_backend(
        self,
        provider_id: str,
        prompt: str,
        image_bytes: bytes,
    ) -> str:
        aiimg_star = self.plugin.context.get_registered_star("astrbot_plugin_aiimg")
        if not aiimg_star or not aiimg_star.activated or not aiimg_star.star_cls:
            raise ValueError("AiImg 插件未激活")

        instance = aiimg_star.star_cls
        registry = getattr(instance, "registry", None)
        if not registry:
            raise ValueError("AiImg registry 不可用")

        backend = registry.get_video_backend(provider_id)
        if not backend:
            raise ValueError(f"未找到视频后端: {provider_id}")

        mime = "image/jpeg"
        try:
            from ..core.utils import detect_image_mime
            mime = detect_image_mime(image_bytes)
        except Exception:
            pass

        image_url = f"data:{mime};base64,{_b64(image_bytes)}"

        video_url = await backend.generate_video_url(
            prompt=prompt,
            image_bytes=image_bytes,
            image_url=image_url,
        )

        if not video_url:
            raise ValueError("视频后端返回了空 URL")

        return video_url

    async def _download_video(self, url: str, dest: Path):
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                raise ValueError(f"下载视频失败 HTTP {resp.status_code}")
            content = resp.content
            if len(content) < 12 or content[4:8] != b'ftyp':
                raise ValueError("下载的文件不是有效的 MP4 格式")
            async with aiofiles.open(dest, "wb") as f:
                await f.write(content)

    def _get_tier_backend(self, tier: str) -> str:
        key_map = {
            "normal": "video_normal_default_backend",
            "light_spicy": "video_light_spicy_default_backend",
            "heavy_spicy": "video_heavy_spicy_default_backend",
        }
        key = key_map.get(tier, "")
        if not key:
            return ""
        return str(self.plugin._cfg(key, "") or "").strip()

    @staticmethod
    def _extract_completion(resp) -> str:
        if isinstance(resp, str):
            return resp
        if hasattr(resp, "completion_text"):
            return resp.completion_text
        if isinstance(resp, dict):
            for key in ("content", "text", "message", "completion", "completion_text"):
                val = resp.get(key)
                if val:
                    if isinstance(val, str):
                        return val
            for key in ("choices",):
                choices = resp.get(key)
                if choices and isinstance(choices, list) and len(choices) > 0:
                    item = choices[0]
                    if isinstance(item, dict):
                        msg = item.get("message") or item.get("text") or item.get("content") or item
                        if isinstance(msg, dict):
                            return msg.get("content", "")
                        return str(msg)
                    return str(item)
        return str(resp)


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")
