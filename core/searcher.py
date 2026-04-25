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
1. **最高优先级**：clothing_type（服装类型）、description 中的服装与姿势表述、body_focus（身体焦点）——这些直接决定"拍的是什么"
2. **中等优先级**：scene（场景）
3. **低优先级**：composition（构图）
4. style（风格）和 atmosphere（氛围）仅作为辅助参考，不作为主要匹配依据

# 规则
1. 最多选择 {max_select} 张图片
2. 匹配标准宽松：完全匹配、大部分匹配、语义可能相关的图片都应返回；只有完全不匹配才排除
3. 宁可多返回也不要漏掉可能匹配的图片，空结果是最差体验
4. 只输出 JSON，不要输出解释"""


class ImageSearcher:
    def __init__(self, context, db: WardrobeDatabase, store: ImageStore, vector_searcher=None):
        self.context = context
        self.db = db
        self.store = store
        self.vector_searcher = vector_searcher
        self._pools_text_cache = None
        self._pools_text_ts = 0

    async def _get_pools_text(self) -> str:
        now = time.time()
        if self._pools_text_cache and now - self._pools_text_ts < 300:
            return self._pools_text_cache

        try:
            from .pools import ALL_POOLS
            plugin = getattr(self.context, '_wardrobe_plugin', None)
            pools = await plugin.get_merged_pools() if plugin else ALL_POOLS
        except Exception:
            from .pools import ALL_POOLS
            pools = ALL_POOLS

        search_pools = {k: v for k, v in pools.items() if k in ("style", "scene", "atmosphere", "clothing_type")}
        lines = []
        for key, values in search_pools.items():
            lines.append(f"## {key}")
            for v in values:
                lines.append(f"- {v}")
            lines.append("")

        self._pools_text_cache = "\n".join(lines)
        self._pools_text_ts = now
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
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        meta = {"persona_mismatch": False, "searched_persona": persona, "persona_scope": "global"}

        if self.vector_searcher and self.vector_searcher.available and exclude_current_persona and current_persona:
            if persona_mode == "no_persona_only":
                candidates = await self._vector_search(user_query, k=candidate_limit, persona="")
                logger.info(
                    "[Wardrobe] 向量检索（no_persona_only）: %d张 persona=无人格",
                    len(candidates),
                )
                if candidates:
                    candidates = self._sort_by_favorite(candidates)
                    meta["searched_persona"] = ""
                else:
                    logger.info("[Wardrobe] 无人格池无结果，no_persona_only模式不回退其他人格")
                    return [], meta
            else:
                candidates = await self._vector_search(user_query, k=candidate_limit, persona="")
                logger.info(
                    "[Wardrobe] 向量检索（fallback_other优先无人格）: %d张 persona=无人格",
                    len(candidates),
                )
                if candidates:
                    candidates = self._sort_by_favorite(candidates)
                    meta["searched_persona"] = ""
                else:
                    candidates = await self._vector_search(user_query, k=candidate_limit, exclude_persona=current_persona)
                    logger.info(
                        "[Wardrobe] 向量检索（fallback_other回退）: %d张 exclude=%s",
                        len(candidates), current_persona,
                    )
                    if candidates:
                        candidates = self._sort_by_favorite(candidates)
                        meta["searched_persona"] = f"非{current_persona}"
                        meta["persona_mismatch"] = True
                    else:
                        return [], meta
        else:
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

            if exclude_current_persona and current_persona:
                if persona_mode == "no_persona_only":
                    candidates = await self._query_candidates(
                        query_conditions, limit=candidate_limit, persona="", user_query=user_query,
                    )
                    logger.info(
                        "[Wardrobe] 无人格池搜索结果: %d张 (no_persona_only)",
                        len(candidates),
                    )
                    if candidates:
                        meta["searched_persona"] = ""
                    else:
                        logger.info("[Wardrobe] 无人格池无结果，no_persona_only模式不回退其他人格")
                        return [], meta
                else:
                    candidates = await self._query_candidates(
                        query_conditions, limit=candidate_limit, persona="", user_query=user_query,
                    )
                    logger.info(
                        "[Wardrobe] 无人格池搜索结果: %d张 (fallback_other)",
                        len(candidates),
                    )
                    if candidates:
                        meta["searched_persona"] = ""
                    else:
                        candidates = await self._query_candidates_excluding_persona(
                            query_conditions, exclude_persona=current_persona, limit=candidate_limit,
                            user_query=user_query,
                        )
                        logger.info(
                            "[Wardrobe] 排除人格搜索结果: %d张 exclude=%s (fallback_other回退)",
                            len(candidates), current_persona,
                        )
                        if candidates:
                            meta["searched_persona"] = f"非{current_persona}"
                            meta["persona_mismatch"] = True
                        else:
                            return [], meta
            else:
                candidates = await self._search_by_scope(
                    query_conditions, persona_scope=persona_scope,
                    named_persona=named_persona, current_persona=current_persona,
                    limit=candidate_limit, meta=meta, user_query=user_query,
                    persona_mode=persona_mode,
                )

        if not candidates:
            logger.info("[Wardrobe] 未找到候选图片")
            return [], meta

        if prioritize_unused:
            candidates.sort(key=lambda r: r.get("use_count", 0) or 0)

        if len(candidates) <= max_select:
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
        persona_mode: str = "no_persona_only",
    ) -> list[dict[str, Any]]:
        logger.info(
            "[Wardrobe] 搜索策略: scope=%s current_persona=%s named_persona=%s persona_mode=%s",
            persona_scope, current_persona or "无", named_persona or "无", persona_mode,
        )

        if persona_scope == "self" and current_persona:
            candidates = await self._query_candidates(conditions, limit=limit, persona="", user_query=user_query)
            logger.info("[Wardrobe] 无人格池搜索结果: %d张", len(candidates))
            if candidates:
                meta["searched_persona"] = ""
                return candidates
            if persona_mode == "no_persona_only":
                logger.info("[Wardrobe] 无人格池无结果，no_persona_only模式不回退其他人格")
                return []
            else:
                candidates = await self._query_candidates_excluding_persona(
                    conditions, limit=limit, exclude_persona=current_persona, user_query=user_query,
                )
                logger.info("[Wardrobe] 其他人格池搜索结果: %d张 exclude=%s", len(candidates), current_persona)
                if candidates:
                    meta["persona_mismatch"] = True
                    meta["searched_persona"] = f"非{current_persona}"
                    return candidates
                return []

        if persona_scope == "other" and current_persona:
            candidates = await self._query_candidates_excluding_persona(conditions, limit=limit, exclude_persona=current_persona, user_query=user_query)
            logger.info("[Wardrobe] 排除人格搜索结果: %d张 exclude=%s", len(candidates), current_persona)
            if candidates:
                meta["searched_persona"] = f"非{current_persona}"
                return candidates
            logger.info("[Wardrobe] 非当前人格池无结果，回退全局搜索")
            candidates = await self._query_candidates(conditions, limit=limit, persona="", user_query=user_query)
            meta["searched_persona"] = ""
            return candidates

        if persona_scope == "named" and named_persona:
            candidates = await self._query_candidates(conditions, limit=limit, persona=named_persona, user_query=user_query)
            logger.info("[Wardrobe] 指定人格搜索结果: %d张 persona=%s", len(candidates), named_persona)
            if candidates:
                meta["searched_persona"] = named_persona
                return candidates
            logger.info("[Wardrobe] 指定人格池无结果，回退全局搜索 persona=%s", named_persona)
            meta["persona_mismatch"] = True
            candidates = await self._query_candidates(conditions, limit=limit, persona="", user_query=user_query)
            meta["searched_persona"] = ""
            return candidates

        logger.info("[Wardrobe] 全局搜索")
        return await self._query_candidates(conditions, limit=limit, persona="", user_query=user_query)

    async def _vector_search(self, user_query: str, k: int, persona: Optional[str] = None, exclude_persona: str = "") -> list[dict[str, Any]]:
        if not self.vector_searcher or not self.vector_searcher.available:
            logger.info("[Wardrobe] 向量检索不可用: vector_searcher=%s available=%s",
                        self.vector_searcher is not None,
                        self.vector_searcher.available if self.vector_searcher else False)
            return []

        logger.info("[Wardrobe] 向量检索开始: query=%s k=%d persona=%s exclude_persona=%s",
                    user_query[:100], k, "无人格" if persona == "" else (persona or "全局"), exclude_persona or "无")
        wardrobe_results = await self.vector_searcher.search(
            query=user_query,
            k=k,
            persona=persona,
            exclude_persona=exclude_persona,
        )
        if not wardrobe_results:
            logger.info("[Wardrobe] 向量检索无结果: query=%s", user_query[:100])
            return []

        results = []
        for wid, similarity in wardrobe_results:
            img = await self.db.get_image(wid)
            if img:
                img["_similarity"] = similarity
                results.append(img)
        logger.info("[Wardrobe] 向量检索命中: %d张 (原始返回%d个ID, 数据库匹配%d张)",
                    len(results), len(wardrobe_results), len(results))
        return results

    async def _query_candidates_excluding_persona(
        self, conditions: dict[str, Any], *, exclude_persona: str, limit: int = 20, user_query: str = ""
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

        vec_results = await self._vector_search(user_query or " ".join(keywords or []), k=limit, exclude_persona=exclude_persona)
        if vec_results:
            logger.info("[Wardrobe] 向量检索命中（排除人格）: %d张 exclude=%s", len(vec_results), exclude_persona)
            return self._sort_by_favorite(vec_results)

        logger.info("[Wardrobe] 向量检索无结果（排除人格），回退LIKE检索 exclude=%s", exclude_persona)
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

        pools_text = await self._get_pools_text()
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
        self, conditions: dict[str, Any], limit: int = 20, persona: str = "", user_query: str = ""
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

        vec_results = await self._vector_search(user_query or " ".join(keywords or []), k=limit, persona=persona)
        if vec_results:
            logger.info("[Wardrobe] 向量检索命中: %d张 persona=%s", len(vec_results), "无人格" if persona == "" else (persona or "全局"))
            return self._sort_by_favorite(vec_results)

        logger.info("[Wardrobe] 向量检索无结果，回退LIKE检索 persona=%s", "无人格" if persona == "" else (persona or "全局"))
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
