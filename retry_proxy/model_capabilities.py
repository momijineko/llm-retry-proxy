import json
import re


MODEL_REJECTION_CODES = {
    "invalid_model", "model_not_found", "unsupported_model",
    "model_not_supported", "model_unsupported", "model_access_denied",
    "model_disabled", "model_forbidden", "model_not_available",
    "model_not_allowed", "model_not_enabled",
}
MODEL_REJECTION_MESSAGE = re.compile(
    r"(?:\bmodel\b.{0,160}\b(?:does not exist|not found|is not supported|"
    r"unsupported|disabled|not available|not allowed|not enabled|forbidden|"
    r"access denied|permission denied|not permitted)\b"
    r"|\b(?:invalid|unsupported)\s+model\b"
    r"|模型.{0,160}(?:不存在|不支持|无效|已禁用|未启用|不可用|不允许|"
    r"无权限|没有权限|无可用渠道)"
    r"|分组.{0,160}(?:不支持|已禁用|未启用).{0,80}模型"
    r"|不支持(?:该|此)?模型)",
    re.IGNORECASE,
)

_GEMINI_ENDPOINT_PATH = re.compile(
    r"(?:^|/)models/[^/:]+:(?:generatecontent|streamgeneratecontent|"
    r"embedcontent|batchgeneratecontent)(?:/|$)",
    re.IGNORECASE,
)
_IMAGE_MODEL_MARKERS = (
    "image", "imagen", "nano-banana", "dall-e", "dalle", "flux",
    "seedream", "recraft", "ideogram", "stable-image",
    "stable-diffusion", "sdxl",
)
_IMAGE_MODEL_MARKER = re.compile(
    r"(?:^|[-_.])(?:" + "|".join(
        re.escape(marker) for marker in _IMAGE_MODEL_MARKERS
    ) + r")(?=$|[-_.]|\d)"
)


def classify_endpoint_family(path):
    """Return the route family used by capability filtering and rejection learning."""
    normalized = str(path or "").strip("/").lower()
    if not normalized:
        return ""
    if _GEMINI_ENDPOINT_PATH.search(normalized):
        return "gemini"
    segments = normalized.split("/")
    if "images" in segments:
        return "images"
    if len(segments) >= 2 and segments[-2:] == ["chat", "completions"]:
        return "chat"
    for family in ("responses", "messages", "embeddings", "audio", "realtime"):
        if family in segments:
            return family
    return ""


def is_image_model_name(model):
    """Recognize common image-generation model names, including vendor prefixes."""
    value = str(model or "").strip().lower()
    leaf = value.rsplit("/", 1)[-1]
    if "embed" in leaf:
        return False
    return _IMAGE_MODEL_MARKER.search(leaf) is not None


def is_model_rejection_response(status_code, body):
    """Recognize explicit model capability failures, including model-specific 403s."""
    if status_code not in (400, 403, 404, 422) or not body:
        return False
    try:
        payload = json.loads(body)
    except (ValueError, TypeError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict):
        return False

    error = payload.get("error", payload)
    stack = [error]
    messages = []
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    stack.append(item)
                elif isinstance(item, str):
                    normalized = re.sub(r"[\s-]+", "_", item.strip().lower())
                    if key.lower() in ("code", "type", "reason") and normalized in MODEL_REJECTION_CODES:
                        return True
                    if key.lower() in ("message", "detail", "error_description"):
                        messages.append(item)
        elif isinstance(value, list):
            stack.extend(value)
    return any(MODEL_REJECTION_MESSAGE.search(message) for message in messages)
