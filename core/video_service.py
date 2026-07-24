import asyncio
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
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

## 输出格式
输出严格的 JSON 对象，包含 reasoning 和 prompt 两个字段：

```json
{
  "reasoning": "简要分析：图片的场景、人物姿态、服装特点是什么，因此选择了哪些动态变化词，为什么这些动态符合该档位标准",
  "prompt": "参考图中身材傲人的少女，她..."
}
```

规则：
- reasoning 写清你的分析依据：图中实际看到了什么 → 因此选择了什么动态
- prompt 以「参考图中身材傲人的少女，她」开头，一段连贯中文
- 只输出 JSON 对象本体，不要 Markdown 代码块包裹，不要前后解释
- 不要输出任何 JSON 之外的内容"""

_VIDEO_TIMEOUT = 600
_DOWNLOAD_TIMEOUT = 300


@dataclass
class VideoSendResult:
    success: bool
    terminated: bool = False
    message: str = ""


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
        auto_send: bool = False,
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

        # 提取图片描述信息传给提示词生成模型
        image_description = self._build_image_description(image)

        self.plugin._spawn_bg_task(self._process_video(
            video_id, image_id, image_path, tier, tier_label,
            user_thoughts, backend_override, persona, image_description,
            auto_send=auto_send,
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
        image_description: str = "",
        reuse_prompt: str = "",
        auto_send: bool = False,
    ):
        try:
            async with aiofiles.open(image_path, "rb") as f:
                image_bytes = await f.read()

            provider_id = backend_override.strip() or self._get_tier_backend(tier)
            if not provider_id:
                raise ValueError(f"未配置「{tier_label}」档的视频后端")

            if reuse_prompt.strip():
                generated_prompt = reuse_prompt.strip()
                logger.debug("[VideoService] 重试复用已有提示词 video_id=%s len=%d", video_id, len(generated_prompt))
            else:
                prompt_provider_id = str(self.plugin._cfg("video_prompt_provider_id", "") or "").strip()
                if not prompt_provider_id:
                    raise ValueError("未配置视频提示词生成模型")

                logger.debug("[VideoService] 开始生成视频提示词 video_id=%s tier=%s", video_id, tier_label)

                generated_prompt = await self._generate_prompt(
                    prompt_provider_id, image_bytes, tier, tier_label, user_thoughts, image_description
                )

                logger.debug("[VideoService] 提示词生成完成 video_id=%s len=%d", video_id, len(generated_prompt))

                await self.plugin.db.update_video(video_id, generated_prompt=generated_prompt)

            logger.debug("[VideoService] 开始调用视频后端 video_id=%s backend=%s", video_id, provider_id)

            video_url = await self._call_video_backend(provider_id, generated_prompt, image_bytes)

            logger.info("[VideoService] 视频生成完成: video_id=%s url=%s", video_id, video_url)

            video_filename = f"{video_id}.mp4"
            video_path = self.videos_dir / video_filename
            await self._download_video(video_url, video_path)

            await self.plugin.db.update_video(
                video_id,
                video_path=video_filename,
                video_url=video_url,
                provider_id=provider_id,
                status="done",
            )

            if auto_send:
                try:
                    await self._auto_send_video(video_id, video_path, video_url)
                except Exception as send_err:
                    logger.warning("[VideoService] 视频自动发送失败 video_id=%s error=%s", video_id, send_err)

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
        image_description: str = "",
    ) -> str:
        system_prompt = await self.load_system_prompt()

        user_prompt = f"【必须遵守以下档位】{tier_label}档\n"
        user_prompt += f"请严格使用 system prompt 中\"### {tier_label}档\"段落的规则生成视频提示词。提示词必须符合该档位标准。\n"
        if image_description.strip():
            user_prompt += f"\n【图片已分析出的信息】\n{image_description.strip()}\n"
            user_prompt += "请基于以上图片信息生成视频提示词，不要重复描述图片已有内容，只写动态变化。\n"
        if user_thoughts.strip():
            user_prompt += f"\n用户附加想法：{user_thoughts.strip()}\n"
        user_prompt += "\n请根据图片生成视频提示词。"

        mime = "image/jpeg"
        try:
            from ..core.utils import detect_image_mime
            mime = detect_image_mime(image_bytes)
        except Exception:
            pass

        # 检查直连 API 配置
        api_base = str(self.plugin._cfg("video_prompt_base_url", "") or "").strip()
        api_key = str(self.plugin._cfg("video_prompt_api_key", "") or "").strip()
        api_model = str(self.plugin._cfg("video_prompt_model", "") or "").strip()
        use_direct_api = api_base and api_key and api_model

        if use_direct_api:
            logger.debug("[VideoService] 使用直连 API 生成提示词 model=%s image_size=%d", api_model, len(image_bytes))
            raw_text = await self._call_direct_vision_api(
                api_base, api_key, api_model, system_prompt, user_prompt,
                image_bytes, mime
            )
        else:
            logger.debug("[VideoService] 回退 AstrBot Provider 生成提示词 provider=%s image_size=%d",
                        prompt_provider_id, len(image_bytes))
            raw_text = await self._call_astrbot_llm_generate(
                prompt_provider_id, system_prompt, user_prompt,
                image_bytes, mime
            )

        if not raw_text:
            raise ValueError("提示词生成模型返回了空结果")

        prompt_text, reasoning_text = self._parse_json_prompt(raw_text)
        if reasoning_text:
            logger.debug("[VideoService] 模型推理依据: %s", reasoning_text)

        if not prompt_text:
            raise ValueError("提示词生成模型返回了空提示词")

        thinking_markers = [
            "用户意图", "图片分析", "档位判定", "我来分析", "再想想",
            "重新选", "确认", "检查", "思考一下", "让我想想",
        ]
        found_markers = [m for m in thinking_markers if m in prompt_text]
        if found_markers:
            logger.warning("[VideoService] 提示词仍含思维链特征: %s", found_markers)

        return prompt_text

    async def _call_direct_vision_api(
        self, api_base: str, api_key: str, model: str,
        system_prompt: str, user_prompt: str,
        image_bytes: bytes, mime: str,
    ) -> str:
        """直接 HTTP 调用 OpenAI 兼容 Vision API，确保图片正确传递。"""
        import time as _time
        import httpx as _httpx

        url = api_base.rstrip("/") + "/chat/completions"
        b64 = _b64(image_bytes)
        data_url = f"data:{mime};base64,{b64}"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            "temperature": 0.8,
        }

        t0 = _time.perf_counter()
        async with _httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            elapsed = _time.perf_counter() - t0
            status = resp.status_code
            logger.debug("[VideoService] 直连 API 完成 耗时=%.2fs status=%d", elapsed, status)

            if status != 200:
                raise ValueError(f"直连 API 返回 {status}: {resp.text[:500]}")

            body = resp.json()
            choices = body.get("choices", [])
            if not choices:
                raise ValueError("直连 API 无 choices 返回")

            msg = choices[0].get("message", {})
            text = msg.get("content", "") or ""
            return text.strip()

    async def _call_astrbot_llm_generate(
        self, provider_id: str, system_prompt: str, user_prompt: str,
        image_bytes: bytes, mime: str,
    ) -> str:
        """回退方案：通过 AstrBot Provider 调用多模态模型。"""
        import tempfile
        import os as _os_module
        import time as _time

        ext = "jpg"
        try:
            from ..core.utils import mime_to_ext
            ext = mime_to_ext(mime)
        except Exception:
            pass

        temp_fd, temp_path = tempfile.mkstemp(suffix=f".{ext}")
        try:
            _os_module.write(temp_fd, image_bytes)
        finally:
            _os_module.close(temp_fd)

        resolved_path = str(Path(temp_path).resolve())

        try:
            t0 = _time.perf_counter()
            try:
                llm_resp = await self.plugin.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    image_urls=[resolved_path],
                )
            except (TypeError, AttributeError) as e:
                logger.warning("[VideoService] image_urls 列表不兼容，回退字符串: %s", e)
                llm_resp = await self.plugin.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    image_urls=resolved_path,
                )

            elapsed = _time.perf_counter() - t0
            raw_text = (getattr(llm_resp, "completion_text", "") or "").strip()
            logger.debug("[VideoService] AstrBot模型返回 耗时=%.2fs len=%d", elapsed, len(raw_text))
            return raw_text
        finally:
            try:
                _os_module.remove(temp_path)
            except Exception:
                pass

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
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise ValueError(f"下载视频失败 HTTP {resp.status_code}")
                first_chunk = None
                async with aiofiles.open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        if first_chunk is None:
                            first_chunk = chunk
                            if len(chunk) < 12 or chunk[4:8] != b'ftyp':
                                raise ValueError("下载的文件不是有效的 MP4 格式")
                        await f.write(chunk)
                if first_chunk is None:
                    raise ValueError("下载的文件为空")

        await self._faststart_if_needed(dest)

    async def _faststart_if_needed(self, video_path: Path):
        try:
            needs = await asyncio.to_thread(self._check_moov_position, video_path)
            if not needs:
                return
        except Exception as e:
            logger.warning("[VideoService] moov 位置检测失败，尝试直接 faststart: %s", e)

        try:
            await asyncio.to_thread(self._run_ffmpeg_faststart, video_path)
            logger.debug("[VideoService] ffmpeg faststart 完成: %s", video_path.name)
        except FileNotFoundError:
            logger.warning("[VideoService] ffmpeg 不可用，尝试纯 Python faststart")
            try:
                await asyncio.to_thread(self._python_faststart, video_path)
                logger.debug("[VideoService] 纯 Python faststart 完成: %s", video_path.name)
            except Exception as e2:
                logger.warning("[VideoService] 纯 Python faststart 也失败，视频 moov 仍在末尾: %s", e2)
        except Exception as e:
            logger.warning("[VideoService] ffmpeg faststart 失败: %s", e)

    @staticmethod
    def _check_moov_position(path: Path) -> bool:
        _SKIP_ATOMS = {b'free', b'skip', b'wide', b'mdat'}
        with open(path, "rb") as f:
            offset = 0
            seen_significant_before_moov = False
            while True:
                f.seek(offset)
                header = f.read(8)
                if len(header) < 8:
                    break
                size = int.from_bytes(header[:4], "big")
                atom_type = header[4:8]
                if size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    size = int.from_bytes(ext, "big")
                elif size == 0:
                    size = path.stat().st_size - offset
                if size < 8:
                    break
                if atom_type == b'moov':
                    return seen_significant_before_moov
                if atom_type != b'ftyp' and atom_type not in _SKIP_ATOMS:
                    seen_significant_before_moov = True
                offset += size
        return True

    @staticmethod
    def _run_ffmpeg_faststart(path: Path):
        import subprocess
        tmp = path.with_suffix(".tmp.mp4")
        cmd = [
            "ffmpeg", "-y", "-i", str(path),
            "-movflags", "+faststart", "-c", "copy",
            str(tmp),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg exit {result.returncode}: {result.stderr.decode(errors='replace')[:300]}")
        tmp.replace(path)

    _CONTAINER_ATOMS = frozenset({
        b'moov', b'trak', b'mdia', b'minf', b'stbl',
        b'edts', b'udta', b'meta', b'moof', b'traf',
        b'mvex', b'sinf', b'schi', b'rinf',
    })

    @staticmethod
    def _fix_chunk_offsets(moov_data: bytes, delta: int) -> bytes:
        buf = bytearray(moov_data)
        VideoService._fix_chunk_offsets_recursive(buf, 8, len(moov_data), delta)
        return bytes(buf)

    @staticmethod
    def _fix_chunk_offsets_recursive(buf: bytearray, start: int, end: int, delta: int):
        offset = start
        while offset < end:
            if offset + 8 > end:
                break
            size = int.from_bytes(buf[offset:offset + 4], "big")
            atom_type = buf[offset + 4:offset + 8]
            header_size = 8
            if size == 1:
                if offset + 16 > end:
                    break
                size = int.from_bytes(buf[offset + 8:offset + 16], "big")
                header_size = 16
            elif size == 0:
                size = end - offset
            if size < 8 or offset + size > end:
                break
            if atom_type == b'stco':
                VideoService._update_stco(buf, offset, delta)
            elif atom_type == b'co64':
                VideoService._update_co64(buf, offset, delta)
            elif atom_type in VideoService._CONTAINER_ATOMS:
                child_start = offset + header_size
                if atom_type == b'meta':
                    child_start = offset + 12
                VideoService._fix_chunk_offsets_recursive(buf, child_start, offset + size, delta)
            offset += size

    @staticmethod
    def _update_stco(buf: bytearray, offset: int, delta: int):
        entry_count_pos = offset + 12
        if entry_count_pos + 4 > len(buf):
            return
        entry_count = int.from_bytes(buf[entry_count_pos:entry_count_pos + 4], "big")
        offsets_start = entry_count_pos + 4
        for i in range(entry_count):
            pos = offsets_start + i * 4
            if pos + 4 > len(buf):
                break
            old_val = int.from_bytes(buf[pos:pos + 4], "big")
            new_val = old_val + delta
            buf[pos:pos + 4] = new_val.to_bytes(4, "big")

    @staticmethod
    def _update_co64(buf: bytearray, offset: int, delta: int):
        entry_count_pos = offset + 12
        if entry_count_pos + 4 > len(buf):
            return
        entry_count = int.from_bytes(buf[entry_count_pos:entry_count_pos + 4], "big")
        offsets_start = entry_count_pos + 4
        for i in range(entry_count):
            pos = offsets_start + i * 8
            if pos + 8 > len(buf):
                break
            old_val = int.from_bytes(buf[pos:pos + 8], "big")
            new_val = old_val + delta
            buf[pos:pos + 8] = new_val.to_bytes(8, "big")

    @staticmethod
    def _python_faststart(path: Path):
        with open(path, "rb") as f:
            data = f.read()

        atoms = []
        offset = 0
        while offset < len(data):
            if offset + 8 > len(data):
                break
            size = int.from_bytes(data[offset:offset + 4], "big")
            atom_type = data[offset + 4:offset + 8]
            if size == 1:
                if offset + 16 > len(data):
                    break
                size = int.from_bytes(data[offset + 8:offset + 16], "big")
            elif size == 0:
                size = len(data) - offset
            if size < 8 or offset + size > len(data):
                break
            atoms.append((atom_type, offset, size))
            offset += size

        ftyp_idx = None
        moov_idx = None
        for i, (atype, _, _) in enumerate(atoms):
            if atype == b'ftyp' and ftyp_idx is None:
                ftyp_idx = i
            if atype == b'moov' and moov_idx is None:
                moov_idx = i

        if moov_idx is None:
            raise ValueError("未找到 moov atom")
        if ftyp_idx is not None and moov_idx == ftyp_idx + 1:
            return
        if ftyp_idx is None and moov_idx == 0:
            return

        moov_off, moov_size = atoms[moov_idx][1], atoms[moov_idx][2]
        moov_data = data[moov_off:moov_off + moov_size]

        moov_data = VideoService._fix_chunk_offsets(moov_data, moov_size)

        other_parts = []
        for i, (atype, aoff, asize) in enumerate(atoms):
            if i != moov_idx:
                other_parts.append(data[aoff:aoff + asize])

        if ftyp_idx is not None:
            adj = ftyp_idx if ftyp_idx < moov_idx else ftyp_idx - 1
            out = b''.join(other_parts[:adj + 1]) + moov_data + b''.join(other_parts[adj + 1:])
        else:
            out = moov_data + b''.join(other_parts)

        with open(path, "wb") as f:
            f.write(out)

    def _parse_json_prompt(self, raw_text: str):
        """解析模型返回的 JSON: {"reasoning": "...", "prompt": "..."}
        
        返回 (prompt, reasoning)。如果 JSON 解析失败，整个文本作为 prompt。
        """
        import json as _json
        import re as _re

        # 1. 尝试直接解析 JSON
        try:
            data = _json.loads(raw_text)
            if isinstance(data, dict):
                return data.get("prompt", "").strip(), data.get("reasoning", "").strip()
        except (_json.JSONDecodeError, ValueError):
            pass

        # 2. 剥离 Markdown 代码块后重试
        cleaned = _re.sub(r'^```(?:json)?\s*', '', raw_text.strip())
        cleaned = _re.sub(r'\s*```$', '', cleaned)
        try:
            data = _json.loads(cleaned)
            if isinstance(data, dict):
                return data.get("prompt", "").strip(), data.get("reasoning", "").strip()
        except (_json.JSONDecodeError, ValueError):
            pass

        # 3. 花括号匹配提取
        brace_matches = list(_re.finditer(r'\{', raw_text))
        if brace_matches:
            start = brace_matches[0].start()
            depth = 0
            end = -1
            for i, ch in enumerate(raw_text[start:], start):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > start:
                try:
                    data = _json.loads(raw_text[start:end])
                    if isinstance(data, dict):
                        return data.get("prompt", "").strip(), data.get("reasoning", "").strip()
                except (_json.JSONDecodeError, ValueError):
                    pass

        # 4. 兜底：整段文本作为 prompt
        logger.warning("[VideoService] JSON 解析失败，原始文本作为 prompt")
        return raw_text.strip(), ""

    def _build_image_description(self, image: dict) -> str:
        """从图片数据库记录中提取关键信息，构建图片描述文本"""
        fields = [
            ("描述", "description"),
            ("服装", "clothing_type"),
            ("风格", "style"),
            ("场景", "scene"),
            ("姿势", "pose_type"),
            ("表情", "expression"),
            ("氛围", "atmosphere"),
            ("关键特征", "key_features"),
            ("道具", "prop_objects"),
            ("身体焦点", "body_focus"),
            ("景别", "shot_size"),
            ("角度", "camera_angle"),
            ("动态", "dynamic_level"),
            ("动作风格", "action_style"),
            ("朝向", "body_orientation"),
            ("构图", "composition"),
            ("背景", "background"),
            ("色调", "color_tone"),
            ("魅力特征", "allure_features"),
            ("暴露特征", "exposure_features"),
        ]
        parts = []
        for label, key in fields:
            val = image.get(key)
            if val:
                if isinstance(val, list):
                    val = ", ".join(str(v) for v in val if v)
                else:
                    val = str(val).strip()
                if val and val.lower() not in ("none", "null", "", "nan"):
                    parts.append(f"{label}：{val}")
        return "\n".join(parts)

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

    def _get_send_umo_path(self) -> Path:
        return self.plugin.data_dir / "video_send_umo.json"

    async def load_send_umo(self) -> dict:
        path = self._get_send_umo_path()
        if path.exists():
            try:
                async with aiofiles.open(path, "r", encoding="utf-8") as f:
                    content = await f.read()
                    data = json.loads(content)
                    if isinstance(data, dict):
                        return data
            except Exception:
                pass
        return {}

    async def save_send_umo(self, umo: str, auto_send: bool = False) -> None:
        self.plugin.data_dir.mkdir(parents=True, exist_ok=True)
        path = self._get_send_umo_path()
        data = {"umo": umo.strip(), "auto_send": auto_send}
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))

    async def _auto_send_video(self, video_id: str, video_path: Path, video_url: str = ""):
        config = await self.load_send_umo()
        umo = config.get("umo", "").strip()
        if not umo:
            logger.debug("[VideoService] 未配置发送会话，跳过自动发送 video_id=%s", video_id)
            return
        result = await self._send_video_to_conversation(umo, video_path, video_id, video_url)
        if result.terminated:
            logger.debug("[VideoService] 自动发送：上传被终止，可能仍在后台进行 video_id=%s", video_id)

    async def send_video_by_id(self, video_id: str) -> VideoSendResult:
        self._ensure_dirs()
        await self.plugin._ensure_db()
        video = await self.plugin.db.get_video(video_id)
        if not video:
            raise ValueError(f"未找到视频: {video_id}")
        if video.get("status") != "done":
            raise ValueError("只有已完成的视频才能发送")
        video_path_str = video.get("video_path", "")
        if not video_path_str:
            raise ValueError("视频文件路径为空")
        video_path = self.videos_dir / video_path_str
        if not video_path.exists():
            raise ValueError("视频文件不存在")
        config = await self.load_send_umo()
        umo = config.get("umo", "").strip()
        if not umo:
            raise ValueError("未配置发送会话，请先在视频设置中配置")
        video_url = video.get("video_url", "")
        return await self._send_video_to_conversation(umo, video_path, video_id, video_url)

    @staticmethod
    def _is_upload_terminated(error: Exception) -> bool:
        error_str = str(error).lower()
        return "retcode=1200" in error_str and "terminated" in error_str

    async def _send_video_to_conversation(self, umo: str, video_path: Path, video_id: str = "", video_url: str = ""):
        from astrbot.api.message_components import Video as VideoComp
        from astrbot.api.event import MessageChain

        _SEND_TIMEOUT = 120
        _URL_SEND_TIMEOUT = 300
        _TERMINATED_GRACE_PERIOD = 30

        file_size_mb = video_path.stat().st_size / 1024 / 1024
        video_comp = VideoComp.fromFileSystem(str(video_path))
        chain = MessageChain(chain=[video_comp])
        send_error = None
        is_terminated = False
        try:
            logger.debug(
                "[VideoService] 尝试文件路径发送 video_id=%s path=%s size=%.1fMB",
                video_id, video_path.name, file_size_mb,
            )
            result = await asyncio.wait_for(
                self.plugin.context.send_message(umo, chain),
                timeout=_SEND_TIMEOUT,
            )
            success = result is not None
        except asyncio.TimeoutError:
            logger.warning(
                "[VideoService] 文件路径发送超时 video_id=%s timeout=%ds size=%.1fMB",
                video_id, _SEND_TIMEOUT, file_size_mb,
            )
            send_error = TimeoutError(f"发送超时({_SEND_TIMEOUT}s)")
            success = False
        except Exception as e:
            is_terminated = self._is_upload_terminated(e)
            if is_terminated:
                logger.warning(
                    "[VideoService] 文件路径发送被终止（上传可能仍在后台进行）"
                    "video_id=%s size=%.1fMB error=%s",
                    video_id, file_size_mb, e,
                )
            else:
                logger.warning(
                    "[VideoService] 文件路径发送异常 video_id=%s error_type=%s error=%s",
                    video_id, type(e).__name__, e,
                )
            send_error = e
            success = False

        if not success and is_terminated:
            logger.debug(
                "[VideoService] 等待 %d 秒（NapCat 可能仍在后台上传）video_id=%s",
                _TERMINATED_GRACE_PERIOD, video_id,
            )
            await asyncio.sleep(_TERMINATED_GRACE_PERIOD)

        if not success and video_url:
            logger.debug(
                "[VideoService] %s，尝试 URL 发送 video_id=%s",
                "文件路径发送被终止" if is_terminated else f"文件路径发送失败（{type(send_error).__name__}）",
                video_id,
            )
            try:
                success = await asyncio.wait_for(
                    self._send_video_as_url(umo, video_url, video_id),
                    timeout=_URL_SEND_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[VideoService] URL 发送超时 video_id=%s timeout=%ds",
                    video_id, _URL_SEND_TIMEOUT,
                )
            except Exception as e_url:
                if self._is_upload_terminated(e_url):
                    logger.warning(
                        "[VideoService] URL 发送也被终止（上传可能仍在后台进行）video_id=%s",
                        video_id,
                    )
                else:
                    logger.warning("[VideoService] URL 发送失败 video_id=%s error=%s", video_id, e_url)

        if not success:
            callback_base = self._get_callback_api_base()
            if callback_base:
                logger.debug(
                    "[VideoService] callback_api_base 已配置，跳过 base64 发送（该模式下 base64 不可用）video_id=%s",
                    video_id,
                )
            else:
                logger.debug("[VideoService] 尝试 base64 发送 video_id=%s", video_id)
                try:
                    success = await asyncio.wait_for(
                        self._send_video_as_base64(umo, video_path, video_id),
                        timeout=_SEND_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning("[VideoService] base64 发送超时 video_id=%s", video_id)
                except Exception as e2:
                    logger.warning("[VideoService] base64 发送失败 video_id=%s error=%s", video_id, e2)

        if not success and video_url:
            logger.debug("[VideoService] 尝试纯文本 URL 兜底 video_id=%s", video_id)
            try:
                from astrbot.api.message_components import Plain
                plain_chain = MessageChain(chain=[Plain(text=video_url)])
                await asyncio.wait_for(
                    self.plugin.context.send_message(umo, plain_chain),
                    timeout=30,
                )
                logger.debug("[VideoService] 纯文本 URL 已发送 video_id=%s", video_id)
            except Exception as e_plain:
                logger.warning("[VideoService] 纯文本 URL 发送也失败 video_id=%s error=%s", video_id, e_plain)

        if success:
            logger.info("[VideoService] 视频已发送到会话 video_id=%s umo=%s", video_id, umo[:50])
            return VideoSendResult(success=True, message="视频已发送")
        else:
            if is_terminated:
                logger.warning(
                    "[VideoService] 视频发送方法均返回终止错误，但上传可能仍在后台进行 "
                    "video_id=%s umo=%s size=%.1fMB "
                    "建议：增大 NapCat 上传超时或检查网络",
                    video_id, umo[:50], file_size_mb,
                )
                return VideoSendResult(
                    success=False,
                    terminated=True,
                    message="视频上传被终止，但可能仍在后台进行，请稍后检查聊天记录",
                )
            else:
                logger.warning(
                    "[VideoService] 视频发送失败（所有方式均失败） video_id=%s umo=%s",
                    video_id, umo[:50],
                )
                return VideoSendResult(success=False, message="视频发送失败")

    async def _send_video_as_url(self, umo: str, video_url: str, video_id: str = "") -> bool:
        from astrbot.api.message_components import Video as VideoComp
        from astrbot.api.event import MessageChain

        if not video_url or not video_url.startswith("http"):
            raise ValueError("视频 URL 为空或非 HTTP 链接")

        video_comp = VideoComp.fromURL(video_url)
        chain = MessageChain(chain=[video_comp])
        result = await self.plugin.context.send_message(umo, chain)
        return result is not None

    async def _send_video_as_base64(self, umo: str, video_path: Path, video_id: str = "") -> bool:
        from astrbot.api.message_components import Video as VideoComp
        from astrbot.api.event import MessageChain

        if not video_path.exists():
            raise ValueError("视频文件不存在")

        file_size = video_path.stat().st_size
        max_size = 50 * 1024 * 1024
        if file_size > max_size:
            raise ValueError(
                f"视频文件过大({file_size / 1024 / 1024:.1f}MB)，"
                f"无法使用 base64 发送(上限50MB)，请配置 callback_api_base"
            )

        import base64

        def _read_and_encode():
            with open(video_path, "rb") as f:
                data = f.read()
            return base64.b64encode(data).decode("ascii")

        b64_data = await asyncio.to_thread(_read_and_encode)
        logger.debug(
            "[VideoService] base64 编码完成 video_id=%s size=%.1fMB b64_len=%d",
            video_id, file_size / 1024 / 1024, len(b64_data),
        )

        video_comp = VideoComp(file=f"base64://{b64_data}")
        chain = MessageChain(chain=[video_comp])
        result = await self.plugin.context.send_message(umo, chain)
        return result is not None

    def _get_callback_api_base(self) -> str:
        try:
            from astrbot.core import astrbot_config
            return str(astrbot_config.get("callback_api_base", "") or "").strip()
        except Exception:
            pass
        return ""

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
