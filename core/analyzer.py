import asyncio
import base64
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp

from astrbot.api import logger

from .pools import ALL_POOLS
from .utils import detect_image_mime, mime_to_ext, parse_json_response


ANALYZE_SYSTEM_PROMPT = """# 角色
你是专业的图片分析助手，负责对图片进行详细的属性标注。

# 任务
分析给定的图片，提取以下属性信息。预定义值池仅供参考和优先选用，如果池中没有更合适的值，允许自行填写更准确的描述。尤其表情和姿势变化丰富，不要被池子限制。

# 预定义值池
{pools_text}

# 输出格式
输出 JSON 对象，字段如下：

```json
{{
  "category": "人物 或 衣服",
  "style": ["优先从风格池选择，也可自行填写，可多选"],
  "clothing_type": "优先从服装类型池选择，也可自行填写。多个服装类型用顿号分隔，如：跳裙（JSK）、连裤袜、高跟鞋",
  "exposure_level": "从暴露程度池中选择",
  "exposure_features": ["非常规暴露的身体部位"],
  "key_features": ["3-5个最突出的视觉标识"],
  "prop_objects": ["画面中的道具/物品"],
  "allure_features": ["具有吸引力或微妙暗示感的神态、动作与姿态细节"],
  "body_focus": ["画面刻意聚焦的身体部位"],
  "scene": ["优先从场景池选择，也可自行填写，可多选"],
  "atmosphere": ["优先从氛围池选择，也可自行填写，可多选"],
  "pose_type": "优先从姿势池选择，姿势变化丰富，池外更准确的姿势可直接填写（仅人物分类需要）",
  "body_orientation": "从身体朝向池中选择（仅人物分类需要）",
  "dynamic_level": "从动态感池中选择（仅人物分类需要）",
  "action_style": ["优先从动作风格池选择，也可自行填写，可多选（仅人物分类需要）"],
  "shot_size": "从景别池中选择（仅人物分类需要）",
  "camera_angle": "从拍摄角度池中选择（仅人物分类需要）",
  "expression": "优先从表情池选择，表情变化丰富，池外更准确的表情可直接填写（仅人物分类需要）",
  "color_tone": "自由填写颜色描述",
  "composition": "自由填写画面构图描述",
  "background": "自由填写背景环境描述",
  "description": "按规则3详细描述",
  "ref_strength": "style / full / reimagine（仅人物分类需要，衣服分类填 style）",
  "ref_strength_reason": "用2-3句话描述人物的姿势状态与构图，说明评级依据。要像在给朋友描述这张图的姿势一样自然具体，说出身体在做什么、曲线如何、构图有什么设计。禁止使用抽象评价（如'视觉表现力较强''具有参考模仿价值''氛围感'），用具体描述代替（仅人物分类需要，衣服分类填空字符串）"
}}
```

# 规则
1. category 判断：如果图片中有人物（脸部、身体），则填"人物"；否则填"衣服"
2. 如果 category 是"衣服"，则 pose_type、body_orientation、dynamic_level、action_style、shot_size、camera_angle、expression 填空字符串或空数组
3. description 用一段式客观描述图片，以人物为绝对重点，按从头到脚顺序详细描述。描述需覆盖以下颗粒度：发型与发色、头饰结构与材质、面部可见部分与妆容、服装款式与颜色（精确到具体色调）、面料质感（厚度、透明度、光泽、纹理）、装饰细节（图案、花纹、配饰位置）、肢体姿势与手指脚趾状态、所持物品的具体结构与外观。保留合理的风格定性，去除纯文学比喻。环境、背景、光线等次要元素简化但不省略，省略文字水印等无关信息，需明确空间方位和前后层次关系。整体信息密度要高，所有可见元素均需覆盖，闭上眼睛能完整还原画面

   # description 示例（同一张图）
   ❌ 失败（太笼统，缺少颗粒度，无法还原画面）：
   图中是一位扮演草系精灵的少女，留着浅金色的超粗三股麻花长辫，头戴由白色大朵鲜花和绿叶组成的花环头饰，她坐在黑色电竞椅上，双腿蜷曲抬起，一只脚踩在电竞椅的可伸缩脚踏上，另一只脚被花藤缠绕抬起。她的右手举着背面印有心形图案的粉色智能手机，遮挡住大半张脸，仅露出一只眼睛看向镜头，左手牵拉着一支装饰着白色小玫瑰和绿叶的藤蔓道具。她穿着浅绿色带白色蕾丝花边的挂脖精灵服饰，手臂套着带蕾丝荷叶边的透明白色袖套，腿和脚都缠绕着印绿色枝叶、点缀白色花朵的薄网纱配饰，手臂可见纹身图案。整体呈现出清新梦幻的居家自拍氛围。

   ✅ 优秀（按从头到脚顺序，每个区域到细节级别，可还原画面）：
   一位身着森林精灵风格 cosplay 的少女斜坐在黑色电竞椅上对镜自拍，头戴奶金色长假发，左侧有一条粗麻花辫从耳后垂至腰际，发尾为白色，额前齐刘海长度到眉峰；头顶佩戴白色花朵与绿色叶子组成的花冠，花冠右侧垂下一小段枝叶，末端有一朵白色小花蕾。面部大半被手机遮挡，露出右眼，画有上挑眼线；颈部佩戴绿色 choker 项圈，正中有一枚金色小吊坠。身着嫩绿色露肩裙装，面料带缎面光泽，V 字领口，领口与双肩袖口均有多层白色蕾丝荷叶边，腰部收窄，左侧腰腹位置有白色蕾丝花纹。右臂抬起持手机，手腕处有白色荷叶边纱袖，手背上有白色花纹，指甲涂淡粉色；左臂向前伸出，小臂佩戴宽版金色臂环，表面有浮雕花纹，手背上有黑色藤蔓状纹身，手指上有黑色线条，食指与拇指捏着一根透明细线。双腿弯曲抬起，皮肤白皙，左大腿外侧有一块淡褐色不规则印记。小腿及双脚缠绕白色薄纱与绿色花藤，花藤上有白色玫瑰花与绿色叶片，从脚踝延伸至脚背；双脚赤裸，右脚踩在椅子右侧延伸出的黑色方形脚托上，左脚脚尖向前触碰花弓末端。右手所持手机为粉色外壳，表面有白色爱心图案，背面为方形摄像头模组，含三个大圆形摄像头孔和一个小圆形闪光灯孔。画面右侧有一道弧形花弓，由绿色藤蔓构成，藤蔓上有多种形态的绿色叶片与白色花朵，包括全开、半开、花苞三种状态，从画面右上延伸至右下，透明细线绷紧在花弓两端形成弓弦。座椅为黑色电竞椅，高靠背，皮质表面，两侧有护腰凸起，宽扶手，底部为黑色五星脚架带滚轮。背景为室内环境，人物身后是浅棕色木框玻璃门，玻璃磨砂半透明，地面为浅棕色木地板。光线从前方照射，整体明亮柔和。
4. exposure_features：只记录非常规暴露部位（日常穿着会露的手臂、小腿、常规肩膀不要记录）。如：乳沟、侧乳露出、露背、露肩、腰部裸露、大腿根部露出、臀部/臀线露出、短裙走光、下装消失、透视可见内衣、内衣肩带滑落、吊带滑落等
5. key_features：提取3-5个最独特的视觉标识——看到这个词就能想起这张图。包括独特服装细节、标志性道具、特殊姿势符号、身体特征、场景标志性元素等
6. prop_objects：记录画面中可辨识的具体物品/道具，包括手持物品、身边摆件、背景中的醒目物件
7. allure_features：记录具有吸引力或微妙暗示感的神态、动作与姿态细节。分三个层次：
   - 明确诱惑：眼神迷离/上挑、咬唇/舔唇、手指轻触唇边/颈侧/锁骨、撩头发、胸部挤压、臀部扭动/翘起、双腿张开/抬起、湿身/衣物滑落/半褪、丝袜破损等
   - 姿态暗示：整体姿态或肢体语言带来的微妙吸引力。如：叠腿展示腿部曲线、S曲线站姿的身体线条感、俯身/后仰的角度暗示、慵懒舒展中的身体延伸感、侧坐时腰臀线条的呈现等。这类姿态本身不是擦边动作，但通过肢体走向和线条展示了身体魅力
   - 不要记录：普通的微笑、直视镜头、正常站坐等毫无暗示感的常态
8. body_focus：仅当画面通过构图、景别、角度等方式刻意突出某个身体部位时才记录。全身均衡构图不要记录
9. 如果某个属性无法判断（图片模糊、被遮挡等），该字段填空字符串或空数组，不要猜测
10. 只输出 JSON，不要输出解释或其他内容
11. ref_strength 评估标准（严格判断！此字段仅评估姿势与构图的参考价值，与服装美观度完全无关，剪影判断时必须完全排除服装轮廓。目标分布：full约20%，style约30%，reimagine约50%。边界模糊时一律优先降级。每张图根据实际内容独立判断）：
   评估时从以下维度思考（不需要按维度输出，只需综合判断）：
   - 轮廓辨识度：仅看人物剪影，能否一眼识别姿势的独特性？
   - 肢体设计感：姿势是日常功能性的，还是专门为拍照设计的？肢体是否有舒展/收缩的张力？
   - 身体线条呈现：姿势是否刻意且自然地展示了人体线条（肩颈/腰臀/腿部等）？注意与服装暴露程度无关，只看姿势带来的线条变化
   - 功能性判定：姿势的唯一作用是否只是展示服装？身体是否无明显倾斜、扭转或曲线变化？

   综合以上维度，给出评级：
   - "full"：轮廓剪影有独特性和记忆点，姿势有明确设计感和肢体张力，身体线条展示充分，或通过姿态角度刻意展示身体魅力。判断标准：如果只看人物轮廓剪影，这个姿势仍然有独立看点和直接模仿价值
   - "style"：姿势有刻意的设计意图，身体有明确的角度变化或肢体调整（非日常自然姿态），不是随便站坐就能复现的，需要刻意摆出才能达到图中效果。判断标准：姿势不单调，但也不足以作为单独模仿的对象，仅能参考其整体感觉和氛围
   - "reimagine"：日常自然姿态或纯功能性展示姿态，不需要刻意摆拍，普通人在日常生活中自然就会这样站坐，身体无明显倾斜、扭转或曲线，构图无设计意图。判断标准：如果只看人物轮廓剪影，这就是一个标准的"人形衣架"，姿势本身没有值得保留的视觉特征。典型包括：正面直立对镜自拍、背对镜头展示服装、正面完全端正展示服装等

# 用户描述处理
如果用户提供了描述，请参考以下规则：
1. 用户描述中可能包含服装/单品的专有名称，请原样保留这些名称，不要尝试解释或发散
2. 用户描述中的信息应融入 description 字段，但保持专有名称不变
3. 如果用户描述提到具体特征，请在描述中体现这些特征"""


class ImageAnalyzer:
    def __init__(self, context, plugin=None):
        self.context = context
        self.plugin = plugin

    async def _build_pools_text(self, persona: str = "") -> str:
        pools = await self.plugin.get_merged_pools() if self.plugin else ALL_POOLS
        pools = {k: list(v) for k, v in pools.items()}

        # 人格级风格池覆盖：若该人格配置了自定义 style 池，则替换全局 style 池
        persona_key = (persona or "").strip()
        if persona_key and self.plugin:
            try:
                persona_styles = await self.plugin.get_style_pool_for_persona(persona_key)
                if persona_styles is not None:
                    pools["style"] = list(persona_styles)
            except Exception as exc:
                logger.warning("[Wardrobe] 分析时获取人格风格池失败 persona=%s error=%s", persona_key, exc)

        lines = []
        for key, values in pools.items():
            lines.append(f"## {key}")
            for v in values:
                lines.append(f"- {v}")
            lines.append("")
        return "\n".join(lines)

    async def analyze_image(
        self,
        image_bytes: bytes,
        user_description: str = "",
        *,
        primary_provider_id: str,
        secondary_provider_id: str = "",
        timeout_seconds: float = 60.0,
        persona: str = "",
    ) -> Optional[dict[str, Any]]:
        pools_text = await self._build_pools_text(persona=persona)
        system_prompt = ANALYZE_SYSTEM_PROMPT.format(pools_text=pools_text)

        mime = detect_image_mime(image_bytes)
        ext = mime_to_ext(mime)

        temp_path = ""
        try:
            temp_fd, temp_path = tempfile.mkstemp(suffix=f".{ext}")
            try:
                import os
                os.write(temp_fd, image_bytes)
            finally:
                os.close(temp_fd)
            resolved_path = str(Path(temp_path).resolve())
        except Exception as e:
            logger.warning("[Wardrobe] 保存临时图片失败: %s", e)
            self._cleanup_temp(temp_path)
            return None

        try:
            prompt_text = "请分析这张图片的属性。"
            if user_description and user_description.strip():
                prompt_text += f"\n\n【用户描述】{user_description.strip()}\n\n请参考用户描述进行分析，注意：用户描述中的专有名词（如服装名称）请原样保留，不要发散解释。"

            # 读取 Responses API 配置
            use_responses = False
            resp_providers: list[dict] = []
            if self.plugin:
                use_responses = bool(self.plugin._cfg("save_use_responses_api", False))
                resp_providers = self._parse_responses_providers()

            # 查找 token_router 插件
            token_router = self._find_token_router()

            # 构建尝试链路
            # Responses 模式：从 resp_providers 列表按顺序，配合 token_router 按日用量选择 active
            # 非 Responses 模式：用框架 provider_id（primary + secondary），无日用量路由
            attempts: list[tuple[str, bool]] = []
            if use_responses and resp_providers:
                # 优先用 token_router 决策的 active_id，其余按列表顺序作为 per-call 错误回退
                if token_router:
                    providers_for_router = [
                        {"id": p["id"], "daily_limit": p["daily_limit"]} for p in resp_providers
                    ]
                    active_id = token_router.get_active_storage_provider(providers_for_router)
                else:
                    active_id = resp_providers[0]["id"]
                attempts.append((active_id, True))
                for p in resp_providers:
                    if p["id"] != active_id:
                        attempts.append((p["id"], True))
            else:
                # 非 Responses 模式：框架 provider，无日用量路由
                if not primary_provider_id and not secondary_provider_id:
                    logger.warning("[Wardrobe] 未配置存图模型，无法分析图片")
                    return None
                if primary_provider_id:
                    attempts.append((primary_provider_id, False))
                if secondary_provider_id and secondary_provider_id != primary_provider_id:
                    attempts.append((secondary_provider_id, False))

            for provider_id, is_responses in attempts:
                if not provider_id:
                    continue
                try:
                    t0 = time.perf_counter()
                    if is_responses:
                        p_cfg = self._get_responses_provider_cfg(provider_id, resp_providers)
                        if not p_cfg:
                            logger.warning("[Wardrobe] 未找到 Responses 提供商配置 id=%s", provider_id)
                            continue
                        result, tokens = await asyncio.wait_for(
                            self._call_responses_api(
                                p_cfg["api_key"], p_cfg["base_url"], p_cfg["model"],
                                system_prompt, prompt_text, image_bytes, mime,
                            ),
                            timeout=timeout_seconds,
                        )
                    else:
                        result, tokens = await asyncio.wait_for(
                            self._call_vision_model(provider_id, system_prompt, prompt_text, resolved_path),
                            timeout=timeout_seconds,
                        )
                    elapsed = time.perf_counter() - t0
                    logger.debug("[Wardrobe] 图片分析完成 provider=%s 耗时=%.2fs tokens=%d", provider_id, elapsed, tokens)
                    if result:
                        if token_router and tokens > 0:
                            token_router.record_storage_usage(provider_id, tokens)
                        return result
                    logger.warning("[Wardrobe] 模型返回结果解析失败 provider=%s，尝试下一个模型", provider_id)
                except asyncio.TimeoutError:
                    logger.warning("[Wardrobe] 存图模型超时 provider=%s", provider_id)
                except Exception as e:
                    logger.warning("[Wardrobe] 存图模型调用失败 provider=%s error=%s", provider_id, e)

            logger.error("[Wardrobe] 存图模型均不可用")
            return None
        finally:
            self._cleanup_temp(temp_path)

    @staticmethod
    def _cleanup_temp(temp_path: str):
        try:
            import os
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

    def _find_token_router(self):
        """跨插件查找 token_router 实例。找不到或无目标方法时返回 None。"""
        try:
            stars = self.context.get_all_stars()
        except Exception:
            return None
        for meta in stars or []:
            p_id = str(getattr(meta, "id", "") or "")
            p_name = str(getattr(meta, "name", "") or "")
            root_dir_name = str(getattr(meta, "root_dir_name", "") or "")
            if "token_router" not in p_id and "token_router" not in p_name and "token_router" not in root_dir_name:
                continue
            for attr in ("star_instance", "instance", "star_cls"):
                candidate = getattr(meta, attr, None)
                if candidate is not None and hasattr(candidate, "get_active_storage_provider"):
                    return candidate
        return None

    def _parse_responses_providers(self) -> list[dict]:
        """解析 save_responses_providers 配置，返回有效的 Responses API 提供商列表。

        每项: {"id", "base_url", "api_key", "model", "daily_limit"}
        model 从 provider_id 的 '/' 后面解析。
        跳过 provider_id/api_key 为空的项。
        """
        if not self.plugin:
            return []
        raw = self.plugin._cfg("save_responses_providers", [])
        if not isinstance(raw, list):
            return []
        result: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("provider_id", "") or "").strip()
            api_key = str(item.get("api_key", "") or "").strip()
            if not pid or not api_key:
                continue
            # 从 provider_id 解析模型名（'/' 后面的部分）
            model = pid.split("/", 1)[1] if "/" in pid else pid
            base_url = str(item.get("base_url", "https://ark.cn-beijing.volces.com") or "").strip()
            daily_limit = int(item.get("daily_limit", 1500000) or 1500000)
            result.append({
                "id": pid,
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
                "daily_limit": daily_limit,
            })
        return result

    @staticmethod
    def _get_responses_provider_cfg(provider_id: str, providers: list[dict]) -> Optional[dict]:
        """从 providers 列表中按 provider_id 查找配置。"""
        for p in providers:
            if p["id"] == provider_id:
                return p
        return None

    async def _call_responses_api(
        self,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        prompt_text: str,
        image_bytes: bytes,
        mime: str,
    ) -> tuple[Optional[dict[str, Any]], int]:
        """通过豆包 Responses API（带 web_search 工具）分析图片。

        Returns:
            (解析后的属性 JSON, 总 token 用量)。失败时返回 (None, 0)。
        """
        url = f"{base_url.rstrip('/')}/api/v3/responses"
        img_b64 = base64.b64encode(image_bytes).decode("ascii")
        image_data_uri = f"data:{mime};base64,{img_b64}"

        # 追加联网搜索引导：cosplay 等场景必须调用 web_search
        search_guidance = (
            "\n\n# 联网搜索指引（已启用 web_search 工具）\n"
            "本次已启用联网搜索。请按以下规则使用：\n"
            "1. 如果图片是 cosplay（角色扮演），必须调用 web_search 搜索该角色的出处（作品名、角色名）。"
            "在 key_features 中记录\"cosplay: 作品名/角色名\"，在 description 开头提及角色出处。\n"
            "2. 如果图片涉及可识别的品牌 logo、IP 角色、特定作品元素，也应搜索确认。\n"
            "3. 日常穿搭、纯风景等不涉及上述内容的图片无需搜索。"
        )
        final_prompt = prompt_text + search_guidance

        body: dict[str, Any] = {
            "model": model,
            "stream": False,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_prompt}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": final_prompt},
                        {"type": "input_image", "image_url": image_data_uri},
                    ],
                },
            ],
            "tools": [{"type": "web_search"}],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.warning(
                        "[Wardrobe] Responses API 返回 %d: %s",
                        resp.status,
                        error_text[:500],
                    )
                    return None, 0

                raw_text = await resp.text()
                data = json.loads(raw_text)

        # 解析 output -> output_text
        message = ""
        try:
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for content_item in item.get("content", []):
                        if content_item.get("type") == "output_text":
                            message = content_item.get("text", "")
                            break
                    if message:
                        break
        except (KeyError, IndexError, TypeError) as e:
            logger.warning("[Wardrobe] Responses API 响应解析失败: %s", e)
            return None, 0

        if not message:
            logger.warning("[Wardrobe] Responses API 返回空响应")
            return None, 0

        # 解析 usage
        usage = data.get("usage", {})
        total_tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

        result = parse_json_response(message)
        if not result:
            logger.warning("[Wardrobe] Responses API 返回内容 JSON 解析失败")
        return result, total_tokens

    async def _call_vision_model(
        self,
        provider_id: str,
        system_prompt: str,
        prompt_text: str,
        image_path: str,
    ) -> tuple[Optional[dict[str, Any]], int]:
        """通过 AstrBot provider 系统调用视觉模型。

        Returns:
            (解析后的属性 JSON, 总 token 用量)。失败时返回 (None, 0)。
        """
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt_text,
                system_prompt=system_prompt,
                image_urls=[image_path],
            )
        except (TypeError, AttributeError) as e:
            logger.warning("[Wardrobe] image_urls 列表格式不兼容，回退字符串模式: %s", e)
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt_text,
                system_prompt=system_prompt,
                image_urls=image_path,
            )
        except Exception:
            raise

        raw_text = (getattr(llm_resp, "completion_text", "") or "").strip()
        if not raw_text:
            return None, 0

        # 提取 token 用量
        usage_obj = getattr(llm_resp, "usage", None)
        total_tokens = 0
        if usage_obj:
            total_tokens = getattr(usage_obj, "total", 0) or 0

        return parse_json_response(raw_text), total_tokens
