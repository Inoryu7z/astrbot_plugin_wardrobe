import json
from typing import Any, Optional


def parse_json_response(text: str) -> Optional[dict[str, Any]]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start : i + 1])
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError:
                    return None
    return None


_IMAGE_MAGIC = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "image/webp",
    b"BM": "image/bmp",
}


def detect_image_mime(data: bytes) -> str:
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for magic, mime in _IMAGE_MAGIC.items():
        if data[: len(magic)] == magic:
            return mime
    return "image/jpeg"


def mime_to_ext(mime: str) -> str:
    return {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/bmp": "bmp",
    }.get(mime, "jpg")


def ensure_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        return [v]
    return []


def ensure_str(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v:
        return v[0]
    return ""


# aiimg 自拍模式生成的提示词固定首尾，存入衣柜库时剥离以仅保留中间描述部分。
# 维护多个已知前缀以兼容不同版本的 aiimg 默认配置：
# - _AI_PROMPT_PREFIX_OPTIMIZED：wardrobe 优化版（推荐，aiimg 应升级至此）
# - _AI_PROMPT_PREFIX_LEGACY：aiimg 旧版默认配置（兼容已生成的历史提示词）
_AI_PROMPT_PREFIX_OPTIMIZED = "以前三张参考图中同一少女为基准，完整保留少女五官、身材等全部人体身份特征，绝对禁止任何拼图，使用少女的面部特征为她本人生成一张新的写真：她有着白皙细腻的皮肤，纤细的身姿与格外饱满的曲线形成鲜明对比，"
_AI_PROMPT_PREFIX_LEGACY = "以参考图中这位少女为基准，完整保留其五官、身材等全部人体身份特征，绝对禁止任何拼图，为她本人生成一张新的写真：她有着白皙细腻的皮肤，纤细的身姿与格外饱满的曲线形成鲜明对比，"
_AI_PROMPT_SUFFIX = "完全保留少女的面部特征与丰满的身材。"

_KNOWN_AI_PROMPT_PREFIXES = (
    _AI_PROMPT_PREFIX_OPTIMIZED,
    _AI_PROMPT_PREFIX_LEGACY,
)


def strip_ai_prompt_affixes(text: str) -> str:
    """剥离 ai_prompt 的固定前缀与后缀，只保留中间部分。

    尝试匹配多个已知前缀（兼容 aiimg 不同版本的默认配置）。
    仅当文本以指定前缀/后缀开头/结尾时才移除，未匹配时原样返回。
    """
    if not isinstance(text, str) or not text:
        return text
    for prefix in _KNOWN_AI_PROMPT_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text.endswith(_AI_PROMPT_SUFFIX):
        text = text[: -len(_AI_PROMPT_SUFFIX)]
    return text.strip()
