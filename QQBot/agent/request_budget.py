"""Conservative request projection; never modifies persisted history."""
import json
from .special_session import estimate_text_tokens


class ContextBudgetError(ValueError):
    pass


def request_cost(messages, tools):
    # Include roles, call arguments and schemas, not merely content strings.
    cost = estimate_text_tokens(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False))
    for m in messages:
        if isinstance(m.get("content"), list):
            for part in m["content"]:
                if part.get("type") == "image_url":
                    cost += 16384  # conservative configurable-model fallback
                elif part.get("type") not in ("text",):
                    raise ContextBudgetError("此多模态输入暂不支持可靠预算，请转为文件或文本。")
    return cost


def fit_request(messages, tools, window=128000, output_reserve=8192):
    budget = max(0, int(window) - int(output_reserve) - max(1024, int(window) // 20))
    result = list(messages)
    # Keep the latest user request and its entire ongoing tool exchange.
    last_user = max((i for i, m in enumerate(result) if m.get("role") == "user"), default=0)
    while request_cost(result, tools) > budget:
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
