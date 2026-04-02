# from nonebot import rule
# from nonebot.adapters.onebot.v11 import GroupMessageEvent
#
# ENABLED_GROUPS = ["718734404", "584221061"]
#
#
# def group_command_rule(prefixes: list = None):
#     """群聊命令规则"""
#     if prefixes is None:
#         prefixes = ["!", "/", " ", ""]
#
#     async def _prefix_rule(event: GroupMessageEvent) -> bool:
#         text = event.get_plaintext().strip()
#         return any(text.startswith(p) for p in prefixes)
#
#     async def _group_rule(event: GroupMessageEvent) -> bool:
#         return str(event.group_id) in ENABLED_GROUPS
#
#     return rule.Rule(_prefix_rule, _group_rule)