"""Authenticated, versioned group callback path; no private button dispatch."""
import asyncio
from types import SimpleNamespace
from .group_state import scope_for,identity_for
from .group_service import GroupService
from .group_commands import switch_reply

async def handle(bot,event,user,group,data):
    from . import context,turn
    from .group_runtime import get_store
    from .button_actions import ActionResult
    token=context._current_platform.set('qq_official')
    try:
        role=turn._perm_manager.get_role(user).value
        if not data.startswith('qb1:group.apply:'):
            return ActionResult(1,message='旧群按钮已失效，请 @机器人 发送 /群面板 刷新。')
        op=data.removeprefix('qb1:group.apply:')
        if len(op)!=24 or any(c not in '0123456789abcdef' for c in op):return ActionResult(1,message='按钮数据无效。')
        if role!='admin':
            return ActionResult(5,message='请在群内 @机器人 发送 /群操作 '+op+'，通过消息事件重新验证管理权限。')
        view=SimpleNamespace(platform='qq_official',self_id=str(bot.self_id),group_id=group,user_id=user,is_group=True)
        service=GroupService(get_store());scope=scope_for(view);actor=identity_for(view)
        state=await asyncio.wait_for(asyncio.to_thread(service.consume,scope,actor,op,'admin'),2)
        reply=switch_reply(service,scope,actor,state) if state['config'].get('persona_pending') else None
        return ActionResult(0,message=reply.plain() if reply else '操作已完成。',rows=reply.rows if reply else None)
    except (ValueError,PermissionError):
        return ActionResult(1,message='操作已过期、配置已变化或权限不足，请刷新 /群面板。')
    except asyncio.TimeoutError:
        return ActionResult(1,message='操作结果待确认，请查看 /群状态，不要重复点击。')
    finally:context._current_platform.reset(token)
