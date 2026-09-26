import json
import math
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

# ============================================================================
# 关键词召回（字面路）
#
# 存在理由：向量检索把整段描述压成一个向量，细粒度特征（某个手势、某个配饰）
# 在长描述里被稀释，短 query 与其算相似度还过不了 min_similarity 阈值——
# 结果就是"语义相近但特征不对"的图占满候选池。字面匹配是确定性的，用来兜这个底。
#
# 设计要点：
# 1. 命中即给【基础分】，基础分只按字段权重算，**与词频无关**——避免"用户偏好导致
#    该特征在库里高频 → IDF 低 → 被降权"的倒帮忙。
# 2. IDF 只作为【区分度加成】，且有上限、有 df 下限（df 太小的词多半是噪声/错字）。
# 3. 字面命中项不受 min_similarity 阈值限制，保底进入候选池。
# ============================================================================
_KEYWORD_ENABLED = True

# 字段权重：命中即按此给基础分（用户标注 > 魅力特征 > 关键特征 > 其余）
# 说明：allure_features 权重高于 key_features——多数取图诉求是冲着这个字段去的。
_KEYWORD_FIELD_WEIGHTS: list[tuple[str, float]] = [
    ("user_tags", 4.0),
    ("allure_features", 3.5),
    ("key_features", 3.0),
    ("exposure_features", 1.5),
    ("body_focus", 1.5),
    ("prop_objects", 1.5),
    ("description", 1.0),
    ("clothing_type", 0.5),
    ("style", 0.5),
]
_KEYWORD_BASE_WEIGHT = 1.0     # 基础分系数
_KEYWORD_BASE_FLOOR = 0.2      # 基础分的 df 折扣下限：泛词可以打折，但不许打到 0
                               # （否则用户偏好的高频特征会被降到跟没命中一样，帮倒忙）
_KEYWORD_IDF_WEIGHT = 1.0      # 区分度加成系数
_KEYWORD_IDF_MAX = 3.0         # IDF 上限，防止极稀有噪声词被抬上天
_KEYWORD_MIN_DF = 2            # 命中数低于此值不享受 IDF 加成（多半是噪声）
_KEYWORD_BONUS = 0.5           # 字面分归一化后可加成的最大相似度
_KEYWORD_MAX_TERMS = 6         # 全局只保留区分度最高的词元数（压泛词、控开销）
_KEYWORD_TERMS_PER_IMAGE = 3   # 每张图最多累计几个词元
_KEYWORD_TERM_DECAY = (1.0, 0.5, 0.25)  # 第 2、3 个词元边际衰减：
                               # 否则"命中一堆泛词"的图会靠数量盖过"命中一个稀有特征"的图；
                               # 保留多个高价值词（如同时命中三个条件）的累加优势
_KEYWORD_MAX_ITEMS = 30        # 最多把多少张字面命中图并入候选

# jieba 是 AstrBot 框架自带依赖（backend/app/requirements.txt: jieba>=0.42.1），
# 不需要写进本插件 requirements；但依然 try 包一层，取不到时回退到子串滑窗。
try:
    import logging as _std_logging

    import jieba

    jieba.setLogLevel(_std_logging.ERROR)
    _JIEBA_AVAILABLE = True
except Exception:  # pragma: no cover
    jieba = None
    _JIEBA_AVAILABLE = False

# 纯虚词/量词才进停用词表；泛词（cosplay、风格等）交给 IDF 自动降权，不手工列
_TERM_STOPWORDS = {
    "一张", "一个", "一张图", "照片", "图片", "画面", "这张", "那种", "这种",
    "的", "了", "和", "与", "有", "在", "是", "这", "那", "也", "都", "要",
    "我", "你", "她", "他", "它", "请", "给", "来", "去", "上", "下", "里",
}
_TERM_SPLIT_RE = re.compile(r"[，。、；：！？,.!?;:\s（）()\[\]【】《》\"'“”…·]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _extract_query_terms(query: str) -> list[str]:
    """按标点把 query 切成短语（虚词短语丢弃）。

    不在这里做滑窗——子串展开放到召回阶段按 df 筛选后再做，
    否则"重点是cosplay的服装"会被切成"是cos""lay的"这类垃圾片段，
    其中恰好在图库里出现过的那些会白白堆高无关图的分数。
    """
    if not query:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for seg in _TERM_SPLIT_RE.split(query):
        seg = seg.strip()
        if len(seg) < 2 or seg in _TERM_STOPWORDS or seg in seen:
            continue
        seen.add(seg)
        out.append(seg)
    return out


def _expand_phrase(phrase: str) -> list[str]:
    """把一个短语展开成候选词元，按可靠性排序：短语本身 > jieba 分词 > 纯中文子串。

    jieba 优先：它能正确切出"竖中指手势"→"中指"、"超薄白丝"→"超薄/白丝"这类词，
    比盲切子串准得多（jieba 是 AstrBot 框架自带依赖，云端可用）。
    子串滑窗只作兜底，覆盖 jieba 切不出来的新词/领域词（如某些服饰术语）。
    df=0 的候选会在召回阶段自然淘汰。
    """
    out: list[str] = [phrase]
    if _JIEBA_AVAILABLE and jieba is not None:
        try:
            for w in jieba.lcut_for_search(phrase):
                w = w.strip()
                if len(w) < 2 or w in _TERM_STOPWORDS:
                    continue
                out.append(w)
        except Exception:
            pass
    for n in (4, 3, 2):
        if n >= len(phrase):
            continue
        for i in range(len(phrase) - n + 1):
            sub = phrase[i:i + n]
            if sub in _TERM_STOPWORDS:
                continue
            if all(_CJK_RE.match(ch) for ch in sub):
                out.append(sub)
    seen: set[str] = set()
    uniq: list[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


# ============================================================================
# 索引文本组装
#
# 字段顺序与标签必须与入库时（main.py:_index_to_vector）一致——rerank 是拿
# query 与候选文档算相关度，字面命中项的文档若只给 description、向量命中项给
# 完整拼接，两边的文档不同构，字面项在精排阶段会天然吃亏。
# ============================================================================
_DOC_TEXT_FIELDS: list[tuple[str, str, bool]] = [
    ("description", "", False),
    ("user_tags", "标签", False),
    ("style", "风格", True),
    ("clothing_type", "服装", False),
    ("exposure_features", "暴露特征", True),
    ("key_features", "关键特征", True),
    ("prop_objects", "道具", True),
    ("allure_features", "魅力特征", True),
    ("body_focus", "身体焦点", True),
]


def _field_text(value: Any, is_list: bool) -> str:
    """把字段值转成纯文本：SQLite 原始字符串（可能是 JSON 数组的字面量）与已解析的 list 都能吃。"""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value if v)
    text = str(value or "").strip()
    if is_list and text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
        if isinstance(parsed, list):
            return " ".join(str(v) for v in parsed if v)
    return text


def _compose_doc_text(fields: dict[str, Any]) -> str:
    """按入库时的顺序与标签拼出完整索引文本。"""
    parts: list[str] = []
    for key, label, is_list in _DOC_TEXT_FIELDS:
        text = _field_text(fields.get(key, ""), is_list)
        if text:
            parts.append(f"{label}: {text}" if label else text)
    return " ".join(parts)



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

            # 字面路：细粒度特征在长描述里会被向量稀释甚至被阈值砍掉，
            # 这里用确定性匹配把命中的图保底送进候选池（不受 min_similarity 限制）。
            try:
                kw_scores = await self._keyword_recall(
                    processed_query, persona, exclude_persona, filter_no_persona
                )
            except Exception as e:
                # 带上栈：字面路被自己的 except 吞掉过一次（P0 tuple 取负），
                # 只留一行 warning 的话表面上"功能已上线"、实际整条失效，很难发现。
                logger.warning("[Wardrobe] 关键词召回失败（回退纯向量）: %s", e, exc_info=True)
                kw_scores = {}

            if kw_scores:
                max_kw = max(v[0] for v in kw_scores.values()) or 1.0
                kw_ratio: dict[str, float] = {}
                merged: dict[str, tuple[float, str]] = {}
                for wid, sim, content in filtered:
                    merged[wid] = (sim, content)
                for wid, (ks, kw_content) in kw_scores.items():
                    ratio = ks / max_kw
                    kw_ratio[wid] = ratio
                    bonus = _KEYWORD_BONUS * ratio
                    if wid in merged:
                        merged[wid] = (merged[wid][0] + bonus, merged[wid][1])
                    else:
                        # 只靠字面命中的图向量分低于阈值（它们本来就是被阈值挡掉才需要兜底），
                        # 若直接拿 bonus 参与排序，会被任何一张过了阈值的向量命中压到底部，
                        # 再被下面的截断丢掉——字面路等于白做。所以给它们与向量分同一量纲：
                        # 以阈值作基准再加字面加成，既能参与竞争，也不会凭空盖过更贴的向量结果。
                        merged[wid] = (min_similarity + bonus, kw_content)
                filtered = [(wid, sim, content) for wid, (sim, content) in merged.items()]
                # 同分时按字面证据强弱定序：否则"向量分 + 弱字面加成"和"阈值 + 强字面加成"
                # 打平时会靠字典插入顺序（向量项在前）决定胜负，字面命中又被挤掉。
                filtered.sort(key=lambda x: (-x[1], -kw_ratio.get(x[0], 0.0)))
                # 候选池回到 k 张的规模：字面路不再是不受 search_candidate_limit 约束的第二池子
                if len(filtered) > k:
                    filtered = filtered[:k]

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

    async def _keyword_recall(
        self,
        query: str,
        persona: Optional[str],
        exclude_persona: str,
        filter_no_persona: bool,
    ) -> dict[str, tuple[float, str]]:
        """字面关键词召回，返回 {wardrobe_id: (关键词分, 文档文本)}。

        与向量路互补：向量负责语义，字面负责细粒度特征的确定性命中。
        任何异常都返回空 dict，调用方回退为纯向量结果，不影响取图。
        """
        if not _KEYWORD_ENABLED:
            return {}
        phrases = _extract_query_terms(query)
        if not phrases:
            return {}
        candidates: list[str] = []
        for p in phrases:
            candidates.extend(_expand_phrase(p))
        seen_c: set[str] = set()
        terms = [c for c in candidates if not (c in seen_c or seen_c.add(c))]
        db = self.db or (getattr(self.plugin, "db", None) if self.plugin else None)
        if db is None:
            return {}

        fields = [f for f, _ in _KEYWORD_FIELD_WEIGHTS]
        cols = ", ".join(["id"] + fields)
        conds: list[str] = []
        params: list[Any] = []
        if filter_no_persona:
            conds.append("(persona = '' OR persona IS NULL)")
        elif persona:
            conds.append("persona = ?")
            params.append(persona)
        if exclude_persona:
            conds.append("persona != ?")
            params.append(exclude_persona)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""

        try:
            import aiosqlite

            async with aiosqlite.connect(db.db_path) as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(f"SELECT {cols} FROM images {where}", params) as cur:
                    rows = [dict(r) for r in await cur.fetchall()]
        except Exception as e:
            logger.warning("[Wardrobe] 关键词召回查询失败: %s", e)
            return {}

        if not rows:
            return {}

        # 第一轮：统计每个候选词元的命中数 df，并记录每张图命中了哪些词元、字段权重之和
        df: dict[str, int] = {t: 0 for t in terms}
        row_hits: dict[str, dict[str, float]] = {}
        row_content: dict[str, str] = {}
        for row in rows:
            wid = str(row.get("id", "") or "")
            if not wid:
                continue
            texts = {f: str(row.get(f, "") or "") for f, _ in _KEYWORD_FIELD_WEIGHTS}
            hit: dict[str, float] = {}
            for t in terms:
                fs = sum(w for f, w in _KEYWORD_FIELD_WEIGHTS if t in texts[f])
                if fs > 0:
                    hit[t] = fs
                    df[t] += 1
            if hit:
                row_hits[wid] = hit
                # 送 rerank 的文档用与入库同构的完整文本（原先只取 description，
                # 与向量命中项的文档不同构，字面项在精排阶段天然吃亏）
                row_content[wid] = _compose_doc_text(row)

        if not row_hits:
            return {}

        n = max(1, len(rows))

        # 每个短语只保留一个代表词元：优先短语本身（最精确），
        # 短语整体命中不了时才退到 df 最大的子串。避免同一特征被多个子串重复计分。
        representatives: set[str] = set()
        for p in phrases:
            if df.get(p, 0) > 0:
                representatives.add(p)
                continue
            cands = [c for c in _expand_phrase(p) if c != p and df.get(c, 0) > 0]
            if not cands:
                continue
            # jieba 可能切出多个有效词（"超薄"/"白丝"），都保留；
            # 只有"命中同一批图（df 相同）且互为子串"的才剔除，避免同一特征重复计分。
            ordered = sorted(cands, key=lambda c: (-df[c], -len(c)))
            picked: list[str] = []
            for c in ordered:
                if any(c in other and df[c] == df[other] for other in picked):
                    continue
                picked.append(c)
            representatives.update(picked)

        if not representatives:
            return {}

        # 只保留区分度最高的若干个词元：query 里的泛词（服装/照片/风格/动作）各自成词后
        # 会叠加出可观分数，盖过真正稀有的特征词——这一刀把它们压下去。
        if len(representatives) > _KEYWORD_MAX_TERMS:
            ranked_terms = sorted(
                representatives, key=lambda t: (-math.log(n / (df[t] + 1)), -len(t))
            )
            representatives = set(ranked_terms[:_KEYWORD_MAX_TERMS])

        # 第二轮：基础分（按 df 打折、有下限）+ 封顶的 IDF 加成
        scores: dict[str, tuple[float, str]] = {}
        for wid, hit in row_hits.items():
            contributions: list[float] = []
            for t, fs in hit.items():
                if t not in representatives:
                    continue
                d = df.get(t, 0)
                # 基础分：命中就给，但按 df 打折——否则"服装/照片/风格"这类泛词
                # 各自贡献一点、叠加起来会盖过真正稀有的特征词。打折有下限，
                # 保证高频偏好特征仍有可观分数（不至于被打成没命中）。
                df_factor = max(_KEYWORD_BASE_FLOOR, 1.0 - (d / n))
                contrib = fs * _KEYWORD_BASE_WEIGHT * df_factor
                if d >= _KEYWORD_MIN_DF:
                    idf = math.log(n / (d + 1))
                    contrib += _KEYWORD_IDF_WEIGHT * min(_KEYWORD_IDF_MAX, max(0.0, idf))
                contributions.append(contrib)
            if not contributions:
                continue
            contributions.sort(reverse=True)
            score = 0.0
            for i, c in enumerate(contributions[:_KEYWORD_TERMS_PER_IMAGE]):
                decay = _KEYWORD_TERM_DECAY[i] if i < len(_KEYWORD_TERM_DECAY) else _KEYWORD_TERM_DECAY[-1]
                score += c * decay
            if score > 0:
                scores[wid] = (score, row_content.get(wid, ""))

        if not scores:
            return {}

        ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
        trimmed = dict(ranked[:_KEYWORD_MAX_ITEMS])
        logger.debug(
            "[Wardrobe] 关键词召回: 词元%d个 命中%d张 并入%d张",
            len(terms), len(scores), len(trimmed),
        )
        return trimmed

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
            return _compose_doc_text(rec)
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
