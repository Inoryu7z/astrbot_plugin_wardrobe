import json
import re
from typing import Any, Optional


def parse_json_response(text: str) -> Optional[dict[str, Any]]:
    text = text.strip()

    json_block_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if json_block_match:
        block = json_block_match.group(1).strip()
        try:
            data = json.loads(block)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

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
