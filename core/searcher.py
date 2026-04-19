import asyncio
import json
import time
from typing import Any, Optional

from astrbot.api import logger

from .database import WardrobeDatabase
from .image_store import ImageStore
from .utils import parse_json_response


SEARCH_PARSE_SYSTEM_PROMPT = """# 角色
你是图片检索意图解析助手。根据用户的自然语言描述，生成结构化的查询条件。

# 任务
解析用户的检索意图，输出 JSON 格式的查询条件。

# 可用查询字段
- category: "人物" 或 "衣服"（可选）
- style: 风格列表，如 ["甜系洛丽塔", "哥特洛丽塔"]（可选）
- exposure_level: "保守"/"适度"/"略暴露"/"暴露"（可选）
- scene: 场景列表（可选）
- atmosphere: 氛围列表，如 ["性感", "可爱"]（可选）
- keywords: 关键词列表，用于描述匹配（可选）
- persona: 人格名称（可选）

# 人格判断规则
当前对话人格：{current_persona}
已有的人格目录：{persona_names}

判断逻辑：
- 如果用户明确提到某个人格名（如"星织有没有拍过xxx"），且该名称在人格目录中，则 persona 填写该名称
- 如果用户用"你""自己"等指代当前对话人格，且当前对话人格在人格目录中，则 persona 填写当前对话人格
- 如果用户没有提到任何人格，且语气是泛泛询问（如"有没有穿洛丽塔的美少女"），则 persona 留空，表示全局搜索
- 如果提到的人格名不在目录中，persona 也留空

# 规则
1. 只输出 JSON，不要输出解释
2. 用户可能描述得很模糊，尽量推断最合理的查询条件
3. 如果用户没有明确指定分类，不要填写 category
4. keywords 用于捕捉无法用预定义值表达的特征"""

SEARCH_SELECT_SYSTEM_PROMPT = """# 角色
你是图片选择助手。从给定的候选图片中，选出最符合用户需求的图片。

# 任务
根据用户的检索描述和候选图片的属性信息，选出最匹配的图片。

# 输出格式
输出 JSON 对象：
```json
{{
  "selected_ids": ["选中的图片ID列表"],
  "reason": "选择理由"
}}
```

# 规则
1. 最多选择 {max_select} 张图片
2. 优先选择最匹配用户描述的图片
3. 如果没有完全匹配的，选择最接近的
4. 只输出 JSON，不要输出解释"""


class ImageSearcher:
    def __init__(self, context, db: WardrobeDatabase, store: ImageStore):
        self.context = context
        self.db = db
        self.store = store

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
    ) -> list[dict[str, Any]]:
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

        resolved_persona = persona
        if query_conditions.get("persona"):
            resolved_persona = query_conditions.pop("persona")

        candidates = await self._query_candidates(query_conditions, limit=candidate_limit, persona=resolved_persona)
        if not candidates:
            logger.info("[Wardrobe] 未找到候选图片 persona=%s", resolved_persona or "无")
            return []

        if len(candidates) <= max_select:
            return candidates

        selected = await self._select_from_candidates(
            user_query,
            candidates,
            max_select=max_select,
            primary_provider_id=primary_provider_id,
            secondary_provider_id=secondary_provider_id,
            timeout_seconds=timeout_seconds,
        )
        return selected

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

        system_prompt = SEARCH_PARSE_SYSTEM_PROMPT.format(
            current_persona=current_persona or "未设置",
            persona_names=persona_names or "无",
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
        self, conditions: dict[str, Any], limit: int = 20, persona: str = ""
    ) -> list[dict[str, Any]]:
        category = conditions.get("category")
        style = conditions.get("style")
        exposure_level = conditions.get("exposure_level")
        scene = conditions.get("scene")
        atmosphere = conditions.get("atmosphere")
        keywords = conditions.get("keywords")

        results = await self.db.search_images(
            category=category,
            exposure_level=exposure_level,
            style=style,
            scene=scene,
            atmosphere=atmosphere,
            persona=persona,
            limit=limit,
        )

        if not results and keywords:
            results = await self.db.search_by_description(
                keywords=keywords,
                category=category,
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
                    id_set = set(selected_ids)
                    return [c for c in candidates if c["id"] in id_set]
            except asyncio.TimeoutError:
                logger.warning("[Wardrobe] 取图模型（选择）超时 provider=%s", provider_id)
            except Exception as e:
                logger.warning("[Wardrobe] 取图模型（选择）失败 provider=%s error=%s", provider_id, e)

        return candidates[:max_select]
