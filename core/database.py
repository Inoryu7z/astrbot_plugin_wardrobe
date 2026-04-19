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
    image_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    persona TEXT DEFAULT '',
    created_by TEXT DEFAULT ''
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_category ON images(category);
CREATE INDEX IF NOT EXISTS idx_exposure_level ON images(exposure_level);
CREATE INDEX IF NOT EXISTS idx_style ON images(style);
CREATE INDEX IF NOT EXISTS idx_scene ON images(scene);
"""

_UPDATABLE_FIELDS = frozenset({
    "category", "style", "clothing_type", "exposure_level", "persona",
    "scene", "atmosphere", "pose_type", "body_orientation",
    "dynamic_level", "action_style", "shot_size", "camera_angle",
    "expression", "color_tone", "composition", "background",
    "description", "image_path", "updated_at",
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
                await db.executescript(_CREATE_INDEX_SQL)
                try:
                    await db.execute("ALTER TABLE images ADD COLUMN persona TEXT DEFAULT ''")
                except Exception:
                    pass
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
        persona: str = "",
        image_path: str,
        created_by: str = "",
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
                        description, persona, image_path, created_at, updated_at, created_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                        persona,
                        image_path,
                        now,
                        now,
                        created_by,
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

    async def search_images(
        self,
        *,
        category: Optional[str] = None,
        exposure_level: Optional[str] = None,
        style: Optional[list[str]] = None,
        scene: Optional[list[str]] = None,
        atmosphere: Optional[list[str]] = None,
        persona: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []

        if category:
            conditions.append("category = ?")
            params.append(category)

        if exposure_level:
            conditions.append("exposure_level = ?")
            params.append(exposure_level)

        if persona:
            conditions.append("persona = ?")
            params.append(persona)

        if style:
            style_conditions = []
            for s in style:
                style_conditions.append("style LIKE ?")
                params.append(f'%{s}%')
            conditions.append(f"({' OR '.join(style_conditions)})")

        if scene:
            scene_conditions = []
            for s in scene:
                scene_conditions.append("scene LIKE ?")
                params.append(f'%{s}%')
            conditions.append(f"({' OR '.join(scene_conditions)})")

        if atmosphere:
            atm_conditions = []
            for a in atmosphere:
                atm_conditions.append("atmosphere LIKE ?")
                params.append(f'%{a}%')
            conditions.append(f"({' OR '.join(atm_conditions)})")

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = f"SELECT * FROM images {where_clause} ORDER BY created_at DESC LIMIT ?"
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_dict(row) for row in rows]

    async def search_by_description(
        self,
        *,
        keywords: list[str],
        category: Optional[str] = None,
        persona: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []

        if category:
            conditions.append("category = ?")
            params.append(category)

        if persona:
            conditions.append("persona = ?")
            params.append(persona)

        desc_conditions = []
        for kw in keywords:
            desc_conditions.append("description LIKE ?")
            params.append(f'%{kw}%')
        conditions.append(f"({' OR '.join(desc_conditions)})")

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = f"SELECT * FROM images {where_clause} ORDER BY created_at DESC LIMIT ?"
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_dict(row) for row in rows]

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
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []
        if category:
            conditions.append("category = ?")
            params.append(category)
        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)
        params.extend([limit, offset])
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = f"SELECT * FROM images {where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?"
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        for key in ("style", "scene", "atmosphere", "action_style"):
            val = d.get(key)
            if isinstance(val, str):
                try:
                    d[key] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    d[key] = []
        return d
