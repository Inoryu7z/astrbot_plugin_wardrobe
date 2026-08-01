import asyncio
import json
import secrets
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

import uvicorn
from quart import (
    Quart,
    Response,
    jsonify,
    redirect,
    request,
    send_file,
    send_from_directory,
    session,
)

from astrbot.api import logger

_TOKEN_TTL = 86400 * 7

STYLE_GROUPS = {
    "洛丽塔系": ["甜系洛丽塔", "古典洛丽塔", "哥特洛丽塔", "中华风洛丽塔", "海军风洛丽塔", "田园风洛丽塔", "蒸汽朋克洛丽塔", "公主风洛丽塔", "古董风洛丽塔", "朋克洛丽塔", "Ero洛丽塔", "暗黑洛丽塔"],
    "JK系": ["水手服", "西装JK", "连衣裙JK", "知性学院风", "英伦学院风", "预科生风格", "百褶裙学院风", "制服通勤风", "超短裙JK"],
    "汉服系": ["魏晋风汉服", "唐风汉服", "宋风汉服", "明风汉服", "新中式禅意温婉风", "改良汉服", "古风飘逸仕女风", "明制日常汉服", "宋制雅致汉服"],
    "甜系": ["奶甜少女风", "清甜初恋风", "宫廷甜妹风", "芭蕾少女风", "仙气公主风", "软萌居家风", "梦幻奶油风", "草莓少女风", "性感甜妹风", "复古甜妹风", "芭蕾风连衣裙"],
    "纯欲系": ["柔焦纯欲风", "轻熟纯欲风", "居家纯欲风", "纯欲少女风", "雾面纯欲风", "清透纯欲风", "诱惑纯欲风", "挂脖露肩装", "甜欲露背装", "甜欲露脐装"],
    "法式优雅系": ["法式优雅风", "法式浪漫风", "温柔淑女风", "轻礼服千金风", "精致约会风", "英式复古优雅风"],
    "暗黑系": ["哥特暗黑风", "赛博朋克风", "暗黑少女风", "维多利亚暗黑风", "暗黑哥特风", "哥特风", "地雷系", "水色天使风"],
    "日韩系": ["日系软妹风", "日系森女风", "韩系甜美风", "韩系温柔风", "日系通勤风", "韩系清冷风", "量产型甜美风"],
    "性感系": ["性感兔女郎风", "性感女仆风", "高开叉旗袍风", "魅惑吊带风"],
    "其他": ["女仆装", "旗袍", "改良韩服温柔风", "复古风", "波西米亚风", "维多利亚复古风", "赛博机械风", "cosplay风", "花嫁", "中华娘风", "修女风", "巫女服", "女儿服", "日式体操服"],
}

SCENE_GROUPS = {
    "日常": ["日常休闲", "通勤上学", "居家休息", "逛街购物", "校园日常", "办公室通勤", "居家懒人风", "街头随拍"],
    "社交": ["约会", "聚会派对", "下午茶", "餐厅用餐", "朋友聚会", "正式晚宴", "夜店派对", "咖啡厅约会"],
    "拍摄": ["拍照写真", "cosplay", "漫展活动", "舞台表演", "婚礼场合", "艺术写真", "商业拍摄", "私房写真"],
    "季节": ["春季穿搭", "夏季穿搭", "秋季穿搭", "冬季穿搭", "初夏清凉风", "深秋复古风", "寒冬保暖风"],
    "氛围场景": ["浪漫氛围", "度假氛围", "酷帅氛围", "梦幻氛围", "清新田园氛围", "复古怀旧氛围", "赛博未来氛围"],
    "私密": ["夜晚诱惑约会", "卧室情趣场景", "性感私密写真", "魅惑室内拍摄"],
}


class WardrobeWebServer:
    def __init__(self, plugin, config: dict):
        self.plugin = plugin
        self.config = config
        self.host = str(plugin._cfg("webui_host", "127.0.0.1") or "127.0.0.1")
        self.port = int(plugin._cfg("webui_port", 18921) or 18921)
        self._tokens: dict[str, float] = {}
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task | None = None
        self._web_dir = Path(__file__).parent.parent / "web"

    @property
    def password(self):
        return str(self.plugin._cfg("webui_password", "wardrobe") or "wardrobe")

    def _is_token_valid(self, token: str) -> bool:
        if token not in self._tokens:
            return False
        if time.time() - self._tokens[token] > _TOKEN_TTL:
            del self._tokens[token]
            return False
        return True

    def _cleanup_expired_tokens(self):
        now = time.time()
        expired = [t for t, ts in self._tokens.items() if now - ts > _TOKEN_TTL]
        for t in expired:
            del self._tokens[t]

    def _create_app(self) -> Quart:
        app = Quart(
            "wardrobe_webui",
            static_folder=str(self._web_dir),
            static_url_path="/static",
        )
        app.secret_key = secrets.token_hex(32)
        app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024 * 1024
        app.config["BODY_TIMEOUT"] = 1200

        @app.errorhandler(404)
        async def handle_404(e):
            logger.debug("[Wardrobe] WebUI 404: %s %s", request.method, request.path)
            if request.path.startswith("/api/"):
                return jsonify({"error": "未找到"}), 404
            return jsonify({"error": "未找到"}), 404

        @app.errorhandler(Exception)
        async def handle_exception(e):
            logger.error("[Wardrobe] WebUI未捕获异常: %s", e, exc_info=True)
            return jsonify({"error": f"服务器内部错误: {e}"}), 500

        @app.errorhandler(413)
        async def handle_413(e):
            logger.warning("[Wardrobe] WebUI请求体过大: %s", e)
            return jsonify({"error": "上传文件过大，请压缩后重试"}), 413

        @app.before_request
        async def auth_check():
            if request.path.startswith("/static/") or request.path == "/login":
                return None
            if request.path == "/api/login":
                return None
            token = (
                request.headers.get("X-Wardrobe-Token", "")
                or request.cookies.get("wardrobe_token", "")
                or session.get("token", "")
            )
            if not self._is_token_valid(token):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "未授权"}), 401
                return redirect("/login")

        @app.route("/login")
        async def login_page():
            return await send_from_directory(str(self._web_dir), "login.html")

        @app.route("/")
        async def index():
            return await send_from_directory(str(self._web_dir), "index.html")

        @app.route("/api/login", methods=["POST"])
        async def api_login():
            data = await request.get_json(silent=True) or {}
            pwd = data.get("password", "")
            if secrets.compare_digest(pwd, self.password):
                self._cleanup_expired_tokens()
                token = secrets.token_hex(32)
                self._tokens[token] = time.time()
                session["token"] = token
                resp = jsonify({"success": True, "token": token})
                resp.set_cookie("wardrobe_token", token, max_age=86400 * 7, httponly=True, samesite="Lax")
                return resp
            return jsonify({"success": False, "error": "密码错误"}), 403

        @app.route("/api/logout", methods=["POST"])
        async def api_logout():
            token = session.pop("token", "") or request.cookies.get("wardrobe_token", "")
            self._tokens.pop(token, None)
            resp = jsonify({"success": True})
            resp.delete_cookie("wardrobe_token")
            return resp

        @app.route("/api/stats")
        async def api_stats():
            await self.plugin._ensure_db()
            stats = await self.plugin.db.get_stats()
            return jsonify(stats)

        @app.route("/api/stats/detail")
        async def api_stats_detail():
            await self.plugin._ensure_db()
            category = request.args.get("category", "")
            persona = request.args.get("persona", "")
            favorite = request.args.get("favorite", "")

            dist = await self.plugin.db.get_tag_distribution(
                category=category or None,
                persona=persona or None,
                favorite=favorite if favorite in ("favorite", "like", "meh") else None,
            )

            try:
                pools = await self.plugin.get_merged_pools()
            except Exception:
                pools = {}

            custom_style = set(pools.get("style", [])) - set(STYLE_GROUPS.get("其他", []))
            for group_tags in STYLE_GROUPS.values():
                custom_style -= set(group_tags)
            style_groups = dict(STYLE_GROUPS)
            if custom_style:
                style_groups["自定义"] = sorted(custom_style)

            custom_scene = set(pools.get("scene", [])) - set(SCENE_GROUPS.get("私密", []))
            for group_tags in SCENE_GROUPS.values():
                custom_scene -= set(group_tags)
            scene_groups = dict(SCENE_GROUPS)
            if custom_scene:
                scene_groups["自定义"] = sorted(custom_scene)

            dist["style_groups"] = style_groups
            dist["scene_groups"] = scene_groups

            return jsonify(dist)

        @app.route("/api/stats/timeline")
        async def api_stats_timeline():
            await self.plugin._ensure_db()
            category = request.args.get("category", "")
            persona = request.args.get("persona", "")
            favorite = request.args.get("favorite", "")
            timeline = await self.plugin.db.get_timeline(
                category=category or None,
                persona=persona or None,
                favorite=favorite if favorite in ("favorite", "like", "meh") else None,
            )
            return jsonify(timeline)

        @app.route("/api/images")
        async def api_images():
            await self.plugin._ensure_db()
            page = max(1, int(request.args.get("page", 1)))
            per_page = min(100, max(1, int(request.args.get("per_page", 24))))
            category = request.args.get("category", "")
            persona = request.args.get("persona", "")
            if persona == "__none__":
                persona = ""
            else:
                persona = persona or None
            style = request.args.get("style", "")
            scene = request.args.get("scene", "")
            shot_size = request.args.get("shot_size", "")
            atmosphere = request.args.get("atmosphere", "")
            favorite = request.args.get("favorite", "")
            ref_strength = request.args.get("ref_strength", "")
            sort_by = request.args.get("sort_by", "created_at")
            lightweight = request.args.get("lightweight", "") == "1"

            offset = (page - 1) * per_page

            needs_search = style or scene or atmosphere or shot_size or (persona is not None) or category or (favorite in ("favorite", "like", "meh")) or ref_strength
            if needs_search:
                style_list = [style] if style else None
                scene_list = [scene] if scene else None
                atmosphere_list = [atmosphere] if atmosphere else None
                images = await self.plugin.db.search_images(
                    category=category or None,
                    persona=persona,
                    style=style_list,
                    scene=scene_list,
                    atmosphere=atmosphere_list,
                    shot_size=shot_size or None,
                    favorite=favorite if favorite in ("favorite", "like", "meh") else None,
                    ref_strength=ref_strength or None,
                    sort_by=sort_by,
                    limit=per_page,
                    offset=offset,
                )
                total = await self.plugin.db.search_count(
                    category=category or None,
                    persona=persona,
                    style=style_list,
                    scene=scene_list,
                    atmosphere=atmosphere_list,
                    shot_size=shot_size or None,
                    favorite=favorite if favorite in ("favorite", "like", "meh") else None,
                    ref_strength=ref_strength or None,
                )
            elif lightweight:
                images = await self.plugin.db.list_images_lightweight(
                    category=category or None,
                    shot_size=shot_size or None,
                    persona=persona,
                    favorite=favorite if favorite in ("favorite", "like", "meh") else None,
                    ref_strength=ref_strength or None,
                    sort_by=sort_by,
                    limit=per_page,
                    offset=offset,
                )
                total = await self.plugin.db.search_count(
                    category=category or None,
                    shot_size=shot_size or None,
                    persona=persona,
                    favorite=favorite if favorite in ("favorite", "like", "meh") else None,
                    ref_strength=ref_strength or None,
                )
            else:
                images = await self.plugin.db.list_images(
                    category=category or None, shot_size=shot_size or None,
                    favorite=favorite if favorite in ("favorite", "like", "meh") else None,
                    ref_strength=ref_strength or None,
                    sort_by=sort_by,
                    limit=per_page, offset=offset
                )
                total = await self.plugin.db.search_count(
                    category=category or None,
                    shot_size=shot_size or None,
                    favorite=favorite if favorite in ("favorite", "like", "meh") else None,
                    ref_strength=ref_strength or None,
                )

            result = {
                "images": images,
                "total": total,
                "page": page,
                "per_page": per_page,
            }
            return jsonify(result)

        @app.route("/api/images/failed")
        async def api_images_failed():
            await self.plugin._ensure_db()
            ids = await self.plugin.db.get_failed_image_ids()
            return jsonify({"ids": ids})

        @app.route("/api/images/<image_id>")
        async def api_image_detail(image_id):
            await self.plugin._ensure_db()
            image = await self.plugin.db.get_image(image_id)
            if not image:
                return jsonify({"error": "未找到图片"}), 404
            return jsonify(image)

        @app.route("/api/images/<image_id>", methods=["DELETE"])
        async def api_image_delete(image_id):
            await self.plugin._ensure_db()
            image = await self.plugin.db.get_image(image_id)
            if not image:
                return jsonify({"error": "未找到图片"}), 404
            deleted = await self.plugin.db.delete_image(image_id)
            if deleted and image.get("image_path"):
                await self.plugin.store.delete_image(image["image_path"])
            if deleted:
                await self.plugin._cleanup_videos_for_image(image_id)
            return jsonify({"success": bool(deleted)})

        @app.route("/api/images/<image_id>", methods=["PUT"])
        async def api_image_update(image_id):
            await self.plugin._ensure_db()
            image = await self.plugin.db.get_image(image_id)
            if not image:
                return jsonify({"error": "未找到图片"}), 404

            data = await request.get_json(silent=True) or {}
            if not data:
                return jsonify({"error": "无更新数据"}), 400

            list_fields = {"style", "scene", "atmosphere", "action_style",
                           "exposure_features", "key_features", "prop_objects", "allure_features", "body_focus"}
            update_data = {}
            for key, val in data.items():
                if key in list_fields:
                    if isinstance(val, str):
                        try:
                            import json as _json
                            val = _json.loads(val)
                        except (ValueError, TypeError):
                            val = [v.strip() for v in val.replace("，", ",").split(",") if v.strip()]
                    if not isinstance(val, list):
                        val = [str(val)]
                    update_data[key] = val
                elif key in ("category", "clothing_type", "exposure_level", "pose_type",
                             "body_orientation", "dynamic_level", "shot_size", "camera_angle",
                             "expression", "color_tone", "composition", "background",
                             "description", "user_tags", "persona", "favorite", "ref_strength",
                             "ai_prompt"):
                    update_data[key] = str(val) if val is not None else ""
                # use_count 字段已改为只读（由系统自动维护），不允许 WebUI 编辑

            if not update_data:
                return jsonify({"error": "无有效更新字段"}), 400

            updated = await self.plugin.db.update_image(image_id, **update_data)
            if not updated:
                return jsonify({"error": "更新失败"}), 500

            if self.plugin.vector_searcher and self.plugin.vector_searcher.available:
                try:
                    updated_image = await self.plugin.db.get_image(image_id)
                    if updated_image:
                        await self.plugin._index_to_vector(
                            image_id,
                            updated_image.get("description", ""),
                            updated_image.get("user_tags", ""),
                            exposure_features=updated_image.get("exposure_features", []),
                            key_features=updated_image.get("key_features", []),
                            prop_objects=updated_image.get("prop_objects", []),
                            allure_features=updated_image.get("allure_features", []),
                            body_focus=updated_image.get("body_focus", []),
                            category=updated_image.get("category", ""),
                            persona=updated_image.get("persona", ""),
                        )
                except Exception as e:
                    logger.warning("[Wardrobe] 编辑后向量索引重建失败: %s", e)

            return jsonify({"success": True})

        @app.route("/api/images/<image_id>/reanalyze", methods=["POST"])
        async def api_image_reanalyze(image_id):
            await self.plugin._ensure_db()
            logger.debug("[Wardrobe] WebUI重新分析请求: id=%s", image_id)
            image = await self.plugin.db.get_image(image_id)
            if not image:
                return jsonify({"error": "未找到图片"}), 404

            image_path_str = image.get("image_path", "")
            if not image_path_str:
                return jsonify({"error": "图片路径为空"}), 400

            path = self.plugin.store.get_image_path(image_path_str)
            if not path.exists():
                return jsonify({"error": "图片文件不存在"}), 404

            data = await request.get_json(silent=True) or {}
            user_description = data.get("description", "").strip()[:2000]

            try:
                import aiofiles
                async with aiofiles.open(path, "rb") as f:
                    image_bytes = await f.read()

                if not image_bytes:
                    return jsonify({"error": "图片文件为空"}), 400

                primary = str(self.plugin._cfg("save_provider_id", "") or "").strip()
                secondary = str(self.plugin._cfg("save_secondary_provider_id", "") or "").strip()
                timeout = float(self.plugin._cfg("save_timeout_seconds", 60.0) or 60.0)

                if not primary and not secondary:
                    return jsonify({"error": "未配置存图模型"}), 400

                attrs = await self.plugin.analyzer.analyze_image(
                    image_bytes,
                    user_description=user_description,
                    primary_provider_id=primary,
                    secondary_provider_id=secondary,
                    timeout_seconds=timeout,
                    persona=image.get("persona", ""),
                )

                if not attrs:
                    logger.warning("[Wardrobe] WebUI重新分析失败: 模型返回空结果 id=%s", image_id)
                    return jsonify({"error": "模型分析失败"}), 500

                from ..core.utils import ensure_list

                update_data = {}
                for field in ("exposure_features", "key_features", "prop_objects", "allure_features", "body_focus"):
                    update_data[field] = ensure_list(attrs.get(field))

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

                if user_description:
                    update_data["user_tags"] = user_description

                await self.plugin.db.update_image(image_id, **update_data)
                logger.debug("[Wardrobe] 重新分析完成: id=%s 分类=%s",
                            image_id, attrs.get("category", ""))

                if self.plugin.vector_searcher and self.plugin.vector_searcher.available:
                    try:
                        updated_image = await self.plugin.db.get_image(image_id)
                        if updated_image:
                            await self.plugin._index_to_vector(
                                image_id,
                                updated_image.get("description", ""),
                                updated_image.get("user_tags", ""),
                                exposure_features=updated_image.get("exposure_features", []),
                                key_features=updated_image.get("key_features", []),
                                prop_objects=updated_image.get("prop_objects", []),
                                allure_features=updated_image.get("allure_features", []),
                                body_focus=updated_image.get("body_focus", []),
                                category=updated_image.get("category", ""),
                                persona=updated_image.get("persona", ""),
                            )
                    except Exception as e:
                        logger.warning("[Wardrobe] 重新分析后向量索引重建失败: %s", e)

                updated = await self.plugin.db.get_image(image_id)
                return jsonify({"success": True, "image": updated})

            except Exception as e:
                logger.error("[Wardrobe] 重新分析失败: %s", e, exc_info=True)
                return jsonify({"error": f"重新分析失败: {e}"}), 500

        @app.route("/api/images/batch-delete", methods=["POST"])
        async def api_images_batch_delete():
            await self.plugin._ensure_db()
            data = await request.get_json(silent=True) or {}
            ids = data.get("ids", [])
            if not ids:
                return jsonify({"error": "未指定图片"}), 400
            deleted_count = 0
            for image_id in ids:
                image = await self.plugin.db.get_image(image_id)
                if image:
                    ok = await self.plugin.db.delete_image(image_id)
                    if ok:
                        deleted_count += 1
                        if image.get("image_path"):
                            await self.plugin.store.delete_image(image["image_path"])
                        if self.plugin.vector_searcher:
                            try:
                                await self.plugin.vector_searcher.remove_image(image_id)
                            except Exception:
                                pass
                        await self.plugin._cleanup_videos_for_image(image_id)
            return jsonify({"success": True, "deleted": deleted_count})

        @app.route("/api/images/batch-favorite", methods=["POST"])
        async def api_images_batch_favorite():
            await self.plugin._ensure_db()
            data = await request.get_json(silent=True) or {}
            ids = data.get("ids", [])
            fav = data.get("favorite", "none")
            if not ids:
                return jsonify({"error": "未指定图片"}), 400
            if fav not in ("favorite", "like", "meh", "none"):
                return jsonify({"error": "无效的收藏值，应为 favorite/like/meh/none"}), 400
            updated = 0
            for image_id in ids:
                ok = await self.plugin.db.update_image(image_id, favorite=fav)
                if ok:
                    updated += 1
            return jsonify({"success": True, "updated": updated, "favorite": fav})

        @app.route("/api/images/batch-clear-use-count", methods=["POST"])
        async def api_images_batch_clear_use_count():
            await self.plugin._ensure_db()
            data = await request.get_json(silent=True) or {}
            ids = data.get("ids", [])
            if not ids:
                return jsonify({"error": "未指定图片"}), 400
            cleared = await self.plugin.db.batch_clear_use_counts(ids)
            return jsonify({"success": True, "cleared": cleared})

        @app.route("/api/images/batch-add-style", methods=["POST"])
        async def api_images_batch_add_style():
            await self.plugin._ensure_db()
            data = await request.get_json(silent=True) or {}
            ids = data.get("ids", [])
            style_name = (data.get("style_name", "") or "").strip()
            if not ids:
                return jsonify({"error": "未指定图片"}), 400
            if not style_name:
                return jsonify({"error": "未指定风格"}), 400
            updated = 0
            for image_id in ids:
                img = await self.plugin.db.get_image(image_id)
                if not img:
                    continue
                current = img.get("style", []) or []
                if isinstance(current, str):
                    current = [current]
                if style_name not in current:
                    current.append(style_name)
                    ok = await self.plugin.db.update_image(image_id, style=current)
                    if ok:
                        updated += 1
                else:
                    updated += 1
            return jsonify({"success": True, "updated": updated, "style_name": style_name})

        @app.route("/api/images/ids")
        async def api_images_ids():
            await self.plugin._ensure_db()
            category = request.args.get("category", "")
            persona = request.args.get("persona", "")
            if persona == "__none__":
                persona = ""
            else:
                persona = persona or None
            style = request.args.get("style", "")
            scene = request.args.get("scene", "")
            atmosphere = request.args.get("atmosphere", "")
            shot_size = request.args.get("shot_size", "")
            favorite = request.args.get("favorite", "")
            ref_strength = request.args.get("ref_strength", "")

            style_list = [style] if style else None
            scene_list = [scene] if scene else None
            atmosphere_list = [atmosphere] if atmosphere else None

            ids = await self.plugin.db.get_ids_by_filter(
                category=category or None,
                persona=persona,
                style=style_list,
                scene=scene_list,
                atmosphere=atmosphere_list,
                shot_size=shot_size or None,
                favorite=favorite if favorite in ("favorite", "like", "meh") else None,
                ref_strength=ref_strength or None,
            )
            return jsonify({"ids": ids})

        @app.route("/api/images/export", methods=["POST"])
        async def api_images_export():
            try:
                await self.plugin._ensure_db()
                data = await request.get_json(silent=True) or {}
                ids = data.get("ids", [])
                if not ids:
                    return jsonify({"error": "未选择图片"}), 400

                file_path, added_files = await self.plugin.export_images_zip(ids)
                if not file_path or added_files == 0:
                    return jsonify({"error": "没有可导出的图片"}), 400

                logger.debug("[Wardrobe] 图片导出: %d张图片", added_files)
                return await send_file(
                    str(file_path),
                    mimetype="application/zip",
                    as_attachment=True,
                    attachment_filename=f"wardrobe_images_{time.strftime('%Y%m%d_%H%M%S')}.zip",
                )
            except Exception as e:
                logger.error("[Wardrobe] 图片导出失败: %s", e, exc_info=True)
                return jsonify({"error": f"导出失败: {e}"}), 500

        @app.route("/api/backup/export-selected", methods=["POST"])
        async def api_backup_export_selected():
            try:
                await self.plugin._ensure_db()
                data = await request.get_json(silent=True) or {}
                ids = data.get("ids", [])
                if not ids:
                    return jsonify({"error": "未选择图片"}), 400

                file_path, total_records, added_files = await self.plugin.build_selected_backup_zip(ids)
                if not file_path or total_records == 0:
                    return jsonify({"error": "没有可导出的数据"}), 400

                logger.debug("[Wardrobe] 选择备份导出: %d条记录, %d个图片文件", total_records, added_files)
                return await send_file(
                    str(file_path),
                    mimetype="application/zip",
                    as_attachment=True,
                    attachment_filename=f"wardrobe_backup_selected_{time.strftime('%Y%m%d_%H%M%S')}.zip",
                )
            except Exception as e:
                logger.error("[Wardrobe] 选择备份导出失败: %s", e, exc_info=True)
                return jsonify({"error": f"导出失败: {e}"}), 500

        @app.route("/api/images/upload", methods=["POST"])
        async def api_image_upload():
            try:
                await self.plugin._ensure_db()
                files = await request.files
                file = files.get("image")
                if not file:
                    return jsonify({"error": "未选择图片"}), 400

                image_bytes = file.read()
                if not image_bytes:
                    return jsonify({"error": "图片为空"}), 400

                form = await request.form
                persona = form.get("persona", "")
                persona = self.plugin._resolve_persona(persona)
                description = form.get("description", "")

                max_size = int(self.plugin._cfg("max_image_size_mb", 10) or 10)
                if len(image_bytes) > max_size * 1024 * 1024:
                    return jsonify({"error": f"图片过大，限制{max_size}MB"}), 400

                logger.debug("[Wardrobe] WebUI上传图片: 大小=%.2fKB 人格=%s 描述=%s", len(image_bytes) / 1024, persona or "无", description or "无")
                image_id, attrs, duplicate = await self.plugin._save_image_from_bytes(
                    image_bytes, persona=persona, created_by="webui", user_description=description
                )

                if duplicate:
                    logger.debug("[Wardrobe] WebUI上传跳过: 图片重复 existing_id=%s", duplicate.get("id", ""))
                    return jsonify({"duplicate": True, "existing_id": duplicate.get("id", ""), "existing_persona": duplicate.get("persona", "")})

                if not image_id:
                    logger.warning("[Wardrobe] 上传保存失败: id缺失")
                    return jsonify({"error": "保存失败，请检查存图模型是否已配置"}), 500

                logger.debug("[Wardrobe] 上传完成: id=%s 分类=%s",
                            image_id,
                            attrs.get("category", "") if attrs else "")

                return jsonify({"success": True, "image_id": image_id})
            except Exception as e:
                logger.error("[Wardrobe] WebUI上传异常: %s", e, exc_info=True)
                return jsonify({"error": f"服务器内部错误: {e}"}), 500

        @app.route("/api/images/<image_id>/toggle-style", methods=["POST"])
        async def api_images_toggle_style(image_id):
            await self.plugin._ensure_db()
            data = await request.get_json(silent=True) or {}
            style_name = (data.get("style_name", "") or "").strip()
            if not style_name:
                return jsonify({"error": "未指定风格"}), 400
            img = await self.plugin.db.get_image(image_id)
            if not img:
                return jsonify({"error": "图片不存在"}), 404
            current = img.get("style", []) or []
            if isinstance(current, str):
                current = [current]
            if style_name in current:
                current.remove(style_name)
                added = False
            else:
                current.append(style_name)
                added = True
            ok = await self.plugin.db.update_image(image_id, style=current)
            if not ok:
                return jsonify({"error": "更新失败"}), 500
            return jsonify({"success": True, "style": current, "added": added})

        @app.route("/api/images/<image_id>/favorite", methods=["PATCH"])
        async def api_image_favorite(image_id):
            await self.plugin._ensure_db()
            image = await self.plugin.db.get_image(image_id)
            if not image:
                return jsonify({"error": "未找到图片"}), 404

            data = await request.get_json(silent=True) or {}
            fav = data.get("favorite", "none")
            if fav not in ("favorite", "like", "meh", "none"):
                return jsonify({"error": "无效的收藏值，应为 favorite/like/meh/none"}), 400

            await self.plugin.db.update_image(image_id, favorite=fav)
            return jsonify({"success": True, "favorite": fav})

        @app.route("/api/search")
        async def api_search():
            await self.plugin._ensure_db()
            query = request.args.get("q", "").strip()
            persona = request.args.get("persona", "")
            if persona == "__none__":
                persona = ""
            else:
                persona = persona or None
            category = request.args.get("category", "")
            favorite = request.args.get("favorite", "")
            limit = min(100, max(1, int(request.args.get("limit", 50))))

            if not query:
                return jsonify({"images": []})

            exclude_persona = request.args.get("exclude_persona", "")

            vec_results = []
            if self.plugin.vector_searcher and self.plugin.vector_searcher.available:
                try:
                    wardrobe_results = await self.plugin.vector_searcher.search(
                        query=query, k=limit, persona=persona,
                        exclude_persona=exclude_persona or "",
                    )
                    if wardrobe_results:
                        for wid, similarity in wardrobe_results:
                            img = await self.plugin.db.get_image(wid)
                            if img:
                                if category and img.get("category") != category:
                                    continue
                                if favorite in ("favorite", "like", "meh") and img.get("favorite") != favorite:
                                    continue
                                img["_similarity"] = similarity
                                vec_results.append(img)
                        logger.debug("[Wardrobe] WebUI搜索向量检索命中: %d张 query=%s", len(vec_results), query[:50])
                except Exception as e:
                    logger.warning("[Wardrobe] WebUI搜索向量检索失败: %s", e)

            if vec_results:
                return jsonify({"images": vec_results})

            keywords = [k.strip() for k in query.replace("，", ",").split(",") if k.strip()]
            results = await self.plugin.db.search_by_description(
                keywords=keywords,
                category=category or None,
                persona=persona,
                exclude_persona=exclude_persona or None,
                limit=limit,
            )
            logger.debug("[Wardrobe] WebUI搜索LIKE检索: %d张 query=%s", len(results), query[:50])
            return jsonify({"images": results})

        @app.route("/api/filters")
        async def api_filters():
            await self.plugin._ensure_db()
            try:
                stats = await self.plugin.db.get_stats()
            except Exception:
                stats = {"total": 0, "by_category": {}, "by_exposure": {}}
            personas = self.plugin._get_personas_config()
            persona_names = [p.get("name", "") for p in personas if p.get("name")]

            try:
                pools = await self.plugin.get_merged_pools()
            except Exception as e:
                logger.warning("[Wardrobe] /api/filters get_merged_pools 失败: %s", e)
                pools = {}

            return jsonify({
                "categories": list(stats.get("by_category", {}).keys()),
                "personas": persona_names,
                "pools": {k: list(v) for k, v in pools.items()},
            })

        @app.route("/api/image-file/<image_id>")
        async def api_image_file(image_id):
            await self.plugin._ensure_db()
            image = await self.plugin.db.get_image(image_id)
            if not image:
                return jsonify({"error": "未找到图片"}), 404
            image_path = self.plugin.store.get_image_path(image["image_path"])
            if not image_path.exists():
                return jsonify({"error": "文件不存在"}), 404
            resp = await send_from_directory(str(image_path.parent), image_path.name)
            resp.headers['Cache-Control'] = 'public, max-age=604800'
            resp.headers['ETag'] = f'"{image_id}"'
            return resp

        @app.route("/api/image-file/<image_id>/thumbnail")
        async def api_image_file_thumbnail(image_id):
            await self.plugin._ensure_db()
            image = await self.plugin.db.get_image(image_id)
            if not image:
                return jsonify({"error": "未找到图片"}), 404
            thumb_path = await self.plugin.store.ensure_thumbnail(image["image_path"])
            if not thumb_path.exists():
                return jsonify({"error": "文件不存在"}), 404
            resp = await send_from_directory(str(thumb_path.parent), thumb_path.name)
            resp.headers['Cache-Control'] = 'public, max-age=604800'
            resp.headers['ETag'] = f'"thumb-{image_id}"'
            return resp

        @app.route("/api/pools", methods=["GET"])
        async def api_get_pools():
            try:
                pools = await self.plugin.get_merged_pools()
            except Exception as e:
                logger.warning("[Wardrobe] /api/pools get_merged_pools 失败: %s", e)
                pools = {}
            return jsonify({"pools": {k: list(v) for k, v in pools.items()}})

        @app.route("/api/pools", methods=["POST"])
        async def api_update_pool():
            data = await request.get_json(silent=True) or {}
            pool_key = data.get("key", "").strip()
            action = data.get("action", "")
            value = data.get("value", "").strip()

            if not pool_key or not action:
                return jsonify({"error": "参数不完整"}), 400

            pools = await self.plugin.get_merged_pools()

            if action == "add_value":
                if not value:
                    return jsonify({"error": "值不能为空"}), 400
                if pool_key not in pools:
                    pools[pool_key] = []
                if value not in pools[pool_key]:
                    pools[pool_key].append(value)
            elif action == "remove_value":
                if pool_key in pools and value in pools[pool_key]:
                    pools[pool_key].remove(value)
            elif action == "add_pool":
                if not value:
                    return jsonify({"error": "池名不能为空"}), 400
                if value not in pools:
                    pools[value] = []
                pool_key = value
            elif action == "remove_pool":
                if pool_key in pools:
                    del pools[pool_key]
            else:
                return jsonify({"error": "未知操作"}), 400

            await self.plugin.save_custom_pools(pools)
            return jsonify({"success": True, "pools": {k: list(v) for k, v in pools.items()}})

        # ---------- 人格级风格池（供 aiimg 补拍使用） ----------

        @app.route("/api/persona-style-pools", methods=["GET"])
        async def api_get_persona_style_pools():
            personas = self.plugin._get_personas_config()
            persona_names = [p.get("name", "") for p in personas if p.get("name")]
            pools = await self.plugin._load_persona_style_pools()
            try:
                global_pools = await self.plugin.get_merged_pools()
                global_style_pool = list(global_pools.get("style", []))
            except Exception:
                global_style_pool = []
            return jsonify({
                "personas": persona_names,
                "pools": {k: list(v) for k, v in pools.items()},
                "global_style_pool": global_style_pool,
            })

        @app.route("/api/persona-style-pools", methods=["POST"])
        async def api_save_persona_style_pool():
            data = await request.get_json(silent=True) or {}
            persona = str(data.get("persona", "") or "").strip()
            action = str(data.get("action", "") or "").strip()
            if not persona:
                return jsonify({"error": "人格名不能为空"}), 400

            if action == "delete":
                await self.plugin.delete_persona_style_pool(persona)
                return jsonify({"success": True})

            styles = data.get("styles", [])
            if not isinstance(styles, list):
                return jsonify({"error": "styles 必须是列表"}), 400
            await self.plugin.save_persona_style_pool(persona, styles)
            return jsonify({"success": True, "persona": persona, "styles": [str(s) for s in styles]})

        @app.route("/api/backup/export")
        async def api_backup_export():
            try:
                include_videos = request.args.get("include_videos", "") == "1"
                file_path, total_records, added_files = await self.plugin.build_backup_zip(include_videos=include_videos)
                logger.debug("[Wardrobe] 备份导出: %d条记录, %d个图片文件, 包含视频=%s", total_records, added_files, include_videos)

                return await send_file(
                    str(file_path),
                    mimetype="application/zip",
                    as_attachment=True,
                    attachment_filename=f"wardrobe_backup_{time.strftime('%Y%m%d_%H%M%S')}.zip",
                )
            except Exception as e:
                logger.error("[Wardrobe] 备份导出失败: %s", e, exc_info=True)
                return jsonify({"error": f"导出失败: {e}"}), 500

        @app.route("/api/backup/import", methods=["POST"])
        async def api_backup_import():
            tmp_dirs = []
            try:
                await self.plugin._ensure_db()
                files = await request.files
                uploaded_files = files.getlist("backup")
                if not uploaded_files:
                    return jsonify({"error": "未选择备份文件"}), 400

                file_info = []
                for file in uploaded_files:
                    file_bytes = file.read()
                    if not file_bytes:
                        continue
                    tmp_dir = tempfile.mkdtemp(prefix="wardrobe_restore_")
                    tmp_dirs.append(tmp_dir)
                    zip_path = Path(tmp_dir) / "backup.zip"
                    async with aiofiles_open(zip_path, "wb") as f:
                        await f.write(file_bytes)

                    def _extract_metadata(zp):
                        with zipfile.ZipFile(str(zp), "r") as zf:
                            if "backup_metadata.json" in zf.namelist():
                                return json.loads(zf.read("backup_metadata.json"))
                        return {}

                    meta = await asyncio.to_thread(_extract_metadata, zip_path)
                    backup_type = meta.get("type", "full")
                    export_time = meta.get("export_time", "")
                    file_info.append({
                        "tmp_dir": tmp_dir,
                        "zip_path": zip_path,
                        "type": backup_type,
                        "export_time": export_time,
                    })

                if not file_info:
                    return jsonify({"error": "所有备份文件为空"}), 400

                file_info.sort(key=lambda x: (0 if x["type"] == "full" else 1, x["export_time"]))

                total_imported = 0
                total_copied_files = 0
                total_imported_videos = 0
                total_copied_videos = 0
                total_in_backup = 0

                for info in file_info:
                    tmp_dir = info["tmp_dir"]
                    zip_path = info["zip_path"]

                    def _extract(zp, td):
                        with zipfile.ZipFile(str(zp), "r") as zf:
                            zf.extractall(td)

                    await asyncio.to_thread(_extract, zip_path, tmp_dir)

                    records_path = Path(tmp_dir) / "records.json"
                    if not records_path.exists():
                        continue

                    def _read_records(rp):
                        with open(str(rp), "r", encoding="utf-8") as f:
                            return json.load(f)

                    records = await asyncio.to_thread(_read_records, records_path)
                    if not isinstance(records, list):
                        continue

                    total_in_backup += len(records)

                    images_src = Path(tmp_dir) / "images"
                    images_dst = self.plugin.store.images_dir
                    copied_files = 0

                    if images_src.exists():
                        def _copy_images(src, dst, recs):
                            nonlocal copied_files
                            for rec in recs:
                                img_filename = rec.get("image_path", "")
                                if not img_filename:
                                    continue
                                s = src / img_filename
                                d = dst / img_filename
                                if s.exists() and not d.exists():
                                    shutil.copy2(str(s), str(d))
                                    copied_files += 1

                        await asyncio.to_thread(_copy_images, images_src, images_dst, records)

                    total_copied_files += copied_files
                    imported = await self.plugin.db.import_records(records, skip_existing=True)
                    total_imported += imported

                    videos_path = Path(tmp_dir) / "videos.json"
                    if videos_path.exists():
                        try:
                            def _read_videos(vp):
                                with open(str(vp), "r", encoding="utf-8") as f:
                                    return json.load(f)

                            video_records = await asyncio.to_thread(_read_videos, videos_path)
                            if isinstance(video_records, list):
                                videos_src = Path(tmp_dir) / "videos"
                                self.plugin.video_service._ensure_dirs()
                                videos_dst = self.plugin.video_service.videos_dir
                                copied_videos = 0
                                if videos_src.exists():
                                    def _copy_videos(src, dst, vrecs):
                                        nonlocal copied_videos
                                        for vrec in vrecs:
                                            vfilename = vrec.get("video_path", "")
                                            if not vfilename:
                                                continue
                                            s = src / vfilename
                                            d = dst / vfilename
                                            if s.exists() and not d.exists():
                                                shutil.copy2(str(s), str(d))
                                                copied_videos += 1
                                    await asyncio.to_thread(_copy_videos, videos_src, videos_dst, video_records)
                                total_copied_videos += copied_videos
                                imported_videos = await self.plugin.db.import_video_records(video_records, skip_existing=True)
                                total_imported_videos += imported_videos
                        except Exception as e:
                            logger.warning("[Wardrobe] 备份恢复视频数据失败: %s", e)

                    # 恢复按人格热度记录（image_usage 表）
                    image_usage_path = Path(tmp_dir) / "image_usage.json"
                    if image_usage_path.exists():
                        try:
                            def _read_image_usage(iup):
                                with open(str(iup), "r", encoding="utf-8") as f:
                                    return json.load(f)
                            usage_records = await asyncio.to_thread(_read_image_usage, image_usage_path)
                            if isinstance(usage_records, list):
                                imported_usage = await self.plugin.db.import_image_usage_records(
                                    usage_records, skip_existing=True
                                )
                                logger.debug("[Wardrobe] 备份恢复热度记录: %d 条", imported_usage)
                        except Exception as e:
                            logger.warning("[Wardrobe] 备份恢复热度数据失败: %s", e)

                    video_settings_path = Path(tmp_dir) / "video_settings.json"
                    if video_settings_path.exists():
                        try:
                            def _read_video_settings(vsp):
                                with open(str(vsp), "r", encoding="utf-8") as f:
                                    return json.load(f)
                            vsettings = await asyncio.to_thread(_read_video_settings, video_settings_path)
                            if isinstance(vsettings, dict):
                                if "video_send_umo" in vsettings:
                                    try:
                                        data = json.loads(vsettings["video_send_umo"])
                                        umo = data.get("umo", "")
                                        auto_send = data.get("auto_send", False)
                                        await self.plugin.video_service.save_send_umo(umo, auto_send)
                                    except Exception:
                                        pass
                                if "video_system_prompt" in vsettings:
                                    try:
                                        await self.plugin.video_service.save_system_prompt(vsettings["video_system_prompt"])
                                    except Exception:
                                        pass
                        except Exception as e:
                            logger.warning("[Wardrobe] 备份恢复视频设置失败: %s", e)

                if total_imported > 0 and self.plugin.vector_searcher and self.plugin.vector_searcher.available:
                    try:
                        await self.plugin.vector_searcher.index_existing_images()
                        logger.debug("[Wardrobe] 备份恢复后向量索引重建完成")
                    except Exception as e:
                        logger.warning("[Wardrobe] 备份恢复后向量索引重建失败: %s", e)

                logger.info("[Wardrobe] 备份恢复: 导入%d条图片记录, 复制%d个图片文件, 导入%d条视频记录, 复制%d个视频文件", total_imported, total_copied_files, total_imported_videos, total_copied_videos)
                return jsonify({
                    "success": True,
                    "imported": total_imported,
                    "copied_files": total_copied_files,
                    "total_in_backup": total_in_backup,
                    "imported_videos": total_imported_videos,
                    "copied_videos": total_copied_videos,
                })
            except Exception as e:
                logger.error("[Wardrobe] 备份恢复失败: %s", e, exc_info=True)
                return jsonify({"error": f"恢复失败: {e}"}), 500
            finally:
                for tmp_dir in tmp_dirs:
                    await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)

        @app.route("/api/videos")
        async def api_videos():
            await self.plugin._ensure_db()
            page = max(1, int(request.args.get("page", 1)))
            per_page = min(100, max(1, int(request.args.get("per_page", 50))))
            persona = request.args.get("persona", "")
            if persona == "__none__":
                persona = ""
            else:
                persona = persona or None
            tier = request.args.get("tier", "")
            status = request.args.get("status", "")
            source_image_id = request.args.get("source_image_id", "")
            offset = (page - 1) * per_page

            t0 = time.perf_counter()

            videos = await self.plugin.db.list_videos_lightweight(
                persona=persona,
                tier=tier or None,
                status=status or None,
                source_image_id=source_image_id or None,
                limit=per_page,
                offset=offset,
            )
            for v in videos:
                sid = str(v.get("source_image_id", ""))
                v["source_thumbnail"] = f"/api/image-file/{sid}/thumbnail" if sid else None

            total = await self.plugin.db.count_videos(
                persona=persona,
                tier=tier or None,
                status=status or None,
                source_image_id=source_image_id or None,
            )

            elapsed = time.perf_counter() - t0
            if elapsed > 1.0:
                logger.debug("[Wardrobe] /api/videos 耗时 %.2fs (videos=%d total=%d filters: persona=%s tier=%s status=%s)",
                             elapsed, len(videos), total, persona, tier or "-", status or "-")

            return jsonify({"videos": videos, "total": total, "page": page, "per_page": per_page})

        @app.route("/api/videos/generate", methods=["POST"])
        async def api_video_generate():
            await self.plugin._ensure_db()
            data = await request.get_json(silent=True) or {}
            image_id = (data.get("image_id") or "").strip()
            tier = (data.get("tier") or "normal").strip()
            user_thoughts = (data.get("user_thoughts") or "").strip()
            backend_override = (data.get("backend_override") or "").strip()
            auto_send = bool(data.get("auto_send", False))

            if not image_id:
                return jsonify({"error": "未指定图片"}), 400
            if tier not in ("normal", "light_spicy", "heavy_spicy"):
                return jsonify({"error": "无效的档位"}), 400

            video_enabled = bool(self.plugin._cfg("video_enabled", False))
            if not video_enabled:
                return jsonify({"error": "图片转视频功能未启用"}), 400

            prompt_provider_id = str(self.plugin._cfg("video_prompt_provider_id", "") or "").strip()
            api_base = str(self.plugin._cfg("video_prompt_base_url", "") or "").strip()
            api_key = str(self.plugin._cfg("video_prompt_api_key", "") or "").strip()
            api_model = str(self.plugin._cfg("video_prompt_model", "") or "").strip()
            has_prompt_config = bool(prompt_provider_id) or (api_base and api_key and api_model)
            if not has_prompt_config:
                return jsonify({"error": "未配置视频提示词生成模型，请在插件设置中配置"}), 400

            try:
                video_id = await self.plugin.video_service.generate_video(
                    image_id=image_id,
                    tier=tier,
                    user_thoughts=user_thoughts,
                    backend_override=backend_override,
                    auto_send=auto_send,
                )
                logger.debug("[Wardrobe] WebUI 触发视频生成: image_id=%s tier=%s video_id=%s auto_send=%s", image_id, tier, video_id, auto_send)
                return jsonify({"success": True, "video_id": video_id, "status": "generating"})
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            except Exception as e:
                logger.error("[Wardrobe] 视频生成触发失败: %s", e, exc_info=True)
                return jsonify({"error": f"生成失败: {e}"}), 500

        @app.route("/api/videos/<video_id>")
        async def api_video_detail(video_id):
            await self.plugin._ensure_db()
            video = await self.plugin.db.get_video(video_id)
            if not video:
                return jsonify({"error": "未找到视频"}), 404
            sid = str(video.get("source_image_id", ""))
            video["source_thumbnail"] = f"/api/image-file/{sid}/thumbnail" if sid else None
            return jsonify({"video": video})

        @app.route("/api/videos/<video_id>", methods=["DELETE"])
        async def api_video_delete(video_id):
            await self.plugin._ensure_db()
            self.plugin.video_service._ensure_dirs()
            video = await self.plugin.db.get_video(video_id)
            if not video:
                return jsonify({"error": "未找到视频"}), 404

            deleted = await self.plugin.db.delete_video(video_id)
            if deleted and video.get("video_path"):
                video_file = self.plugin.video_service.videos_dir / video["video_path"]
                try:
                    if video_file.exists():
                        video_file.unlink()
                except Exception:
                    pass
            return jsonify({"success": bool(deleted)})

        @app.route("/api/videos/<video_id>/retry", methods=["POST"])
        async def api_video_retry(video_id):
            await self.plugin._ensure_db()
            self.plugin.video_service._ensure_dirs()
            video = await self.plugin.db.get_video(video_id)
            if not video:
                return jsonify({"error": "未找到视频"}), 404
            if video.get("status") not in ("failed",):
                if video.get("status") == "generating":
                    return jsonify({"error": "视频正在生成中，请勿重复重试"}), 409
                return jsonify({"error": "只有失败状态的视频可以重试"}), 400
            source_image_id = video.get("source_image_id")
            if not source_image_id:
                return jsonify({"error": "视频缺少源图片"}), 400
            image = await self.plugin.db.get_image(source_image_id)
            if not image:
                return jsonify({"error": "源图片不存在"}), 404
            image_path = self.plugin.store.get_image_path(image["image_path"])
            if not image_path or not image_path.exists():
                return jsonify({"error": "源图片文件不存在"}), 404
            tier = video.get("tier", "normal")
            tier_label = {"normal": "正常", "light_spicy": "轻荤", "heavy_spicy": "重荤"}.get(tier, tier)
            user_thoughts = video.get("user_thoughts", "")
            backend_override = video.get("provider_id", "")
            persona = video.get("persona", "")
            await self.plugin.db.update_video(video_id, status="generating", error_message="")
            image_description = self.plugin.video_service._build_image_description(image)
            old_prompt = (video.get("generated_prompt") or "").strip()
            umo_config = await self.plugin.video_service.load_send_umo()
            auto_send = bool(umo_config.get("auto_send", False)) and bool(umo_config.get("umo", "").strip())
            self.plugin._spawn_bg_task(
                self.plugin.video_service._process_video(
                    video_id, source_image_id, image_path, tier, tier_label,
                    user_thoughts, backend_override, persona, image_description,
                    reuse_prompt=old_prompt,
                    auto_send=auto_send,
                )
            )
            logger.debug("[Wardrobe] WebUI 重试视频生成: video_id=%s tier=%s", video_id, tier)
            return jsonify({"success": True, "video_id": video_id})

        @app.route("/api/videos/<video_id>/file")
        async def api_video_file(video_id):
            await self.plugin._ensure_db()
            self.plugin.video_service._ensure_dirs()
            video = await self.plugin.db.get_video(video_id)
            if not video:
                return jsonify({"error": "未找到视频"}), 404

            video_path_str = video.get("video_path", "")
            if not video_path_str:
                return jsonify({"error": "视频文件路径为空"}), 404

            video_file = self.plugin.video_service.videos_dir / video_path_str
            if not video_file.exists():
                return jsonify({"error": "视频文件不存在"}), 404

            resp = await send_file(str(video_file), mimetype='video/mp4')
            resp.headers['Cache-Control'] = 'public, max-age=604800'
            resp.headers['ETag'] = f'"{video_id}"'
            return resp

        @app.route("/api/video-settings/prompt")
        async def api_video_prompt_get():
            try:
                text = await self.plugin.video_service.load_system_prompt()
                return jsonify({"prompt": text})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @app.route("/api/video-settings/prompt", methods=["POST"])
        async def api_video_prompt_save():
            data = await request.get_json(silent=True) or {}
            text = data.get("prompt", "")
            try:
                await self.plugin.video_service.save_system_prompt(text)
                logger.debug("[Wardrobe] 视频系统提示词已保存 len=%d", len(text))
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @app.route("/api/video-settings/umo")
        async def api_video_umo_get():
            try:
                config = await self.plugin.video_service.load_send_umo()
                return jsonify({"umo": config.get("umo", ""), "auto_send": config.get("auto_send", False)})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @app.route("/api/video-settings/umo", methods=["POST"])
        async def api_video_umo_save():
            data = await request.get_json(silent=True) or {}
            umo = str(data.get("umo", "") or "").strip()
            auto_send = bool(data.get("auto_send", False))
            try:
                await self.plugin.video_service.save_send_umo(umo, auto_send)
                logger.debug("[Wardrobe] 视频发送会话已保存 umo=%s auto_send=%s", umo[:30] if umo else "(空)", auto_send)
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @app.route("/api/videos/<video_id>/send", methods=["POST"])
        async def api_video_send(video_id):
            try:
                result = await self.plugin.video_service.send_video_by_id(video_id)
                return jsonify({
                    "success": result.success,
                    "terminated": result.terminated,
                    "message": result.message,
                })
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            except Exception as e:
                logger.error("[Wardrobe] 视频发送失败: %s", e, exc_info=True)
                return jsonify({"error": f"发送失败: {e}"}), 500

        return app

    async def start(self):
        if self._server_task and not self._server_task.done():
            logger.warning("[Wardrobe] WebUI 已在运行")
            return

        app = self._create_app()
        config = uvicorn.Config(
            app=app,
            host=self.host,
            port=self.port,
            log_level="warning",
            loop="asyncio",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())

        for _ in range(50):
            if getattr(self._server, "started", False):
                logger.info("[Wardrobe] WebUI 已启动: http://%s:%d", self.host, self.port)
                if self.password == "wardrobe":
                    logger.warning("[Wardrobe] WebUI 使用默认密码 'wardrobe'，请及时修改！")
                return
            if self._server_task.done():
                error = self._server_task.exception()
                logger.error("[Wardrobe] WebUI 启动失败: %s", error)
                self._server = None
                self._server_task = None
                return
            await asyncio.sleep(0.1)

        logger.warning("[Wardrobe] WebUI 启动耗时较长，仍在后台继续启动")

    async def stop(self):
        if self._server:
            self._server.should_exit = True
        if self._server_task:
            try:
                await self._server_task
            except Exception:
                pass
        self._server = None
        self._server_task = None
        self._tokens.clear()
        logger.info("[Wardrobe] WebUI 已停止")


def aiofiles_open(path, mode="r"):
    import aiofiles
    return aiofiles.open(path, mode)
