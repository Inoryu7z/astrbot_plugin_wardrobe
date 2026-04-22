import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger

from .database import WardrobeDatabase

try:
    from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
    from astrbot.core.provider.provider import EmbeddingProvider

    _VECTORD_AVAILABLE = True
except ImportError:
    _VECTORD_AVAILABLE = False


class WardrobeVectorSearcher:
    def __init__(
        self,
        data_dir: str,
        embedding_provider: Any = None,
        db: WardrobeDatabase | None = None,
    ):
        self.data_dir = data_dir
        self.embedding_provider = embedding_provider
        self.db = db
        self._faiss_db = None
        self._initialized = False
        self._id_map: dict[str, str] = {}
        self._reverse_map: dict[str, str] = {}

        if not _VECTORD_AVAILABLE:
            logger.info("[Wardrobe] FaissVecDB 不可用，向量检索已禁用")

    @property
    def available(self) -> bool:
        return _VECTORD_AVAILABLE and self._embedding_provider is not None and self._initialized

    @property
    def _embedding_provider(self):
        return self.embedding_provider

    async def initialize(self):
        if not _VECTORD_AVAILABLE:
            return
        if not self.embedding_provider:
            logger.info("[Wardrobe] 未配置 Embedding Provider，向量检索已禁用")
            return

        try:
            db_path = os.path.join(self.data_dir, "wardrobe_vec.db")
            index_path = os.path.join(self.data_dir, "wardrobe_vec.index")

            self._check_dimension(index_path)

            self._faiss_db = FaissVecDB(db_path, index_path, self.embedding_provider)
            await self._faiss_db.initialize()
            self._initialized = True

            await self._rebuild_id_map()

            logger.info("[Wardrobe] 向量检索已初始化")
        except Exception as e:
            logger.warning("[Wardrobe] 向量检索初始化失败（将回退到本地检索）: %s", e)
            self._initialized = False

    def _check_dimension(self, index_path: str):
        if not os.path.exists(index_path):
            return
        try:
            import faiss

            old_index = faiss.read_index(index_path)
            old_dim = old_index.d
            new_dim = self.embedding_provider.get_dim()
            if old_dim != new_dim:
                logger.warning(
                    "[Wardrobe] FAISS 索引维度不匹配: 旧=%d 新=%d，删除旧索引重建",
                    old_dim, new_dim,
                )
                os.remove(index_path)
                db_path = os.path.join(self.data_dir, "wardrobe_vec.db")
                if os.path.exists(db_path):
                    os.remove(db_path)
        except Exception as e:
            logger.debug("[Wardrobe] 维度检查跳过: %s", e)

    async def _rebuild_id_map(self):
        if not self._faiss_db:
            return
        try:
            db_path = os.path.join(self.data_dir, "wardrobe_vec.db")
            if not os.path.exists(db_path):
                return

            import aiosqlite

            duplicate_doc_ids = []
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute("SELECT doc_id, metadata FROM documents") as cursor:
                    async for row in cursor:
                        doc_id = str(row[0])
                        metadata_str = row[1] or "{}"
                        try:
                            metadata = json.loads(metadata_str) if isinstance(metadata_str, str) else metadata_str
                        except (json.JSONDecodeError, TypeError):
                            metadata = {}

                        wardrobe_id = metadata.get("wardrobe_id", "")
                        if not wardrobe_id:
                            continue

                        if wardrobe_id in self._id_map:
                            duplicate_doc_ids.append(self._id_map[wardrobe_id])

                        self._id_map[wardrobe_id] = doc_id
                        self._reverse_map[doc_id] = wardrobe_id

            for dup_doc_id in duplicate_doc_ids:
                self._reverse_map.pop(dup_doc_id, None)
                try:
                    await self._faiss_db.delete(dup_doc_id)
                    logger.debug("[Wardrobe] 清理重复向量索引: doc_id=%s", dup_doc_id)
                except Exception as e:
                    logger.debug("[Wardrobe] 清理重复向量索引失败: doc_id=%s error=%s", dup_doc_id, e)

            if duplicate_doc_ids:
                logger.info("[Wardrobe] 清理重复向量索引: %d条", len(duplicate_doc_ids))

            logger.info("[Wardrobe] 向量索引ID映射重建完成: %d条记录", len(self._id_map))
        except Exception as e:
            logger.debug("[Wardrobe] 向量索引ID映射重建跳过: %s", e)

    async def add_image(self, wardrobe_id: str, text: str, category: str = "", persona: str = ""):
        if not self.available:
            return
        if not text or not text.strip():
            return

        if wardrobe_id in self._id_map:
            await self.remove_image(wardrobe_id)

        content = text[:4000] if len(text) > 4000 else text
        metadata = {
            "wardrobe_id": wardrobe_id,
            "category": category,
            "persona": persona or "",
            "importance": 0.5,
            "create_time": time.time(),
            "last_access_time": time.time(),
            "session_id": None,
            "persona_id": persona or "",
        }

        try:
            doc_id = await self._faiss_db.insert(content=content, metadata=metadata)
            self._id_map[wardrobe_id] = str(doc_id)
            self._reverse_map[str(doc_id)] = wardrobe_id
            logger.debug("[Wardrobe] 向量索引已添加: wardrobe_id=%s doc_id=%s", wardrobe_id, doc_id)
        except Exception as e:
            logger.warning("[Wardrobe] 向量索引添加失败: wardrobe_id=%s error=%s", wardrobe_id, e)

    async def remove_image(self, wardrobe_id: str):
        if not self.available:
            return
        doc_id = self._id_map.pop(wardrobe_id, None)
        if not doc_id:
            return
        self._reverse_map.pop(doc_id, None)
        try:
            await self._faiss_db.delete(doc_id)
        except Exception as e:
            logger.debug("[Wardrobe] 向量索引删除失败: doc_id=%s error=%s", doc_id, e)

    async def search(
        self,
        query: str,
        k: int = 20,
        persona: str = "",
        exclude_persona: str = "",
        min_similarity: float = 0.5,
    ) -> list[str]:
        if not self.available:
            return []

        if not query or not query.strip():
            return []

        processed_query = query[:2000] if len(query) > 2000 else query

        try:
            metadata_filters = {}
            if persona:
                metadata_filters["persona_id"] = persona

            fetch_k = k * 3 if metadata_filters else k * 2
            results = await self._faiss_db.retrieve(
                query=processed_query,
                k=k,
                fetch_k=fetch_k,
                rerank=False,
                metadata_filters=metadata_filters if metadata_filters else None,
            )

            wardrobe_ids = []
            seen = set()
            for result in results:
                if result.similarity < min_similarity:
                    continue

                doc_data = result.data
                meta = doc_data.get("metadata", {})
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (json.JSONDecodeError, TypeError):
                        meta = {}

                wid = meta.get("wardrobe_id", "")
                if not wid:
                    wid = self._reverse_map.get(str(doc_data.get("id", "")), "")

                if not wid:
                    continue

                if wid in seen:
                    continue
                seen.add(wid)

                if exclude_persona:
                    doc_persona = meta.get("persona_id", meta.get("persona", ""))
                    if doc_persona == exclude_persona:
                        continue

                wardrobe_ids.append(wid)

            logger.debug("[Wardrobe] 向量检索: query=%s 返回%d张(阈值%.2f)", query[:50], len(wardrobe_ids), min_similarity)
            return wardrobe_ids
        except Exception as e:
            logger.warning("[Wardrobe] 向量检索失败（将回退到本地检索）: %s", e)
            return []

    async def index_existing_images(self):
        if not self.available or not self.db:
            return

        logger.info("[Wardrobe] 开始索引已有图片...")
        try:
            records = await self.db.get_all_records()
            indexed = 0
            skipped = 0
            for rec in records:
                wid = rec.get("id", "")
                if wid in self._id_map:
                    skipped += 1
                    continue

                text_parts = []
                desc = rec.get("description", "")
                if desc:
                    text_parts.append(desc)
                tags = rec.get("user_tags", "")
                if tags:
                    text_parts.append(f"标签: {tags}")
                exp_feat = rec.get("exposure_features", "")
                if exp_feat:
                    if isinstance(exp_feat, list):
                        exp_feat = " ".join(str(v) for v in exp_feat if v)
                    if exp_feat:
                        text_parts.append(f"暴露特征: {exp_feat}")
                key_feat = rec.get("key_features", "")
                if key_feat:
                    if isinstance(key_feat, list):
                        key_feat = " ".join(str(v) for v in key_feat if v)
                    if key_feat:
                        text_parts.append(f"关键特征: {key_feat}")
                props = rec.get("prop_objects", "")
                if props:
                    if isinstance(props, list):
                        props = " ".join(str(v) for v in props if v)
                    if props:
                        text_parts.append(f"道具: {props}")
                allure = rec.get("allure_features", "")
                if allure:
                    if isinstance(allure, list):
                        allure = " ".join(str(v) for v in allure if v)
                    if allure:
                        text_parts.append(f"魅力特征: {allure}")
                bf = rec.get("body_focus", "")
                if bf:
                    if isinstance(bf, list):
                        bf = " ".join(str(v) for v in bf if v)
                    if bf:
                        text_parts.append(f"身体焦点: {bf}")

                text = " ".join(text_parts)
                if not text.strip():
                    skipped += 1
                    continue

                await self.add_image(
                    wardrobe_id=wid,
                    text=text,
                    category=rec.get("category", ""),
                    persona=rec.get("persona", ""),
                )
                indexed += 1

            logger.info("[Wardrobe] 已有图片索引完成: 新索引%d张, 跳过%d张", indexed, skipped)
        except Exception as e:
            logger.error("[Wardrobe] 索引已有图片失败: %s", e, exc_info=True)

    async def terminate(self):
        self._faiss_db = None
        self._initialized = False
        self._id_map.clear()
        self._reverse_map.clear()
