import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger

from .database import WardrobeDatabase

try:
    from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
    from astrbot.core.provider.provider import EmbeddingProvider
    from astrbot.core.provider.provider import RerankProvider

    _VECTORD_AVAILABLE = True
except ImportError:
    _VECTORD_AVAILABLE = False

# 解析"重点是X。"前缀，X 为本次自拍的核心视觉焦点
_FOCUS_PREFIX_RE = re.compile(r"^重点是(.+?)[。.]")
# 焦点路召回的 similarity 加权倍数（让焦点匹配的图更易进入候选池）
_FOCUS_WEIGHT = 1.3


class WardrobeVectorSearcher:
    def __init__(
        self,
        data_dir: str,
        embedding_provider: Any = None,
        db: WardrobeDatabase | None = None,
        plugin: Any = None,
    ):
        self.data_dir = data_dir
        self.embedding_provider = embedding_provider
        self.db = db
        self.plugin = plugin
        self.rerank_provider: Any = None
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
        except Exception:
            pass

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
                except Exception:
                    pass

            if duplicate_doc_ids:
                logger.debug("[Wardrobe] 清理重复向量索引: %d条", len(duplicate_doc_ids))

            logger.debug("[Wardrobe] 向量索引ID映射重建完成: %d条记录", len(self._id_map))
        except Exception:
            pass

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
        persona: Optional[str] = None,
        exclude_persona: str = "",
        min_similarity: float | None = None,
    ) -> list[tuple[str, float]]:
        if not self.available:
            return []

        if not query or not query.strip():
            return []

        if min_similarity is None:
            min_similarity = float(self.plugin._cfg("vector_search_min_similarity", 0.5) or 0.5) if self.plugin else 0.5

        processed_query = query[:2000] if len(query) > 2000 else query

        # 解析"重点是X。"前缀：X 是本次自拍的核心视觉焦点。
        # 长 query 中 X 的语义会被其余描述稀释导致召回不到，因此对 X 单独做一路召回，
        # 再与主路结果融合（焦点路 similarity 加权），确保焦点相关图能进入候选池。
        focus_match = _FOCUS_PREFIX_RE.match(processed_query)
        focus_term = focus_match.group(1).strip() if focus_match else ""

        try:
            metadata_filters = {}
            filter_no_persona = persona is not None and persona == ""
            if persona and not filter_no_persona:
                metadata_filters["persona_id"] = persona

            # 扩大候选池：让 LLM 选择阶段有足够多的候选图片可选，
            # 避免"好图被相似度筛掉、候选池太小导致热度平衡机制用不上"的问题。
            # 有 metadata_filters 时按人格池过滤，命中率更低，需要更大的 fetch_k。
            fetch_k = k * 5 if metadata_filters else k * 3
            if filter_no_persona or exclude_persona:
                fetch_k = max(fetch_k, k * 5)

            # 主路：完整 query 检索
            filtered = await self._retrieve_and_filter(
                processed_query, k, fetch_k, min_similarity,
                metadata_filters, filter_no_persona, exclude_persona,
            )

            # 焦点路：用 X 单独检索，融合两路结果（焦点路加权）
            if focus_term and 1 <= len(focus_term) <= 100:
                focus_filtered = await self._retrieve_and_filter(
                    focus_term, k, fetch_k, min_similarity,
                    metadata_filters, filter_no_persona, exclude_persona,
                )
                if focus_filtered:
                    main_count = len(filtered)
                    wid_map: dict[str, tuple[float, str]] = {}
                    for wid, sim, content in filtered:
                        wid_map[wid] = (sim, content)
                    for wid, sim, content in focus_filtered:
                        weighted_sim = sim * _FOCUS_WEIGHT
                        if wid not in wid_map or weighted_sim > wid_map[wid][0]:
                            wid_map[wid] = (weighted_sim, content)
                    filtered = [(wid, sim, content) for wid, (sim, content) in wid_map.items()]
                    logger.debug(
                        "[Wardrobe] 焦点多路召回: 主路%d张 焦点路%d张 融合后%d张 (focus=%s)",
                        main_count, len(focus_filtered), len(filtered), focus_term,
                    )

            if not filtered:
                return []

            reranked = await self._rerank_results(processed_query, filtered)
            if reranked is not None:
                return reranked

            return [(r[0], r[1]) for r in filtered]
        except Exception as e:
            logger.warning("[Wardrobe] 向量检索失败（将回退到本地检索）: %s", e)
            return []

    async def _retrieve_and_filter(
        self,
        query: str,
        k: int,
        fetch_k: int,
        min_similarity: float,
        metadata_filters: dict,
        filter_no_persona: bool,
        exclude_persona: str,
    ) -> list[tuple[str, float, str]]:
        """向量检索 + 过滤，返回 (wardrobe_id, similarity, doc_content) 列表。"""
        results = await self._faiss_db.retrieve(
            query=query,
            k=k,
            fetch_k=fetch_k,
            rerank=False,
            metadata_filters=metadata_filters if metadata_filters else None,
        )

        filtered: list[tuple[str, float, str]] = []
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

            doc_persona = meta.get("persona_id", meta.get("persona", ""))
            if filter_no_persona:
                if doc_persona:
                    continue

            if exclude_persona:
                if doc_persona == exclude_persona:
                    continue

            doc_content = doc_data.get("content", "")
            filtered.append((wid, result.similarity, doc_content))
        return filtered

    async def _rerank_results(
        self,
        query: str,
        candidates: list[tuple[str, float, str]],
    ) -> list[tuple[str, float]] | None:
        if not self.rerank_provider:
            return None

        rerank_min = int(self.plugin._cfg("rerank_min_candidates", 3) or 3) if self.plugin else 3
        if len(candidates) < rerank_min:
            return None

        rerank_top_k = int(self.plugin._cfg("rerank_top_k", 0) or 0) if self.plugin else 0

        if len(query) > 512:
            query = query[:512]

        documents = []
        for wid, sim, doc_content in candidates:
            if doc_content:
                documents.append(doc_content)
            else:
                reconstructed = await self._reconstruct_doc_text(wid)
                documents.append(reconstructed)

        try:
            top_n = rerank_top_k if rerank_top_k > 0 else len(documents)
            rerank_results = await self.rerank_provider.rerank(query, documents, top_n=top_n)

            if not rerank_results:
                return None

            output: list[tuple[str, float]] = []
            for rr in rerank_results:
                idx = rr.index
                if 0 <= idx < len(candidates):
                    output.append((candidates[idx][0], rr.relevance_score))

            logger.debug(
                "[Wardrobe] 重排序完成: 候选%d张 → 保留%d张",
                len(candidates), len(output),
            )
            return output
        except Exception as e:
            logger.warning("[Wardrobe] 重排序失败，使用原始排序: %s", e)
            return None

    async def _reconstruct_doc_text(self, wardrobe_id: str) -> str:
        if not self.db:
            return ""
        try:
            rec = await self.db.get_image(wardrobe_id)
            if not rec:
                return ""
            text_parts = []
            desc = rec.get("description", "")
            if desc:
                text_parts.append(desc)
            tags = rec.get("user_tags", "")
            if tags:
                text_parts.append(f"标签: {tags}")
            style_val = rec.get("style", "")
            if isinstance(style_val, list):
                style_val = " ".join(str(v) for v in style_val if v)
            if style_val:
                text_parts.append(f"风格: {style_val}")
            clothing = rec.get("clothing_type", "")
            if clothing:
                text_parts.append(f"服装: {clothing}")
            for field, label in [
                ("exposure_features", "暴露特征"),
                ("key_features", "关键特征"),
                ("prop_objects", "道具"),
                ("allure_features", "魅力特征"),
                ("body_focus", "身体焦点"),
            ]:
                val = rec.get(field, "")
                if isinstance(val, list):
                    val = " ".join(str(v) for v in val if v)
                if val:
                    text_parts.append(f"{label}: {val}")
            return " ".join(text_parts)
        except Exception:
            return ""

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
                style_val = rec.get("style", "")
                if style_val:
                    if isinstance(style_val, list):
                        style_val = " ".join(str(v) for v in style_val if v)
                    if style_val:
                        text_parts.append(f"风格: {style_val}")
                clothing = rec.get("clothing_type", "")
                if clothing:
                    text_parts.append(f"服装: {clothing}")
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
        if self._faiss_db:
            try:
                if hasattr(self._faiss_db, 'save'):
                    await self._faiss_db.save()
                elif hasattr(self._faiss_db, 'persist'):
                    await self._faiss_db.persist()
            except Exception as e:
                logger.debug("[Wardrobe] 向量索引持久化失败: %s", e)
        self._faiss_db = None
        self._initialized = False
        self._id_map.clear()
        self._reverse_map.clear()
