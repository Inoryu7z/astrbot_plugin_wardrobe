from datetime import datetime, timedelta
from pathlib import Path
import uuid
from typing import Optional
import asyncio
import hashlib
import json
import time
import zipfile

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import on_llm_tool_respond
from astrbot.api.message_components import Image, Video
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.analyzer import ImageAnalyzer
from .core.database import WardrobeDatabase
from .core.image_store import ImageStore
from .core.searcher import ImageSearcher
from .core.utils import detect_image_mime, ensure_list, ensure_str, mime_to_ext, strip_ai_prompt_affixes
from .core.video_service import VideoService
from .webui import WardrobeWebServer

try:
    from .core.vector_searcher import WardrobeVectorSearcher
    from astrbot.core.provider.provider import EmbeddingProvider
    from astrbot.core.provider.provider import RerankProvider
    _VEC_AVAILABLE = True
except ImportError:
    _VEC_AVAILABLE = False

_MAX_IMAGE_SIZE_MB = 20
_MAX_DESCRIPTION_LEN = 2000
# 仅监听 aiimg_generate 这一个统一入口工具。
# aiimg_draw / aiimg_edit 内部最终都走 aiimg_generate，所以只需监听这一个即可覆盖所有 LLM 工具调用路径。
# 命令路径（/自拍 /aiimg 等）则由 on_after_message_sent 钩子兜底。
_AIIMG_GENERATE_TOOLS = frozenset({"aiimg_generate"})
_BACKUP_STATE_FILE = "backup_state.json"
# 数据文件迁移标识。本次：将人格级自定义风格池重置为默认版本。
_STYLE_POOL_RESET_MIGRATION = "style_pool_reset_2026_08"
_MIGRATION_STATE_FILE = "migration_state.json"


@register(
    "astrbot_plugin_wardrobe",
    "Inoryu7z",
    "图片衣柜管理插件，支持智能分类、语义检索和参考图接口",
    "3.0.7",
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
        self.video_service = VideoService(self)
        self.data_dir = data_dir
        self._db_initialized = False
        self._db_init_event = asyncio.Event()
        self._db_init_event.set()
        self._webui: Optional[WardrobeWebServer] = None
        self._last_auto_saved: dict[str, str] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        # 评论队列惰性初始化：首次投递评论时才创建并启动消费协程，避免 __init__ 依赖事件循环
        self._comment_queue: Optional[asyncio.Queue] = None

        self.context._wardrobe_plugin = self

        self._run_data_migrations()

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
                    logger.debug("[Wardrobe] 使用配置的 Embedding Provider: %s", emb_id)
            if not embedding_provider:
                try:
                    embedding_providers = self.context.get_all_embedding_providers()
                    if embedding_providers:
                        embedding_provider = embedding_providers[0]
                        logger.debug("[Wardrobe] 使用默认 Embedding Provider")
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
                logger.debug("[Wardrobe] 使用配置的 Rerank Provider: %s", rerank_id)
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
            additions = v.get("additions", []) if isinstance(v, dict) else v
            removals = v.get("removals", []) if isinstance(v, dict) else []
            if k in merged:
                merged[k] = [item for item in merged[k] if item not in removals]
                for item in additions:
                    if item not in merged[k]:
                        merged[k].append(item)
            else:
                merged[k] = list(additions)
        return merged

    async def _load_custom_pools(self) -> dict:
        import json
        path = self.data_dir / "custom_pools.json"
        if path.exists():
            try:
                data = await asyncio.to_thread(self._read_custom_pools_file, path)
                if not isinstance(data, dict):
                    return {}
                # 迁移旧格式（list）到新格式（{additions, removals}）
                for k, v in data.items():
                    if isinstance(v, list):
                        data[k] = {"additions": v, "removals": []}
                    elif not isinstance(v, dict):
                        data[k] = {"additions": [], "removals": []}
                    else:
                        data[k] = {
                            "additions": v.get("additions", []) if isinstance(v.get("additions"), list) else [],
                            "removals": v.get("removals", []) if isinstance(v.get("removals"), list) else [],
                        }
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
            if k in ALL_POOLS:
                additions = [item for item in v if item not in default]
                removals = [item for item in default if item not in v]
                if additions or removals:
                    custom[k] = {"additions": additions, "removals": removals}
            else:
                if v:
                    custom[k] = {"additions": list(v), "removals": []}
        path = self.data_dir / "custom_pools.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(custom, ensure_ascii=False, indent=2)
        await asyncio.to_thread(self._write_custom_pools_file, path, content)
        logger.debug("[Wardrobe] 自定义池子已保存")

    @staticmethod
    def _write_custom_pools_file(path: Path, content: str):
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    # ---------- 人格级风格池（供 aiimg 补拍使用） ----------

    async def get_style_pool_for_persona(self, persona_name: str) -> list[str] | None:
        """返回指定人格的自定义风格池。

        返回 None 表示该人格未配置自定义风格池，调用方应回退到全局风格池。
        返回空列表表示该人格显式配置为空池（极少见）。
        """
        persona_name = (persona_name or "").strip()
        if not persona_name:
            return None
        pools = await self._load_persona_style_pools()
        if persona_name in pools:
            return list(pools[persona_name])
        return None

    async def _load_persona_style_pools(self) -> dict[str, list[str]]:
        import json
        path = self.data_dir / "persona_style_pools.json"
        if path.exists():
            try:
                data = await asyncio.to_thread(self._read_persona_style_pools_file, path)
                if not isinstance(data, dict):
                    return {}
                result = {}
                for k, v in data.items():
                    if isinstance(v, list):
                        result[k] = [str(item) for item in v if str(item).strip()]
                return result
            except Exception:
                pass
        return {}

    @staticmethod
    def _read_persona_style_pools_file(path: Path):
        import json
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    async def save_persona_style_pool(self, persona_name: str, styles: list[str]):
        """保存指定人格的自定义风格池。"""
        import json
        persona_name = (persona_name or "").strip()
        if not persona_name:
            return
        pools = await self._load_persona_style_pools()
        cleaned = [str(s).strip() for s in styles if str(s).strip()]
        pools[persona_name] = cleaned
        path = self.data_dir / "persona_style_pools.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(pools, ensure_ascii=False, indent=2)
        await asyncio.to_thread(self._write_persona_style_pools_file, path, content)
        logger.debug("[Wardrobe] 人格风格池已保存: %s (%d 项)", persona_name, len(cleaned))

    async def delete_persona_style_pool(self, persona_name: str):
        """删除指定人格的自定义风格池，使其回退到全局池。"""
        import json
        persona_name = (persona_name or "").strip()
        pools = await self._load_persona_style_pools()
        if persona_name in pools:
            del pools[persona_name]
            path = self.data_dir / "persona_style_pools.json"
            content = json.dumps(pools, ensure_ascii=False, indent=2)
            await asyncio.to_thread(self._write_persona_style_pools_file, path, content)
            logger.debug("[Wardrobe] 人格风格池已删除: %s", persona_name)

    @staticmethod
    def _write_persona_style_pools_file(path: Path, content: str):
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
            if not self.vector_searcher or not self.vector_searcher.available:
                old_vs = self.vector_searcher
                self.vector_searcher = self._init_vector_searcher(self.data_dir)
                if self.vector_searcher:
                    self.searcher.vector_searcher = self.vector_searcher
                    if not old_vs:
                        logger.debug("[Wardrobe] 向量检索器延迟初始化成功")
                    else:
                        logger.debug("[Wardrobe] 向量检索器重新初始化成功")
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

            logger.info("[Wardrobe] 发现需重分析: 新字段%d张, ref_strength回填%d张", len(need_reanalyze), len(need_ref_strength_backfill))

            primary = str(self._cfg("save_provider_id", "") or "").strip()
            secondary = str(self._cfg("save_secondary_provider_id", "") or "").strip()
            timeout = float(self._cfg("save_timeout_seconds", 60.0) or 60.0)

            if not primary and not secondary:
                logger.debug("[Wardrobe] 未配置存图模型，跳过旧图重分析")
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
                        persona=rec.get("persona", ""),
                    )

                    if not attrs:
                        failed += 1
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
                            style=ensure_list(attrs.get("style")),
                            clothing_type=ensure_str(attrs.get("clothing_type")),
                            category=str(attrs.get("category", rec.get("category", ""))),
                            persona=rec.get("persona", ""),
                        )

                    success += 1

                    if i < len(need_reanalyze) - 1:
                        await asyncio.sleep(30)

                except Exception:
                    failed += 1

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
                            persona=rec.get("persona", ""),
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
                            await asyncio.sleep(30)

                    except Exception:
                        rs_failed += 1

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

            logger.debug("[Wardrobe] 发现 %d 张图片缺少文件哈希，开始回填...", len(need_backfill))

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
                except Exception:
                    pass

            logger.debug("[Wardrobe] 文件哈希回填完成: %d/%d", backfilled, len(need_backfill))
        except Exception as e:
            logger.error("[Wardrobe] 文件哈希回填任务异常: %s", e, exc_info=True)

    def _cfg(self, key: str, default=None):
        return self.config.get(key, default)

    def _spawn_bg_task(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    # ============ 图片评论（ai_comment）============

    def _resolve_comment_provider(self) -> str:
        """解析评论模型：评论模型 → 存图主模型 → 存图备用模型。"""
        for key in ("ai_comment_provider_id", "save_provider_id", "save_secondary_provider_id"):
            p = str(self._cfg(key, "") or "").strip()
            if p:
                return p
        return ""

    def _enqueue_comment(self, image_id: str, delay: int | None = None):
        """投递图片评论任务。延迟后进入串行队列，逐个生成避免模型压力。

        仅在开启「图片评论」时生效。delay=None 读取配置 ai_comment_delay_seconds；
        delay<=0 立即入队。队列与消费协程在首次投递时惰性创建。
        """
        if not self._cfg("ai_comment_enabled", False):
            return
        if self._comment_queue is None:
            self._comment_queue = asyncio.Queue()
            self._spawn_bg_task(self._comment_queue_loop())
        if delay is None:
            try:
                delay = max(0, int(self._cfg("ai_comment_delay_seconds", 3600) or 3600))
            except Exception:
                delay = 3600
        if delay <= 0:
            self._comment_queue.put_nowait(image_id)
        else:
            self._spawn_bg_task(self._delayed_comment_enqueue(image_id, delay))

    async def _delayed_comment_enqueue(self, image_id: str, delay: int):
        try:
            await asyncio.sleep(delay)
            self._comment_queue.put_nowait(image_id)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _comment_queue_loop(self):
        """串行消费评论队列，避免并发调用模型。"""
        while True:
            image_id = await self._comment_queue.get()
            try:
                await self._generate_comment_for_image(image_id)
            except Exception as e:
                logger.warning("[Wardrobe] 评论队列处理异常 id=%s error=%s", image_id, e)
            finally:
                self._comment_queue.task_done()

    async def _generate_comment_for_image(self, image_id: str) -> str:
        """为图片生成点评并入库。成功返回点评文本，失败返回空字符串。"""
        try:
            await self._ensure_db()
            image = await self.db.get_image(image_id)
            if not image:
                return ""
            path = self.store.get_image_path(image.get("image_path", ""))
            if not path.exists():
                logger.warning("[Wardrobe] 评论图片文件不存在 id=%s", image_id)
                return ""

            provider = self._resolve_comment_provider()
            if not provider:
                logger.warning("[Wardrobe] 未配置评论模型，跳过评论 id=%s", image_id)
                return ""

            import aiofiles
            async with aiofiles.open(path, "rb") as f:
                image_bytes = await f.read()
            if not image_bytes:
                return ""

            timeout = float(self._cfg("save_timeout_seconds", 60.0) or 60.0)
            comment = await self.analyzer.generate_comment(
                image_bytes, provider_id=provider, timeout_seconds=timeout
            )
            if not comment:
                logger.warning("[Wardrobe] 评论生成返回空 id=%s", image_id)
                return ""
            await self.db.update_image(image_id, ai_comment=comment)
            logger.info("[Wardrobe] 图片评论生成完成 id=%s len=%d", image_id, len(comment))
            return comment
        except Exception as e:
            logger.warning("[Wardrobe] 图片评论生成异常 id=%s error=%s", image_id, e)
            return ""

    async def _load_backup_state(self) -> dict:
        path = self.data_dir / "backups" / _BACKUP_STATE_FILE
        if path.exists():
            try:
                content = await asyncio.to_thread(path.read_text, encoding="utf-8")
                data = json.loads(content)
                return data if isinstance(data, dict) else {}
            except Exception:
                pass
        return {}

    async def _save_backup_state(self, state: dict):
        path = self.data_dir / "backups" / _BACKUP_STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(state, ensure_ascii=False, indent=2)
        await asyncio.to_thread(self._write_text_file, path, content)

    @staticmethod
    def _write_text_file(path: Path, content: str):
        with open(str(path), "w", encoding="utf-8") as f:
            f.write(content)

    def _run_data_migrations(self):
        """运行数据文件迁移。

        本次迁移（style_pool_reset_2026_08）：默认风格池已更新，需将人格级自定义
        风格池（persona_style_pools.json）清除，使其回退为新的默认 STYLE_POOL。
        迁移幂等：通过 migration_state.json 记录已应用的迁移，避免重复执行。
        """
        state_path = self.data_dir / _MIGRATION_STATE_FILE
        state: dict = {}
        try:
            if state_path.exists():
                raw = state_path.read_text(encoding="utf-8")
                state = json.loads(raw) if raw.strip() else {}
                if not isinstance(state, dict):
                    state = {}
        except Exception:
            state = {}

        applied = set(state.get("applied_migrations", []) or [])
        if _STYLE_POOL_RESET_MIGRATION in applied:
            return

        persona_pool_path = self.data_dir / "persona_style_pools.json"
        if persona_pool_path.exists():
            try:
                persona_pool_path.unlink()
                logger.info(
                    "[Wardrobe] 迁移 %s：已清除人格级自定义风格池，回退为默认风格池",
                    _STYLE_POOL_RESET_MIGRATION,
                )
            except Exception as e:
                logger.warning(
                    "[Wardrobe] 迁移 %s：清除 persona_style_pools.json 失败: %s",
                    _STYLE_POOL_RESET_MIGRATION, e,
                )

        applied.add(_STYLE_POOL_RESET_MIGRATION)
        state["applied_migrations"] = sorted(applied)
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(
                "[Wardrobe] 迁移 %s：写入迁移状态失败: %s",
                _STYLE_POOL_RESET_MIGRATION, e,
            )

    async def _build_backup_to_file(
        self,
        output_path: Path,
        include_videos: bool = False,
        incremental: bool = False,
        throttle_sleep: float = 0.0,
    ) -> tuple[int, int, int]:
        await self._ensure_db()

        state = await self._load_backup_state()
        since_ts = state.get("last_backup_ts", "")

        if incremental and since_ts:
            records = await self.db.get_records_since(since_ts)
        else:
            records = await self.db.get_all_records()
            incremental = False

        images_dir = self.store.images_dir
        total_records = len(records)

        video_records = []
        videos_dir = None
        if include_videos:
            if incremental and since_ts:
                video_records = await self.db.get_video_records_since(since_ts)
            else:
                video_records = await self.db.get_all_video_records()
            self.video_service._ensure_dirs()
            videos_dir = self.video_service.videos_dir

        # image_usage（按人格热度）仅在全量备份时导出。
        # 增量备份的 since_ts 基于 images.created_at，与 image_usage 的变更时间不对应，
        # 增量导出会漏掉已存在图片的新热度记录，所以增量备份不包含 image_usage。
        image_usage_records = []
        if not incremental:
            image_usage_records = await self.db.get_all_image_usage_records()

        video_settings = {}
        umo_path = self.video_service._get_send_umo_path()
        if umo_path.exists():
            try:
                video_settings["video_send_umo"] = umo_path.read_text(encoding="utf-8")
            except Exception:
                pass
        prompt_path = self.video_service.get_system_prompt_path()
        if prompt_path.exists():
            try:
                video_settings["video_system_prompt"] = prompt_path.read_text(encoding="utf-8")
            except Exception:
                pass

        backup_type = "incremental" if incremental else "full"
        _sleep = throttle_sleep

        def _build():
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(str(output_path), "w", zipfile.ZIP_DEFLATED) as zf:
                metadata = json.dumps({
                    "version": "3.0",
                    "type": backup_type,
                    "export_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "total_records": total_records,
                    "total_videos": len(video_records),
                    "include_video_files": include_videos,
                }, ensure_ascii=False)
                zf.writestr("backup_metadata.json", metadata)
                zf.writestr("records.json", json.dumps(records, ensure_ascii=False, indent=2))
                if video_records:
                    zf.writestr("videos.json", json.dumps(video_records, ensure_ascii=False, indent=2))
                if image_usage_records:
                    zf.writestr("image_usage.json", json.dumps(image_usage_records, ensure_ascii=False, indent=2))
                added_files = 0
                for rec in records:
                    img_filename = rec.get("image_path", "")
                    if not img_filename:
                        continue
                    img_path = images_dir / img_filename
                    if img_path.exists():
                        zf.write(str(img_path), f"images/{img_filename}", compress_type=zipfile.ZIP_STORED)
                        added_files += 1
                        if _sleep > 0:
                            time.sleep(_sleep)
                added_videos = 0
                if include_videos and videos_dir:
                    for vrec in video_records:
                        vfilename = vrec.get("video_path", "")
                        if not vfilename:
                            continue
                        vpath = videos_dir / vfilename
                        if vpath.exists():
                            zf.write(str(vpath), f"videos/{vfilename}", compress_type=zipfile.ZIP_STORED)
                            added_videos += 1
                            if _sleep > 0:
                                time.sleep(_sleep)
                if video_settings:
                    zf.writestr("video_settings.json", json.dumps(video_settings, ensure_ascii=False, indent=2))
            return added_files, added_videos

        added_files, added_videos = await asyncio.to_thread(_build)
        return total_records, added_files, added_videos

    async def _weekly_daily_selfie_decay_loop(self):
        while True:
            now = datetime.now()
            days_until_monday = (7 - now.weekday()) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            next_monday = now + timedelta(days=days_until_monday)
            next_monday = next_monday.replace(hour=4, minute=0, second=0, microsecond=0)
            wait_seconds = (next_monday - now).total_seconds()
            if wait_seconds <= 0:
                wait_seconds = 7 * 86400
            logger.debug(
                "[Wardrobe] 补拍衰减: 下次执行在 %.1f 小时后",
                wait_seconds / 3600,
            )
            await asyncio.sleep(wait_seconds)
            try:
                await self._ensure_db()
                affected = await self.db.decay_daily_selfie_use_counts(amount=1)
                logger.debug(
                    "[Wardrobe] 补拍衰减完成: %d 张图片的 daily_selfie_use_count 减1",
                    affected,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[Wardrobe] 补拍衰减任务异常: %s", e)
                await asyncio.sleep(3600)

    async def build_backup_zip(self, include_videos: bool = False) -> tuple[Path, int, int]:
        backup_dir = self.data_dir / "backups"
        backup_dir.mkdir(exist_ok=True)
        import uuid
        export_path = backup_dir / f"wardrobe_manual_export_{uuid.uuid4().hex[:8]}.zip"

        total_records, added_files, _ = await self._build_backup_to_file(
            export_path,
            include_videos=include_videos,
            incremental=False,
            throttle_sleep=0.0,
        )

        return export_path, total_records, added_files

    async def build_selected_backup_zip(self, image_ids: list[str]) -> tuple[Path, int, int]:
        await self._ensure_db()
        records = await self.db.get_records_by_ids(image_ids)
        if not records:
            return Path(""), 0, 0

        backup_dir = self.data_dir / "backups"
        backup_dir.mkdir(exist_ok=True)
        import uuid
        export_path = backup_dir / f"wardrobe_selected_export_{uuid.uuid4().hex[:8]}.zip"

        images_dir = self.store.images_dir
        total_records = len(records)

        video_records = []
        for rec in records:
            vids = await self.db.get_videos_by_image_id(rec["id"])
            video_records.extend(vids)
        if video_records:
            self.video_service._ensure_dirs()

        # 导出选中图片的按人格热度记录
        image_usage_records = await self.db.get_image_usage_by_ids(image_ids)

        video_settings = {}
        umo_path = self.video_service._get_send_umo_path()
        if umo_path.exists():
            try:
                video_settings["video_send_umo"] = umo_path.read_text(encoding="utf-8")
            except Exception:
                pass
        prompt_path = self.video_service.get_system_prompt_path()
        if prompt_path.exists():
            try:
                video_settings["video_system_prompt"] = prompt_path.read_text(encoding="utf-8")
            except Exception:
                pass

        def _build():
            export_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(str(export_path), "w", zipfile.ZIP_DEFLATED) as zf:
                metadata = json.dumps({
                    "version": "3.0",
                    "type": "full",
                    "export_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "total_records": total_records,
                    "total_videos": len(video_records),
                    "include_video_files": bool(video_records),
                    "selected_export": True,
                }, ensure_ascii=False)
                zf.writestr("backup_metadata.json", metadata)
                zf.writestr("records.json", json.dumps(records, ensure_ascii=False, indent=2))
                if video_records:
                    zf.writestr("videos.json", json.dumps(video_records, ensure_ascii=False, indent=2))
                if image_usage_records:
                    zf.writestr("image_usage.json", json.dumps(image_usage_records, ensure_ascii=False, indent=2))
                added_files = 0
                for rec in records:
                    img_filename = rec.get("image_path", "")
                    if not img_filename:
                        continue
                    img_path = images_dir / img_filename
                    if img_path.exists():
                        zf.write(str(img_path), f"images/{img_filename}", compress_type=zipfile.ZIP_STORED)
                        added_files += 1
                added_videos = 0
                if video_records:
                    videos_dir = self.video_service.videos_dir
                    for vrec in video_records:
                        vfilename = vrec.get("video_path", "")
                        if not vfilename:
                            continue
                        vpath = videos_dir / vfilename
                        if vpath.exists():
                            zf.write(str(vpath), f"videos/{vfilename}", compress_type=zipfile.ZIP_STORED)
                            added_videos += 1
                if video_settings:
                    zf.writestr("video_settings.json", json.dumps(video_settings, ensure_ascii=False, indent=2))
            return added_files, added_videos

        added_files, _ = await asyncio.to_thread(_build)
        return export_path, total_records, added_files

    async def export_images_zip(self, image_ids: list[str]) -> tuple[Path, int]:
        await self._ensure_db()
        records = await self.db.get_records_by_ids(image_ids)
        if not records:
            return Path(""), 0

        backup_dir = self.data_dir / "backups"
        backup_dir.mkdir(exist_ok=True)
        import uuid
        export_path = backup_dir / f"wardrobe_images_export_{uuid.uuid4().hex[:8]}.zip"

        images_dir = self.store.images_dir

        def _build():
            export_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(str(export_path), "w", zipfile.ZIP_DEFLATED) as zf:
                added_files = 0
                for rec in records:
                    img_filename = rec.get("image_path", "")
                    if not img_filename:
                        continue
                    img_path = images_dir / img_filename
                    if img_path.exists():
                        zf.write(str(img_path), img_filename, compress_type=zipfile.ZIP_STORED)
                        added_files += 1
            return added_files

        added_files = await asyncio.to_thread(_build)
        return export_path, added_files

    async def _auto_backup_loop(self):
        while True:
            try:
                if not self._cfg("auto_backup_enabled", True):
                    await asyncio.sleep(3600)
                    continue

                now = datetime.now()
                target = now.replace(hour=4, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
                logger.debug("[Wardrobe] 下次备份在 %.1f 小时后", wait_seconds / 3600)
                await asyncio.sleep(wait_seconds)

                if not self._cfg("auto_backup_enabled", True):
                    continue

                now = datetime.now()
                is_first_of_month = now.day == 1
                backup_dir = self.data_dir / "backups"
                backup_dir.mkdir(exist_ok=True)
                date_str = now.strftime("%Y-%m-%d")

                if is_first_of_month:
                    backup_path = backup_dir / f"full_{date_str}.zip"
                    total_records, added_files, _ = await self._build_backup_to_file(
                        backup_path,
                        include_videos=False,
                        incremental=False,
                        throttle_sleep=0.5,
                    )
                    logger.info("[Wardrobe] 月度全量备份完成: %d条记录, %d个图片文件", total_records, added_files)

                    state = await self._load_backup_state()
                    state["last_full_backup_ts"] = now.isoformat()
                    state["last_backup_ts"] = now.isoformat()
                    state["last_full_backup_date"] = date_str
                    await self._save_backup_state(state)
                else:
                    backup_path = backup_dir / f"incr_{date_str}.zip"
                    total_records, added_files, _ = await self._build_backup_to_file(
                        backup_path,
                        include_videos=False,
                        incremental=True,
                        throttle_sleep=0.1,
                    )
                    logger.info("[Wardrobe] 每日增量备份完成: %d条新记录, %d个新图片文件", total_records, added_files)

                    state = await self._load_backup_state()
                    state["last_backup_ts"] = now.isoformat()
                    await self._save_backup_state(state)

                await self._cleanup_old_backups()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("[Wardrobe] 自动备份失败: %s", e, exc_info=True)
                await asyncio.sleep(3600)

    async def list_backups(self) -> list[dict]:
        """列出 backups 目录下所有备份文件，返回按时间倒序的元信息列表。"""
        backup_dir = self.data_dir / "backups"
        if not backup_dir.exists():
            return []
        result: list[dict] = []
        for f in backup_dir.iterdir():
            if not f.is_file() or not f.name.endswith(".zip"):
                continue
            try:
                stat = f.stat()
            except OSError:
                continue
            # 文件名前缀推断类型
            name = f.name
            if name.startswith("full_"):
                btype = "全量备份"
            elif name.startswith("incr_"):
                btype = "增量备份"
            elif name.startswith("wardrobe_images_export_"):
                btype = "图片导出"
            elif name.startswith("wardrobe_selected_export_"):
                btype = "选择备份"
            elif name.startswith("wardrobe_manual_export_"):
                btype = "手动备份"
            else:
                btype = "备份"
            result.append({
                "filename": name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "type": btype,
            })
        result.sort(key=lambda x: x["mtime"], reverse=True)
        return result

    async def delete_backup(self, filename: str) -> bool:
        """删除指定备份文件（仅限 backups 目录内的 .zip）。"""
        backup_dir = self.data_dir / "backups"
        # 防路径穿越：只取文件名部分
        safe_name = Path(filename).name
        if not safe_name.endswith(".zip"):
            return False
        target = (backup_dir / safe_name).resolve()
        try:
            target.relative_to(backup_dir.resolve())
        except ValueError:
            return False
        if not target.exists() or not target.is_file():
            return False
        try:
            target.unlink()
            logger.info("[Wardrobe] 已删除备份: %s", safe_name)
            return True
        except Exception as e:
            logger.warning("[Wardrobe] 删除备份失败 %s: %s", safe_name, e)
            return False

    async def _cleanup_old_backups(self):
        try:
            backup_dir = self.data_dir / "backups"
            if not backup_dir.exists():
                return

            now = datetime.now()
            cutoff = now - timedelta(days=7)

            latest_full = None
            to_delete = []

            for f in backup_dir.iterdir():
                if not f.is_file():
                    continue
                if f.name == _BACKUP_STATE_FILE:
                    continue
                if not f.name.endswith(".zip"):
                    continue

                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if f.name.startswith("full_"):
                    if latest_full is None or mtime > datetime.fromtimestamp(latest_full.stat().st_mtime):
                        latest_full = f

                if mtime < cutoff:
                    to_delete.append(f)

            if latest_full and latest_full in to_delete:
                to_delete.remove(latest_full)

            for f in to_delete:
                try:
                    f.unlink()
                    logger.debug("[Wardrobe] 清理旧备份: %s", f.name)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("[Wardrobe] 清理旧备份失败: %s", e)

    async def _video_retention_loop(self):
        while True:
            try:
                now = datetime.now()
                target = now.replace(hour=5, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
                logger.debug("[Wardrobe] 视频保留策略: 下次清理在 %.1f 小时后", wait_seconds / 3600)
                await asyncio.sleep(wait_seconds)

                await self._ensure_db()
                expired = await self.db.get_expired_auto_save_videos(max_age_days=7)
                if not expired:
                    continue

                self.video_service._ensure_dirs()
                deleted = 0
                for v in expired:
                    video_path_str = v.get("video_path", "")
                    if video_path_str:
                        video_file = self.video_service.videos_dir / video_path_str
                        try:
                            if video_file.exists():
                                video_file.unlink()
                        except Exception:
                            pass
                    await self.db.delete_video(v["id"])
                    deleted += 1

                logger.debug("[Wardrobe] 视频保留策略: 清理了 %d 个超过7天的自动保存视频", deleted)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("[Wardrobe] 视频保留策略任务异常: %s", e)
                await asyncio.sleep(3600)

    async def _meh_cleanup_loop(self):
        while True:
            try:
                now = datetime.now()
                target = now.replace(hour=5, minute=30, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
                logger.debug("[Wardrobe] 无感清理: 下次清理在 %.1f 小时后", wait_seconds / 3600)
                await asyncio.sleep(wait_seconds)

                await self._ensure_db()
                expired = await self.db.get_expired_meh_images(max_age_days=30)
                if not expired:
                    continue

                deleted = 0
                for img in expired:
                    image_id = img.get("id", "")
                    if not image_id:
                        continue

                    ok = await self.db.delete_image(image_id)
                    if not ok:
                        continue

                    if img.get("image_path"):
                        await self.store.delete_image(img["image_path"])

                    if self.vector_searcher:
                        try:
                            await self.vector_searcher.remove_image(image_id)
                        except Exception:
                            pass

                    await self._cleanup_videos_for_image(image_id)
                    deleted += 1

                logger.debug("[Wardrobe] 无感清理: 删除了 %d 张超过30天的无感图片", deleted)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("[Wardrobe] 无感清理任务异常: %s", e)
                await asyncio.sleep(3600)

    async def _ensure_all_thumbnails(self):
        try:
            await self._ensure_db()
            images = await self.db.list_images_lightweight(limit=99999)
            generated = 0
            for img in images:
                try:
                    thumb_path = self.store.get_thumbnail_path(img["image_path"])
                    if not thumb_path.exists():
                        await self.store.ensure_thumbnail(img["image_path"])
                        generated += 1
                except Exception:
                    pass
            if generated > 0:
                logger.debug("[Wardrobe] 批量缩略图生成完成: 新生成 %d 张", generated)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("[Wardrobe] 批量缩略图生成失败: %s", e)

    async def _index_to_vector(self, image_id: str, description: str, user_tags: str,
                                exposure_features: list | None = None,
                                key_features: list | None = None,
                                prop_objects: list | None = None,
                                allure_features: list | None = None,
                                body_focus: list | None = None,
                                style: list | None = None,
                                clothing_type: str = "",
                                category: str = "", persona: str = ""):
        if not self.vector_searcher or not self.vector_searcher.available:
            return
        text_parts = []
        if description:
            text_parts.append(description)
        if user_tags:
            text_parts.append(f"标签: {user_tags}")
        if style:
            text_parts.append(f"风格: {' '.join(str(v) for v in style if v)}")
        if clothing_type:
            text_parts.append(f"服装: {clothing_type}")
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
                logger.error("[Wardrobe] WebUI 启动失败: %s", e, exc_info=True)

        self._spawn_bg_task(self._auto_backup_loop())
        self._spawn_bg_task(self._ensure_all_thumbnails())
        self._spawn_bg_task(self._weekly_daily_selfie_decay_loop())
        self._spawn_bg_task(self._video_retention_loop())
        self._spawn_bg_task(self._meh_cleanup_loop())

    @on_llm_tool_respond()
    async def on_aiimg_tool_respond(self, event: AstrMessageEvent, tool, tool_args, tool_result):
        '''AiImg 生图工具调用后的自动存图钩子'''
        self._spawn_bg_task(self._auto_save_aiimg_image(event, tool))

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        '''消息发送后钩子：检测 AiImg 命令方式生成的图片/视频并自动存图'''
        self._spawn_bg_task(self._auto_save_aiimg_image(event, tool=None))
        self._spawn_bg_task(self._auto_save_video_from_message(event))

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
        logger.debug("[Wardrobe] 开始存图，图片大小=%.2fKB 人格=%s 用户描述=%s", len(image_bytes) / 1024, persona or "无", user_description or "无")

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

        logger.debug(
            "[Wardrobe] 分析结果: 分类=%s 暴露=%s 描述=%s",
            attrs.get("category", "人物"),
            attrs.get("exposure_level", ""),
            attrs.get("description", ""),
        )

        return f"图片已保存到衣柜库（ID: {image_id}）"

    async def _save_image_from_bytes(
        self,
        image_bytes: bytes,
        *,
        persona: str = "",
        created_by: str = "",
        user_description: str = "",
        ai_prompt: str = "",
    ) -> tuple:
        await self._ensure_db()

        # 剥离 aiimg 自拍模式固定首尾，仅保留中间描述部分入库。
        ai_prompt = strip_ai_prompt_affixes(ai_prompt)

        max_size = _MAX_IMAGE_SIZE_MB
        if len(image_bytes) > max_size * 1024 * 1024:
            logger.warning("[Wardrobe] 图片过大 (%.1fMB)", len(image_bytes) / 1024 / 1024)
            return None, None, None

        file_hash = hashlib.md5(image_bytes).hexdigest()
        existing = await self.db.get_image_by_hash(file_hash)
        if existing:
            logger.debug("[Wardrobe] 图片重复，跳过保存: hash=%s 已存在ID=%s 人格=%s", file_hash, existing["id"], existing.get("persona", ""))
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
            persona=persona,
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
                ai_prompt=ai_prompt,
            )
            await self._index_to_vector(image_id, user_description or "模型分析失败，无描述", user_description,
                                         category="人物", persona=persona,
                                         style=[], clothing_type="")
            self._enqueue_comment(image_id)
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
            ai_prompt=ai_prompt,
        )

        desc_text = ensure_str(attrs.get("description"))
        await self._index_to_vector(
            image_id, desc_text, user_description,
            exposure_features=ensure_list(attrs.get("exposure_features")),
            key_features=ensure_list(attrs.get("key_features")),
            prop_objects=ensure_list(attrs.get("prop_objects")),
            allure_features=ensure_list(attrs.get("allure_features")),
            body_focus=ensure_list(attrs.get("body_focus")),
            style=ensure_list(attrs.get("style")),
            clothing_type=ensure_str(attrs.get("clothing_type")),
            category=category, persona=persona,
        )

        self._enqueue_comment(image_id)
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

        # _last_image_by_user 的值格式：{"path": Path, "mode": str, "prompt": str}
        # 仅保存自拍模式 (mode="selfie") 的图片
        if isinstance(entry, dict):
            image_path = entry.get("path")
            image_mode = entry.get("mode", "")
            ai_prompt = entry.get("prompt", "") or ""
        else:
            image_path = entry
            image_mode = ""
            ai_prompt = ""

        if not image_path:
            return

        if image_mode != "selfie":
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

            logger.debug(
                "[Wardrobe] AiImg 自动存图开始，图片大小=%.2fKB 人格=%s tool=%s",
                len(image_bytes) / 1024, persona or "无", tool_name or "command",
            )

            # 自动存图不传 user_description：此时没有用户主动提供的描述，
            # AI 分析模型会根据图片内容自动生成描述，无需额外文本。
            # 仅 /存图 命令路径才会传入用户描述。
            # ai_prompt 来自 aiimg 插件记录的生成提示词，原样透传入库。
            image_id, attrs, duplicate = await self._save_image_from_bytes(
                image_bytes, persona=persona, created_by=user_id, ai_prompt=ai_prompt,
            )

            if duplicate:
                logger.debug(
                    "[Wardrobe] AiImg 自动存图跳过：图片重复，已存在ID=%s",
                    duplicate.get("id", ""),
                )
                return

            if image_id:
                if attrs:
                    logger.debug(
                        "[Wardrobe] AiImg 自动存图完成 ID=%s 分类=%s 描述=%s",
                        image_id, attrs.get("category", ""),
                        attrs.get("description", ""),
                    )
                else:
                    logger.debug("[Wardrobe] AiImg 自动存图完成（分析失败）ID=%s", image_id)
            else:
                logger.warning("[Wardrobe] AiImg 自动存图失败")

        except Exception as e:
            logger.error("[Wardrobe] AiImg 自动存图异常: %s", e)

    async def _auto_save_video_from_message(self, event: AstrMessageEvent):
        enabled = self._cfg("auto_save_video_enabled")
        if enabled is None:
            enabled = self._cfg("auto_save_aiimg_enabled", False)
        if not enabled:
            return

        message_obj = getattr(event, "message_obj", None)
        if not message_obj:
            return

        message_chain = getattr(message_obj, "message", [])
        for comp in message_chain:
            if isinstance(comp, Video):
                video_src = getattr(comp, "url", None) or getattr(comp, "file", None) or getattr(comp, "path", None)
                if video_src:
                    self._spawn_bg_task(self._save_video_to_wardrobe(video_src, event))
                break

    async def _save_video_to_wardrobe(self, video_src: str, event: AstrMessageEvent):
        try:
            await self._ensure_db()
            self.video_service._ensure_dirs()

            video_src = str(video_src or "").strip()
            if not video_src:
                return

            video_filename = None
            video_path = None

            if video_src.startswith(("http://", "https://")):
                import httpx
                tmp_path = self.video_service.videos_dir / f"_tmp_{uuid.uuid4().hex}.mp4"
                try:
                    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
                        async with client.stream("GET", video_src) as resp:
                            if resp.status_code != 200:
                                logger.warning("[Wardrobe] 自动存视频下载失败 HTTP %d src=%s", resp.status_code, video_src[:80])
                                return
                            first_chunk = None
                            file_hash = hashlib.md5()
                            import aiofiles
                            async with aiofiles.open(tmp_path, "wb") as f:
                                async for chunk in resp.aiter_bytes(chunk_size=65536):
                                    if first_chunk is None:
                                        first_chunk = chunk
                                        if len(chunk) < 12 or chunk[4:8] != b'ftyp':
                                            tmp_path.unlink(missing_ok=True)
                                            return
                                    file_hash.update(chunk)
                                    await f.write(chunk)
                            if first_chunk is None:
                                logger.warning("[Wardrobe] 自动存视频下载失败: src=%s", video_src[:80])
                                tmp_path.unlink(missing_ok=True)
                                return

                    final_hash = file_hash.hexdigest()
                    video_filename = f"auto_{final_hash}.mp4"
                    video_path = self.video_service.videos_dir / video_filename
                    if video_path.exists():
                        logger.debug("[Wardrobe] 自动存视频跳过：文件已存在 hash=%s", final_hash)
                        tmp_path.unlink(missing_ok=True)
                        return
                    tmp_path.rename(video_path)
                except Exception as e:
                    if tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)
                    logger.warning("[Wardrobe] 自动存视频下载异常: %s src=%s", e, video_src[:80])
                    return
            else:
                video_bytes = await self._download_or_read_image(video_src)
                if not video_bytes:
                    logger.warning("[Wardrobe] 自动存视频下载失败: src=%s", video_src[:80])
                    return
                if len(video_bytes) < 12 or video_bytes[4:8] != b'ftyp':
                    return
                file_hash_val = hashlib.md5(video_bytes).hexdigest()
                video_filename = f"auto_{file_hash_val}.mp4"
                video_path = self.video_service.videos_dir / video_filename
                if video_path.exists():
                    return
                import aiofiles
                async with aiofiles.open(video_path, "wb") as f:
                    await f.write(video_bytes)

            await self.video_service._faststart_if_needed(video_path)

            source_image_id = await self._find_source_image_id(event)

            persona = await self._get_auto_save_persona(event)

            await self.db.add_video(
                source_image_id=source_image_id,
                video_path=video_filename,
                video_url=video_src if video_src.startswith(("http://", "https://")) else "",
                provider_id="auto_save",
                tier="normal",
                persona=persona,
                status="done",
            )

            logger.debug(
                "[Wardrobe] 自动存视频完成 source_image=%s persona=%s",
                source_image_id or "无", persona or "无",
            )

        except Exception as e:
            logger.error("[Wardrobe] 自动存视频异常: %s", e)

    async def _save_video_from_bytes(
        self,
        video_bytes: bytes,
        *,
        persona: str = "",
        source_image_path: str = "",
        created_by: str = "",
    ) -> str:
        await self._ensure_db()

        if len(video_bytes) < 12 or video_bytes[4:8] != b'ftyp':
            return ""

        file_hash = hashlib.md5(video_bytes).hexdigest()
        self.video_service._ensure_dirs()

        video_filename = f"auto_{file_hash}.mp4"
        video_path = self.video_service.videos_dir / video_filename
        if video_path.exists():
            logger.debug("[Wardrobe] _save_video_from_bytes 跳过：文件已存在 hash=%s", file_hash)
            return ""

        import aiofiles
        async with aiofiles.open(video_path, "wb") as f:
            await f.write(video_bytes)

        await self.video_service._faststart_if_needed(video_path)

        source_image_id = ""
        if source_image_path:
            source_image_id = await self._find_source_image_id_by_path(source_image_path)

        await self.db.add_video(
            source_image_id=source_image_id,
            video_path=video_filename,
            provider_id=created_by or "external",
            tier="normal",
            persona=persona,
            status="done",
        )

        logger.debug(
            "[Wardrobe] _save_video_from_bytes 完成 hash=%s size=%dKB source_image=%s persona=%s",
            file_hash, len(video_bytes) // 1024, source_image_id or "无", persona or "无",
        )
        return video_filename

    async def _find_source_image_id_by_path(self, image_path: str) -> str:
        if not image_path:
            return ""
        path = Path(image_path)
        if not path.exists():
            return ""
        try:
            import aiofiles
            async with aiofiles.open(path, "rb") as f:
                img_bytes = await f.read()
            if img_bytes:
                img_hash = hashlib.md5(img_bytes).hexdigest()
                existing = await self.db.get_image_by_hash(img_hash)
                if existing:
                    return existing["id"]
        except Exception:
            pass
        return ""

    async def _find_source_image_id(self, event: AstrMessageEvent) -> str:
        star = self.context.get_registered_star("astrbot_plugin_aiimg")
        if not star or not star.activated or not star.star_cls:
            return ""

        instance = star.star_cls
        last_image_dict = getattr(instance, "_last_image_by_user", None)
        if not last_image_dict:
            return ""

        user_id = str(event.get_sender_id() or "")
        entry = last_image_dict.get(user_id)
        if not entry:
            return ""

        image_path = entry.get("path") if isinstance(entry, dict) else entry
        if not image_path:
            return ""

        path = Path(image_path)
        if not path.exists():
            return ""

        try:
            import aiofiles
            async with aiofiles.open(path, "rb") as f:
                img_bytes = await f.read()
            if img_bytes:
                img_hash = hashlib.md5(img_bytes).hexdigest()
                existing = await self.db.get_image_by_hash(img_hash)
                if existing:
                    return existing["id"]
        except Exception:
            pass

        return ""

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
                except Exception:
                    pass

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
                except Exception:
                    pass
        except Exception:
            pass
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

        await self._cleanup_videos_for_image(image_id)

        return f"已删除图片（ID: {image_id}）"

    async def _cleanup_videos_for_image(self, image_id: str):
        try:
            videos = await self.db.get_videos_by_image_id(image_id)
            if not videos:
                return
            self.video_service._ensure_dirs()
            for v in videos:
                video_path_str = v.get("video_path", "")
                if video_path_str:
                    video_file = self.video_service.videos_dir / video_path_str
                    try:
                        if video_file.exists():
                            video_file.unlink()
                    except Exception:
                        pass
            await self.db.delete_videos_by_image_id(image_id)
            logger.debug("[Wardrobe] 已清理图片 %s 关联的 %d 个视频", image_id, len(videos))
        except Exception as e:
            logger.warning("[Wardrobe] 清理关联视频失败 image_id=%s error=%s", image_id, e)

    async def _do_get_stats(self) -> str:
        await self._ensure_db()

        stats = await self.db.get_stats()
        video_count = stats.get("video_count", 0)
        lines = [f"衣柜库共有 {stats['total']} 张图片, {video_count} 个视频"]

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
        self, query: str, current_persona: str = "",
        min_similarity: float | None = None,
        daily_selfie_mode: bool = False,
    ) -> Optional[dict]:
        await self._ensure_db()
        await self._ensure_vector_searcher()

        primary = str(self._cfg("search_provider_id", "") or "").strip()
        secondary = str(self._cfg("search_secondary_provider_id", "") or "").strip()
        timeout = float(self._cfg("search_timeout_seconds", 30.0) or 30.0)
        candidate_limit = int(self._cfg("search_candidate_limit", 20) or 20)

        if not primary and not secondary:
            logger.warning("[Wardrobe] 参考图搜索：未配置取图模型")
            return None

        resolved_persona = self._resolve_persona(current_persona)
        persona_names = self._get_persona_names_str()

        logger.debug(
            "[Wardrobe] 参考图搜索: query=%s exclude_persona=%s min_similarity=%s",
            query, resolved_persona or "无", min_similarity,
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
            persona_mode=str(self._cfg("search_persona_mode", "no_persona_only")),
            prioritize_unused=bool(self._cfg("search_prioritize_unused", False)),
            min_similarity=min_similarity,
            daily_selfie_mode=daily_selfie_mode,
        )

        if not results:
            logger.debug("[Wardrobe] 参考图搜索：未找到匹配图片（已排除当前人格）")
            return None

        best = results[0]
        image_path = self.store.get_image_path(best["image_path"])
        if not image_path.exists():
            logger.warning("[Wardrobe] 参考图搜索：图片文件不存在 %s", best["image_path"])
            return None

        logger.debug(
            "[Wardrobe] 参考图搜索完成: ID=%s 描述=%s",
            best["id"], best.get("description", ""),
        )

        try:
            await self.db.increment_use_count_by_persona(best["id"], resolved_persona)
        except Exception:
            pass

        if daily_selfie_mode:
            try:
                await self.db.increment_daily_selfie_use_count(best["id"])
                logger.debug(
                    "[Wardrobe] 补拍参考图计数+1: id=%s daily_selfie_use_count=%s",
                    best["id"], best.get("daily_selfie_use_count", 0),
                )
            except Exception:
                pass

        return {
            "image_path": str(image_path),
            "description": best.get("description", ""),
            "persona": best.get("persona", ""),
            "image_id": best["id"],
            "ref_strength": best.get("ref_strength", "style"),
        }

    async def _ensure_vector_searcher(self):
        if self.vector_searcher and self.vector_searcher.available:
            return
        new_vs = self._init_vector_searcher(self.data_dir)
        if new_vs:
            self.vector_searcher = new_vs
            self.searcher.vector_searcher = new_vs
            if not new_vs._initialized:
                await new_vs.initialize()
            if new_vs.available:
                await new_vs.index_existing_images()
                logger.debug("[Wardrobe] 向量检索器延迟初始化成功（搜索时触发）")
            if not self.rerank_provider:
                self.rerank_provider = self._init_rerank_provider()
                if self.rerank_provider:
                    new_vs.rerank_provider = self.rerank_provider

    async def _do_search_image(
        self, event: AstrMessageEvent, query: str, persona: str = ""
    ) -> str:
        await self._ensure_db()
        await self._ensure_vector_searcher()

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

        logger.debug(
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
            persona_mode=str(self._cfg("search_persona_mode", "no_persona_only") or "no_persona_only"),
            prioritize_unused=bool(self._cfg("search_prioritize_unused", False)),
        )

        logger.debug(
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
                await self.db.increment_use_count_by_persona(r["id"], current_persona)
            except Exception:
                pass

        if not image_paths:
            return "图片文件不存在"

        chain = [Image.fromFileSystem(path=p) for p in image_paths]
        mc = event.chain_result(chain)
        await self.context.send_message(event.unified_msg_origin, mc)

        parts = [f"已发送 {len(image_paths)} 张匹配的图片"]

        include_user_tags = True
        for i, r in enumerate(results[:3], 1):
            desc = r.get("description", "")
            img_persona = r.get("persona", "")
            if desc:
                parts.append(f"图片{i}描述：{desc}")
            if include_user_tags:
                ut = r.get("user_tags", "")
                if ut:
                    parts.append(f"图片{i}用户标注：{ut}")

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

    # ============ 素材库（部位素材 assets）============

    async def save_asset(self, image_bytes: bytes, user_note: str = "") -> dict:
        """上传部位素材图，调用存图模型产出短标签+完整描述并入库。

        Returns:
            {"asset_id": str, "short_tag": str, "description": str, "analyzed": bool, "error": str}
        """
        await self._ensure_db()

        user_note = (user_note or "").strip()
        if len(user_note) > _MAX_DESCRIPTION_LEN:
            user_note = user_note[:_MAX_DESCRIPTION_LEN]

        max_size = _MAX_IMAGE_SIZE_MB
        if len(image_bytes) > max_size * 1024 * 1024:
            return {"error": f"图片过大，限制{max_size}MB"}

        primary = str(self._cfg("save_provider_id", "") or "").strip()
        secondary = str(self._cfg("save_secondary_provider_id", "") or "").strip()
        timeout = float(self._cfg("save_timeout_seconds", 60.0) or 60.0)

        short_tag = ""
        description = ""
        analyzed = False
        if primary or secondary:
            attrs = await self.analyzer.analyze_asset(
                image_bytes,
                user_note=user_note,
                primary_provider_id=primary,
                secondary_provider_id=secondary,
                timeout_seconds=timeout,
            )
            if attrs:
                short_tag = ensure_str(attrs.get("short_tag"))
                description = ensure_str(attrs.get("description"))
                analyzed = True
        else:
            logger.warning("[Wardrobe] 未配置存图模型，素材仅保存原始图片")

        filename = await self.store.save_image(image_bytes)
        asset_id = await self.db.add_asset(
            short_tag=short_tag,
            description=description,
            user_note=user_note,
            image_path=filename,
        )
        return {
            "asset_id": asset_id,
            "short_tag": short_tag,
            "description": description,
            "analyzed": analyzed,
        }

    async def list_assets(self) -> list[dict]:
        """返回全部素材的短标签总览（供 LLM 总览感知）。"""
        await self._ensure_db()
        records = await self.db.list_assets()
        return [
            {
                "asset_id": r["id"],
                "short_tag": r.get("short_tag", "") or "",
            }
            for r in records
        ]

    async def get_asset_detail(self, asset_id: str) -> Optional[dict]:
        """返回单个素材的完整信息（含描述与图片路径）。

        描述中的「参考图N」占位符由调用方在运行时替换为真实序号。
        """
        await self._ensure_db()
        rec = await self.db.get_asset(asset_id)
        if not rec:
            return None
        image_path = self.store.get_image_path(rec["image_path"])
        if not image_path.exists():
            logger.warning("[Wardrobe] 素材图片文件不存在 %s", rec["image_path"])
            return None
        return {
            "asset_id": rec["id"],
            "short_tag": rec.get("short_tag", "") or "",
            "description": rec.get("description", "") or "",
            "image_path": str(image_path),
        }

    async def delete_asset(self, asset_id: str) -> bool:
        await self._ensure_db()
        rec = await self.db.get_asset(asset_id)
        if not rec:
            return False
        ok = await self.db.delete_asset(asset_id)
        if ok and rec.get("image_path"):
            try:
                await self.store.delete_image(rec["image_path"])
            except Exception as e:
                logger.debug("[Wardrobe] 删除素材图片失败: %s", e)
        return ok
