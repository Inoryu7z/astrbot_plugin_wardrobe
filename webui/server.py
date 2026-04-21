import asyncio
import io
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
    jsonify,
    redirect,
    request,
    send_file,
    send_from_directory,
    session,
)

from astrbot.api import logger

_TOKEN_TTL = 86400 * 7


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
        app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024
        app.config["BODY_TIMEOUT"] = 600

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

        @app.route("/api/images")
        async def api_images():
            await self.plugin._ensure_db()
            page = max(1, int(request.args.get("page", 1)))
            per_page = min(100, max(1, int(request.args.get("per_page", 24))))
            category = request.args.get("category", "")
            persona = request.args.get("persona", "")
            style = request.args.get("style", "")
            scene = request.args.get("scene", "")
            shot_size = request.args.get("shot_size", "")
            atmosphere = request.args.get("atmosphere", "")
            favorite = request.args.get("favorite", "")
            sort_by = request.args.get("sort_by", "created_at")
            lightweight = request.args.get("lightweight", "") == "1"

            offset = (page - 1) * per_page

            needs_search = style or scene or atmosphere or shot_size or persona or category or (favorite in ("favorite", "like"))
            if needs_search:
                style_list = [style] if style else None
                scene_list = [scene] if scene else None
                atmosphere_list = [atmosphere] if atmosphere else None
                images = await self.plugin.db.search_images(
                    category=category or None,
                    persona=persona or None,
                    style=style_list,
                    scene=scene_list,
                    atmosphere=atmosphere_list,
                    shot_size=shot_size or None,
                    favorite=favorite if favorite in ("favorite", "like") else None,
                    sort_by=sort_by,
                    limit=per_page,
                    offset=offset,
                )
                total = await self.plugin.db.search_count(
                    category=category or None,
                    persona=persona or None,
                    style=style_list,
                    scene=scene_list,
                    atmosphere=atmosphere_list,
                    shot_size=shot_size or None,
                    favorite=favorite if favorite in ("favorite", "like") else None,
                )
            elif lightweight:
                images = await self.plugin.db.list_images_lightweight(
                    category=category or None,
                    shot_size=shot_size or None,
                    persona=persona or None,
                    favorite=favorite if favorite in ("favorite", "like") else None,
                    sort_by=sort_by,
                    limit=per_page,
                    offset=offset,
                )
                total = await self.plugin.db.search_count(
                    category=category or None,
                    shot_size=shot_size or None,
                    persona=persona or None,
                    favorite=favorite if favorite in ("favorite", "like") else None,
                )
            else:
                images = await self.plugin.db.list_images(
                    category=category or None, shot_size=shot_size or None,
                    favorite=favorite if favorite in ("favorite", "like") else None,
                    sort_by=sort_by,
                    limit=per_page, offset=offset
                )
                total = await self.plugin.db.search_count(
                    category=category or None,
                    shot_size=shot_size or None,
                    favorite=favorite if favorite in ("favorite", "like") else None,
                )

            result = {
                "images": images,
                "total": total,
                "page": page,
                "per_page": per_page,
            }
            return jsonify(result)

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
                             "description", "user_tags", "persona", "favorite"):
                    update_data[key] = str(val) if val is not None else ""

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
                            category=updated_image.get("category", ""),
                            persona=updated_image.get("persona", ""),
                        )
                except Exception as e:
                    logger.warning("[Wardrobe] 编辑后向量索引重建失败: %s", e)

            return jsonify({"success": True})

        @app.route("/api/images/<image_id>/reanalyze", methods=["POST"])
        async def api_image_reanalyze(image_id):
            await self.plugin._ensure_db()
            logger.info("[Wardrobe] WebUI重新分析请求: id=%s", image_id)
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
                )

                if not attrs:
                    logger.warning("[Wardrobe] WebUI重新分析失败: 模型返回空结果 id=%s", image_id)
                    return jsonify({"error": "模型分析失败"}), 500

                def _ensure_list(v):
                    if isinstance(v, list):
                        return v
                    if isinstance(v, str) and v:
                        return [v]
                    return []

                def _ensure_str(v):
                    if v is None:
                        return ""
                    return str(v)

                update_data = {}
                for field in ("exposure_features", "key_features", "prop_objects", "allure_features", "body_focus"):
                    update_data[field] = _ensure_list(attrs.get(field))

                for field in ("style", "scene", "atmosphere", "action_style",
                              "clothing_type", "exposure_level", "pose_type",
                              "body_orientation", "dynamic_level", "shot_size",
                              "camera_angle", "expression", "color_tone",
                              "composition", "background", "description", "category"):
                    val = attrs.get(field)
                    if val is not None:
                        if isinstance(val, list):
                            update_data[field] = val
                        else:
                            update_data[field] = str(val)

                if user_description:
                    update_data["user_tags"] = user_description

                await self.plugin.db.update_image(image_id, **update_data)
                logger.info("[Wardrobe] WebUI重新分析完成: id=%s category=%s exposure=%s description=%s",
                            image_id, attrs.get("category", ""), attrs.get("exposure_level", ""),
                            attrs.get("description", ""))

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
            return jsonify({"success": True, "deleted": deleted_count})

        @app.route("/api/images/batch-reanalyze", methods=["POST"])
        async def api_images_batch_reanalyze():
            try:
                await self.plugin._ensure_db()
                data = await request.get_json(silent=True) or {}
                ids = data.get("ids", [])
                if not ids:
                    return jsonify({"error": "未指定图片"}), 400

                logger.info("[Wardrobe] WebUI批量重新分析请求: %d张图片", len(ids))

                primary = str(self.plugin._cfg("save_provider_id", "") or "").strip()
                secondary = str(self.plugin._cfg("save_secondary_provider_id", "") or "").strip()
                timeout = float(self.plugin._cfg("save_timeout_seconds", 60.0) or 60.0)

                if not primary and not secondary:
                    return jsonify({"error": "未配置存图模型"}), 400

                success = 0
                failed = 0
                for image_id in ids:
                    image = await self.plugin.db.get_image(image_id)
                    if not image:
                        failed += 1
                        continue

                    image_path_str = image.get("image_path", "")
                    if not image_path_str:
                        failed += 1
                        continue

                    path = self.plugin.store.get_image_path(image_path_str)
                    if not path.exists():
                        failed += 1
                        continue

                    try:
                        import aiofiles
                        async with aiofiles.open(path, "rb") as f:
                            image_bytes = await f.read()

                        if not image_bytes:
                            failed += 1
                            continue

                        attrs = await self.plugin.analyzer.analyze_image(
                            image_bytes,
                            user_description="",
                            primary_provider_id=primary,
                            secondary_provider_id=secondary,
                            timeout_seconds=timeout,
                        )

                        if not attrs:
                            logger.warning("[Wardrobe] 批量重新分析: 模型返回空结果 id=%s", image_id)
                            failed += 1
                            continue

                        def _ensure_list(v):
                            if isinstance(v, list):
                                return v
                            if isinstance(v, str) and v:
                                return [v]
                            return []

                        update_data = {}
                        for field in ("exposure_features", "key_features", "prop_objects", "allure_features", "body_focus"):
                            update_data[field] = _ensure_list(attrs.get(field))

                        for field in ("style", "scene", "atmosphere", "action_style",
                                      "clothing_type", "exposure_level", "pose_type",
                                      "body_orientation", "dynamic_level", "shot_size",
                                      "camera_angle", "expression", "color_tone",
                                      "composition", "background", "description", "category"):
                            val = attrs.get(field)
                            if val is not None:
                                if isinstance(val, list):
                                    update_data[field] = val
                                else:
                                    update_data[field] = str(val)

                        await self.plugin.db.update_image(image_id, **update_data)
                        logger.info("[Wardrobe] 批量重新分析完成: id=%s category=%s",
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
                                logger.warning("[Wardrobe] 批量重新分析后向量索引重建失败: %s", e)

                        success += 1
                    except Exception as e:
                        logger.warning("[Wardrobe] 批量重新分析失败 id=%s error=%s", image_id, e)
                        failed += 1

                logger.info("[Wardrobe] 批量重新分析完成: 成功%d 失败%d 共%d张", success, failed, len(ids))
                return jsonify({"success": True, "reanalyzed": success, "failed": failed})
            except Exception as e:
                logger.error("[Wardrobe] 批量重新分析异常: %s", e, exc_info=True)
                return jsonify({"error": f"批量重新分析失败: {e}"}), 500

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

                logger.info("[Wardrobe] WebUI上传图片: 大小=%.2fKB 人格=%s 描述=%s", len(image_bytes) / 1024, persona or "无", description or "无")
                image_id, attrs, duplicate = await self.plugin._save_image_from_bytes(
                    image_bytes, persona=persona, created_by="webui", user_description=description
                )

                if duplicate:
                    logger.info("[Wardrobe] WebUI上传跳过: 图片重复 existing_id=%s", duplicate.get("id", ""))
                    return jsonify({"duplicate": True, "existing_id": duplicate.get("id", ""), "existing_persona": duplicate.get("persona", "")})

                if not image_id:
                    logger.warning("[Wardrobe] WebUI上传失败: 保存失败")
                    return jsonify({"error": "保存失败，请检查存图模型是否已配置"}), 500

                logger.info("[Wardrobe] WebUI上传完成: id=%s category=%s exposure=%s description=%s",
                            image_id,
                            attrs.get("category", "") if attrs else "",
                            attrs.get("exposure_level", "") if attrs else "",
                            attrs.get("description", "") if attrs else "")

                return jsonify({"success": True, "image_id": image_id})
            except Exception as e:
                logger.error("[Wardrobe] WebUI上传异常: %s", e, exc_info=True)
                return jsonify({"error": f"服务器内部错误: {e}"}), 500

        @app.route("/api/images/batch-upload", methods=["POST"])
        async def api_images_batch_upload():
            try:
                await self.plugin._ensure_db()
                files = await request.files
                file_list = files.getlist("images")
                if not file_list:
                    return jsonify({"error": "未选择图片"}), 400

                form = await request.form
                persona = form.get("persona", "")
                persona = self.plugin._resolve_persona(persona)
                description = form.get("description", "")

                max_size = int(self.plugin._cfg("max_image_size_mb", 10) or 10)
                results = []
                errors = []

                for i, file in enumerate(file_list):
                    if not file or not file.filename:
                        continue
                    image_bytes = file.read()
                    if not image_bytes:
                        errors.append({"index": i, "name": file.filename, "error": "图片为空"})
                        continue
                    if len(image_bytes) > max_size * 1024 * 1024:
                        errors.append({"index": i, "name": file.filename, "error": f"图片过大，限制{max_size}MB"})
                        continue

                    try:
                        image_id, attrs, duplicate = await self.plugin._save_image_from_bytes(
                            image_bytes, persona=persona, created_by="webui", user_description=description
                        )
                        if duplicate:
                            errors.append({"index": i, "name": file.filename, "error": "图片重复", "duplicate_id": duplicate.get("id", "")})
                        elif image_id:
                            results.append({"index": i, "name": file.filename, "image_id": image_id})
                        else:
                            errors.append({"index": i, "name": file.filename, "error": "保存失败"})
                    except Exception as e:
                        errors.append({"index": i, "name": file.filename, "error": str(e)})

                return jsonify({
                    "success": True,
                    "uploaded": len(results),
                    "failed": len(errors),
                    "results": results,
                    "errors": errors,
                })
            except Exception as e:
                logger.error("[Wardrobe] WebUI批量上传异常: %s", e, exc_info=True)
                return jsonify({"error": f"服务器内部错误: {e}"}), 500

        @app.route("/api/images/<image_id>/favorite", methods=["PATCH"])
        async def api_image_favorite(image_id):
            await self.plugin._ensure_db()
            image = await self.plugin.db.get_image(image_id)
            if not image:
                return jsonify({"error": "未找到图片"}), 404

            data = await request.get_json(silent=True) or {}
            fav = data.get("favorite", "none")
            if fav not in ("favorite", "like", "none"):
                return jsonify({"error": "无效的收藏值，应为 favorite/like/none"}), 400

            await self.plugin.db.update_image(image_id, favorite=fav)
            return jsonify({"success": True, "favorite": fav})

        @app.route("/api/search")
        async def api_search():
            await self.plugin._ensure_db()
            query = request.args.get("q", "").strip()
            persona = request.args.get("persona", "")
            category = request.args.get("category", "")
            favorite = request.args.get("favorite", "")
            limit = min(100, max(1, int(request.args.get("limit", 50))))

            if not query:
                return jsonify({"images": []})

            keywords = [k.strip() for k in query.replace("，", ",").split(",") if k.strip()]
            results = await self.plugin.db.search_by_description(
                keywords=keywords,
                category=category or None,
                persona=persona or None,
                limit=limit,
            )
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
            return await send_from_directory(str(image_path.parent), image_path.name)

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

        @app.route("/api/backup/export")
        async def api_backup_export():
            try:
                await self.plugin._ensure_db()
                records = await self.plugin.db.get_all_records()
                images_dir = self.plugin.store.images_dir

                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    metadata = json.dumps({
                        "version": "1.0",
                        "export_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "total_records": len(records),
                    }, ensure_ascii=False)
                    zf.writestr("backup_metadata.json", metadata)
                    zf.writestr("records.json", json.dumps(records, ensure_ascii=False, indent=2))

                    added_files = 0
                    for rec in records:
                        img_filename = rec.get("image_path", "")
                        if not img_filename:
                            continue
                        img_path = images_dir / img_filename
                        if img_path.exists():
                            zf.write(str(img_path), f"images/{img_filename}")
                            added_files += 1

                buf.seek(0)
                logger.info("[Wardrobe] 备份导出: %d条记录, %d个图片文件", len(records), added_files)

                return await send_file(
                    buf,
                    mimetype="application/zip",
                    as_attachment=True,
                    attachment_filename=f"wardrobe_backup_{time.strftime('%Y%m%d_%H%M%S')}.zip",
                )
            except Exception as e:
                logger.error("[Wardrobe] 备份导出失败: %s", e, exc_info=True)
                return jsonify({"error": f"导出失败: {e}"}), 500

        @app.route("/api/backup/import", methods=["POST"])
        async def api_backup_import():
            tmp_dir = None
            try:
                await self.plugin._ensure_db()
                files = await request.files
                file = files.get("backup")
                if not file:
                    return jsonify({"error": "未选择备份文件"}), 400

                file_bytes = file.read()
                if not file_bytes:
                    return jsonify({"error": "备份文件为空"}), 400

                tmp_dir = tempfile.mkdtemp(prefix="wardrobe_restore_")
                zip_path = Path(tmp_dir) / "backup.zip"
                async with aiofiles_open(zip_path, "wb") as f:
                    await f.write(file_bytes)

                def _extract():
                    with zipfile.ZipFile(str(zip_path), "r") as zf:
                        zf.extractall(tmp_dir)

                await asyncio.to_thread(_extract)

                records_path = Path(tmp_dir) / "records.json"
                if not records_path.exists():
                    return jsonify({"error": "无效的备份文件：缺少 records.json"}), 400

                def _read_records():
                    with open(str(records_path), "r", encoding="utf-8") as f:
                        return json.load(f)

                records = await asyncio.to_thread(_read_records)
                if not isinstance(records, list):
                    return jsonify({"error": "无效的备份文件：records.json 格式错误"}), 400

                images_src = Path(tmp_dir) / "images"
                images_dst = self.plugin.store.images_dir
                copied_files = 0

                if images_src.exists():
                    def _copy_images():
                        nonlocal copied_files
                        for rec in records:
                            img_filename = rec.get("image_path", "")
                            if not img_filename:
                                continue
                            src = images_src / img_filename
                            dst = images_dst / img_filename
                            if src.exists() and not dst.exists():
                                shutil.copy2(str(src), str(dst))
                                copied_files += 1

                    await asyncio.to_thread(_copy_images)

                imported = await self.plugin.db.import_records(records, skip_existing=True)

                logger.info("[Wardrobe] 备份恢复: 导入%d条记录, 复制%d个图片文件", imported, copied_files)
                return jsonify({
                    "success": True,
                    "imported": imported,
                    "copied_files": copied_files,
                    "total_in_backup": len(records),
                })
            except Exception as e:
                logger.error("[Wardrobe] 备份恢复失败: %s", e, exc_info=True)
                return jsonify({"error": f"恢复失败: {e}"}), 500
            finally:
                if tmp_dir:
                    await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)

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


async def aiofiles_open(path, mode="r"):
    import aiofiles
    return aiofiles.open(path, mode)
