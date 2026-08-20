import asyncio
import json
import time
from typing import Any, Optional

from astrbot.api import logger

from .database import WardrobeDatabase
from .image_store import ImageStore
from .utils import parse_json_response

try:
    from .vector_searcher import WardrobeVectorSearcher
    _VEC_AVAILABLE = True
except ImportError:
    _VEC_AVAILABLE = False

# 补拍取图硬编码参数（原配置项已移除，固定取值）
_DAILY_SELFIE_RECALL_K = 40    # 补拍候选召回数量
_DAILY_SELFIE_COLD_SLACK = 1   # 补拍冷梯队容差
_DAILY_SELFIE_COLD_SEATS = 3   # 补拍冷图配额席位


SEARCH_PARSE_SYSTEM_PROMPT = """# 角色
你是图片检索意图解析助手。根据用户的自然语言描述，生成结构化的查询条件。

# 任务
解析用户的检索意图，输出 JSON 格式的查询条件。

# 可用查询字段
- category: "人物" 或 "衣服"（可选）
- style: 风格列表，从风格池中选择（可选）
- exposure_level: "保守"/"轻微"/"中等"/"明显"/"极限"（可选）
- scene: 场景列表，从场景池中选择（可选）
- atmosphere: 氛围列表，从氛围池中选择（可选）
- pose_type: 从姿势类型池中选择（可选）
- body_focus: 身体焦点列表，如"胸部特写""臀部特写""腿部特写"等（可选）
- keywords: 关键词列表，用于描述匹配（可选）
- persona: 人格名称（可选，仅在 persona_scope 为 named 时填写具体名称）
- persona_scope: 人格搜索范围，必填，取值如下：
  - "self": 用户在指代自己/当前人格（如"发一张你的cos照""你有没有洛丽塔"），指代模糊时默认为此
  - "other": 用户明确要别人的/非当前人格的图（如"有没有别人的漂亮图片""其他人的cos"）
  - "named": 用户明确提到某个具体人格名（如"星织有没有拍过xxx"），此时 persona 填写该名称
  - "global": 用户泛泛询问不涉及任何人格（如"有没有穿洛丽塔的美少女"），或人格无关的纯内容搜索

# 预定义值池（请优先从中选择）
{pools_text}

# 人格判断规则
当前对话人格：{current_persona}
已有的人格目录：{persona_names}

判断逻辑：
- 用户用"你""自己""我"等指代当前对话人格 → persona_scope="self"
- 用户说"别人""其他人""别的"等明确排除当前人格 → persona_scope="other"
- 用户明确提到某个具体人格名且在人格目录中 → persona_scope="named"，persona 填写该名称
- 用户没有提到任何人格且语气泛泛 → persona_scope="self"（指代模糊默认当作在说自己）
- 纯内容搜索完全不涉及人格 → persona_scope="global"
- 如果提到的人格名不在目录中 → persona_scope="global"

# 规则
1. 只输出 JSON，不要输出解释
2. 用户可能描述得很模糊，尽量推断最合理的查询条件
3. 如果用户没有明确指定分类，不要填写 category
4. style/scene/atmosphere 请优先从预定义值池中选择，确保与存图时的标签一致
5. keywords 用于捕捉无法用预定义值表达的特征"""

SEARCH_SELECT_SYSTEM_PROMPT = """# 角色
你是图片选择助手。从给定的候选图片中，选出符合用户需求的图片。

# 任务
根据用户的检索描述和候选图片的属性信息，选出匹配的图片。

# 输出格式
输出 JSON 对象：
```json
{{
  "selected_ids": ["选中的图片ID列表"],
  "reason": "选择理由"
}}
```

# 选择策略（优先级从高到低）
0. **绝对最高优先级**：user_tags（用户标签）——这是用户本人对图片的明确标注，优先级高于一切其他字段。当 user_tags 与 description、style、clothing_type 等任何字段冲突时，一律以 user_tags 为准，不得被 description 或自身判断误导。
   - 例：description 写"图片在cos知更鸟"，但 user_tags 写"cos朵莉亚" → 该图片应被视为"cos朵莉亚"，按朵莉亚匹配，而非知更鸟
   - 用户标签代表用户真实意图，即使你认为描述更准确也必须服从 user_tags
1. **高优先级**：clothing_type（服装类型）、description 中的服装与姿势表述、body_focus（身体焦点）——这些直接决定"拍的是什么"
2. **中等优先级**：scene（场景）
3. **低优先级**：composition（构图）
4. style（风格）和 atmosphere（氛围）仅作为辅助参考，不作为主要匹配依据

# 热度平衡（use_count）
每个候选图片都有 use_count 字段，表示该图片被当前人格取用的次数。当多张图片内容匹配度**相近**时，优先选择 use_count 较低的图片，让每张图片都有被使用的机会。
- **内容匹配度始终优先于热度平衡**：例如用户要"粉色水手服"，100热度的粉色图优先于0热度的黑色图
- **只有内容匹配度相近时才考虑热度**：例如两张都是粉色水手服，选0热度的那张
- **没有合适匹配时返回空列表**：不要为了低热度而选择不匹配的图片

# 规则
1. 最多选择 {max_select} 张图片
2. 匹配标准宽松：完全匹配、大部分匹配、语义可能相关的图片都应返回；只有完全不匹配才排除
3. 宁可多返回也不要漏掉可能匹配的图片，空结果是最差体验
4. 只输出 JSON，不要输出解释"""


# 喜爱程度对"有效热度"的折扣系数：同一热度下优先选喜爱的图。
# 特别喜爱(favorite)=实际*0.6（-40%），普通喜爱(like)=实际*0.8（-20%）。
_LIKE_HEAT_FACTOR = 0.8
_FAVORITE_HEAT_FACTOR = 0.6


def _heat_factor(favorite: str) -> float:
    """按喜爱程度返回有效热度系数。"""
    if favorite == "favorite":
        return _FAVORITE_HEAT_FACTOR
    if favorite == "like":
        return _LIKE_HEAT_FACTOR
    return 1.0


class ImageSearcher:
    def __init__(self, context, db: WardrobeDatabase, store: ImageStore, vector_searcher=None):
        self.context = context
        self.db = db
        self.store = store
        self.vector_searcher = vector_searcher
        self._pools_text_cache = None
        self._pools_text_ts = 0
        self._pools_text_persona = ""

    def _cfg_value(self, key: str, default):
        """读取 wardrobe 插件配置，失败或不可用时回退默认值。"""
        try:
            plugin = getattr(self.context, "_wardrobe_plugin", None)
            if plugin is not None:
                return plugin._cfg(key, default)
        except Exception:
            pass
        return default

    async def _merge_cold_seats(
        self,
        user_query: str,
        candidates: list[dict[str, Any]],
        current_persona: str,
        seats: int,
    ) -> list[dict[str, Any]]:
        """A1 冷图配额席位：用 query 取一段"不过滤相似度"的最近邻，
        从中挑热度最低的 seats 张并入候选池，让再冷门/相似度再低的图也有机会被冷梯队捞到。"""
        try:
            if seats <= 0:
                return candidates
            existing_ids = {c["id"] for c in candidates}
            # min_similarity=0.0 = 不过滤相似度（与 cosplay 语义一致），只按距离取最近，再挑最冷
            seed = await self._vector_search(user_query, k=seats * 2, persona="", min_similarity=0.0)
            if not seed:
                return candidates
            extra = [c for c in seed if c["id"] not in existing_ids]
            if not extra:
                return candidates
            # 注入按人格热度，取最冷的 seats 张
            if current_persona and current_persona.strip():
                counts = await self.db.get_use_counts_by_persona(
                    [c["id"] for c in extra], current_persona.strip()
                )
                for c in extra:
                    c["use_count"] = counts.get(c["id"], 0)
            else:
                for c in extra:
                    c["use_count"] = 0
            extra.sort(
                key=lambda c_: (
                    int(c_.get("use_count", 0) or 0)
                    + int(c_.get("daily_selfie_use_count", 0) or 0)
                )
            )
            extra = extra[:seats]
            logger.debug(
                "[Wardrobe] 冷图配额席位合并 +%d 张 (query=%s)", len(extra), user_query[:50]
            )
            return candidates + extra
        except Exception as exc:
            logger.warning("[Wardrobe] 冷图配额合并失败: %s", exc)
            return candidates

    async def _get_pools_text(self, persona: str = "") -> str:
        persona_key = (persona or "").strip()
        now = time.time()
        # 缓存键包含 persona，避免不同人格命中同一份缓存
        if (
            self._pools_text_cache
            and now - self._pools_text_ts < 300
            and self._pools_text_persona == persona_key
        ):
            return self._pools_text_cache

        plugin = getattr(self.context, '_wardrobe_plugin', None)
        try:
            from .pools import ALL_POOLS
            pools = await plugin.get_merged_pools() if plugin else ALL_POOLS
            pools = {k: list(v) for k, v in pools.items()}
        except Exception:
            from .pools import ALL_POOLS
            pools = {k: list(v) for k, v in ALL_POOLS.items()}

        # 人格级风格池覆盖：若该人格配置了自定义 style 池，则替换全局 style 池
        # get_style_pool_for_persona 返回 None 表示未配置（回退全局），返回 list 则覆盖
        if persona_key and plugin:
            try:
                persona_styles = await plugin.get_style_pool_for_persona(persona_key)
                if persona_styles is not None:
                    pools["style"] = list(persona_styles)
            except Exception as exc:
                logger.warning("[Wardrobe] 获取人格风格池失败 persona=%s error=%s", persona_key, exc)

        search_pools = {k: v for k, v in pools.items() if k in ("style", "scene", "atmosphere", "clothing_type")}
        lines = []
        for key, values in search_pools.items():
            lines.append(f"## {key}")
            for v in values:
                lines.append(f"- {v}")
            lines.append("")

        self._pools_text_cache = "\n".join(lines)
        self._pools_text_ts = now
        self._pools_text_persona = persona_key
        return self._pools_text_cache

    @staticmethod
    def _sort_by_favorite(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fav_order = {"favorite": 0, "like": 1}
        return sorted(results, key=lambda r: fav_order.get(r.get("favorite", "none"), 2))

    async def search(
        self,
        user_query: str,
        *,
        primary_provider_id: str,
        secondary_provider_id: str = "",
        timeout_seconds: float = 30.0,
        candidate_limit: int = 20,
        max_select: int = 1,
        persona: str = "",
        current_persona: str = "",
        persona_names: str = "",
        exclude_current_persona: bool = False,
        persona_mode: str = "no_persona_only",
        prioritize_unused: bool = False,
        min_similarity: float | None = None,
        daily_selfie_mode: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        meta = {"persona_mismatch": False, "searched_persona": persona, "persona_scope": "global"}

        # 补拍召回扩池：拉大候选数量，让更多相关但低热度/未用图进入候选池。
        # 这里只放宽候选数量，不改相似度阈值。
        if daily_selfie_mode:
            _recall_k = _DAILY_SELFIE_RECALL_K
            if _recall_k and _recall_k > candidate_limit:
                candidate_limit = int(_recall_k)

        if self.vector_searcher and self.vector_searcher.available and exclude_current_persona and current_persona:
            if persona_mode == "no_persona_only":
                candidates = await self._vector_search(user_query, k=candidate_limit, persona="", min_similarity=min_similarity)
                logger.debug(
                    "[Wardrobe] 向量检索（no_persona_only）: %d张 persona=无人格",
                    len(candidates),
                )
                if candidates:
                    candidates = self._sort_by_favorite(candidates)
                    meta["searched_persona"] = ""
                else:
                    logger.debug("[Wardrobe] 无人格池无结果，no_persona_only模式不回退其他人格")
                    return [], meta
            else:
                candidates = await self._vector_search(user_query, k=candidate_limit, persona="", min_similarity=min_similarity)
                logger.debug(
                    "[Wardrobe] 向量检索（fallback_other优先无人格）: %d张 persona=无人格",
                    len(candidates),
                )
                if candidates:
                    candidates = self._sort_by_favorite(candidates)
                    meta["searched_persona"] = ""
                else:
                    candidates = await self._vector_search(user_query, k=candidate_limit, exclude_persona=current_persona, min_similarity=min_similarity)
                    logger.debug(
                        "[Wardrobe] 向量检索（fallback_other回退）: %d张 exclude=%s",
                        len(candidates), current_persona,
                    )
                    if candidates:
                        candidates = self._sort_by_favorite(candidates)
                        meta["searched_persona"] = f"非{current_persona}"
                        meta["persona_mismatch"] = True
                    else:
                        return [], meta
        elif self.vector_searcher and self.vector_searcher.available:
            # ================================================================
            # 主路径：向量检索可用 → 跳过意图解析模型，直接语义搜索
            #
            # 使用场景：用户通过对话调用 search_wardrobe_image 工具时
            # （即 _do_search_image 入口，exclude_current_persona=False）
            #
            # 设计理由：对话模型在调用工具时已经把用户需求组织为自然语言 query，
            # 无需再额外调一次 LLM 做意图解析。直接用 query 文本进向量库做语义匹配。
            #
            # persona 参数由上层 _do_search_image 在调用前已解析完成，
            # 直接传给 _vector_search 即可按人格池过滤。
            # ================================================================
            candidates = await self._vector_search(user_query, k=candidate_limit, persona=persona, min_similarity=min_similarity)
            logger.debug(
                "[Wardrobe] 向量检索（用户搜图-跳过意图解析）: %d张 persona=%s",
                len(candidates), "无人格" if persona == "" else (persona or "全局"),
            )
            if candidates:
                candidates = self._sort_by_favorite(candidates)
                meta["searched_persona"] = persona or "全局"
                meta["persona_scope"] = "vector"
            else:
                logger.debug("[Wardrobe] 向量检索无结果，回退 LEGACY 意图解析+LIKE")
                candidates = await self._legacy_parse_and_search(
                    user_query, primary_provider_id, secondary_provider_id,
                    timeout_seconds, candidate_limit, current_persona, persona_names,
                    persona_mode, meta, min_similarity=min_similarity,
                )
        else:
            # ================================================================
            # LEGACY 路径：向量检索不可用 → 意图解析模型 + LIKE 模糊匹配
            #
            # 触发条件：未配置 Embedding Provider 或向量库初始化失败
            #
            # 此路径会：
            #   1. 额外调用一次 LLM（_parse_query）将自然语言转为结构化条件
            #   2. 优先用结构化条件做 SQL LIKE 匹配
            #   3. 如果 LIKE 也没结果，再用 _search_by_description 做关键词搜索
            #
            # 如果日后所有部署环境都配置了 Embedding Provider，
            # 此路径及其依赖的 _parse_query / _search_by_scope 可整体废弃。
            # ================================================================
            candidates = await self._legacy_parse_and_search(
                user_query, primary_provider_id, secondary_provider_id,
                timeout_seconds, candidate_limit, current_persona, persona_names,
                persona_mode, meta, min_similarity=min_similarity,
            )

        if not candidates:
            logger.debug("[Wardrobe] 未找到候选图片")
            return [], meta

        # 按当前人格注入 use_count（按人格独立热度）
        # current_persona 为空时（空人格）不记热度，use_count 保持 0
        if current_persona and current_persona.strip():
            try:
                use_counts = await self.db.get_use_counts_by_persona(
                    [c["id"] for c in candidates], current_persona.strip()
                )
                for c in candidates:
                    c["use_count"] = use_counts.get(c["id"], 0)
                    c["_heat_persona"] = current_persona.strip()
            except Exception as exc:
                logger.warning("[Wardrobe] 获取按人格热度失败: %s", exc)
        else:
            for c in candidates:
                c["use_count"] = 0

        if prioritize_unused:
            def _effective_use_count(r):
                base = r.get("use_count", 0) or 0
                # 喜爱折扣：特别喜爱 -40%，普通喜爱 -20%
                return int(base * _heat_factor(r.get("favorite", "none")))
            candidates.sort(key=_effective_use_count)

        if daily_selfie_mode:
            if not self._cfg_value("daily_selfie_fair_mode", True):
                # 关闭公平轮换时，保留原有按 daily_selfie_use_count 衰减过滤的逻辑
                _DECAY_FACTOR = 0.6
                _EXCLUDE_THRESHOLD = 0.05
                before_count = len(candidates)
                filtered = []
                for c in candidates:
                    dsuc = int(c.get("daily_selfie_use_count", 0) or 0)
                    if dsuc <= 0:
                        filtered.append(c)
                        continue
                    weight = _DECAY_FACTOR ** dsuc
                    if weight >= _EXCLUDE_THRESHOLD:
                        filtered.append(c)
                candidates = filtered
                logger.debug(
                    "[Wardrobe] 补拍衰减过滤: %d -> %d (排除%d张, factor=%.1f, threshold=%.2f)",
                    before_count, len(candidates), before_count - len(candidates),
                    _DECAY_FACTOR, _EXCLUDE_THRESHOLD,
                )
                if not candidates:
                    logger.debug("[Wardrobe] 补拍衰减过滤后无候选图片")
                    return [], meta
            else:
                # 先并入冷图配额席位，保证极冷门/低相似度图也有机会进池
                _seats = _DAILY_SELFIE_COLD_SEATS
                if _seats > 0 and candidates:
                    candidates = await self._merge_cold_seats(
                        user_query, candidates, current_persona, _seats
                    )
                # 补拍公平轮换：只保留"相对池内最低热度±slack"的冷梯队，
                # 并 round-robin 排序，保证低热度/未用图优先被取图模型看到。
                # 热度 = person 使用数 + 每日补拍累计使用数，并按喜爱程度打折。
                _slack = _DAILY_SELFIE_COLD_SLACK
                before_count = len(candidates)
                min_heat = 0
                if candidates:
                    def _heat(c_):
                        raw = int(c_.get("use_count", 0) or 0) + int(c_.get("daily_selfie_use_count", 0) or 0)
                        return int(raw * _heat_factor(c_.get("favorite", "none")))
                    he = [_heat(c) for c in candidates]
                    min_heat = min(he)
                    kept = [c for c in candidates if _heat(c) <= min_heat + _slack]
                    candidates = kept
                    # round-robin：热度升序 → 最近使用升序 → 相似度降序
                    candidates.sort(
                        key=lambda c_: (
                            _heat(c_),
                            c_.get("last_used_at", "") or "",
                            -float(c_.get("_similarity", 0) or 0),
                        )
                    )
                logger.debug(
                    "[Wardrobe] 补拍冷梯队: %d -> %d (min_heat=%d slack=%d)",
                    before_count, len(candidates), min_heat, _slack,
                )
                if not candidates:
                    logger.debug("[Wardrobe] 补拍冷梯队后无候选图片")
                    return [], meta

        if daily_selfie_mode and self._cfg_value("daily_selfie_fair_mode", True):
            # 补拍公平模式：不走 LLM 取图，直接取冷梯队排序后的前 max_select 张（deterministic）。
            # 排序已保证 低热度 → 久未用 → 更贴 优先，向量召回排序即足以定夺，省一次 LLM 调用。
            selected = candidates[:max_select]
            logger.debug(
                "[Wardrobe] 补拍公平模式，直接取冷梯队排序结果 %d/%d 张",
                len(selected), len(candidates),
            )
        elif len(candidates) <= max_select:
            selected = candidates
        else:
            selected = await self._select_from_candidates(
                user_query,
                candidates,
                max_select=max_select,
                primary_provider_id=primary_provider_id,
                secondary_provider_id=secondary_provider_id,
                timeout_seconds=timeout_seconds,
            )

        for r in selected:
            if current_persona and r.get("persona") and r["persona"] != current_persona:
                meta["persona_mismatch"] = True
                break

        return selected, meta

    async def _search_by_scope(
        self,
        conditions: dict[str, Any],
        *,
        persona_scope: str,
        named_persona: str,
        current_persona: str,
        limit: int,
        meta: dict[str, Any],
        user_query: str = "",
        min_similarity: float | None = None,
    ) -> list[dict[str, Any]]:
        logger.debug(
            "[Wardrobe] 搜索策略: scope=%s current_persona=%s named_persona=%s",
            persona_scope, current_persona or "无", named_persona or "无",
        )

        if persona_scope == "self" and current_persona:
            candidates = await self._query_candidates(conditions, limit=limit, persona=current_persona, user_query=user_query, min_similarity=min_similarity)
            logger.debug("[Wardrobe] 当前人格池搜索结果: %d张 persona=%s", len(candidates), current_persona)
            if candidates:
                meta["searched_persona"] = current_persona
                return candidates
            return []

        if persona_scope == "other" and current_persona:
            candidates = await self._query_candidates_excluding_persona(conditions, limit=limit, exclude_persona=current_persona, user_query=user_query, min_similarity=min_similarity)
            logger.debug("[Wardrobe] 排除人格搜索结果: %d张 exclude=%s", len(candidates), current_persona)
            if candidates:
                meta["searched_persona"] = f"非{current_persona}"
                return candidates
            return []

        if persona_scope == "named" and named_persona:
            candidates = await self._query_candidates(conditions, limit=limit, persona=named_persona, user_query=user_query, min_similarity=min_similarity)
            logger.debug("[Wardrobe] 指定人格搜索结果: %d张 persona=%s", len(candidates), named_persona)
            if candidates:
                meta["searched_persona"] = named_persona
                return candidates
            return []

        return await self._query_candidates(conditions, limit=limit, persona=None, user_query=user_query, min_similarity=min_similarity)

    async def _legacy_parse_and_search(
        self,
        user_query: str,
        primary_provider_id: str,
        secondary_provider_id: str,
        timeout_seconds: float,
        candidate_limit: int,
        current_persona: str,
        persona_names: str,
        persona_mode: str,
        meta: dict[str, Any],
        min_similarity: float | None = None,
    ) -> list[dict[str, Any]]:
        # ================================================================
        # LEGACY：意图解析模型 + LIKE 模糊匹配
        #
        # 调用方：
        #   1. 向量检索完全不可用时（未配置 Embedding Provider）
        #   2. 向量检索可用但返回空结果时的回退
        #
        # 流程：
        #   ① _parse_query() 调 LLM 把自然语言映射到 pool 标签
        #   ② _search_by_scope() 根据 persona_scope 决定搜哪个池子
        #   ③ _query_candidates() 优先向量（可能仍不可用），然后 LIKE 回退
        # ================================================================
        query_conditions = await self._parse_query(
            user_query,
            primary_provider_id=primary_provider_id,
            secondary_provider_id=secondary_provider_id,
            timeout_seconds=timeout_seconds,
            current_persona=current_persona,
            persona_names=persona_names,
        )
        if not query_conditions:
            query_conditions = {"keywords": [user_query]}

        existing_keywords = query_conditions.get("keywords") or []
        if user_query not in existing_keywords:
            query_conditions["keywords"] = [user_query] + existing_keywords

        persona_scope = query_conditions.pop("persona_scope", "global")
        named_persona = query_conditions.pop("persona", "")
        meta["persona_scope"] = persona_scope

        candidates = await self._search_by_scope(
            query_conditions, persona_scope=persona_scope,
            named_persona=named_persona, current_persona=current_persona,
            limit=candidate_limit, meta=meta, user_query=user_query,
            min_similarity=min_similarity,
        )
        return candidates

    async def _vector_search(self, user_query: str, k: int, persona: Optional[str] = None, exclude_persona: str = "", min_similarity: float | None = None) -> list[dict[str, Any]]:
        if not self.vector_searcher or not self.vector_searcher.available:
            logger.debug("[Wardrobe] 向量检索不可用: vector_searcher=%s available=%s",
                        self.vector_searcher is not None,
                        self.vector_searcher.available if self.vector_searcher else False)
            return []

        logger.debug("[Wardrobe] 向量检索开始: query=%s k=%d persona=%s exclude_persona=%s",
                    user_query[:100], k, "无人格" if persona == "" else (persona or "全局"), exclude_persona or "无")
        wardrobe_results = await self.vector_searcher.search(
            query=user_query,
            k=k,
            persona=persona,
            exclude_persona=exclude_persona,
            min_similarity=min_similarity,
        )
        if not wardrobe_results:
            logger.debug("[Wardrobe] 向量检索无结果: query=%s", user_query[:100])
            return []

        results = []
        for wid, similarity in wardrobe_results:
            img = await self.db.get_image(wid)
            if img:
                img["_similarity"] = similarity
                results.append(img)
        logger.debug("[Wardrobe] 向量检索命中: %d张", len(results))
        return results

    async def _query_candidates_excluding_persona(
        self, conditions: dict[str, Any], *, exclude_persona: str, limit: int = 20, user_query: str = "",
        min_similarity: float | None = None,
    ) -> list[dict[str, Any]]:
        category = conditions.get("category")
        style = conditions.get("style")
        exposure_level = conditions.get("exposure_level")
        scene = conditions.get("scene")
        atmosphere = conditions.get("atmosphere")
        pose_type = conditions.get("pose_type")
        body_focus = conditions.get("body_focus")
        shot_size = conditions.get("shot_size")
        keywords = conditions.get("keywords")

        vec_results = await self._vector_search(user_query or " ".join(keywords or []), k=limit, exclude_persona=exclude_persona, min_similarity=min_similarity)
        if vec_results:
            logger.debug("[Wardrobe] 向量检索命中（排除人格）: %d张 exclude=%s", len(vec_results), exclude_persona)
            return self._sort_by_favorite(vec_results)

        logger.debug("[Wardrobe] 向量检索无结果（排除人格），回退LIKE检索 exclude=%s", exclude_persona)
        results = await self.db.search_images(
            category=category,
            exposure_level=exposure_level,
            style=style,
            scene=scene,
            atmosphere=atmosphere,
            pose_type=pose_type,
            body_focus=body_focus,
            persona=None,
            exclude_persona=exclude_persona,
            shot_size=shot_size,
            limit=limit,
        )

        if not results and keywords:
            results = await self.db.search_by_description(
                keywords=keywords,
                category=category,
                persona=None,
                exclude_persona=exclude_persona,
                limit=limit,
            )

        if not results and keywords and category:
            results = await self.db.search_by_description(
                keywords=keywords,
                persona=None,
                exclude_persona=exclude_persona,
                limit=limit,
            )

        return results

    async def _parse_query(
        self,
        user_query: str,
        *,
        primary_provider_id: str,
        secondary_provider_id: str,
        timeout_seconds: float,
        current_persona: str = "",
        persona_names: str = "",
    ) -> Optional[dict[str, Any]]:
        providers = [p for p in [primary_provider_id, secondary_provider_id] if p.strip()]
        if not providers:
            return None

        pools_text = await self._get_pools_text(persona=current_persona)
        system_prompt = SEARCH_PARSE_SYSTEM_PROMPT.format(
            current_persona=current_persona or "未设置",
            persona_names=persona_names or "无",
            pools_text=pools_text,
        )

        for provider_id in providers:
            try:
                llm_resp = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=user_query,
                        system_prompt=system_prompt,
                    ),
                    timeout=timeout_seconds,
                )
                raw = (getattr(llm_resp, "completion_text", "") or "").strip()
                result = parse_json_response(raw)
                if result:
                    return result
            except asyncio.TimeoutError:
                logger.warning("[Wardrobe] 取图模型（意图解析）超时 provider=%s", provider_id)
            except Exception as e:
                logger.warning("[Wardrobe] 取图模型（意图解析）失败 provider=%s error=%s", provider_id, e)

        return None

    async def _query_candidates(
        self, conditions: dict[str, Any], limit: int = 20, persona: str = "", user_query: str = "",
        min_similarity: float | None = None,
    ) -> list[dict[str, Any]]:
        category = conditions.get("category")
        style = conditions.get("style")
        exposure_level = conditions.get("exposure_level")
        scene = conditions.get("scene")
        atmosphere = conditions.get("atmosphere")
        keywords = conditions.get("keywords")
        pose_type = conditions.get("pose_type")
        body_focus = conditions.get("body_focus")
        shot_size = conditions.get("shot_size")

        vec_results = await self._vector_search(user_query or " ".join(keywords or []), k=limit, persona=persona, min_similarity=min_similarity)
        if vec_results:
            logger.debug("[Wardrobe] 向量检索命中: %d张 persona=%s", len(vec_results), "无人格" if persona == "" else (persona or "全局"))
            return self._sort_by_favorite(vec_results)

        logger.debug("[Wardrobe] 向量检索无结果，回退LIKE检索 persona=%s", "无人格" if persona == "" else (persona or "全局"))
        results = await self.db.search_images(
            category=category,
            exposure_level=exposure_level,
            style=style,
            scene=scene,
            atmosphere=atmosphere,
            pose_type=pose_type,
            body_focus=body_focus,
            persona=persona,
            shot_size=shot_size,
            limit=limit,
        )

        if not results and keywords:
            results = await self.db.search_by_description(
                keywords=keywords,
                category=category,
                persona=persona,
                limit=limit,
            )

        if not results and keywords and category:
            results = await self.db.search_by_description(
                keywords=keywords,
                persona=persona,
                limit=limit,
            )

        return results

    async def _select_from_candidates(
        self,
        user_query: str,
        candidates: list[dict[str, Any]],
        *,
        max_select: int,
        primary_provider_id: str,
        secondary_provider_id: str,
        timeout_seconds: float,
    ) -> list[dict[str, Any]]:
        providers = [p for p in [primary_provider_id, secondary_provider_id] if p.strip()]
        if not providers:
            return candidates[:max_select]

        candidates_info = []
        for c in candidates:
            info = {
                "id": c["id"],
                "category": c.get("category", ""),
                "style": c.get("style", []),
                "clothing_type": c.get("clothing_type", ""),
                "exposure_level": c.get("exposure_level", ""),
                "exposure_features": c.get("exposure_features", []),
                "key_features": c.get("key_features", []),
                "prop_objects": c.get("prop_objects", []),
                "allure_features": c.get("allure_features", []),
                "body_focus": c.get("body_focus", []),
                "scene": c.get("scene", []),
                "atmosphere": c.get("atmosphere", []),
                "description": c.get("description", ""),
                "user_tags": c.get("user_tags", ""),
                "use_count": c.get("use_count", 0),
            }
            if c.get("category") == "人物":
                info.update({
                    "pose_type": c.get("pose_type", ""),
                    "action_style": c.get("action_style", []),
                    "expression": c.get("expression", ""),
                    "shot_size": c.get("shot_size", ""),
                })
            candidates_info.append(info)

        prompt = (
            f"用户需求：{user_query}\n\n"
            f"候选图片：\n{json.dumps(candidates_info, ensure_ascii=False, indent=2)}"
        )
        system = SEARCH_SELECT_SYSTEM_PROMPT.format(max_select=max_select)

        for provider_id in providers:
            try:
                llm_resp = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system,
                    ),
                    timeout=timeout_seconds,
                )
                raw = (getattr(llm_resp, "completion_text", "") or "").strip()
                result = parse_json_response(raw)
                if result and "selected_ids" in result:
                    selected_ids = result["selected_ids"]
                    if not selected_ids:
                        return []
                    id_set = set(selected_ids)
                    return [c for c in candidates if c["id"] in id_set]
            except asyncio.TimeoutError:
                logger.warning("[Wardrobe] 取图模型（选择）超时 provider=%s", provider_id)
            except Exception as e:
                logger.warning("[Wardrobe] 取图模型（选择）失败 provider=%s error=%s", provider_id, e)

        return candidates[:max_select]
