import asyncio
import secrets
import time
import uuid
from pathlib import Path

import hypercorn.asyncio
from hypercorn.config import Config as HypercornConfig
from quart import (
    Quart,
    jsonify,
    request,
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
        self._server_task: asyncio.Task | None = None
        self._shutdown_event: asyncio.Event = asyncio.Event()
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
        app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
        app.config["BODY_TIMEOUT"] = 300

        @app.errorhandler(Exception)
        async def handle_exception(e):
            logger.error("[Wardrobe] WebUI未捕获异常: %s", e, exc_info=True)
            return jsonify({"error": f"服务器内部错误: {e}"}), 500

        @app.errorhandler(413)
        async def handle_413(e):
            logger.warning("[Wardrobe] WebUI请求体过大: %s", e)
            return jsonify({"error": "上传文件过大，请压缩后重试"}), 413

        @app.before_request
        async def log_request():
            if request.path.startswith("/api/"):
                logger.info("[Wardrobe] WebUI请求: %s %s", request.method, request.path)

        @app.after_request
        async def log_response(response):
            if request.path.startswith("/api/"):
                logger.info("[Wardrobe] WebUI响应: %s %s -> %s", request.method, request.path, response.status_code)
            return response

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
                return await send_from_directory(str(self._web_dir), "login.html")

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

            offset = (page - 1) * per_page

            needs_search = style or scene or atmosphere or shot_size or persona or category
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
                )
            else:
                images = await self.plugin.db.list_images(
                    category=category or None, shot_size=shot_size or None, limit=per_page, offset=offset
                )
                total_stats = await self.plugin.db.get_stats()
                total = total_stats["total"]

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
                image_id, attrs = await self.plugin._save_image_from_bytes(
                    image_bytes, persona=persona, created_by="webui", user_description=description
                )

                if not image_id:
                    return jsonify({"error": "保存失败，请检查存图模型是否已配置"}), 500

                return jsonify({"success": True, "image_id": image_id})
            except Exception as e:
                logger.error("[Wardrobe] WebUI上传异常: %s", e, exc_info=True)
                return jsonify({"error": f"服务器内部错误: {e}"}), 500

        @app.route("/api/search")
        async def api_search():
            await self.plugin._ensure_db()
            query = request.args.get("q", "").strip()
            persona = request.args.get("persona", "")
            category = request.args.get("category", "")
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

        return app

    async def start(self):
        self._shutdown_event.clear()

        base_port = self.port
        max_port_attempts = 10

        for port_offset in range(max_port_attempts):
            candidate_port = base_port + port_offset
            config = HypercornConfig()
            config.bind = [f"{self.host}:{candidate_port}"]
            config.graceful_timeout = 5
            config.errorlog = "-"
            config.accesslog = "-"

            started = asyncio.Event()

            async def _shutdown_trigger():
                await self._shutdown_event.wait()

            app = self._create_app()

            self._server_task = asyncio.create_task(
                hypercorn.asyncio.serve(
                    app, config,
                    shutdown_trigger=_shutdown_trigger,
                )
            )

            await asyncio.sleep(0.8)

            if self._server_task.done():
                try:
                    self._server_task.result()
                except (asyncio.CancelledError, SystemExit):
                    continue
                except BaseException as e:
                    if "Address already in use" in str(e) or isinstance(e, OSError):
                        logger.warning(
                            "[Wardrobe] 端口 %d 被占用，尝试下一个端口", candidate_port
                        )
                        continue
                    logger.error("[Wardrobe] WebUI 启动失败: %s", e)
                    return
            else:
                self.port = candidate_port
                logger.info("[Wardrobe] WebUI 已启动: http://%s:%d", self.host, self.port)
                if self.password == "wardrobe":
                    logger.warning("[Wardrobe] WebUI 使用默认密码 'wardrobe'，请及时修改！")
                if candidate_port != base_port:
                    logger.info(
                        "[Wardrobe] 注意：原端口 %d 被占用，实际使用端口 %d", base_port, candidate_port
                    )
                return

        logger.error(
            "[Wardrobe] WebUI 启动失败: 端口 %d-%d 均被占用",
            base_port, base_port + max_port_attempts - 1,
        )

    async def stop(self):
        self._shutdown_event.set()
        if self._server_task and not self._server_task.done():
            try:
                await asyncio.wait_for(self._server_task, timeout=8)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._server_task.cancel()
                try:
                    await self._server_task
                except asyncio.CancelledError:
                    pass
            except Exception:
                pass
        self._tokens.clear()
        logger.info("[Wardrobe] WebUI 已停止")
