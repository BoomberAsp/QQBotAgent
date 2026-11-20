"""Lucima desktop navigation expressed as reusable QQ page descriptions."""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .long_tasks import SCHEDULE_TASKS


SHOP_MAX = 200
THEMES = {"默认": "default", "亮色": "light", "暗色": "dark"}
THEME_NAMES = {value: name for name, value in THEMES.items()}


@dataclass(frozen=True)
class Page:
    title: str
    parent: str
    body: str
    links: tuple[tuple[str, str], ...] = ()


def page_link(label: str, target: str) -> tuple[str, str]:
    return label, "菜单 " + target


def page_links(*pairs):
    return tuple(page_link(*pair) for pair in pairs)


HOME_LINKS = page_links(("基本信息", "info"), ("自动任务", "auto"), ("主动任务", "manual"),
                        ("佣兵团", "guild"), ("工具箱", "tools"), ("运行日志", "logs"),
                        ("退出账号", "logout"), ("切换明暗", "theme"), ("切换账号", "accounts"))
PAGES = {
    "home": Page("璐茜玛大厅", "", "选择要进入的目录。", HOME_LINKS),
    "info": Page("基本信息", "home", "账号状态、资源、团员、物品和队伍。",
                 page_links(("账号状态", "profile"), ("资源", "resources"), ("团员", "heroes"),
                            ("物品", "items"), ("队伍", "teams"))),
    "profile": Page("账号状态", "info", "刷新后获取游戏快照并发送账号信息卡。",
                    (("刷新状态", "刷新信息"), ("扫码授权", "登录"))),
    "resources": Page("资源", "info", "查询后，资源数量会显示在当前会话。", (("查看资源", "查看资源"),)),
    "heroes": Page("团员", "info", "按页浏览。可输入 /璐茜玛 查看团员 <页码> [名称]。",
                   (("查看团员", "查看团员 1"),)),
    "items": Page("物品", "info", "按页浏览。可输入 /璐茜玛 查看物品 <页码> [名称]。",
                  (("查看物品", "查看物品 1"),)),
    "teams": Page("队伍", "info", "查看游戏中保存的队伍名称和 ID。", (("查看队伍", "查看队伍 1"),)),
    "auto": Page("自动任务", "home", "查看任务选项，设置托管或执行一次。",
                 page_links(("签到体力", "daily"), ("基地生产", "base"), ("日常循环", "routine"),
                            ("活动奖励", "rewards"), ("每日托管", "schedule"))),
    "daily": Page("签到与体力", "auto", "月历、月卡与活动签到、助战好友奖励和每日体力。",
                  page_links(("每日签到", "auto_signin"), ("助战好友", "auto_friend_support"), ("每日体力", "auto_send_meal"))),
    "base": Page("基地生产", "auto", "到点领取生产资源。", (
        *page_links(("动力转换", "auto_reactor"), ("成长药剂", "auto_potion"), ("升星水晶", "auto_starforce")),
        page_link("技能模块", "tesseract"))),
    "routine": Page("日常循环", "auto", "派遣奖励、免费商店、幻境、招募和竞技场。",
                    page_links(("派遣任务", "auto_dispatch"), ("免费商店", "auto_refresh_secret"), ("虚拟幻境", "abyss_sweep"),
                               ("免费招募", "auto_recruit"), ("竞技场", "arena"))),
    "rewards": Page("活动与奖励", "auto", "领取日／周成就、查看投票与兑换码。",
                    page_links(("成就奖励", "auto_achievement"), ("活动投票", "vote"), ("兑换码", "redeem"))),
    "redeem": Page("兑换码领取", "rewards", "发送 /璐茜玛 兑换 <兑换码>，核对后执行。"),
    "schedule": Page("每日托管", "auto",
                     "发送 /璐茜玛 托管 <任务> <HH:MM>。计划按北京时间运行，需记住登录。",
                     (("托管状态", "托管状态"), ("停止托管", "停止托管"))),
    "manual": Page("主动任务", "home", "选定任务后查看参数和结果。", page_links(("日常商店", "shops"), ("关卡战斗", "combat"))),
    "shops": Page("日常与商店", "manual", "领取邮件、查询货架或准备刷新。",
                  (("领邮件", "领邮件"), *page_links(("商店购买", "purchase"), ("秘密商店", "secret"),
                                                    ("星源商店", "starshop"), ("装备活动", "equip_event")))),
    "purchase": Page("商店购买", "shops",
                     "先查参考价，再发送 /璐茜玛 批量购买 <商品ID:数量:货币ID:单价>。多个商品用空格分隔。",
                     (("查询报价", "长任务查询 商店"),)),
    "secret": Page("秘密商店刷新", "shops",
                   "发送 /璐茜玛 刷新秘密商店 <次数> [免费|钻石] [方案名]。普通目标按金币价自动购买，装备命中时确认。",
                   (("免费 1 次", "刷新秘密商店 1 免费"), ("申请 5 次", "刷新秘密商店 5"))),
    "starshop": Page("星源商店", "shops", "星源商店免费刷新。请选装备筛选方案，命中后由本人按档确认，每件 5 个彩虹星源。",
                     (("查询刷新计数", "查看星源商店"),)),
    "combat": Page("关卡与战斗", "manual", "查看已解锁关卡和支持的执行方式。",
                   page_links(("讨伐扫荡", "hunt"), ("元素探索", "elf"), ("活动关卡", "activity"), ("虚构深渊", "abyss"))),
    "hunt": Page("讨伐扫荡", "combat",
                 "查看通关状态和体力标价。选已通关关卡后，补齐队伍、次数和体力上限确认扫荡。",
                 (("全部属性", "讨伐关卡"), *((element, f"讨伐关卡 {element}") for element in "火木水暗光"),
                  ("发起扫荡", "讨伐扫荡"))),
    "elf": Page("元素探索", "combat",
                "查看通关状态和体力标价。选已通关关卡后，补齐队伍、次数和体力上限确认扫荡。",
                (("全部属性", "元素关卡"), *((element, f"元素关卡 {element}") for element in "火木水暗光"),
                 ("发起扫荡", "元素扫荡"))),
    "activity": Page("活动关卡", "combat",
                     "查看可推进活动，选活动后填写队伍、关数和体力上限。已通关的当期关卡可选关扫荡。",
                     (("选择活动", "活动推进"), ("查看队伍", "查看队伍 1"), ("扫荡选关", "长任务查询 活动扫荡"))),
    "abyss": Page("虚构深渊", "combat",
                  "选可挑战楼层后，填写上下半阵容并确认单层挑战。队内角色 ID 用逗号分隔。",
                  (("选择楼层", "虚构深渊"), ("查看团员", "查看团员 1"))),
    "guild": Page("佣兵团", "home", "选择概况、自动或主动任务。",
                  page_links(("团概况", "guild_info"), ("团自动", "guild_auto"), ("团主动", "guild_manual"))),
    "guild_info": Page("团概况", "guild", "查看等级、成员数与签到状态。", (("刷新概况", "查看团概况"),)),
    "guild_auto": Page("佣兵团自动任务", "guild", "查看签到、捐赠、援助、商店和周奖励。",
                       (("团签到", "团签到"), page_link("自动捐赠", "guild_donate"), page_link("自动援助", "guild_aid"),
                        page_link("团员商店", "guild_store"), page_link("团长商店", "guild_captain"),
                        page_link("周奖励", "guild_weekly"))),
    "guild_manual": Page("佣兵团主动任务", "guild", "团员商店与符合条件的周任务奖励。",
                         page_links(("团员商店", "guild_store"), ("周奖励", "guild_weekly"))),
    "guild_weekly": Page("佣兵团周任务", "guild", "同步个人周任务及全团积分后，可确认领取奖励。",
                         (("查看周任务", "查看团周任务"), ("领周奖励", "团周奖"))),
    "guild_donate": Page("佣兵团捐赠", "guild_auto", "选择金币或英勇证明，发起单次捐赠确认。",
                         (("捐赠金币", "捐赠金币"), ("捐赠证明", "捐赠证明"))),
    "tools": Page("工具箱", "home", "装备、图鉴和运行辅助工具。",
                  page_links(("装备过滤", "filter"), ("图鉴查询", "catalog"), ("后台计时", "timer"), ("自动配装", "gear"))),
    "filter": Page("装备过滤器", "tools", "发送 /璐茜玛 筛装备 [套装名]，按副属性评分查看装备。",
                   (("筛选装备", "筛装备"),)),
    "filter_manage": Page("装备方案管理", "filter",
                          '用 /璐茜玛 保存装备方案 {"name":"方案名","slots":{"ring":[{"sets":["Speed"]}]}} 新建或导入。'
                          "\n复制用 /璐茜玛 复制装备方案 <原名> <新名>。删除用 /璐茜玛 删除装备方案 <名称>。"),
    "guild_store": Page("团员商店", "guild_manual",
                       "先查看货架，再发送 /璐茜玛 购买团店 <商品ID> <数量> <货币ID> <单价>。",
                       (("刷新货架", "查看团店"),)),
    "tesseract": Page("技能模块", "base", "检查并领取已完成的技能模块。", (("领取模块", "技能模块"),)),
    "abyss_sweep": Page("虚拟幻境", "routine",
                      "查询最高通关层和意识同步器。快速扫荡消耗全部同步器，体力消耗 0。",
                      (("快速扫荡", "幻境扫荡"),)),
    "arena": Page("竞技场 NPC", "routine", "查看 NPC 与冷却状态。QQ 端暂未接入挑战。", (("查看竞技场", "查看竞技场"),)),
    "vote": Page("活动投票", "rewards", "查看当前活动和选票。QQ 端暂未接入提交投票。", (("查看投票", "查看投票"),)),
    "equip_event": Page("装备活动刷新", "shops", "查看当前套装、部位和候选。QQ 端暂未接入活动配置。",
                        (("查看装备", "查看装备活动"),)),
    "guild_aid": Page("佣兵团援助", "guild_auto", "查看今日求助和援助奖励。QQ 端暂未接入配置与提交。",
                      (("查看援助", "查看援助"),)),
    "guild_captain": Page("团长商店", "guild_auto", "查看当前权限和商店报价。QQ 端暂未接入购买。",
                          (("查看报价", "查看团长店"),)),
    "timer": Page("后台计时测试", "tools", "测量桥接服务的计时偏差。", (("计时一次", "后台计时"),)),
    "catalog": Page("图鉴查询", "tools", "发送 /璐茜玛 图鉴 <角色名>。可直接查询公开图鉴。",
                    (("查询示例", "图鉴 夏妮"),)),
    "logs": Page("运行日志", "home", "当前账号的任务、历史记录和回执。",
                 (("当前任务", "状态"), ("任务历史", "任务历史 1"), ("查看回执", "回执"),
                  ("请求停止", "取消"), ("清空显示", "清空日志"))),
    "logout": Page("退出当前账号", "home", "退出会结束授权、暂停托管并取消尚未开始的任务。",
                   (("准备退出", "退出账号 确认"),)),
    "theme": Page("切换明暗", "home", "设置此会话生成的图片使用的主题。",
                  tuple((name, "主题 " + name) for name in THEMES)),
    "accounts": Page("切换账号", "home", "切换或添加当前会话中的游戏账号。"),
}

PAGES.update({
    key: Page(title, parent, body)
    for key, title, parent, body in (
        ("gear", "自动配装", "tools", "桌面版仍在预览。"),
    )
})

for action, parent in (
    ("signin", "daily"), ("friend_support", "daily"), ("send_meal", "daily"), ("reactor", "base"),
    ("potion", "base"), ("starforce", "base"), ("dispatch", "routine"), ("recruit", "routine"),
    ("refresh_secret", "routine"), ("achievement", "rewards"),
):
    title = SCHEDULE_TASKS[action]
    options = "派遣可领取已完成的奖励。" if action == "dispatch" else ""
    if action == "refresh_secret":
        options = "托管按免费额度刷新，自动购买 0 件。"
    if action == "send_meal":
        options = "游戏指定的体力投递窗口内执行。"
    PAGES["auto_" + action] = Page(title, parent,
        options + f"\n发送 /璐茜玛 托管 {title} <HH:MM> 设置每日执行时间。",
        (("执行一次", title), ("08:00", f"托管 {title} 08:00"), ("20:00", f"托管 {title} 20:00"),
         ("托管状态", "托管状态"), ("关闭托管", f"停止托管 {title}")))

NAV_NAMES = {PAGES[target].title: target for _, target in
             ((label, command.split()[1]) for label, command in HOME_LINKS)}
NAV_NAMES.update({"退出账号": "logout", "大厅": "home"})


def menu_data(page: str, account: str, summary: str = "", *,
              theme: str = "default", links: tuple[tuple[str, str], ...] = (), replace=False) -> dict:
    view = PAGES[page]
    parents = []
    cursor = view
    while cursor.parent and cursor.parent != "home":
        cursor = PAGES[cursor.parent]
        parents.append(cursor.title)
    path = " / ".join([*reversed(parents), view.title])
    title = "LucimaTools · " + path
    lines = [title, account, summary or view.body]
    buttons = (*(() if replace else view.links), *links)
    if view.parent:
        buttons += (page_link("返回", view.parent), page_link("大厅", "home"))
    return {"menu": "navigation", "message": "\n".join(filter(None, lines)),
            "theme": theme, "buttons": [list(item) for item in buttons]}


def shop_arguments(action: str, arg: str):
    words = unicodedata.normalize("NFKC", arg).split()
    if not words:
        return 1, "free" if action == "refresh_secret" else "star"
    if not re.fullmatch(r"[1-9][0-9]{0,2}", words[0]) or int(words[0]) > SHOP_MAX:
        return None
    count = int(words[0])
    if action == "refresh_star_source":
        return (count, "star") if len(words) == 1 else None
    if len(words) == 1:
        return count, "free" if count == 1 else "choose"
    mode = {"免费": "free", "钻石": "diamond", "free": "free", "diamond": "diamond"}.get(words[1])
    return (count, mode) if len(words) == 2 and mode else None
