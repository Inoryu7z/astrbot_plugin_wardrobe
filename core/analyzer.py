import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

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
  "clothing_type": "优先从服装类型池选择，也可自行填写",
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
  "description": "详细描述图片内容，用于语义检索",
  "ref_strength": "style / full / reimagine（仅人物分类需要，衣服分类填 style）"
}}
```

# 规则
1. category 判断：如果图片中有人物（脸部、身体），则填"人物"；否则填"衣服"
2. 如果 category 是"衣服"，则 pose_type、body_orientation、dynamic_level、action_style、shot_size、camera_angle、expression 填空字符串或空数组
3. description 必须详细，包含所有可见的视觉特征，以便后续语义检索
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
11. ref_strength 评估标准（严格！此字段仅评估姿势与构图的参考价值，与服装美观程度完全无关）：
   - "full"：姿势或构图具有强烈的视觉表现力或身体魅力展示——身体线条有张力或曲线感、肢体动作有情绪表达、画面构图有刻意设计、或通过姿态角度刻意展示身体魅力。判断标准：如果只看人物轮廓剪影，这个姿势仍然有看点和模仿价值。少数图片能达到此级
   - "style"：姿势有一定韵味或自然的美感，但未达到刻意设计的程度——身体有微妙的角度或姿态感，不是最平淡的站坐。可能带有轻微的身体魅力呈现，但不突出。判断标准：姿势不无聊，但也不足以作为直接模仿对象，取其氛围和感觉即可
   - "reimagine"：姿势构图缺乏表现力，也没有刻意的身体魅力展示，纯粹是展示服装的功能性姿态——身体无倾斜无曲线，构图无设计意图。判断标准：如果只看人物轮廓剪影，这就是一个"人形衣架"，姿势本身没有值得保留的视觉叙事或身体魅力

# 用户描述处理
如果用户提供了描述，请参考以下规则：
1. 用户描述中可能包含服装/单品的专有名称，请原样保留这些名称，不要尝试解释或发散
2. 用户描述中的信息应融入 description 字段，但保持专有名称不变
3. 如果用户描述提到具体特征，请在描述中体现这些特征"""


class ImageAnalyzer:
    def __init__(self, context, plugin=None):
        self.context = context
        self.plugin = plugin

    async def _build_pools_text(self) -> str:
        pools = await self.plugin.get_merged_pools() if self.plugin else ALL_POOLS
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
    ) -> Optional[dict[str, Any]]:
        pools_text = await self._build_pools_text()
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

            providers = [p for p in [primary_provider_id, secondary_provider_id] if p.strip()]
            if not providers:
                logger.warning("[Wardrobe] 未配置存图模型，无法分析图片")
                return None

            for provider_id in providers:
                try:
                    t0 = time.perf_counter()
                    result = await asyncio.wait_for(
                        self._call_vision_model(provider_id, system_prompt, prompt_text, resolved_path),
                        timeout=timeout_seconds,
                    )
                    elapsed = time.perf_counter() - t0
                    logger.info("[Wardrobe] 图片分析完成 provider=%s 耗时=%.2fs", provider_id, elapsed)
                    return result
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

    async def _call_vision_model(
        self,
        provider_id: str,
        system_prompt: str,
        prompt_text: str,
        image_path: str,
    ) -> Optional[dict[str, Any]]:
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
            return None

        return parse_json_response(raw_text)
