import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from astrbot.api import logger

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    style TEXT DEFAULT '[]',
    clothing_type TEXT DEFAULT '',
    exposure_level TEXT DEFAULT '',
    scene TEXT DEFAULT '[]',
    atmosphere TEXT DEFAULT '[]',
    pose_type TEXT DEFAULT '',
    body_orientation TEXT DEFAULT '',
    dynamic_level TEXT DEFAULT '',
    action_style TEXT DEFAULT '[]',
    shot_size TEXT DEFAULT '',
    camera_angle TEXT DEFAULT '',
    expression TEXT DEFAULT '',
    color_tone TEXT DEFAULT '',
    composition TEXT DEFAULT '',
    background TEXT DEFAULT '',
    description TEXT DEFAULT '',
    user_tags TEXT DEFAULT '',
    exposure_features TEXT DEFAULT '[]',
    key_features TEXT DEFAULT '[]',
    prop_objects TEXT DEFAULT '[]',
    allure_features TEXT DEFAULT '[]',
    body_focus TEXT DEFAULT '[]',
    image_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    persona TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    favorite TEXT DEFAULT 'none',
    use_count INTEGER DEFAULT 0,
    file_hash TEXT DEFAULT '',
    ref_strength TEXT DEFAULT 'style'
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_category ON images(category);
CREATE INDEX IF NOT EXISTS idx_exposure_level ON images(exposure_level);
CREATE INDEX IF NOT EXISTS idx_style ON images(style);
CREATE INDEX IF NOT EXISTS idx_scene ON images(scene);
CREATE INDEX IF NOT EXISTS idx_favorite ON images(favorite);
CREATE INDEX IF NOT EXISTS idx_file_hash ON images(file_hash);
"""

_UPDATABLE_FIELDS = frozenset({
    "category", "style", "clothing_type", "exposure_level", "persona",
    "scene", "atmosphere", "pose_type", "body_orientation",
    "dynamic_level", "action_style", "shot_size", "camera_angle",
    "expression", "color_tone", "composition", "background",
    "description", "user_tags", "exposure_features", "key_features", "prop_objects", "allure_features", "body_focus",
    "image_path", "updated_at", "favorite", "use_count", "file_hash",
    "ref_strength",
})


class WardrobeDatabase:
    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "wardrobe.db"
        self._lock = asyncio.Lock()

    async def init(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.executescript(_CREATE_TABLE_SQL)
                for col, default in [
                    ("persona", "TEXT DEFAULT ''"),
                    ("created_by", "TEXT DEFAULT ''"),
                    ("user_tags", "TEXT DEFAULT ''"),
                    ("exposure_features", "TEXT DEFAULT '[]'"),
                    ("key_features", "TEXT DEFAULT '[]'"),
                    ("prop_objects", "TEXT DEFAULT '[]'"),
                    ("allure_features", "TEXT DEFAULT '[]'"),
                    ("body_focus", "TEXT DEFAULT '[]'"),
                    ("favorite", "TEXT DEFAULT 'none'"),
                    ("use_count", "INTEGER DEFAULT 0"),
                    ("file_hash", "TEXT DEFAULT ''"),
                    ("ref_strength", "TEXT DEFAULT 'style'"),
                ]:
                    try:
                        await db.execute(f"ALTER TABLE images ADD COLUMN {col} {default}")
                    except Exception:
                        pass
                await db.executescript(_CREATE_INDEX_SQL)
                await db.commit()
        logger.info("[Wardrobe] 数据库初始化完成")

    async def add_image(
        self,
        *,
        category: str,
        style: list[str],
        clothing_type: str,
        exposure_level: str,
        scene: list[str],
        atmosphere: list[str],
        pose_type: str,
        body_orientation: str,
        dynamic_level: str,
        action_style: list[str],
        shot_size: str,
        camera_angle: str,
        expression: str,
        color_tone: str,
        composition: str,
        background: str,
        description: str,
        user_tags: str = "",
        exposure_features: list[str] | None = None,
        key_features: list[str] | None = None,
        prop_objects: list[str] | None = None,
        allure_features: list[str] | None = None,
        body_focus: list[str] | None = None,
        persona: str = "",
        image_path: str,
        created_by: str = "",
        file_hash: str = "",
        ref_strength: str = "style",
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        image_id = str(uuid.uuid4())
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO images (
                        id, category, style, clothing_type, exposure_level,
                        scene, atmosphere, pose_type, body_orientation,
                        dynamic_level, action_style, shot_size, camera_angle,
                        expression, color_tone, composition, background,
                        description, user_tags, exposure_features, key_features, prop_objects, allure_features, body_focus,
                        persona, image_path, created_at, updated_at, created_by, favorite, use_count, file_hash, ref_strength
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        image_id,
                        category,
                        json.dumps(style, ensure_ascii=False),
                        clothing_type,
                        exposure_level,
                        json.dumps(scene, ensure_ascii=False),
                        json.dumps(atmosphere, ensure_ascii=False),
                        pose_type,
                        body_orientation,
                        dynamic_level,
                        json.dumps(action_style, ensure_ascii=False),
                        shot_size,
                        camera_angle,
                        expression,
                        color_tone,
                        composition,
                        background,
                        description,
                        user_tags,
                        json.dumps(exposure_features or [], ensure_ascii=False),
                        json.dumps(key_features or [], ensure_ascii=False),
                        json.dumps(prop_objects or [], ensure_ascii=False),
                        json.dumps(allure_features or [], ensure_ascii=False),
                        json.dumps(body_focus or [], ensure_ascii=False),
                        persona,
                        image_path,
                        now,
                        now,
                        created_by,
                        "none",
                        0,
                        file_hash,
                        ref_strength,
                    ),
                )
                await db.commit()
        return image_id

    async def get_image(self, image_id: str) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM images WHERE id = ?", (image_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    return None
                return self._row_to_dict(row)

    async def get_image_by_hash(self, file_hash: str) -> Optional[dict[str, Any]]:
        if not file_hash:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM images WHERE file_hash = ? LIMIT 1", (file_hash,)
            ) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    return None
                return self._row_to_dict(row)

    async def update_image(self, image_id: str, **kwargs) -> bool:
        if not kwargs:
            return False
        kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
        sets = []
        values = []
        for key, val in kwargs.items():
            if key not in _UPDATABLE_FIELDS:
                continue
            if isinstance(val, list):
                val = json.dumps(val, ensure_ascii=False)
            sets.append(f"{key} = ?")
            values.append(val)
        if not sets:
            return False
        values.append(image_id)
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    f"UPDATE images SET {', '.join(sets)} WHERE id = ?",
                    values,
                )
                await db.commit()
        return True

    async def delete_image(self, image_id: str) -> bool:
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "DELETE FROM images WHERE id = ?", (image_id,)
                )
                await db.commit()
                return cursor.rowcount > 0

    async def increment_use_count(self, image_id: str) -> None:
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE images SET use_count = COALESCE(use_count, 0) + 1, updated_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), image_id),
                )
                await db.commit()

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _build_search_conditions(
        *,
        category: Optional[str] = None,
        exposure_level: Optional[str] = None,
        style: Optional[list[str]] = None,
        scene: Optional[list[str]] = None,
        atmosphere: Optional[list[str]] = None,
        pose_type: Optional[str] = None,
        body_focus: Optional[list[str]] = None,
        persona: str = "",
        exclude_persona: str = "",
        shot_size: Optional[str] = None,
        keywords: Optional[list[str]] = None,
        favorite: Optional[str] = None,
        ref_strength: Optional[str] = None,
    ) -> tuple[list[str], list[Any]]:
        conditions = []
        params: list[Any] = []

        if category:
            conditions.append("category = ?")
            params.append(category)

        if exposure_level:
            conditions.append("exposure_level = ?")
            params.append(exposure_level)

        if pose_type:
            conditions.append("pose_type LIKE ? ESCAPE '\\'")
            params.append(f'%{WardrobeDatabase._escape_like(pose_type)}%')

        if persona:
            conditions.append("persona = ?")
            params.append(persona)

        if exclude_persona:
            conditions.append("persona != ?")
            params.append(exclude_persona)

        if shot_size:
            conditions.append("shot_size = ?")
            params.append(shot_size)

        for field, values in (("style", style), ("scene", scene), ("atmosphere", atmosphere)):
            if values:
                field_conds = []
                for v in values:
                    field_conds.append(f"{field} LIKE ? ESCAPE '\\'")
                    params.append(f'%{WardrobeDatabase._escape_like(v)}%')
                conditions.append(f"({' OR '.join(field_conds)})")

        if body_focus:
            bf_conds = []
            for v in body_focus:
                bf_conds.append("body_focus LIKE ? ESCAPE '\\'")
                params.append(f'%{WardrobeDatabase._escape_like(v)}%')
            conditions.append(f"({' OR '.join(bf_conds)})")

        if favorite and favorite in ("favorite", "like"):
            conditions.append("favorite = ?")
            params.append(favorite)

        if ref_strength:
            conditions.append("ref_strength = ?")
            params.append(ref_strength)

        if keywords:
            kw_conds = []
            for kw in keywords:
                escaped = WardrobeDatabase._escape_like(kw)
                kw_conds.append(
                    "(description LIKE ? ESCAPE '\\' OR user_tags LIKE ? ESCAPE '\\' "
                    "OR exposure_features LIKE ? ESCAPE '\\' OR key_features LIKE ? ESCAPE '\\' "
                    "OR prop_objects LIKE ? ESCAPE '\\' OR allure_features LIKE ? ESCAPE '\\' "
                    "OR body_focus LIKE ? ESCAPE '\\')"
                )
                params.append(f'%{escaped}%')
                params.append(f'%{escaped}%')
                params.append(f'%{escaped}%')
                params.append(f'%{escaped}%')
                params.append(f'%{escaped}%')
                params.append(f'%{escaped}%')
                params.append(f'%{escaped}%')
            conditions.append(f"({' AND '.join(kw_conds)})")

        return conditions, params

    async def search_images(
        self,
        *,
        category: Optional[str] = None,
        exposure_level: Optional[str] = None,
        style: Optional[list[str]] = None,
        scene: Optional[list[str]] = None,
        atmosphere: Optional[list[str]] = None,
        pose_type: Optional[str] = None,
        body_focus: Optional[list[str]] = None,
        persona: str = "",
        exclude_persona: str = "",
        shot_size: Optional[str] = None,
        favorite: Optional[str] = None,
        ref_strength: Optional[str] = None,
        sort_by: str = "created_at",
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions, params = self._build_search_conditions(
            category=category, exposure_level=exposure_level,
            style=style, scene=scene, atmosphere=atmosphere,
            pose_type=pose_type, body_focus=body_focus,
            persona=persona, exclude_persona=exclude_persona,
            shot_size=shot_size, favorite=favorite,
            ref_strength=ref_strength,
        )

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        params.append(limit)
        params.append(offset)

        if sort_by == "use_count":
            order_clause = "use_count DESC, created_at DESC"
        elif sort_by == "favorite":
            order_clause = "CASE favorite WHEN 'favorite' THEN 1 WHEN 'like' THEN 2 ELSE 3 END, created_at DESC"
        else:
            order_clause = "created_at DESC"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = f"SELECT * FROM images {where_clause} ORDER BY {order_clause} LIMIT ? OFFSET ?"
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_dict(row) for row in rows]

    async def search_count(
        self,
        *,
        category: Optional[str] = None,
        exposure_level: Optional[str] = None,
        style: Optional[list[str]] = None,
        scene: Optional[list[str]] = None,
        atmosphere: Optional[list[str]] = None,
        pose_type: Optional[str] = None,
        body_focus: Optional[list[str]] = None,
        persona: str = "",
        exclude_persona: str = "",
        shot_size: Optional[str] = None,
        favorite: Optional[str] = None,
    ) -> int:
        conditions, params = self._build_search_conditions(
            category=category, exposure_level=exposure_level,
            style=style, scene=scene, atmosphere=atmosphere,
            pose_type=pose_type, body_focus=body_focus,
            persona=persona, exclude_persona=exclude_persona,
            shot_size=shot_size, favorite=favorite,
        )

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(f"SELECT COUNT(*) FROM images {where_clause}", params) as cursor:
                return (await cursor.fetchone())[0]

    async def search_by_description(
        self,
        *,
        keywords: list[str],
        category: Optional[str] = None,
        persona: str = "",
        exclude_persona: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions, params = self._build_search_conditions(
            category=category, persona=persona,
            exclude_persona=exclude_persona, keywords=keywords,
        )

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        params.append(limit)
        params.append(offset)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = f"SELECT * FROM images {where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?"
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                results = [self._row_to_dict(row) for row in rows]

        if results:
            return results

        bigram_groups = self._bigram_decompose_grouped(keywords)
        if bigram_groups:
            results = await self._search_with_grouped_keywords(
                bigram_groups, category=category, persona=persona,
                exclude_persona=exclude_persona, limit=limit, offset=offset,
            )
            if results:
                return results

        truncate_groups = self._progressive_truncate_grouped(keywords)
        if truncate_groups:
            results = await self._search_with_grouped_keywords(
                truncate_groups, category=category, persona=persona,
                exclude_persona=exclude_persona, limit=limit, offset=offset,
            )
            if results:
                return results

        char_groups = self._char_and_grouped(keywords)
        if char_groups:
            results = await self._search_with_grouped_keywords(
                char_groups, category=category, persona=persona,
                exclude_persona=exclude_persona, limit=limit, offset=offset,
            )
            if results:
                return results

        return []

    async def _search_with_grouped_keywords(
        self,
        groups: list[list[str]],
        *,
        category: Optional[str] = None,
        persona: str = "",
        exclude_persona: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        base_conditions, base_params = self._build_search_conditions(
            category=category, persona=persona,
            exclude_persona=exclude_persona, keywords=None,
        )

        for group in groups:
            group_conds = []
            for variant in group:
                escaped = WardrobeDatabase._escape_like(variant)
                group_conds.append(
                    "(description LIKE ? ESCAPE '\\' OR user_tags LIKE ? ESCAPE '\\' "
                    "OR exposure_features LIKE ? ESCAPE '\\' OR key_features LIKE ? ESCAPE '\\' "
                    "OR prop_objects LIKE ? ESCAPE '\\' OR allure_features LIKE ? ESCAPE '\\' "
                    "OR body_focus LIKE ? ESCAPE '\\')"
                )
                for _ in range(7):
                    base_params.append(f'%{escaped}%')
            base_conditions.append(f"({' OR '.join(group_conds)})")

        where_clause = ""
        if base_conditions:
            where_clause = "WHERE " + " AND ".join(base_conditions)
        base_params.append(limit)
        base_params.append(offset)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = f"SELECT * FROM images {where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?"
            async with db.execute(sql, base_params) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _bigram_decompose_grouped(keywords: list[str]) -> list[list[str]]:
        groups: list[list[str]] = []
        for kw in keywords:
            if len(kw) <= 1:
                continue
            elif len(kw) == 2:
                groups.append([kw])
            else:
                bigrams = [kw[i:i + 2] for i in range(len(kw) - 1)]
                groups.append(bigrams)
        return groups

    @staticmethod
    def _progressive_truncate_grouped(keywords: list[str]) -> list[list[str]]:
        groups: list[list[str]] = []
        for kw in keywords:
            if len(kw) <= 2:
                continue
            variants = []
            for i in range(len(kw) - 1, 0, -1):
                truncated = kw[:i]
                if truncated:
                    variants.append(truncated)
            if variants:
                groups.append(variants)
        return groups

    @staticmethod
    def _char_and_grouped(keywords: list[str]) -> list[list[str]]:
        groups: list[list[str]] = []
        for kw in keywords:
            if len(kw) <= 1:
                continue
            chars = list(kw)
            groups.append(chars)
        return groups

    async def get_stats(self) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM images") as cursor:
                total = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT category, COUNT(*) FROM images GROUP BY category"
            ) as cursor:
                category_counts = dict(await cursor.fetchall())
            async with db.execute(
                "SELECT exposure_level, COUNT(*) FROM images GROUP BY exposure_level"
            ) as cursor:
                exposure_counts = dict(await cursor.fetchall())
        return {
            "total": total,
            "by_category": category_counts,
            "by_exposure": exposure_counts,
        }

    async def list_images(
        self,
        *,
        category: Optional[str] = None,
        shot_size: Optional[str] = None,
        favorite: Optional[str] = None,
        ref_strength: Optional[str] = None,
        sort_by: str = "created_at",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []
        if category:
            conditions.append("category = ?")
            params.append(category)
        if shot_size:
            conditions.append("shot_size = ?")
            params.append(shot_size)
        if favorite and favorite in ("favorite", "like"):
            conditions.append("favorite = ?")
            params.append(favorite)
        if ref_strength:
            conditions.append("ref_strength = ?")
            params.append(ref_strength)
        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)
        params.extend([limit, offset])
        if sort_by == "use_count":
            order_clause = "use_count DESC, created_at DESC"
        elif sort_by == "favorite":
            order_clause = "CASE favorite WHEN 'favorite' THEN 1 WHEN 'like' THEN 2 ELSE 3 END, created_at DESC"
        else:
            order_clause = "created_at DESC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = f"SELECT * FROM images {where_clause} ORDER BY {order_clause} LIMIT ? OFFSET ?"
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_dict(row) for row in rows]

    async def list_images_lightweight(
        self,
        *,
        category: Optional[str] = None,
        shot_size: Optional[str] = None,
        persona: str = "",
        exclude_persona: str = "",
        favorite: Optional[str] = None,
        ref_strength: Optional[str] = None,
        sort_by: str = "created_at",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []
        if category:
            conditions.append("category = ?")
            params.append(category)
        if shot_size:
            conditions.append("shot_size = ?")
            params.append(shot_size)
        if persona:
            conditions.append("persona = ?")
            params.append(persona)
        if exclude_persona:
            conditions.append("persona != ?")
            params.append(exclude_persona)
        if favorite and favorite in ("favorite", "like"):
            conditions.append("favorite = ?")
            params.append(favorite)
        if ref_strength:
            conditions.append("ref_strength = ?")
            params.append(ref_strength)
        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)
        params.extend([limit, offset])
        if sort_by == "use_count":
            order_clause = "use_count DESC, created_at DESC"
        elif sort_by == "favorite":
            order_clause = "CASE favorite WHEN 'favorite' THEN 1 WHEN 'like' THEN 2 ELSE 3 END, created_at DESC"
        else:
            order_clause = "created_at DESC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = f"SELECT id, category, persona, image_path, created_at, favorite, use_count, ref_strength FROM images {where_clause} ORDER BY {order_clause} LIMIT ? OFFSET ?"
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_all_records(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM images") as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_dict(row) for row in rows]

    async def import_records(self, records: list[dict[str, Any]], skip_existing: bool = True) -> int:
        existing_ids = set()
        if skip_existing:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT id FROM images") as cursor:
                    async for row in cursor:
                        existing_ids.add(row[0])

        imported = 0
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                for rec in records:
                    if skip_existing and rec.get("id") in existing_ids:
                        continue
                    try:
                        await db.execute(
                            """INSERT INTO images (
                                id, category, style, clothing_type, exposure_level,
                                scene, atmosphere, pose_type, body_orientation,
                                dynamic_level, action_style, shot_size, camera_angle,
                                expression, color_tone, composition, background,
                                description, user_tags, exposure_features, key_features, prop_objects, allure_features, body_focus,
                                persona, image_path, created_at, updated_at, created_by, favorite, use_count, file_hash, ref_strength
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                rec.get("id", str(uuid.uuid4())),
                                rec.get("category", "人物"),
                                rec.get("style", "[]") if isinstance(rec.get("style"), str) else json.dumps(rec.get("style", []), ensure_ascii=False),
                                rec.get("clothing_type", ""),
                                rec.get("exposure_level", ""),
                                rec.get("scene", "[]") if isinstance(rec.get("scene"), str) else json.dumps(rec.get("scene", []), ensure_ascii=False),
                                rec.get("atmosphere", "[]") if isinstance(rec.get("atmosphere"), str) else json.dumps(rec.get("atmosphere", []), ensure_ascii=False),
                                rec.get("pose_type", ""),
                                rec.get("body_orientation", ""),
                                rec.get("dynamic_level", ""),
                                rec.get("action_style", "[]") if isinstance(rec.get("action_style"), str) else json.dumps(rec.get("action_style", []), ensure_ascii=False),
                                rec.get("shot_size", ""),
                                rec.get("camera_angle", ""),
                                rec.get("expression", ""),
                                rec.get("color_tone", ""),
                                rec.get("composition", ""),
                                rec.get("background", ""),
                                rec.get("description", ""),
                                rec.get("user_tags", ""),
                                rec.get("exposure_features", "[]") if isinstance(rec.get("exposure_features"), str) else json.dumps(rec.get("exposure_features", []), ensure_ascii=False),
                                rec.get("key_features", "[]") if isinstance(rec.get("key_features"), str) else json.dumps(rec.get("key_features", []), ensure_ascii=False),
                                rec.get("prop_objects", "[]") if isinstance(rec.get("prop_objects"), str) else json.dumps(rec.get("prop_objects", []), ensure_ascii=False),
                                rec.get("allure_features", "[]") if isinstance(rec.get("allure_features"), str) else json.dumps(rec.get("allure_features", []), ensure_ascii=False),
                                rec.get("body_focus", "[]") if isinstance(rec.get("body_focus"), str) else json.dumps(rec.get("body_focus", []), ensure_ascii=False),
                                rec.get("persona", ""),
                                rec.get("image_path", ""),
                                rec.get("created_at", datetime.now(timezone.utc).isoformat()),
                                rec.get("updated_at", datetime.now(timezone.utc).isoformat()),
                                rec.get("created_by", ""),
                                rec.get("favorite", "none"),
                                rec.get("use_count", 0),
                                rec.get("file_hash", ""),
                                rec.get("ref_strength", "style"),
                            ),
                        )
                        imported += 1
                    except Exception as e:
                        logger.warning("[Wardrobe] 导入记录跳过: id=%s error=%s", rec.get("id"), e)
                await db.commit()
        return imported

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        for key in ("style", "scene", "atmosphere", "action_style", "exposure_features", "key_features", "prop_objects", "allure_features", "body_focus"):
            val = d.get(key)
            if isinstance(val, str):
                try:
                    d[key] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    d[key] = []
        return d
