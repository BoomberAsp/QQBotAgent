"""
Task-plane delegation & revoicing — 双平面路由的执行核心。

Bond-System-Design.md §3.3（P0）：
- SOCIAL_TOOLS: 角色面工具白名单（仅 delegate_task 兜底；后续阶段追加
  好感查询/送礼/场景触发等社交工具）。
- run_delegated_task: delegate_task 工具本体（agent-as-tool 模式）。任务面以
  Roxy 中立人格、隔离上下文（persist=False）运行，权限按用户角色过滤，
  单层嵌套（_delegate_depth 守卫）。
- revoice_task_result: 任务面结果 → 角色口吻开场白 + 正文原样透传。
  私聊默认 LLM 转述（flash 小调用），群聊/模板模式用固定模板。

本模块不依赖 nonebot；所有协作对象经参数注入，可独立单测。
"""

import asyncio
import random
from typing import Any, Callable, Dict, List, Optional, Set

# 角色面（社交平面）工具白名单。注意：这些工具不出现在 permissions.py 的
# 角色工具集中——它们只通过路由层的 allowed_tools 下发给角色面，任务面/统一
# 人格的 allowed_tools 不含它们，_execute_tool_calls 的第二层硬拒绝即是兜底防线。
# delegate_task: 角色面把工具任务委托给任务面（agent-as-tool，需防递归）；
# give_gift: 送礼互动（P2），任务面 run_delegated_task 只减 delegate_task，
#   give_gift 本就不在角色工具集里，天然不会泄漏到任务面。
# complete_scene: 剧情收尾（P3a）——刻意不进 SOCIAL_TOOLS：只在 bond_scene
#   特殊会话的 allowed_tools={"complete_scene"} 里暴露，普通角色面/任务面
#   都不应看到它（全局注册仅为路由层可下发，二层硬拒绝兜底）。
# create_image / send_voice: 多模态生成（P4，§7）——角色面专属，不进全局
#   TOOLS.md/permissions（同 give_gift 决策：防污染任务面/unified 前缀，
#   二层硬拒绝兜底）。复合制作（画集/有声故事等）后续走 delegate_task。
# recall_asset: 找回影音记忆（P5，§8.2）——检索 asset_memory 文本卡并重发
#   二进制（或让角色凭文本描述已淡去的原件）。同为角色面专属。
SOCIAL_TOOLS: frozenset = frozenset(
    {"delegate_task", "give_gift", "create_image", "send_voice", "recall_asset"}
)

DEFAULT_TASK_TIMEOUT = 240.0

# LLM 转述失败时的模板开场白（群聊默认走模板，免 LLM 调用）
_TEMPLATE_OPENINGS = (
    "{name}帮你办好了，结果如下：",
    "锵锵——{name}把结果带回来了：",
    "{name}的任务完成报告：",
    "交给{name}的事情办妥了：",
)

# chat_completion 把 API 层错误当普通字符串返回，转述层须识别并降级模板
_LLM_ERROR_PREFIXES = (
    "思考超时", "响应超时", "调用DeepSeek API时出错", "API HTTP错误",
)


def template_opening(short_name: str) -> str:
    """Pick a fixed template opening line for the character."""
    return random.choice(_TEMPLATE_OPENINGS).format(name=short_name)


async def run_delegated_task(
    task: str,
    *,
    agent,
    model_router,
    perm_manager,
    user_id: str,
    group_id: str = "",
    disabled_tools: Optional[Set[str]] = None,
    progress_callback: Optional[Callable[[str], Any]] = None,
    timeout: float = DEFAULT_TASK_TIMEOUT,
) -> str:
    """Execute a tool task on the task plane and return the raw result.

    Called by the delegate_task tool closure in agent_router. Guarantees:
    - 单层嵌套：任务面内再调 delegate_task 会被硬拒绝；
    - 中立人格：任务面 L2 = assistant（全局共享缓存前缀）；
    - 上下文隔离：persist=False —— 不读 RP 历史、不写用户会话、不触发画像抽取；
    - 权限收敛：allowed = 用户角色工具集 − delegate_task − 群禁用工具；
    - contextvars 恢复：persona/bond/fold 在 finally 中还原，外层角色面
      循环不受污染。
    """
    from agent.context import (
        _delegate_depth, _current_personality, _current_bond_context,
        _pending_task_fold,
    )

    if not user_id:
        return "[Error] 缺少用户上下文，无法委派任务。"
    if not task or not task.strip():
        return "[Error] 任务描述为空。请在 task 参数中完整描述要执行的任务。"
    if _delegate_depth.get():
        return "[Error] 不允许嵌套调用 delegate_task（任务智能体不能再委派任务）。"

    role = perm_manager.get_role(user_id)
    allowed: Set[str] = set(perm_manager.get_allowed_tools(role)) - {"delegate_task"}
    if disabled_tools:
        allowed -= set(disabled_tools)
    # Story KB tools follow the *originating* persona, not the task plane's
    # neutral one: a character that is not allowlisted must not gain canon
    # access just by delegating.
    try:
        from lib.story_kb import story_personas
        if _current_personality.get() not in story_personas():
            allowed.discard("story_search")
    except Exception:  # noqa: BLE001
        pass

    tokens = (
        _delegate_depth.set(1),
        _current_personality.set("assistant"),   # 任务面 = Roxy 中立人格
        _current_bond_context.set(""),           # 任务面不注入羁绊状态
    )
    saved_fold = _pending_task_fold.get()
    _pending_task_fold.set(None)
    try:
        complexity = await model_router.classify_complexity(task)
        client = (
            model_router.flash_client if complexity == "simple"
            else model_router.reasoning_client
        )
        # NOTE (Direct Vision, future work): delegate_task 只转发 LLM 撰写的
        # task 文本，不附带本轮用户图片（agent.run 的 images= 参数）。任务面
        # 需要看图时可在 task 里写明图片路径，由 read_file 懒分析兜底；如需
        # 直传，把调用方的 vision_image_paths 透传进来即可。
        result = await asyncio.wait_for(
            agent.run(
                task.strip(), user_id,
                client=client,
                progress_callback=progress_callback,
                session_type="temporary",
                allowed_tools=allowed,
                user_role=role.value,
                persist=False,
            ),
            timeout=timeout,
        )
        if not result or not result.strip():
            return "[Error] 任务智能体返回了空结果，请告知用户任务未能完成。"
        return result
    except asyncio.TimeoutError:
        return f"[Error] 任务执行超时（{int(timeout)}秒），请让用户简化任务或稍后重试。"
    except Exception as e:
        return f"[Error] 任务执行失败: {type(e).__name__}: {e}"
    finally:
        _pending_task_fold.set(saved_fold)
        _delegate_depth.reset(tokens[0])
        _current_personality.reset(tokens[1])
        _current_bond_context.reset(tokens[2])


async def revoice_task_result(
    *,
    client,
    persona_content: str,
    short_name: str,
    user_request: str,
    response: str,
    use_llm: bool = True,
    timeout: float = 20.0,
) -> str:
    """Wrap a task-plane result in the character's voice.

    返回最终发送文本 = 角色口吻开场白 + "\\n\\n" + 任务结果正文。
    正文**原样透传**（Bond-System-Design.md §3.3）：转述 LLM 只看到前 200 字
    摘要且被要求只输出一句开场白，从结构上杜绝改写事实。
    LLM 失败/超时/输出不合格 → 降级模板开场白。
    """
    opening: Optional[str] = None
    if use_llm and client is not None and persona_content:
        try:
            system = (
                persona_content
                + "\n\n---\n你正在扮演上述角色。任务智能体（中立助手）刚替用户完成了一个任务，"
                  "完整结果将由系统原样附在你的开场白之后，你无需也不能复述或改写结果内容。"
            )
            prompt = (
                f"用户刚才的请求：{(user_request or '')[:200]}\n"
                f"任务结果开头：{(response or '')[:200]}\n\n"
                f"请以角色口吻写一句开场白，告诉用户结果已经拿到。"
                f"要求：不超过40字；不使用markdown；不复述结果细节；只输出这句话本身。"
            )
            raw = await client.chat_completion(
                message=prompt,
                history=[{"role": "system", "content": system}],
                timeout_set=timeout,
                purpose="revoice",
            )
            candidate = (raw or "").strip()
            if (
                candidate
                and not candidate.startswith(_LLM_ERROR_PREFIXES)
                and len(candidate) <= 120
                and "\n" not in candidate
            ):
                opening = candidate
        except Exception:
            opening = None
    if not opening:
        opening = template_opening(short_name)

    if not response:
        return opening
    return f"{opening}\n\n{response}"


def repatch_last_assistant(
    session_manager, user_id: str, raw_response: str, final_text: str
) -> bool:
    """Best-effort: rewrite the persisted assistant turn to the re-voiced text.

    仅当会话历史中的最后一条 assistant 消息与任务面原始输出**逐字一致**时
    才替换——若该轮已被 TaskRecord 折叠成紧凑记录（内容不同），保持折叠
    结果不动，避免破坏压缩收益。失败静默。
    """
    try:
        session = session_manager.get(user_id)
        if session is None:
            return False
        for msg in reversed(session.context):
            if msg.get("role") == "assistant":
                if msg.get("content") == raw_response:
                    msg["content"] = final_text
                    session_manager.update(user_id, session)
                    return True
                return False
    except Exception:
        return False
