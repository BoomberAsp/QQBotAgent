"""Short-lived group game dialogs keyed by the authenticated speaker and group."""
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from .group_state import identity_for, scope_for
from .group_reply import GroupReply

POOLS = ('常规招募','几率up招募','神秘招募','银河招募')
SPEED_FORMAT = '我方\n兔子 0 100 220\n敌方\n金人司阍 0 88'
GAMES = {'/抽卡','/十连抽','/测速','/乱速'}


@dataclass
class Dialog:
    game: str
    generation: int
    expires: float
    pool: str = ''
    images: list = field(default_factory=list)
    busy: bool = False


_pending = {}


async def dispatch_game(view,sender,store,command=None,argument=''):
    scope=scope_for(view)
    key=(scope,identity_for(view))
    now=time.monotonic()
    for expired in [k for k,d in _pending.items() if d.expires<=now]:
        _pending.pop(expired,None)
    dialog=_pending.get(key)
    if dialog is None and command not in GAMES:
        return False
    state=store.group(scope)
    if dialog and dialog.generation!=state['generation']:
        _pending.pop(key,None)
        dialog=None
    text=argument.strip() if command else view.text.strip()
    if not command and (view.mentioned or view.reply_to_bot):
        text=re.sub(r'^(?:<@!?[\w.-]+>|\[CQ:at,[^\]\r\n]+\])\s*','',text)
    if (command=='/取消' or (not command and text=='取消')) and dialog:
        _pending.pop(key,None)
        await GroupReply('游戏任务已取消').send(sender)
        return True
    if command in GAMES:
        dialog=Dialog(command,state['generation'],now+300)
        _pending[key]=dialog
    elif command or not dialog or dialog.busy:
        return False
    elif not (view.mentioned or view.reply_to_bot or state['config']['receive_all']):
        return False

    from tools import legacy_tools
    game=dialog.game
    parts=text.split()
    pair=re.fullmatch(r'(\d{1,5})[\s,，]+(\d{1,5})',text)
    pool=next((p for p in POOLS if parts and parts[0].lower() in (p,p.removesuffix('招募'))),None)
    image='image' in view.attachment_kinds or any(a.kind=='image' for a in view.attachments)
    template=text.splitlines()[:1] in (['我方'],['我方成员'],['我方角色'])
    up_reply=dialog.pool and text.startswith('UP ') and bool(text[3:].strip())
    if not command and not ((game=='/乱速' and pair) or
            (game=='/测速' and (image or template)) or
            (game in ('/抽卡','/十连抽') and (pool or up_reply))):
        return False

    dialog.busy=True
    finished=False
    rows=[]
    try:
        if game in ('/抽卡','/十连抽'):
            if not state['config']['features'].get('gacha',True):
                result='本群已关闭抽卡。';finished=True
            elif not pool and not up_reply:
                result='请选择卡池：'+'、'.join(POOLS)+'。UP池可在池名后附角色名。'
                rows=legacy_tools.gacha_pool_rows(ten=game=='/十连抽',as_command=True)
            else:
                pool=pool or dialog.pool
                name=text[3:].strip() if up_reply else ' '.join(parts[1:])
                if pool=='几率up招募' and not name:
                    dialog.pool=pool
                    result='请回复 UP 角色名，或重新选择卡池。'
                else:
                    result=await legacy_tools.gacha_pull(pool,10 if game=='/十连抽' else 1,name or None)
                    finished=True
        elif game=='/乱速':
            if pair and min(map(int,pair.groups()))>0:
                result=legacy_tools.compare_speed_probability(*map(int,pair.groups()))
                finished=True
            else:result='请回复两个正整数速度，例如 190 200。'
        elif template:
            result=legacy_tools.calculate_speed(text)
            finished=not result.startswith('[Speed')
        elif image:
            from .context import _current_user_workspace
            from tools.battle_parser import parse_battle_screenshots
            workspace=store.root/'workspaces'/hashlib.sha256(scope.encode()).hexdigest()
            workspace.mkdir(parents=True,exist_ok=True)
            token=_current_user_workspace.set(str(workspace))
            try:
                paths=[a.path for a in await view.load_attachments() if a.kind=='image' and not a.quoted and a.path
                       and Path(a.path).is_file() and Path(a.path).resolve().is_relative_to(workspace.resolve())]
                dialog.images.extend(paths[:2-len(dialog.images)])
                if len(dialog.images)<2:
                    result='已收到第一张，请再发送另一张跑条截图。' if dialog.images else '未读取到本人上传的截图，请重新上传。'
                else:
                    parsed=json.loads(await parse_battle_screenshots(dialog.images))
                    dialog.images.clear()
                    raw=parsed.get('raw_format')
                    result=('请校正以下数据，把至少一名我方角色末尾的 0 换为已知速度，再发回整段：\n'+raw
                            if raw and parsed.get('mode')!='single' else '未能识别两张有效截图，请重传或发送文字模板。')
            finally:_current_user_workspace.reset(token)
        else:result='请输入战斗数据，或上传跑条前后两张截图。文字示例：\n'+SPEED_FORMAT
    except Exception:
        result='游戏查询未能完成，请重新发起命令。'
        finished=True
    finally:
        dialog.busy=False
    if _pending.get(key) is dialog and dialog.generation==store.group(scope)['generation'] and dialog.expires>time.monotonic():
        if finished:_pending.pop(key,None)
        else:result+='\n5分钟内由本人补充，或发送 /取消。未开启非 @ 接收时请 @机器人补充。'
        await GroupReply(game[1:],[result],rows=rows).send(sender)
    return True
