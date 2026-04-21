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
分析给定的图片，提取以下属性信息。对于每个属性，从预定义值池中选择最匹配的值；如果没有合适的，可以自行填写。

# 预定义值池
{pools_text}

# 输出格式
输出 JSON 对象，字段如下：

```json
{{
  "category": "人物 或 衣服",
  "style": ["从风格池中选择，可多选"],
  "clothing_type": "从服装类型池中选择",
  "exposure_level": "从暴露程度池中选择",
  "exposure_features": ["具有视觉吸引力的身体部位暴露，仅限非常规暴露（非日常穿着会露的部位）。如：乳沟/深V、侧乳/侧胸露出、露背、腰部裸露、大腿根部露出、臀部部分可见/臀线露出、短裙走光、下装消失（仅内衣/丁字裤）、透视可见内衣、内衣肩带滑落、吊带滑落等。不要记录手臂、小腿、常规肩膀露出等日常暴露部位"],
  "key_features": ["图片最突出的3-5个视觉特征，提取最醒目的、能区分此图与其他图的标志性元素。包括但不限于：独特服装细节（蕾丝花边、绑带设计、镂空剪裁、高开叉）、标志性道具（粉色兔子玩偶、雪花头饰、蕾丝手套、红酒杯、羽毛扇）、特殊姿势符号（猫爪手势、剪刀手、双手托腮）、身体特征（腰链、腿环、纹身位置、特殊发型发色）、以及场景标志性元素（花瓣浴、落地窗、霓虹灯、镜面反射）。聚焦于看到这个词就能想起这张图的独特标识"],
  "prop_objects": ["画面中的道具/物品，如：手机、兔子玩偶、扇子、花束、镜子等"],
  "allure_features": ["具有性暗示或诱惑感的神态与动作细节，与服装暴露无关。重点记录：眼神神态（含情脉脉、眼神迷离、眼神上挑）、嘴部动作（咬嘴唇、舔唇、微张嘴唇、吐舌）、手部小动作（手指轻触唇边/颈侧/锁骨、撩头发、手轻抚身体）、身体局部动态（胸部挤压/抖动、臀部扭动、腰部扭动）、大幅姿态的诱惑感（双腿张开/抬起、臀部翘起/抬起、胸部前倾/挤压等）、以及特殊视觉元素（湿身/水渍、衣物滑落/半褪、丝袜破损/勾丝）。不要记录普通的微笑、直视镜头等常态表情"],
  "body_focus": ["画面刻意突出/聚焦的身体部位，如：胸部特写、臀部特写、腿部特写、腰部特写、肩颈特写等。仅当画面明显聚焦某个部位时才记录，全身均衡构图不要记录"],
  "scene": ["从场景池中选择，可多选"],
  "atmosphere": ["从氛围池中选择，可多选"],
  "pose_type": "从姿势类型池中选择（仅人物分类需要）",
  "body_orientation": "从身体朝向池中选择（仅人物分类需要）",
  "dynamic_level": "从动态感池中选择（仅人物分类需要）",
  "action_style": ["从动作风格池中选择，可多选（仅人物分类需要）"],
  "shot_size": "从景别池中选择（仅人物分类需要）",
  "camera_angle": "从拍摄角度池中选择（仅人物分类需要）",
  "expression": "从表情池中选择（仅人物分类需要）",
  "color_tone": "自由填写颜色描述",
  "composition": "自由填写画面构图描述",
  "background": "自由填写背景环境描述",
  "description": "详细描述图片内容，用于语义检索"
}}
```

# 规则
1. category 判断：如果图片中有人物（脸部、身体），则填"人物"；否则填"衣服"
2. 如果 category 是"衣服"，则 pose_type、body_orientation、dynamic_level、action_style、shot_size、camera_angle、expression 填空字符串或空数组
3. description 必须详细，包含所有可见的视觉特征，以便后续语义检索
4. exposure_features 判断标准：只记录具有视觉吸引力的非常规暴露部位。日常穿着会露出的手臂、小腿、常规肩膀不要记录。重点关注：乳沟/深V、侧乳/侧胸、背部、腰部、大腿根部、臀部/臀线、内衣可见、肩带滑落等
5. key_features 判断标准：提取3-5个最独特的视觉标识，包括服装细节、道具、姿势符号、身体特征、场景元素等，聚焦于"看到这个词就能想起这张图"
6. prop_objects 记录画面中可辨识的具体物品/道具，包括手持物品、身边摆件、背景中的醒目物件
7. allure_features 判断标准：记录具有性暗示或诱惑感的神态与动作，可自由发挥。包括眼神/嘴部/手部的小动作、身体局部动态、大幅姿态的诱惑感、以及湿身/衣物滑落等特殊视觉元素。普通的微笑、直视不要记录
8. body_focus 判断标准：仅当画面通过构图、景别、角度等方式刻意突出某个身体部位时才记录。全身均衡构图不要记录
9. 只输出 JSON，不要输出解释或其他内容

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
                import os
                os.close(temp_fd)
            resolved_path = str(Path(temp_path).resolve())
        except Exception as e:
            logger.warning("[Wardrobe] 保存临时图片失败: %s", e)
            self._cleanup_temp(temp_path)
            return None

        try:
            prompt_text = "请分析这张图片的属性。"
            if user_description.strip():
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
