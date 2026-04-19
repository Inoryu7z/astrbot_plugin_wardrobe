import asyncio
import secrets
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


class WardrobeWebServer:
    def __init__(self, plugin, config: dict):
        self.plugin = plugin
        self.config = config
        self.host = str(config.get("webui_host", "127.0.0.1"))
        self.port = int(config.get("webui_port", 18921))
        self._tokens: set[str] = set()
        self._server_task: asyncio.Task | None = None
        self._web_dir = Path(__file__).parent.parent / "web"

    @property
    def password(self):
        return str(self.plugin._cfg("webui_password", "wardrobe") or "wardrobe")

    def _create_app(self) -> Quart:
        app = Quart(
            "wardrobe_webui",
            static_folder=str(self._web_dir),
            static_url_path="/static",
        )
        app.secret_key = secrets.token_hex(32)

        @app.before_request
        async def auth_check():
            if request.path.startswith("/static/") or request.path == "/login":
                return None
            if request.path == "/api/login":
                return None
            token = request.headers.get("X-Wardrobe-Token", "") or session.get("token", "")
            if token not in self._tokens:
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
            if pwd == self.password:
                token = secrets.token_hex(32)
                self._tokens.add(token)
                session["token"] = token
                return jsonify({"success": True, "token": token})
            return jsonify({"success": False, "error": "密码错误"}), 403

        @app.route("/api/logout", methods=["POST"])
        async def api_logout():
            token = session.pop("token", "")
            self._tokens.discard(token)
            return jsonify({"success": True})

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

            offset = (page - 1) * per_page
            images = await self.plugin.db.list_images(
                category=category or None, limit=per_page, offset=offset
            )

            if persona:
                images = [img for img in images if img.get("persona") == persona]
            if style:
                images = [img for img in images if style in img.get("style", [])]

            total_stats = await self.plugin.db.get_stats()
            result = {
                "images": images,
                "total": total_stats["total"],
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
            await self.plugin._ensure_db()
            files = await request.files
            file = files.get("image")
            if not file:
                return jsonify({"error": "未选择图片"}), 400

            image_bytes = file.read()
            if not image_bytes:
                return jsonify({"error": "图片为空"}), 400

            persona = request.form.get("persona", "")
            persona = self.plugin._resolve_persona(persona)

            max_size = int(self.plugin._cfg("max_image_size_mb", 10) or 10)
            if len(image_bytes) > max_size * 1024 * 1024:
                return jsonify({"error": f"图片过大，限制{max_size}MB"}), 400

            image_id, attrs = await self.plugin._save_image_from_bytes(
                image_bytes, persona=persona, created_by="webui"
            )

            if not image_id:
                return jsonify({"error": "保存失败"}), 500

            return jsonify({"success": True, "image_id": image_id})

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
            stats = await self.plugin.db.get_stats()
            persona_names = str(self.plugin._cfg("persona_names", "") or "").strip()
            personas = [n.strip() for n in persona_names.replace("，", ",").split(",") if n.strip()] if persona_names else []
            return jsonify({
                "categories": list(stats.get("by_category", {}).keys()),
                "personas": personas,
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

        return app

    async def start(self):
        app = self._create_app()
        config = HypercornConfig()
        config.bind = [f"{self.host}:{self.port}"]
        config.graceful_timeout = 5

        logger.info("[Wardrobe] WebUI 启动中: %s:%d", self.host, self.port)

        self._server_task = asyncio.create_task(
            hypercorn.asyncio.serve(app, config)
        )

        await asyncio.sleep(0.5)
        logger.info("[Wardrobe] WebUI 已启动: http://%s:%d", self.host, self.port)
        if self.password == "wardrobe":
            logger.warning("[Wardrobe] WebUI 使用默认密码 'wardrobe'，请及时修改！")

    async def stop(self):
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
        self._tokens.clear()
        logger.info("[Wardrobe] WebUI 已停止")
