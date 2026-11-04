"""System commands shared by private conversations and groups."""
from dataclasses import dataclass

@dataclass(frozen=True)
class PublicCommand:
    name: str
    handler: str
    usage: str
    aliases: tuple = ()
    renders_image: bool = False

PUBLIC_COMMANDS = (
    PublicCommand('/角色详情','_handle_character_detail_command','游戏角色名或别名',renders_image=True),
    PublicCommand('/羁绊详情','_handle_bond_detail_command','游戏羁绊名或别名',renders_image=True),
    PublicCommand('/兑换码','_handle_redeem_code_command','限时兑换码',('/redeem-code',)),
    PublicCommand('/长期兑换码','_handle_redeem_code_command','长期有效兑换码'),
    PublicCommand('/功能','_handle_features_command','功能一览',('/features',),True),
    PublicCommand('/更新日志','_handle_changelog_command','[条数]',('/更新记录','/update record','/update log','/changelog'),True),
)

# These handlers use private session state, alter personal preferences or operate
# the backend. Never invoke them with a group member's private user context.
PRIVATE_ONLY = frozenset("""
/邮件 /免责声明 /按钮探测 /流式探测 /刷新角色数据 /人格切换 /personality
/好感度 /bond /设好感 /剧情 /新会话 /切换会话 /会话列表 /会话 /重命名会话
/删除会话 /压缩会话 /保存为会话 /结束会话 /临时会话 /退出特殊会话 /退出会话
/记忆 /clear /管理工作区 /创建角色 /删除角色 /设形象 /形象 /清除形象 /反馈 /bug /建议
""".split())

async def dispatch_public(command,rest,user_id,*,allow_images=True):
    from . import turn
    handler=getattr(turn,command.handler)
    text=command.name+(' '+rest if rest else '')
    options={'allow_images':allow_images} if command.renders_image else {}
    return await handler(text,user_id,**options)
