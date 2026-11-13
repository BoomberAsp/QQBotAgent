"""Single group command registry, exact aliases and per-command authorization."""
import re
from dataclasses import dataclass,replace
from .group_state import scope_for,identity_for,Conflict
from .group_service import GroupService
from .group_reply import GroupReply
from .public_commands import PUBLIC_COMMANDS, PRIVATE_ONLY, dispatch_public

@dataclass(frozen=True)
class GroupCommand:
    name:str
    who:str='group_or_bot'
    usage:str=''
    aliases:tuple=()

COMMANDS=(
 GroupCommand('/抽卡','any','[卡池] [UP角色]，单抽',('/单抽',)),
 GroupCommand('/十连抽','any','[卡池] [UP角色]'),
 GroupCommand('/乱速','any','[速度1 速度2]',('/luansu',)),
 GroupCommand('/测速','any','[行动值模板或两张截图]',('/测个速','/cesu','/compute speed')),
 GroupCommand('/群状态','any','查看本群状态',('/status','/群信息','/groupinfo','/ginfo')),GroupCommand('/群帮助','any','查看命令',('/帮助','/help','/命令','/介绍','/hello','/Hello','/你是谁')),
 GroupCommand('/群人格','admin','内置名|默认',('/toggle personality',)),
 GroupCommand('/群面板','group_or_bot','打开管理卡片',('/toggle',)),
 GroupCommand('/群功能',usage='gacha|image|voice 开|关'),GroupCommand('/群参与',usage='仅召唤|跟随|自然'),
 GroupCommand('/群接收',usage='开|关'),GroupCommand('/群安静',usage='22:00-08:00|关'),
 GroupCommand('/群主动贴图',usage='开|关'),
 GroupCommand('/群记忆',usage='[添加 内容|清空 确认]'),GroupCommand('/群重置',usage='重置历史（确认后执行）'),
 GroupCommand('/群清除数据',usage='清除群历史、记忆和来源事实（确认后执行）'),
 GroupCommand('/取消','any','取消自己的当前任务',('/结束',)),GroupCommand('/群操作','any','操作令牌'),
 GroupCommand('/问卷答案','any','查看当前问卷草稿'),
 GroupCommand('/问卷详情','any','查看问卷审阅卡'),
 GroupCommand('/确认提交','any','提交当前问卷'),
 GroupCommand('/取消问卷','any','放弃当前问卷草稿',('/退出问卷',)),
 GroupCommand('/重试提交','any','[确认] 重试提交'),
 GroupCommand('/问卷选','any','<草稿> <题号> <选项号>'),
 GroupCommand('/问卷跳过','any','[草稿] [题号]'),
 GroupCommand('/问卷释放','admin','释放本群问卷锁'),
 GroupCommand('/问卷诊断','any','问卷命令诊断'),
)
COMMANDS += tuple(GroupCommand(c.name,'any',c.usage,c.aliases) for c in PUBLIC_COMMANDS)
IDENTITY_COMMANDS=('/绑定身份','/绑定群身份','/确认绑定','/解除身份绑定','/个人记忆','/记住','/删除记忆','/公开记忆')

# QQ can leave the mention in event.content even on a genuine group-@ event.
# Stripping a textual envelope classifies the message; it never grants permission.
_COMMAND_START = re.compile(
    r"^[\s\u200b\ufeff]*(?:(?:<@!?[\w.-]+>|\[CQ:at,[^\]\r\n]+\]|@[^\s/／＃#<>]+(?=\s))"
    r"[\s\u200b\ufeff]*)*([/／#＃])"
)

def command_text(text):
    match = _COMMAND_START.match(text)
    if match is None:
        return None
    return '/' + text[match.end():].strip()

def parse(text):
    text=text.strip()
    if text.startswith('#'):text='/'+text[1:]
    if text.startswith('/toggle '):
        parts=text.split()
        if len(parts)==3 and parts[1] in ('gacha','image','voice') and parts[2] in ('on','off'):
            return next(c for c in COMMANDS if c.name=='/群功能'),parts[1]+' '+('开' if parts[2]=='on' else '关'),True
    for prefix,command in sorted([(p,c) for c in COMMANDS for p in (c.name,*c.aliases)],key=lambda item:len(item[0]),reverse=True):
        if text==prefix or (text.startswith(prefix) and text[len(prefix):len(prefix)+1].isspace()):return command,text[len(prefix):].strip(),prefix.startswith('/toggle')
    return None,'',False

def authorized(who,role,actor_role):return who=='any' or role=='admin' or (who=='group_or_bot' and actor_role in ('owner','admin'))

def status(config):
    return [f"人格：{config['persona']}",f"参与：{dict(mention='仅召唤',follow='跟随',natural='自然')[config['mode']]}",
            '非 @ 上下文接收：'+('开启' if config['receive_all'] else '关闭'),
            '平台全量事件：'+{'unknown':'未知/尚未观测','on':'已观测到全量消息','off':'已关闭'}.get(config.get('platform_full_events','unknown'),'未知'),
            '群主需在机器人设置中开启全量推送；本地开关无法代替。',
            '安静时段：'+(config['quiet'] or '未设置'),
            '功能策略：'+ '、'.join(k+('开' if v else '关') for k,v in config['features'].items()),
            '功能策略开启不代表尚未接入的工具已可用；私人会话与邮箱请私聊。']

def operation_button(service,scope,actor,label,action,revision,callback=False):
    from lib.button_spec import Btn
    token=service.token(scope,actor,action,revision)
    return Btn(label=label,data='qb1:group.apply:'+token if callback else '/群操作 '+token,action='cb' if callback else 'cmd',reply=True,modal='确认清空本群历史？' if action.get('choice')=='clear' and callback else '')

def panel(service,scope,actor,role,state):
    from lib.button_spec import Btn
    rows=[];rev=state['config_revision'];c=state['config']
    for key in ('image','gacha','voice'):
        rows.append([operation_button(service,scope,actor,key+'开',dict(kind='patch',patch={'features':{key:True}}),rev),operation_button(service,scope,actor,key+'关',dict(kind='patch',patch={'features':{key:False}}),rev)])
    rows.append([Btn('参与模式','/群参与 ',enter=False,reply=True),Btn('接收开','/群接收 开',reply=True),Btn('接收关','/群接收 关',reply=True)])
    if role=='admin':rows.append([Btn('切换人格','/群人格 ',enter=False,reply=True),operation_button(service,scope,actor,'恢复默认',dict(kind='persona',patch={'persona':'默认','history':'keep'}),rev,True),operation_button(service,scope,actor,'关闭',dict(kind='close'),rev,True)])
    return GroupReply('群管理面板',status(c)+['/群功能 image|gacha|voice 开|关；/群参与 仅召唤|跟随|自然；/群接收 开|关', '/群安静 22:00-08:00|关；/群帮助 查看全部命令。']+([] if role=='admin' else ['人格修改需机器人管理员权限。']),rows)

def switch_reply(service,scope,actor,state):
    p=state['config'].get('persona_pending')
    if not p:return GroupReply('设置已更新',['人格未变化或已完成选择。'])
    rows=[[operation_button(service,scope,actor,'保留历史',dict(kind='history',operation=p['operation_id'],choice='keep'),state['config_revision'],True),operation_button(service,scope,actor,'清空历史',dict(kind='history',operation=p['operation_id'],choice='clear'),state['config_revision'],True)]]
    # Text fallback must remain actionable without keyboards.
    keep=service.token(scope,actor,dict(kind='history',operation=p['operation_id'],choice='keep'))
    clear=service.token(scope,actor,dict(kind='history',operation=p['operation_id'],choice='clear'))
    return GroupReply('已切换为 '+state['config']['persona'],['历史已保留并立即注入切换提示。10分钟内可选择；过期保留。','清空仅删除本群对话，不删除公共记忆、成员事实或好感。','保留：/群操作 '+keep,'清空：/群操作 '+clear],rows)

async def dispatch(view,sender,role,store):
    from . import group_runtime as runtime
    text = command_text(view.text)
    from .group_games import GAMES,dispatch_game
    if text is None:
        return await dispatch_game(view,sender,store)
    if not(view.mentioned or view.reply_to_bot):
        if text=='/取消' and store.group(scope_for(view))['config']['receive_all']:
            await dispatch_game(view,sender,store,'/取消')
        return True
    # Survey commands need the full view (group/scope/sender) — the generic
    # dispatch_public path only passes text+user_id and cannot start a draft.
    from . import survey_flow
    if survey_flow.is_survey_command(text):
        # command_text() already removed the <@openid>/CQ:at prefix that
        # QQ official group content keeps; pass that cleaned text on.
        if await survey_flow.try_handle(replace(view, text=text), sender):
            return True
        await GroupReply('问卷功能当前未启用',
                         ['请管理员开启 SURVEY_ENABLED=1 后重启机器人。']).send(sender)
        return True
    if text.partition(' ')[0] in IDENTITY_COMMANDS:
        await runtime.manage_identity(replace(view,text=text),sender);return True
    if text.split()[0].lower() in PRIVATE_ONLY:
        await GroupReply('此命令请在私聊中使用',['群内可用命令见 /群帮助。']).send(sender)
        return True
    cmd,rest,alias=parse(text)
    if cmd is None:
        await GroupReply('未知命令',['发送 /群帮助 查看本群可用命令。']).send(sender);return True
    if survey_flow.is_survey_command(cmd.name):
        # Defence in depth for registered survey commands: never let them
        # fall through to the generic service.patch() branch.
        rewritten=cmd.name+((' '+rest) if rest else '')
        if await survey_flow.try_handle(replace(view,text=rewritten), sender):
            return True
        await GroupReply('问卷功能当前未启用',
                         ['请管理员开启 SURVEY_ENABLED=1 后重启机器人。']).send(sender)
        return True
    if not authorized(cmd.who,role,view.actor_role):
        await GroupReply('权限不足',['此操作需要机器人管理员权限。' if cmd.who=='admin' else '此操作需要群主、群管理员或机器人管理员权限。']).send(sender);return True
    if cmd.name in GAMES or cmd.name=='/取消':
        if await dispatch_game(view,sender,store,cmd.name,rest):
            return True
    public=next((c for c in PUBLIC_COMMANDS if c.name==cmd.name),None)
    if public is not None:
        from .context import _turn_sender,_current_group_id
        token=_turn_sender.set(sender)
        group_token=_current_group_id.set(view.group_id)
        try:
            images=store.group(scope_for(view))['config']['features'].get('image',True)
            if not await dispatch_public(public,rest,view.user_id,allow_images=images):
                await GroupReply('命令参数有误',[cmd.name+' '+cmd.usage]).send(sender)
        finally:
            _current_group_id.reset(group_token)
            _turn_sender.reset(token)
        return True
    service=GroupService(store);scope=scope_for(view);actor=identity_for(view)
    effective='admin' if role=='admin' else 'group_or_bot' if view.actor_role in ('owner','admin') else 'any'
    service.expire_pending(scope);state=store.group(scope);rev=state['config_revision'];name=cmd.name
    reply=GroupReply('设置已更新')
    try:
        if name=='/群帮助':reply=GroupReply('群命令',[c.name+' '+c.usage+('（仅机管）' if c.who=='admin' else '') for c in COMMANDS if c.name!='/群操作']+['身份与个人事实：/绑定身份、/记住 我……；管理个人记忆请私聊。'])
        elif name=='/群状态':
            reply=GroupReply('本群状态',status(state['config']))
            if not service.personas.is_builtin(state['config']['persona']):reply.lines.append('配置人格无效，运行时使用assistant；请机器人管理员重新设置。')
        elif name=='/群面板':reply=panel(service,scope,actor,role,state)
        elif name=='/取消':
            job=runtime._jobs.get(scope)
            if job and (job[0]==actor or effective in ('admin','group_or_bot')):job[1].cancel();reply.lines=['已取消群任务。']
            else:reply.lines=['没有可由你取消的群任务。']
        elif name=='/群操作':
            changed=service.consume(scope,actor,rest,effective)
            reply=switch_reply(service,scope,actor,changed) if changed['config'].get('persona_pending') else GroupReply('操作已完成')
        elif name=='/群人格':
            changed=service.patch(scope,{'persona':rest,'history':'keep'},actor,effective,rev,prompt=True)
            reply=switch_reply(service,scope,actor,changed)
        elif name in ('/群重置','/群清除数据'):
            action=dict(kind='reset',clear=name=='/群清除数据');token=service.token(scope,actor,action)
            from lib.button_spec import Btn
            reply=GroupReply('请确认',['将清除群对话'+('、公共记忆及本群来源事实' if action['clear'] else '')+'；共享好感保留。','10分钟内发送 /群操作 '+token],[[Btn('确认操作','/群操作 '+token,reply=True)]])
        elif name=='/群记忆':
            if not rest:reply=GroupReply('群公共记忆',state['memory'] or ['暂无记录'])
            elif rest=='清空 确认':store.memory(scope,clear=True,expected=rev)
            elif rest.startswith('添加 '):store.memory(scope,'管理员确认：'+rest[3:].strip(),expected=rev)
            else:raise ValueError('用法：/群记忆 添加 内容 或 /群记忆 清空 确认')
        else:
            if name=='/群功能':
                feature,flag=rest.split();patch={'features':{feature:onoff(flag)}}
            elif name=='/群参与':patch={'mode':{'仅召唤':'mention','跟随':'follow','自然':'natural'}[rest]}
            elif name=='/群接收':patch={'receive_all':onoff(rest)}
            elif name=='/群主动贴图':patch={'proactive_stickers':onoff(rest)}
            elif name=='/群安静':patch={'quiet':'' if rest=='关' else rest}
            service.patch(scope,patch,actor,effective,rev)
    except (ValueError,KeyError,PermissionError,TypeError) as exc:
        reply=GroupReply('操作未完成',[str(exc) if isinstance(exc,(ValueError,PermissionError)) else '参数错误，请查看 /群帮助'])
    if alias:reply.lines.append('旧命令已更名：'+cmd.name+'；兼容期后将移除。')
    await reply.send(sender)
    return True

def onoff(value):
    if value not in ('开','关'):raise ValueError('请输入 开 或 关')
    return value=='开'
