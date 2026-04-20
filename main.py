from pathlib import Path
from typing import Optional
import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import on_llm_tool_respond
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.analyzer import ImageAnalyzer
from .core.database import WardrobeDatabase
from .core.image_store import ImageStore
from .core.searcher import ImageSearcher
from .core.utils import detect_image_mime, mime_to_ext
from .webui import WardrobeWebServer

_MAX_IMAGE_SIZE_MB = 10
_MAX_DESCRIPTION_LEN = 2000
_AIIMG_GENERATE_TOOLS = frozenset({"aiimg_generate"})


@register(
    "astrbot_plugin_wardrobe",
    "Inoryu7z",
    "图片衣柜管理插件，支持智能分类、语义检索和参考图接口",
    "1.6.0",
)
class WardrobePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_wardrobe"
        data_dir.mkdir(parents=True, exist_ok=True)

        self.db = WardrobeDatabase(data_dir)
        self.store = ImageStore(data_dir)
        self.analyzer = ImageAnalyzer(context, plugin=self)
        self.searcher = ImageSearcher(context, self.db, self.store)
        self.data_dir = data_dir
        self._webui: Optional[WardrobeWebServer] = None

        logger.info("[Wardrobe] 插件初始化完成")

    async def _start_webui(self):
        await self._ensure_db()
        try:
            self._webui = WardrobeWebServer(self, self.config)
            await self._webui.start()
        except Exception as e:
            logger.error("[Wardrobe] WebUI 启动失败: %s", e)

    async def terminate(self):
        if self._webui:
            await self._webui.stop()
        logger.info("[Wardrobe] 插件已卸载")

    def get_merged_pools(self) -> dict:
        from .core.pools import ALL_POOLS
        merged = {k: list(v) for k, v in ALL_POOLS.items()}
        custom = self._load_custom_pools()
        for k, v in custom.items():
            if k in merged:
                for item in v:
                    if item not in merged[k]:
                        merged[k].append(item)
            else:
                merged[k] = list(v)
        return merged

    def _load_custom_pools(self) -> dict:
        import json
        path = self.data_dir / "custom_pools.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    async def save_custom_pools(self, merged_pools: dict):
        import json
        from .core.pools import ALL_POOLS
        custom = {}
        for k, v in merged_pools.items():
            default = ALL_POOLS.get(k, [])
            extra = [item for item in v if item not in default]
            if extra or k not in ALL_POOLS:
                custom[k] = extra if k in ALL_POOLS else list(v)
        path = self.data_dir / "custom_pools.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(custom, f, ensure_ascii=False, indent=2)
        logger.info("[Wardrobe] 自定义池子已保存")

    async def _ensure_db(self):
        await self.db.init()

    def _cfg(self, key: str, default=None):
        return self.config.get(key, default)

    @filter.command("存图")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def save_image_command(self, event: AstrMessageEvent, description: str = ""):
        '''保存图片到衣柜库（管理员专用），用法：/存图 [描述或人格名]'''
        persona = self._match_configured_persona(description)
        user_description = "" if persona else description
        result = await self._do_save_image(event, user_description=user_description, persona=persona)
        yield event.plain_result(result)

    @filter.command("删图")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def delete_image_command(self, event: AstrMessageEvent, image_id: str):
        '''删除衣柜库中的图片（管理员专用），用法：/删图 <图片ID>'''
        result = await self._do_delete_image(image_id)
        yield event.plain_result(result)

    @filter.command("衣柜统计")
    async def wardrobe_stats_command(self, event: AstrMessageEvent):
        '''查看衣柜库统计信息'''
        result = await self._do_get_stats()
        yield event.plain_result(result)

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        await self._ensure_db()
        logger.info("[Wardrobe] 数据库已就绪")

        if self._cfg("webui_enabled", False):
            try:
                await self._start_webui()
            except Exception as e:
                logger.error("[Wardrobe] WebUI 启动失败: %s", e)

    @on_llm_tool_respond()
    async def on_aiimg_tool_respond(self, event: AstrMessageEvent, tool, tool_args, tool_result):
        '''AiImg 生图工具调用后的自动存图钩子'''
        await self._auto_save_aiimg_image(event, tool)

    @filter.llm_tool(name="save_wardrobe_image")
    async def save_wardrobe_image_tool(self, event: AstrMessageEvent, user_description: str = "", persona: str = "") -> str:
        '''将用户发送的图片保存到图片衣柜库中。当用户要求保存、收藏、存储图片时调用此工具。系统会自动分析图片内容并生成标签和描述。此工具仅用于保存已有图片，不能生成新图片。

        Args:
            user_description(string): 用户对图片的额外描述（如有），必须原样写入
            persona(string): 当前对话人格名称。如果你正在扮演某个人格角色（如星织、雪音），必须填写你自己的人格名；如果用户提到了其他人格名，也填写该名称；如果当前没有扮演任何人格角色则留空
        '''
        result = await self._do_save_image(event, user_description=user_description, persona=persona)
        return result

    @filter.llm_tool(name="search_wardrobe_image")
    async def search_wardrobe_image_tool(self, event: AstrMessageEvent, query: str, persona: str = "") -> str:
        '''从图片衣柜库中搜索已有的图片并发送给用户。此工具只能搜索和发送衣柜库中已保存的图片，绝对不能用来生成、绘制或创建新图片。当用户想要查看、寻找、获取某类图片，或要求"发一张以前拍过的/存过的图"时调用此工具。例如：有没有洛丽塔发一张看看、发一张甜美一点的衣服来、以前拍过的挂脖的图发一张。

        Args:
            query(string): 用户的图片需求描述
            persona(string): 当前对话人格名称。如果你正在扮演某个人格角色（如星织、雪音），必须填写你自己的人格名；如果用户提到了其他人格名（如"雪音有没有xxx"），也填写该名称；如果当前没有扮演任何人格角色则留空
        '''
        return await self._do_search_image(event, query=query, persona=persona)

    async def _do_save_image(
        self, event: AstrMessageEvent, user_description: str = "", persona: str = ""
    ) -> str:
        image_bytes = await self._extract_image_bytes(event)
        if not image_bytes:
            return "未检测到图片，请发送图片后再保存"

        persona = self._resolve_persona(persona)
        logger.info("[Wardrobe] 开始存图，图片大小=%.2fKB 人格=%s", len(image_bytes) / 1024, persona or "无")

        created_by = str(event.get_sender_id() or "")
        image_id, attrs = await self._save_image_from_bytes(
            image_bytes, persona=persona, created_by=created_by, user_description=user_description
        )

        if not image_id:
            primary = str(self._cfg("save_provider_id", "") or "").strip()
            secondary = str(self._cfg("save_secondary_provider_id", "") or "").strip()
            if not primary and not secondary:
                return "未配置存图模型，请在插件设置中配置"
            return "图片保存失败"

        if not attrs:
            return f"图片已保存（ID: {image_id}），但模型分析失败，仅保存了原始图片"

        logger.info(
            "[Wardrobe] 分析结果:\n  分类: %s\n  风格: %s\n  服装: %s\n  暴露: %s\n  场景: %s\n  氛围: %s\n  姿势: %s\n  表情: %s\n  景别: %s\n  角度: %s\n  描述: %s",
            attrs.get("category", "人物"),
            ", ".join(attrs.get("style", [])),
            attrs.get("clothing_type", ""),
            attrs.get("exposure_level", ""),
            ", ".join(attrs.get("scene", [])),
            ", ".join(attrs.get("atmosphere", [])),
            attrs.get("pose_type", ""),
            attrs.get("expression", ""),
            attrs.get("shot_size", ""),
            attrs.get("camera_angle", ""),
            attrs.get("description", ""),
        )

        feedback_enabled = bool(self._cfg("save_feedback_enabled", False))
        if feedback_enabled:
            return self._format_save_feedback(image_id, attrs)

        return f"图片已保存到衣柜库（ID: {image_id}）"

    async def _save_image_from_bytes(
        self,
        image_bytes: bytes,
        *,
        persona: str = "",
        created_by: str = "",
        user_description: str = "",
    ) -> tuple:
        await self._ensure_db()

        max_size = int(self._cfg("max_image_size_mb", _MAX_IMAGE_SIZE_MB) or _MAX_IMAGE_SIZE_MB)
        if len(image_bytes) > max_size * 1024 * 1024:
            logger.warning("[Wardrobe] 图片过大 (%.1fMB)", len(image_bytes) / 1024 / 1024)
            return None, None

        if user_description and len(user_description) > _MAX_DESCRIPTION_LEN:
            user_description = user_description[:_MAX_DESCRIPTION_LEN]

        primary = str(self._cfg("save_provider_id", "") or "").strip()
        secondary = str(self._cfg("save_secondary_provider_id", "") or "").strip()
        timeout = float(self._cfg("save_timeout_seconds", 60.0) or 60.0)

        if not primary and not secondary:
            return None, None

        attrs = await self.analyzer.analyze_image(
            image_bytes,
            user_description=user_description,
            primary_provider_id=primary,
            secondary_provider_id=secondary,
            timeout_seconds=timeout,
        )

        if not attrs:
            logger.warning("[Wardrobe] 模型分析失败，无返回结果")
            filename = await self.store.save_image(image_bytes)
            image_id = await self.db.add_image(
                category="人物",
                style=[],
                clothing_type="",
                exposure_level="",
                scene=[],
                atmosphere=[],
                pose_type="",
                body_orientation="",
                dynamic_level="",
                action_style=[],
                shot_size="",
                camera_angle="",
                expression="",
                color_tone="",
                composition="",
                background="",
                description=user_description or "模型分析失败，无描述",
                user_tags=user_description,
                image_path=filename,
                created_by=created_by,
                persona=persona,
            )
            return image_id, None

        category = attrs.get("category", "人物")
        if category not in ("人物", "衣服"):
            category = "人物"

        def _ensure_list(v):
            if isinstance(v, list):
                return v
            if isinstance(v, str) and v:
                return [v]
            return []

        def _ensure_str(v):
            if isinstance(v, str):
                return v
            if isinstance(v, list) and v:
                return v[0]
            return ""

        filename = await self.store.save_image(image_bytes)

        image_id = await self.db.add_image(
            category=category,
            style=_ensure_list(attrs.get("style")),
            clothing_type=_ensure_str(attrs.get("clothing_type")),
            exposure_level=_ensure_str(attrs.get("exposure_level")),
            scene=_ensure_list(attrs.get("scene")),
            atmosphere=_ensure_list(attrs.get("atmosphere")),
            pose_type=_ensure_str(attrs.get("pose_type")),
            body_orientation=_ensure_str(attrs.get("body_orientation")),
            dynamic_level=_ensure_str(attrs.get("dynamic_level")),
            action_style=_ensure_list(attrs.get("action_style")),
            shot_size=_ensure_str(attrs.get("shot_size")),
            camera_angle=_ensure_str(attrs.get("camera_angle")),
            expression=_ensure_str(attrs.get("expression")),
            color_tone=_ensure_str(attrs.get("color_tone")),
            composition=_ensure_str(attrs.get("composition")),
            background=_ensure_str(attrs.get("background")),
            description=_ensure_str(attrs.get("description")),
            user_tags=user_description,
            image_path=filename,
            created_by=created_by,
            persona=persona,
        )

        return image_id, attrs

    async def _auto_save_aiimg_image(self, event: AstrMessageEvent, tool):
        enabled = self._cfg("auto_save_aiimg_enabled")
        if enabled is None:
            enabled = self._cfg("auto_save_gitee_enabled", False)
        if not enabled:
            return

        tool_name = getattr(tool, "name", "") or ""
        if tool_name not in _AIIMG_GENERATE_TOOLS:
            return

        star = self.context.get_registered_star("astrbot_plugin_aiimg")
        if not star or not star.activated or not star.star_cls:
            return
        instance = star.star_cls

        user_id = str(event.get_sender_id() or "")
        last_image_dict = getattr(instance, "_last_image_by_user", None)
        if not last_image_dict:
            return

        image_path = last_image_dict.get(user_id)
        if not image_path:
            return

        path = Path(image_path)
        if not path.exists():
            return

        persona = str(self._cfg("auto_save_aiimg_persona") or "").strip()
        if not persona:
            persona = str(self._cfg("auto_save_gitee_persona", "") or "").strip()
        persona = self._resolve_persona(persona)

        try:
            import aiofiles
            async with aiofiles.open(path, "rb") as f:
                image_bytes = await f.read()

            if not image_bytes:
                return

            logger.info(
                "[Wardrobe] AiImg 自动存图开始，图片大小=%.2fKB 人格=%s tool=%s",
                len(image_bytes) / 1024, persona or "无", tool_name,
            )

            image_id, attrs = await self._save_image_from_bytes(
                image_bytes, persona=persona, created_by=user_id,
            )

            if image_id:
                if attrs:
                    logger.info(
                        "[Wardrobe] AiImg 自动存图完成 ID=%s 分类=%s 描述=%s",
                        image_id, attrs.get("category", ""),
                        attrs.get("description", "")[:100],
                    )
                else:
                    logger.info("[Wardrobe] AiImg 自动存图完成（分析失败）ID=%s", image_id)
            else:
                logger.warning("[Wardrobe] AiImg 自动存图失败")

        except Exception as e:
            logger.error("[Wardrobe] AiImg 自动存图异常: %s", e)

    async def _do_delete_image(self, image_id: str) -> str:
        await self._ensure_db()

        image = await self.db.get_image(image_id)
        if not image:
            return f"未找到ID为 {image_id} 的图片"

        deleted = await self.db.delete_image(image_id)
        if not deleted:
            return f"删除失败（ID: {image_id}）"

        if image.get("image_path"):
            await self.store.delete_image(image["image_path"])

        return f"已删除图片（ID: {image_id}）"

    async def _do_get_stats(self) -> str:
        await self._ensure_db()

        stats = await self.db.get_stats()
        lines = [f"衣柜库共有 {stats['total']} 张图片"]

        by_category = stats.get("by_category", {})
        if by_category:
            cat_parts = [f"{k}: {v}" for k, v in by_category.items()]
            lines.append(f"分类：{', '.join(cat_parts)}")

        by_exposure = stats.get("by_exposure", {})
        if by_exposure:
            exp_parts = [f"{k}: {v}" for k, v in by_exposure.items()]
            lines.append(f"暴露程度：{', '.join(exp_parts)}")

        return "\n".join(lines)

    async def get_reference_image(
        self, query: str, current_persona: str = ""
    ) -> Optional[dict]:
        await self._ensure_db()

        primary = str(self._cfg("search_provider_id", "") or "").strip()
        secondary = str(self._cfg("search_secondary_provider_id", "") or "").strip()
        timeout = float(self._cfg("search_timeout_seconds", 30.0) or 30.0)
        candidate_limit = int(self._cfg("search_candidate_limit", 20) or 20)

        if not primary and not secondary:
            logger.warning("[Wardrobe] 参考图搜索：未配置取图模型")
            return None

        resolved_persona = self._resolve_persona(current_persona)
        persona_names = str(self._cfg("persona_names", "") or "").strip()

        logger.info(
            "[Wardrobe] 参考图搜索: query=%s exclude_persona=%s",
            query, resolved_persona or "无",
        )

        results, search_meta = await self.searcher.search(
            query,
            primary_provider_id=primary,
            secondary_provider_id=secondary,
            timeout_seconds=timeout,
            candidate_limit=candidate_limit,
            max_select=1,
            persona="",
            persona_names=persona_names,
            current_persona=resolved_persona,
            exclude_current_persona=True,
        )

        if not results:
            logger.info("[Wardrobe] 参考图搜索：未找到匹配图片（已排除当前人格）")
            return None

        best = results[0]
        image_path = self.store.get_image_path(best["image_path"])
        if not image_path.exists():
            logger.warning("[Wardrobe] 参考图搜索：图片文件不存在 %s", best["image_path"])
            return None

        logger.info(
            "[Wardrobe] 参考图搜索完成: ID=%s 描述=%s",
            best["id"], best.get("description", "")[:100],
        )

        return {
            "image_path": str(image_path),
            "description": best.get("description", ""),
            "persona": best.get("persona", ""),
            "image_id": best["id"],
        }

    async def _do_search_image(
        self, event: AstrMessageEvent, query: str, persona: str = ""
    ) -> str:
        await self._ensure_db()

        raw_persona = persona.strip()
        resolved_persona = self._resolve_persona(raw_persona)
        current_persona = resolved_persona or raw_persona
        persona_names = str(self._cfg("persona_names", "") or "").strip()

        primary = str(self._cfg("search_provider_id", "") or "").strip()
        secondary = str(self._cfg("search_secondary_provider_id", "") or "").strip()
        timeout = float(self._cfg("search_timeout_seconds", 30.0) or 30.0)
        candidate_limit = int(self._cfg("search_candidate_limit", 20) or 20)
        max_select = int(self._cfg("search_max_select", 1) or 1)

        if not primary and not secondary:
            return "未配置取图模型，请在插件设置中配置"

        logger.info(
            "[Wardrobe] 取图请求: query=%s current_persona=%s resolved=%s",
            query, current_persona or "无", resolved_persona or "无",
        )

        results, search_meta = await self.searcher.search(
            query,
            primary_provider_id=primary,
            secondary_provider_id=secondary,
            timeout_seconds=timeout,
            candidate_limit=candidate_limit,
            max_select=max_select,
            persona=resolved_persona,
            persona_names=persona_names,
            current_persona=current_persona,
        )

        logger.info(
            "[Wardrobe] 取图结果: %d张 scope=%s mismatch=%s searched_persona=%s",
            len(results),
            search_meta.get("persona_scope", "?"),
            search_meta.get("persona_mismatch", False),
            search_meta.get("searched_persona", "?"),
        )

        if not results:
            return "没有找到匹配的图片"

        image_paths = []
        for r in results:
            path = self.store.get_image_path(r["image_path"])
            if path.exists():
                image_paths.append(str(path))

        if not image_paths:
            return "图片文件不存在"

        chain = [Image.fromFileSystem(path=p) for p in image_paths]
        mc = event.chain_result(chain)
        await self.context.send_message(event.unified_msg_origin, mc)

        parts = [f"已发送 {len(image_paths)} 张匹配的图片"]

        for i, r in enumerate(results[:3], 1):
            desc = r.get("description", "")
            img_persona = r.get("persona", "")
            if desc:
                parts.append(f"图片{i}描述：{desc[:200]}")

        if search_meta.get("persona_mismatch") and current_persona:
            scope = search_meta.get("persona_scope", "global")
            if scope == "self":
                parts.append(
                    f"注意：在{current_persona}的图库中未找到匹配图片，"
                    f"以下图片来自其他图库，并非{current_persona}本人的照片。"
                    f"请在回复时向用户说明这一点。"
                )
            elif scope == "named":
                named = search_meta.get("searched_persona", "")
                if named:
                    parts.append(
                        f"注意：在指定人格「{named}」的图库中未找到匹配图片，"
                        f"以下图片来自其他图库。请在回复时向用户说明。"
                    )

        return "\n".join(parts)

    async def _extract_image_bytes(self, event: AstrMessageEvent) -> Optional[bytes]:
        message_obj = getattr(event, "message_obj", None)
        if not message_obj:
            return None

        message_chain = getattr(message_obj, "message", [])
        if not message_chain:
            return None

        for comp in message_chain:
            if isinstance(comp, Image):
                image_url = getattr(comp, "url", None) or getattr(comp, "path", None)
                if image_url:
                    return await self._download_or_read_image(image_url)

        return None

    async def _download_or_read_image(self, source: str) -> Optional[bytes]:
        source = str(source or "").strip()
        if not source:
            return None

        if source.startswith(("http://", "https://")):
            try:
                import httpx
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                    resp = await client.get(source)
                    if resp.status_code == 200:
                        return resp.content
            except Exception as e:
                logger.warning("[Wardrobe] 下载图片失败: %s", e)
            return None

        if source.startswith("file:///"):
            source = source[7:]

        path = Path(source)
        if path.exists():
            import aiofiles
            async with aiofiles.open(path, "rb") as f:
                return await f.read()

        return None

    def _match_configured_persona(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        configured = str(self._cfg("persona_names", "") or "").strip()
        if not configured:
            return ""
        for entry in self._split_persona_entries(configured):
            entry = entry.strip()
            if not entry:
                continue
            canonical, aliases = self._parse_persona_entry(entry)
            if text == canonical or text in aliases:
                return canonical
        return ""

    def _resolve_persona(self, persona: str) -> str:
        persona = persona.strip()
        if not persona:
            return ""
        configured = str(self._cfg("persona_names", "") or "").strip()
        if not configured:
            return persona
        for entry in self._split_persona_entries(configured):
            entry = entry.strip()
            if not entry:
                continue
            canonical, aliases = self._parse_persona_entry(entry)
            if persona == canonical or persona in aliases:
                return canonical
        return persona

    @staticmethod
    def _split_persona_entries(configured: str) -> list[str]:
        text = configured.replace("，", ",")
        entries = []
        current = ""
        depth = 0
        for ch in text:
            if ch in ("[", "［", "（", "("):
                depth += 1
                current += ch
            elif ch in ("]", "］", "）", ")"):
                depth = max(0, depth - 1)
                current += ch
            elif ch == "," and depth == 0:
                entries.append(current)
                current = ""
            else:
                current += ch
        if current.strip():
            entries.append(current)
        return entries

    @staticmethod
    def _parse_persona_entry(entry: str) -> tuple[str, list[str]]:
        import re
        m = re.match(r'^(.+?)[\[［](.+?)[\]］]\s*$', entry)
        if m:
            canonical = m.group(1).strip()
            aliases = [a.strip() for a in m.group(2).replace("，", ",").split(",") if a.strip()]
            return canonical, aliases
        return entry.strip(), []

    @staticmethod
    def _format_save_feedback(image_id: str, attrs: dict) -> str:
        lines = [f"图片已保存（ID: {image_id}）"]
        lines.append(f"分类：{attrs.get('category', '未知')}")
        style = attrs.get("style", [])
        if style:
            lines.append(f"风格：{', '.join(style)}")
        clothing = attrs.get("clothing_type", "")
        if clothing:
            lines.append(f"服装类型：{clothing}")
        exposure = attrs.get("exposure_level", "")
        if exposure:
            lines.append(f"暴露程度：{exposure}")
        scene = attrs.get("scene", [])
        if scene:
            lines.append(f"场景：{', '.join(scene)}")
        atmosphere = attrs.get("atmosphere", [])
        if atmosphere:
            lines.append(f"氛围：{', '.join(atmosphere)}")
        desc = attrs.get("description", "")
        if desc:
            lines.append(f"描述：{desc}")

        category = attrs.get("category", "")
        if category == "人物":
            pose = attrs.get("pose_type", "")
            if pose:
                lines.append(f"姿势：{pose}")
            expr = attrs.get("expression", "")
            if expr:
                lines.append(f"表情：{expr}")
            shot = attrs.get("shot_size", "")
            if shot:
                lines.append(f"景别：{shot}")
            angle = attrs.get("camera_angle", "")
            if angle:
                lines.append(f"角度：{angle}")

        return "\n".join(lines)
