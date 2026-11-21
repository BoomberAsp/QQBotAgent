"""Conservative request projection; never modifies persisted history."""
import json
from urllib.parse import urlsplit

from .special_session import estimate_text_tokens


DEFAULT_IMAGE_TOKENS = 16384


class ContextBudgetError(ValueError):
    pass


def image_token_limit(model, api_base):
    """Conservative per-image bounds for the supported provider/model pairs."""
    if not isinstance(api_base, str):
        return DEFAULT_IMAGE_TOKENS

    try:
        host = urlsplit(api_base).hostname
    except ValueError:
        return DEFAULT_IMAGE_TOKENS
    if host == "api.deepseek.com" and model in (
        "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"
    ):
        # https://api-docs.deepseek.com/guides/vision/#token-usage
        return 1024
    if host == "dashscope.aliyuncs.com" and model == "qwen3.7-plus":
        # Model Studio allows up to 16,384 visual tokens plus two delimiters.
        return 16384 + 2
    return DEFAULT_IMAGE_TOKENS


def request_cost(messages, tools, *, image_tokens=DEFAULT_IMAGE_TOKENS):
    # Keep roles, tool schemas and call arguments in the text estimate.
    projected = []
    visual_cost = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            projected.append(message)
            continue
        parts = []
        for part in content:
            kind = part.get("type")
            if kind == "image_url":
                visual_cost += image_tokens
                image_url = part.get("image_url")
                if isinstance(image_url, dict) and "url" in image_url:
                    part = {**part, "image_url": {**image_url, "url": "<image>"}}
            elif kind != "text":
                raise ContextBudgetError("此多模态输入暂不支持可靠预算，请转为文件或文本。")
            parts.append(part)
        projected.append({**message, "content": parts})
    text_cost = estimate_text_tokens(json.dumps({"messages": projected, "tools": tools}, ensure_ascii=False))
    return text_cost + visual_cost


def fit_request(messages, tools, window=128000, output_reserve=8192, *,
                image_tokens=DEFAULT_IMAGE_TOKENS):
    budget = max(0, int(window) - int(output_reserve) - max(1024, int(window) // 20))
    result = list(messages)
    # Keep the latest user request and its entire ongoing tool exchange.
    last_user = max((i for i, m in enumerate(result) if m.get("role") == "user"), default=0)
    while request_cost(result, tools, image_tokens=image_tokens) > budget:
        start = next((i for i in range(last_user) if result[i].get("role") == "user"), None)
        if start is None:
            raise ContextBudgetError("当前输入、工具或必要上下文过长，请缩小输入或拆分任务。")
        end = next((i for i in range(start + 1, last_user + 1)
                    if result[i].get("role") == "user"), last_user)
        # Volatile system constraints survive trimming historical turns.
        result = result[:start] + [m for m in result[start:end] if m.get("role") == "system"] + result[end:]
        last_user = max(i for i, m in enumerate(result) if m.get("role") == "user")
    calls = set()
    for m in result:
        if m.get("role") == "assistant":
            if calls:
                raise ContextBudgetError("工具结果不完整，请重试当前任务。")
            calls = {c["id"] for c in m.get("tool_calls", [])}
        elif m.get("role") == "tool":
            cid = m.get("tool_call_id")
            if cid not in calls:
                raise ContextBudgetError("工具历史缺少对应调用，请重新发起任务。")
            calls.remove(cid)
        elif calls:
            raise ContextBudgetError("工具结果不完整，请重试当前任务。")
    if calls:
        raise ContextBudgetError("工具结果不完整，请重试当前任务。")
    return result
