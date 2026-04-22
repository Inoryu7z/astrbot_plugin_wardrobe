from pathlib import Path
from typing import Optional
import asyncio
import hashlib

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
from .core.utils import detect_image_mime, ensure_list, ensure_str, mime_to_ext
from .webui import WardrobeWebServer

try:
    from .core.vector_searcher import WardrobeVectorSearcher
    from astrbot.core.provider.provider import EmbeddingProvider
    from astrbot.core.provider.provider import RerankProvider
    _VEC_AVAILABLE = True
except ImportError:
    _VEC_AVAILABLE = False

_MAX_IMAGE_SIZE_MB = 10
_MAX_DESCRIPTION_LEN = 2000
# 仅监听 aiimg_generate 这一个统一入口工具。
# aiimg_draw / aiimg_edit 内部最终都走 aiimg_generate，所以只需监听这一个即可覆盖所有 LLM 工具调用路径。
# 命令路径（/自拍 /aiimg 等）则由 on_after_message_sent 钩子兜底。
_AIIMG_GENERATE_TOOLS = frozenset({"aiimg_generate"})


@register(
    "astrbot_plugin_wardrobe",
    "Inoryu7z",
    "图片衣柜管理插件，支持智能分类、语义检索和参考图接口",
    "2.2.4",
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
        self.vector_searcher = self._init_vector_searcher(data_dir)
        self.rerank_provider = self._init_rerank_provider()
        if self.vector_searcher and self.rerank_provider:
            self.vector_searcher.rerank_provider = self.rerank_provider
        self.searcher = ImageSearcher(context, self.db, self.store, vector_searcher=self.vector_searcher)
        self.data_dir = data_dir
        self._db_initialized = False
        self._db_init_event = asyncio.Event()
        self._db_init_event.set()
        self._webui: Optional[WardrobeWebServer] = None
        self._last_auto_saved: dict[str, str] = {}
        self._bg_tasks: set[asyncio.Task] = set()

        self.context._wardrobe_plugin = self

        logger.info("[Wardrobe] 插件初始化完成")

    async def _start_webui(self):
        await self._ensure_db()
        try:
            self._webui = WardrobeWebServer(self, self.config)
            await self._webui.start()
        except Exception as e:
            logger.error("[Wardrobe] WebUI 启动失败: %s", e)

    def _init_vector_searcher(self, data_dir):
        if not _VEC_AVAILABLE:
            return None
        try:
            emb_id = self._cfg("embedding_provider_id", "")
            embedding_provider = None
            if emb_id:
                provider = self.context.get_provider_by_id(emb_id)
                if provider and isinstance(provider, EmbeddingProvider):
                    embedding_provider = provider
                    logger.info("[Wardrobe] 使用配置的 Embedding Provider: %s", emb_id)
            if not embedding_provider:
                try:
                    embedding_providers = self.context.get_all_embedding_providers()
                    if embedding_providers:
                        embedding_provider = embedding_providers[0]
                        logger.info("[Wardrobe] 使用默认 Embedding Provider")
                except Exception:
                    pass
            if not embedding_provider:
                logger.info("[Wardrobe] 无可用 Embedding Provider，向量检索已禁用")
                return None
            vs = WardrobeVectorSearcher(str(data_dir), embedding_provider=embedding_provider, db=self.db, plugin=self)
            return vs
        except Exception as e:
            logger.warning("[Wardrobe] 向量检索器初始化失败: %s", e)
            return None

    def _init_rerank_provider(self):
        if not _VEC_AVAILABLE:
            return None
        try:
            rerank_id = self._cfg("rerank_provider_id", "")
            if not rerank_id:
                return None
            provider = self.context.get_provider_by_id(rerank_id)
            if provider and isinstance(provider, RerankProvider):
                logger.info("[Wardrobe] 使用配置的 Rerank Provider: %s", rerank_id)
                return provider
            logger.warning("[Wardrobe] Rerank Provider '%s' 未找到或类型不匹配", rerank_id)
            return None
        except Exception as e:
            logger.warning("[Wardrobe] Rerank Provider 初始化失败: %s", e)
            return None

    async def terminate(self):
        if self._webui:
            await self._webui.stop()
        if self.vector_searcher:
            await self.vector_searcher.terminate()
        logger.info("[Wardrobe] 插件已卸载")

    async def get_merged_pools(self) -> dict:
        from .core.pools import ALL_POOLS
        merged = {k: list(v) for k, v in ALL_POOLS.items()}
        custom = await self._load_custom_pools()
        for k, v in custom.items():
            if k in merged:
                for item in v:
                    if item not in merged[k]:
                        merged[k].append(item)
            else:
                merged[k] = list(v)
        return merged

    async def _load_custom_pools(self) -> dict:
        import json
        path = self.data_dir / "custom_pools.json"
        if path.exists():
            try:
                data = await asyncio.to_thread(self._read_custom_pools_file, path)
                if not isinstance(data, dict):
                    return {}
                for k, v in data.items():
                    if not isinstance(v, list):
                        data[k] = []
                return data
            except Exception:
                pass
        return {}

    @staticmethod
    def _read_custom_pools_file(path: Path):
        import json
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

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
        content = json.dumps(custom, ensure_ascii=False, indent=2)
        await asyncio.to_thread(self._write_custom_pools_file, path, content)
        logger.info("[Wardrobe] 自定义池子已保存")

    @staticmethod
    def _write_custom_pools_file(path: Path, content: str):
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    async def _ensure_db(self):
        if self._db_initialized:
            return
        if not self._db_init_event.is_set():
            await self._db_init_event.wait()
            return
        self._db_init_event.clear()
        try:
            await self.db.init()
            self._db_initialized = True
            if not self.vector_searcher:
                self.vector_searcher = self._init_vector_searcher(self.data_dir)
                if self.vector_searcher:
                    self.searcher.vector_searcher = self.vector_searcher
                    logger.info("[Wardrobe] 向量检索器延迟初始化成功")
            if not self.rerank_provider:
                self.rerank_provider = self._init_rerank_provider()
                if self.vector_searcher and self.rerank_provider:
                    self.vector_searcher.rerank_provider = self.rerank_provider
            if self.vector_searcher and not self.vector_searcher._initialized:
                await self.vector_searcher.initialize()
                if self.vector_searcher.available:
                    await self.vector_searcher.index_existing_images()
            self._spawn_bg_task(self._reanalyze_old_images())
            self._spawn_bg_task(self._backfill_file_hashes())
        finally:
            self._db_init_event.set()

    async def _reanalyze_old_images(self):
        try:
            records = await self.db.get_all_records()
            need_reanalyze = []
            need_ref_strength_backfill = []
            for rec in records:
                exp = rec.get("exposure_features", [])
                key = rec.get("key_features", [])
                prop = rec.get("prop_objects", [])
                allure = rec.get("allure_features", [])
                bf = rec.get("body_focus", [])
                rs = rec.get("ref_strength", "style")
                rs_reason = rec.get("ref_strength_reason", "")
                if (isinstance(exp, list) and not exp) and (isinstance(key, list) and not key) and (isinstance(prop, list) and not prop) and (isinstance(allure, list) and not allure) and (isinstance(bf, list) and not bf):
                    need_reanalyze.append(rec)
                elif rec.get("category", "") == "人物" and not rs_reason:
                    has_features = (isinstance(exp, list) and exp) or (isinstance(key, list) and key) or (isinstance(prop, list) and prop) or (isinstance(allure, list) and allure) or (isinstance(bf, list) and bf)
                    if has_features:
                        need_ref_strength_backfill.append(rec)

            if not need_reanalyze and not need_ref_strength_backfill:
                return

            if need_reanalyze:
                logger.info("[Wardrobe] 发现 %d 张旧图需要补充分析新字段，开始逐张重分析...", len(need_reanalyze))

            if need_ref_strength_backfill:
                logger.info("[Wardrobe] 发现 %d 张人物图需要回填 ref_strength，开始逐张重分析...", len(need_ref_strength_backfill))

            primary = str(self._cfg("save_provider_id", "") or "").strip()
            secondary = str(self._cfg("save_secondary_provider_id", "") or "").strip()
            timeout = float(self._cfg("save_timeout_seconds", 60.0) or 60.0)

            if not primary and not secondary:
                logger.info("[Wardrobe] 未配置存图模型，跳过旧图重分析")
                return

            success = 0
            failed = 0
            for i, rec in enumerate(need_reanalyze):
                image_id = rec.get("id", "")
                image_path_str = rec.get("image_path", "")
                if not image_path_str:
                    continue

                path = self.store.get_image_path(image_path_str)
                if not path.exists():
                    logger.debug("[Wardrobe] 旧图重分析跳过：文件不存在 id=%s", image_id)
                    continue

                try:
                    import aiofiles
                    async with aiofiles.open(path, "rb") as f:
                        image_bytes = await f.read()

                    if not image_bytes:
                        continue

                    attrs = await self.analyzer.analyze_image(
                        image_bytes,
                        primary_provider_id=primary,
                        secondary_provider_id=secondary,
                        timeout_seconds=timeout,
                    )

                    if not attrs:
                        failed += 1
                        logger.warning("[Wardrobe] 旧图重分析失败 id=%s", image_id)
                        continue

                    update_data = {}
                    for field in ("exposure_features", "key_features", "prop_objects", "allure_features", "body_focus"):
                        val = ensure_list(attrs.get(field))
                        update_data[field] = val

                    for field in ("style", "scene", "atmosphere", "action_style",
                                  "clothing_type", "exposure_level", "pose_type",
                                  "body_orientation", "dynamic_level", "shot_size",
                                  "camera_angle", "expression", "color_tone",
                                  "composition", "background", "description", "category", "ref_strength", "ref_strength_reason"):
                        val = attrs.get(field)
                        if val is not None:
                            if isinstance(val, list):
                                update_data[field] = val
                            else:
                                update_data[field] = str(val)

                    await self.db.update_image(image_id, **update_data)

                    if self.vector_searcher and self.vector_searcher.available:
                        desc = str(attrs.get("description", rec.get("description", "")))
                        tags = rec.get("user_tags", "")
                        await self._index_to_vector(
                            image_id, desc, tags,
                            exposure_features=ensure_list(attrs.get("exposure_features")),
                            key_features=ensure_list(attrs.get("key_features")),
                            prop_objects=ensure_list(attrs.get("prop_objects")),
                            allure_features=ensure_list(attrs.get("allure_features")),
                            body_focus=ensure_list(attrs.get("body_focus")),
                            category=str(attrs.get("category", rec.get("category", ""))),
                            persona=rec.get("persona", ""),
                        )

                    success += 1
                    logger.info("[Wardrobe] 旧图重分析进度: %d/%d (成功%d 失败%d)", i + 1, len(need_reanalyze), success, failed)

                    if i < len(need_reanalyze) - 1:
                        await asyncio.sleep(2)

                except Exception as e:
                    failed += 1
                    logger.warning("[Wardrobe] 旧图重分析异常 id=%s error=%s", image_id, e)

            logger.info("[Wardrobe] 旧图重分析完成: 成功%d 失败%d 共%d张", success, failed, len(need_reanalyze))

            if need_ref_strength_backfill:
                rs_success = 0
                rs_failed = 0
                for i, rec in enumerate(need_ref_strength_backfill):
                    image_id = rec.get("id", "")
                    image_path_str = rec.get("image_path", "")
                    if not image_path_str:
                        continue

                    path = self.store.get_image_path(image_path_str)
                    if not path.exists():
                        continue

                    try:
                        import aiofiles
                        async with aiofiles.open(path, "rb") as f:
                            image_bytes = await f.read()

                        if not image_bytes:
                            continue

                        attrs = await self.analyzer.analyze_image(
                            image_bytes,
                            primary_provider_id=primary,
                            secondary_provider_id=secondary,
                            timeout_seconds=timeout,
                        )

                        if not attrs:
                            rs_failed += 1
                            continue

                        new_rs = ensure_str(attrs.get("ref_strength", "style"))
                        new_reason = ensure_str(attrs.get("ref_strength_reason", ""))
                        if new_rs in ("full", "style", "reimagine"):
                            await self.db.update_image(image_id, ref_strength=new_rs, ref_strength_reason=new_reason)
                            rs_success += 1
                        else:
                            rs_failed += 1

                        if i < len(need_ref_strength_backfill) - 1:
                            await asyncio.sleep(2)

                    except Exception as e:
                        rs_failed += 1
                        logger.warning("[Wardrobe] ref_strength 回填异常 id=%s error=%s", image_id, e)

                logger.info("[Wardrobe] ref_strength 回填完成: 成功%d 失败%d 共%d张", rs_success, rs_failed, len(need_ref_strength_backfill))
        except Exception as e:
            logger.error("[Wardrobe] 旧图重分析任务异常: %s", e, exc_info=True)

    async def _backfill_file_hashes(self):
        try:
            records = await self.db.get_all_records()
            need_backfill = []
            for rec in records:
                if not rec.get("file_hash", "").strip():
                    need_backfill.append(rec)

            if not need_backfill:
                return

            logger.info("[Wardrobe] 发现 %d 张图片缺少文件哈希，开始回填...", len(need_backfill))

            backfilled = 0
            for rec in need_backfill:
                image_id = rec.get("id", "")
                image_path_str = rec.get("image_path", "")
                if not image_path_str:
                    continue

                path = self.store.get_image_path(image_path_str)
                if not path.exists():
                    continue

                try:
                    import aiofiles
                    async with aiofiles.open(path, "rb") as f:
                        image_bytes = await f.read()

                    if not image_bytes:
                        continue

                    file_hash = hashlib.md5(image_bytes).hexdigest()
                    await self.db.update_image(image_id, file_hash=file_hash)
                    backfilled += 1
                except Exception as e:
                    logger.debug("[Wardrobe] 回填哈希失败 id=%s error=%s", image_id, e)

            logger.info("[Wardrobe] 文件哈希回填完成: %d/%d", backfilled, len(need_backfill))
        except Exception as e:
            logger.error("[Wardrobe] 文件哈希回填任务异常: %s", e, exc_info=True)

    def _cfg(self, key: str, default=None):
        return self.config.get(key, default)

    def _spawn_bg_task(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def _index_to_vector(self, image_id: str, description: str, user_tags: str,
                                exposure_features: list | None = None,
                                key_features: list | None = None,
                                prop_objects: list | None = None,
                                allure_features: list | None = None,
                                body_focus: list | None = None,
                                category: str = "", persona: str = ""):
        if not self.vector_searcher or not self.vector_searcher.available:
            return
        text_parts = []
        if description:
            text_parts.append(description)
        if user_tags:
            text_parts.append(f"标签: {user_tags}")
        if exposure_features:
            text_parts.append(f"暴露特征: {' '.join(str(v) for v in exposure_features if v)}")
        if key_features:
            text_parts.append(f"关键特征: {' '.join(str(v) for v in key_features if v)}")
        if prop_objects:
            text_parts.append(f"道具: {' '.join(str(v) for v in prop_objects if v)}")
        if allure_features:
            text_parts.append(f"魅力特征: {' '.join(str(v) for v in allure_features if v)}")
        if body_focus:
            text_parts.append(f"身体焦点: {' '.join(str(v) for v in body_focus if v)}")
        text = " ".join(text_parts)
        if not text.strip():
            return
        try:
            await self.vector_searcher.add_image(
                wardrobe_id=image_id,
                text=text,
                category=category,
                persona=persona,
            )
        except Exception as e:
            logger.debug("[Wardrobe] 向量索引添加失败: %s", e)

    @filter.command("存图")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def save_image_command(self, event: AstrMessageEvent, description: str = ""):
        '''保存图片到衣柜库（管理员专用），用法：/存图 [人格名] [描述]'''
        persona = ""
        user_description = ""
        text = description.strip()
        if text:
            parts = text.split(None, 1)
            first_word = parts[0]
            matched = self._match_configured_persona(first_word)
            if matched:
                persona = matched
                user_description = parts[1].strip() if len(parts) > 1 else ""
            else:
                user_description = text
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

    async def initialize(self):
        await self._ensure_db()
        logger.info("[Wardrobe] 数据库已就绪")

        if self._cfg("webui_enabled", False) and not self._webui:
            try:
                await self._start_webui()
            except Exception as e:
                logger.error("[Wardrobe] WebUI 启动失败: %s", e)

    @on_llm_tool_respond()
    async def on_aiimg_tool_respond(self, event: AstrMessageEvent, tool, tool_args, tool_result):
        '''AiImg 生图工具调用后的自动存图钩子'''
        await self._auto_save_aiimg_image(event, tool)

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        '''消息发送后钩子：检测 AiImg 命令方式生成的图片并自动存图'''
        await self._auto_save_aiimg_image(event, tool=None)

    @filter.llm_tool(name="save_wardrobe_image")
    async def save_wardrobe_image_tool(self, event: AstrMessageEvent, user_description: str = "", persona: str = "") -> str:
        '''将用户发送的图片保存到图片衣柜库中。当用户要求保存、收藏、存储图片时调用此工具。系统会自动分析图片内容并生成标签和描述。此工具仅用于保存已有图片，不能生成新图片。

        Args:
            user_description(string): 用户对图片的额外描述（如有），必须原样写入
            persona(string): 当前对话人格名称。如果你正在扮演某个人格角色（如星织、雪音），必须填写你自己的人格名；如果用户提到了其他人格名，也填写该名称；如果当前没有扮演任何人格角色则留空
        '''
        if not persona.strip():
            auto_persona = await self._get_current_persona_name(event)
            if auto_persona:
                persona = auto_persona
        result = await self._do_save_image(event, user_description=user_description, persona=persona)
        return result

    @filter.llm_tool(name="search_wardrobe_image")
    async def search_wardrobe_image_tool(self, event: AstrMessageEvent, query: str, persona: str = "") -> str:
        '''从图片衣柜库中搜索已有的图片并发送给用户。此工具只能搜索和发送衣柜库中已保存的图片，绝对不能用来生成、绘制或创建新图片。当用户想要查看、寻找、获取某类图片，或要求"发一张以前拍过的/存过的图"时调用此工具。例如：有没有洛丽塔发一张看看、发一张甜美一点的衣服来、以前拍过的挂脖的图发一张。

        Args:
            query(string): 用户的图片需求描述，必须使用自然语言完整表达用户的意图，不要拆成关键词。例如用户说"色气的jk服"，就填"色气的jk服"，不要填"jk服 色气"。
            persona(string): 当前对话人格名称。如果你正在扮演某个人格角色（如星织、雪音），必须填写你自己的人格名；如果用户提到了其他人格名（如"雪音有没有xxx"），也填写该名称；如果当前没有扮演任何人格角色则留空
        '''
        if not persona.strip():
            auto_persona = await self._get_current_persona_name(event)
            if auto_persona:
                persona = auto_persona
        return await self._do_search_image(event, query=query, persona=persona)

    async def _do_save_image(
        self, event: AstrMessageEvent, user_description: str = "", persona: str = ""
    ) -> str:
        image_bytes = await self._extract_image_bytes(event)
        if not image_bytes:
            return "未检测到图片，请发送图片后再保存"

        persona = self._resolve_persona(persona)
        logger.info("[Wardrobe] 开始存图，图片大小=%.2fKB 人格=%s 用户描述=%s", len(image_bytes) / 1024, persona or "无", user_description or "无")

        created_by = str(event.get_sender_id() or "")
        image_id, attrs, duplicate = await self._save_image_from_bytes(
            image_bytes, persona=persona, created_by=created_by, user_description=user_description
        )

        if duplicate:
            dup_persona = duplicate.get("persona", "")
            dup_id = duplicate.get("id", "")
            persona_info = f"（人格: {dup_persona}）" if dup_persona else ""
            return f"这张图片已存在于衣柜库中{persona_info}，ID: {dup_id}，跳过保存"

        if not image_id:
            primary = str(self._cfg("save_provider_id", "") or "").strip()
            secondary = str(self._cfg("save_secondary_provider_id", "") or "").strip()
            if not primary and not secondary:
                return "未配置存图模型，请在插件设置中配置"
            return "图片保存失败"

        if not attrs:
            return f"图片已保存（ID: {image_id}），但模型分析失败，仅保存了原始图片"

        logger.info(
            "[Wardrobe] 分析结果:\n  分类: %s\n  风格: %s\n  服装: %s\n  暴露: %s\n  场景: %s\n  氛围: %s\n  姿势: %s\n  朝向: %s\n  动态: %s\n  动作风格: %s\n  景别: %s\n  角度: %s\n  表情: %s\n  色调: %s\n  构图: %s\n  背景: %s\n  描述: %s\n  用户标签: %s\n  暴露特征: %s\n  关键特征: %s\n  道具: %s\n  魅力特征: %s\n  身体焦点: %s\n  参考强度: %s\n  评级理由: %s",
            attrs.get("category", "人物"),
            ", ".join(attrs.get("style", [])),
            attrs.get("clothing_type", ""),
            attrs.get("exposure_level", ""),
            ", ".join(attrs.get("scene", [])),
            ", ".join(attrs.get("atmosphere", [])),
            attrs.get("pose_type", ""),
            attrs.get("body_orientation", ""),
            attrs.get("dynamic_level", ""),
            ", ".join(attrs.get("action_style", [])),
            attrs.get("shot_size", ""),
            attrs.get("camera_angle", ""),
            attrs.get("expression", ""),
            attrs.get("color_tone", ""),
            attrs.get("composition", ""),
            attrs.get("background", ""),
            attrs.get("description", ""),
            user_description or "无",
            ", ".join(attrs.get("exposure_features", [])),
            ", ".join(attrs.get("key_features", [])),
            ", ".join(attrs.get("prop_objects", [])),
            ", ".join(attrs.get("allure_features", [])),
            ", ".join(attrs.get("body_focus", [])),
            attrs.get("ref_strength", "style"),
            attrs.get("ref_strength_reason", ""),
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
            return None, None, None

        file_hash = hashlib.md5(image_bytes).hexdigest()
        existing = await self.db.get_image_by_hash(file_hash)
        if existing:
            logger.info("[Wardrobe] 图片重复，跳过保存: hash=%s 已存在ID=%s 人格=%s", file_hash, existing["id"], existing.get("persona", ""))
            return None, None, existing

        if user_description and len(user_description) > _MAX_DESCRIPTION_LEN:
            user_description = user_description[:_MAX_DESCRIPTION_LEN]

        primary = str(self._cfg("save_provider_id", "") or "").strip()
        secondary = str(self._cfg("save_secondary_provider_id", "") or "").strip()
        timeout = float(self._cfg("save_timeout_seconds", 60.0) or 60.0)

        if not primary and not secondary:
            return None, None, None

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
                exposure_features=[],
                key_features=[],
                prop_objects=[],
                allure_features=[],
                body_focus=[],
                image_path=filename,
                created_by=created_by,
                persona=persona,
                file_hash=file_hash,
                ref_strength="style",
                ref_strength_reason="",
            )
            await self._index_to_vector(image_id, user_description or "模型分析失败，无描述", user_description,
                                         category="人物", persona=persona)
            return image_id, None, None

        category = attrs.get("category", "人物")
        if category not in ("人物", "衣服"):
            category = "人物"

        filename = await self.store.save_image(image_bytes)

        image_id = await self.db.add_image(
            category=category,
            style=ensure_list(attrs.get("style")),
            clothing_type=ensure_str(attrs.get("clothing_type")),
            exposure_level=ensure_str(attrs.get("exposure_level")),
            scene=ensure_list(attrs.get("scene")),
            atmosphere=ensure_list(attrs.get("atmosphere")),
            pose_type=ensure_str(attrs.get("pose_type")),
            body_orientation=ensure_str(attrs.get("body_orientation")),
            dynamic_level=ensure_str(attrs.get("dynamic_level")),
            action_style=ensure_list(attrs.get("action_style")),
            shot_size=ensure_str(attrs.get("shot_size")),
            camera_angle=ensure_str(attrs.get("camera_angle")),
            expression=ensure_str(attrs.get("expression")),
            color_tone=ensure_str(attrs.get("color_tone")),
            composition=ensure_str(attrs.get("composition")),
            background=ensure_str(attrs.get("background")),
            description=ensure_str(attrs.get("description")),
            user_tags=user_description,
            exposure_features=ensure_list(attrs.get("exposure_features")),
            key_features=ensure_list(attrs.get("key_features")),
            prop_objects=ensure_list(attrs.get("prop_objects")),
            allure_features=ensure_list(attrs.get("allure_features")),
            body_focus=ensure_list(attrs.get("body_focus")),
            image_path=filename,
            created_by=created_by,
            persona=persona,
            file_hash=file_hash,
            ref_strength=ensure_str(attrs.get("ref_strength", "style")),
            ref_strength_reason=ensure_str(attrs.get("ref_strength_reason", "")),
        )

        desc_text = ensure_str(attrs.get("description"))
        await self._index_to_vector(
            image_id, desc_text, user_description,
            exposure_features=ensure_list(attrs.get("exposure_features")),
            key_features=ensure_list(attrs.get("key_features")),
            prop_objects=ensure_list(attrs.get("prop_objects")),
            allure_features=ensure_list(attrs.get("allure_features")),
            body_focus=ensure_list(attrs.get("body_focus")),
            category=category, persona=persona,
        )

        return image_id, attrs, None

    async def _auto_save_aiimg_image(self, event: AstrMessageEvent, tool=None):
        # 仅自动保存自拍模式生成的图片；文生图/改图不自动存入衣橱。
        enabled = self._cfg("auto_save_aiimg_enabled")
        if enabled is None:
            enabled = self._cfg("auto_save_gitee_enabled", False)
        if not enabled:
            return

        tool_name = ""
        if tool is not None:
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

        entry = last_image_dict.get(user_id)
        if not entry:
            return

        # _last_image_by_user 的值格式：{"path": Path, "mode": str}
        # 仅保存自拍模式 (mode="selfie") 的图片
        if isinstance(entry, dict):
            image_path = entry.get("path")
            image_mode = entry.get("mode", "")
        else:
            image_path = entry
            image_mode = ""

        if not image_path:
            return

        if image_mode != "selfie":
            logger.debug(
                "[Wardrobe] AiImg 自动存图跳过：非自拍模式 (mode=%s)", image_mode or "unknown"
            )
            return

        str_path = str(image_path)
        if self._last_auto_saved.get(user_id) == str_path:
            return
        self._last_auto_saved[user_id] = str_path

        path = Path(image_path)
        if not path.exists():
            return

        persona = await self._get_auto_save_persona(event)

        try:
            import aiofiles
            async with aiofiles.open(path, "rb") as f:
                image_bytes = await f.read()

            if not image_bytes:
                return

            logger.info(
                "[Wardrobe] AiImg 自动存图开始，图片大小=%.2fKB 人格=%s tool=%s",
                len(image_bytes) / 1024, persona or "无", tool_name or "command",
            )

            # 自动存图不传 user_description：此时没有用户主动提供的描述，
            # AI 分析模型会根据图片内容自动生成描述，无需额外文本。
            # 仅 /存图 命令路径才会传入用户描述。
            image_id, attrs, duplicate = await self._save_image_from_bytes(
                image_bytes, persona=persona, created_by=user_id,
            )

            if duplicate:
                logger.info(
                    "[Wardrobe] AiImg 自动存图跳过：图片重复，已存在ID=%s",
                    duplicate.get("id", ""),
                )
                return

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

    async def _get_auto_save_persona(self, event: AstrMessageEvent) -> str:
        conv_persona = await self._get_current_persona_name(event)
        if conv_persona:
            return self._resolve_persona(conv_persona)
        return ""

    async def _get_current_persona_name(self, event: AstrMessageEvent) -> str | None:
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if not umo:
                return None

            persona_id = None

            # 优先从 conversation_manager 获取
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr:
                try:
                    curr_cid = await conv_mgr.get_curr_conversation_id(umo)
                    if curr_cid:
                        conversation = await conv_mgr.get_conversation(umo, curr_cid)
                        if conversation:
                            persona_id = getattr(conversation, "persona_id", None)
                except Exception as e:
                    logger.debug("[Wardrobe] 从 conversation_manager 获取 persona_id 失败: %s", e)

            if persona_id:
                return str(persona_id).strip() or None

            # 回退：从 persona_manager 获取默认人格
            persona_mgr = getattr(self.context, "persona_manager", None)
            if persona_mgr:
                try:
                    persona_obj = None
                    if hasattr(persona_mgr, "get_default_persona_v3"):
                        persona_obj = await persona_mgr.get_default_persona_v3(umo)
                    if persona_obj:
                        name = self._extract_persona_name(persona_obj)
                        if name:
                            return name
                except Exception as e:
                    logger.debug("[Wardrobe] 从 persona_manager 获取默认人格失败: %s", e)
        except Exception as e:
            logger.debug("[Wardrobe] 获取当前人格失败: %s", e)
        return None

    @staticmethod
    def _extract_persona_name(persona_obj) -> str | None:
        if not persona_obj:
            return None
        if isinstance(persona_obj, dict):
            for key in ("name", "persona_id", "id"):
                val = persona_obj.get(key)
                if val and str(val).strip():
                    return str(val).strip()
            return None
        for attr in ("name", "persona_id", "id"):
            if hasattr(persona_obj, attr):
                val = getattr(persona_obj, attr, None)
                if val and str(val).strip():
                    return str(val).strip()
        return None

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

        if self.vector_searcher:
            try:
                await self.vector_searcher.remove_image(image_id)
            except Exception:
                pass

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
        persona_names = self._get_persona_names_str()

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
            prioritize_unused=bool(self._cfg("search_prioritize_unused", False)),
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

        try:
            await self.db.increment_use_count(best["id"])
        except Exception:
            pass

        return {
            "image_path": str(image_path),
            "description": best.get("description", ""),
            "persona": best.get("persona", ""),
            "image_id": best["id"],
            "ref_strength": best.get("ref_strength", "style"),
        }

    async def _do_search_image(
        self, event: AstrMessageEvent, query: str, persona: str = ""
    ) -> str:
        await self._ensure_db()

        raw_persona = persona.strip()
        resolved_persona = self._resolve_persona(raw_persona)
        current_persona = resolved_persona or raw_persona
        persona_names = self._get_persona_names_str()

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
            persona_mode=str(self._cfg("search_persona_mode", "exclude_all") or "exclude_all"),
            prioritize_unused=bool(self._cfg("search_prioritize_unused", False)),
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
            try:
                await self.db.increment_use_count(r["id"])
            except Exception:
                pass

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
        # 仅提取第一张图片。多图场景下用户可逐张保存，或通过 WebUI 批量管理。
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

    def _get_personas_config(self) -> list[dict]:
        personas = self._cfg("personas", [])
        if personas and isinstance(personas, list):
            return personas
        legacy = str(self._cfg("persona_names", "") or "").strip()
        if legacy:
            result = []
            for entry in self._split_persona_entries(legacy):
                entry = entry.strip()
                if not entry:
                    continue
                canonical, aliases = self._parse_persona_entry(entry)
                if canonical:
                    result.append({"name": canonical, "aliases": aliases})
            return result
        return []

    def _get_persona_names_str(self) -> str:
        personas = self._get_personas_config()
        if not personas:
            return ""
        names = [p.get("name", "") for p in personas if p.get("name")]
        return ", ".join(names)

    def _match_configured_persona(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        personas = self._get_personas_config()
        if not personas:
            return ""
        for p in personas:
            canonical = p.get("name", "").strip()
            aliases = p.get("aliases", []) or []
            if text == canonical or text in aliases:
                return canonical
        return ""

    def _resolve_persona(self, persona: str) -> str:
        persona = persona.strip()
        if not persona:
            return ""
        personas = self._get_personas_config()
        if not personas:
            return persona
        for p in personas:
            canonical = p.get("name", "").strip()
            aliases = p.get("aliases", []) or []
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
            ref_strength = attrs.get("ref_strength", "style")
            ref_labels = {"full": "📸完整参考", "style": "🎨风格参考", "reimagine": "🔄重构"}
            lines.append(f"参考强度：{ref_labels.get(ref_strength, ref_strength)}")

        return "\n".join(lines)
