"""Public Lucima help shared by QQ commands and the agent."""

import os
from html import escape


MANUAL_PATH = "/manual"
MANUAL_TITLE = "Lucima QQ 使用手册"
MANUAL_COMMAND = "/lucima 使用手册"
MANUAL_ALIASES = ("/璐茜玛 使用手册", "/璐西玛 使用手册", "/露西玛 使用手册")
MANUAL_ACCESS_NOTE = "在群里先 @ 机器人，私聊可以直接发命令。阅读手册无需登录游戏。"
MANUAL_TOPICS = {
    "登录": (
        "私聊发送 /lucima 登录。在群里先 @ 机器人。打开机器人发来的二维码或网页登录链接，"
        "按网页提示在发起登录的会话发送核对命令。群里的核对命令由发起人发送，密码只在网页填写。\n"
        "临时授权有效 15 分钟，选择「记住登录」可保存最长 30 天，并用于每日托管。"
        "已保存授权失效时可重新扫码。授权成功后可以用 /lucima 刷新信息 登录游戏查看快照。\n"
        "从大厅的「切换账号」可添加或切换本会话的账号，也可发送 /lucima 切换账号 2。"
        "同一游戏账号只能绑定一个 QQ 会话的一个账号槽。改到其他群或私聊使用前，先在原会话发送 /lucima 解绑 并确认。"
        "退出当前账号可在大厅操作，退出后该账号的托管暂停。"
    ),
    "查询": (
        "/lucima 刷新信息　刷新账号状态\n"
        "/lucima 查看资源　查看体力、钻石等资源\n"
        "/lucima 查看团员 1　查看团员列表，可在页码后加名称\n"
        "/lucima 查看物品 1　查看物品列表，可在页码后加名称\n"
        "/lucima 查看队伍 1　查看已保存的队伍和队伍 ID\n"
        "/lucima 查看团概况　查看佣兵团状态\n"
        "/lucima 查看团周任务　查看佣兵团周任务\n"
        "/lucima 筛装备 [套装名]　按副属性分数查看装备\n"
        "/lucima 图鉴 夏妮　查询公开角色图鉴，无需登录\n"
        "账号查询会临时连接游戏。在群里发起的查询结果会显示在原群。"
    ),
    "日常": (
        "从大厅进入「自动任务」可以找到日常领取入口，也可以直接发送下面的命令。\n"
        "/lucima 签到　检查并领取可领的签到奖励\n"
        "/lucima 领邮件　领取邮件附件\n"
        "/lucima 领基地　检查动力转换、成长药剂和升星水晶\n"
        "也可单独发送 /lucima 动力转换、/lucima 成长药剂、/lucima 升星水晶 或 /lucima 技能模块。\n"
        "其他常用命令有 /lucima 每日体力、/lucima 领派遣、/lucima 领成就、"
        "/lucima 免费招募、/lucima 团签到、/lucima 团周奖 和 /lucima 兑换 <兑换码>。"
        "领派遣只领取已完成的奖励。\n"
        "执行前核对确认卡，点「确认执行」或发送卡片上的 /璐茜玛 执行 <编号>。"
        "游戏冷却、开放时段、可领取额度等条件仍然适用。"
    ),
    "托管": (
        "网页授权时勾选「记住登录」。按账号和任务分别设置计划，例如 /lucima 托管 动力转换 20:00。"
        "核对确认卡后，每天北京时间 20:00 起可以排队执行一次。也可在「自动任务」里打开具体任务设置。\n"
        "/lucima 托管状态　查看当前账号的计划和最近托管回执\n"
        "/lucima 停止托管 动力转换　确认后停止该任务\n"
        "/lucima 停止托管　确认后停止当前账号的全部托管\n"
        "QQ 端按托管时间入队。网页授权、刷新信息和手动执行一次均不会自动运行其他任务。"
        "每日计划在设定时间后的一小时内参与排队。若执行时领取条件还未满足，本次跳过，"
        "达到条件时不会补跑。账号退出后托管暂停，授权失效时无法执行。"
    ),
    "商店": (
        "/lucima 刷新秘密商店 1 免费 目标=无　只刷新和筛选，免费额度可用时最多刷新一次\n"
        "/lucima 刷新秘密商店 <次数> 钻石 [方案名]　次数 1–200，确认前核对本次报价和扣款上限\n"
        "秘密商店手动刷新可按确认卡列出的目标自动购买普通道具。"
        "命令末尾可加 目标=招募契约,神秘契约 或 目标=无。\n"
        "/lucima 查看星源商店　查询星源商店刷新计数\n"
        "/lucima 刷新星源商店 <次数> <方案名>　按确认卡上的报价和条件执行，报价不可核实时不会提交\n"
        "商店命中装备后，先看回执中的装备档位、数量和价格，再按原结果卡选择购买或结束本轮。\n"
        "/lucima 长任务查询 商店　查询批量购买参考报价\n"
        "/lucima 批量购买 <商品ID:数量:货币ID:单价>　多项用空格隔开，确认前核对价格和总额\n"
        "/lucima 查看团店　查询团员商店的商品、价格与限购\n"
        "/lucima 购买团店 <商品ID> <数量> <货币ID> <单价>　按当前团员商店货架报价购买"
    ),
    "装备方案": (
        "装备方案按当前账号保存，可用于商店刷新筛选。在大厅进入「工具箱 → 装备过滤」，或发送以下命令。\n"
        '/lucima 保存装备方案 {"name":"速度戒指","slots":{"ring":[{"sets":["Speed"]}]}}　新建或导入方案\n'
        "/lucima 预览装备方案 速度戒指　查看当前背包中的命中结果\n"
        "/lucima 导出装备方案 速度戒指　导出 JSON 供复制和修改\n"
        "/lucima 复制装备方案 速度戒指 备用　复制现有方案\n"
        "/lucima 删除装备方案 备用　确认后删除该方案"
    ),
    "战斗": (
        "/lucima 讨伐关卡　查看已解锁的讨伐关卡\n"
        "/lucima 元素关卡　查看已解锁的元素探索关卡\n"
        "/lucima 查看幻境　查看虚拟幻境进度\n"
        "/lucima 幻境扫荡 <同步器上限>　发起扫荡确认\n"
        "/lucima 长任务查询 活动　查看活动。用 /lucima 查看队伍 1 确认队伍 ID\n"
        "/lucima 活动推进 <活动ID> <队伍ID> <最多关数> <体力上限>　按确认的范围推进活动\n"
        "/lucima 长任务查询 深渊　查看关卡\n"
        "/lucima 虚构深渊 <关卡ID> <上半角色列表> <下半角色列表>　发起单层挑战。"
        "队内角色 ID 用逗号分隔，空位填 -。\n"
        "也可从大厅查看竞技场、活动投票、装备活动和佣兵团援助等状态。详情页会标明 QQ 端支持的操作。"
    ),
    "回执": (
        "/lucima 状态　查看进度或排队状态\n"
        "/lucima 回执 [编号]　取回最近回执或指定回执\n"
        "/lucima 任务历史 [页码]　浏览当前账号的任务记录\n"
        "/lucima 取消　取消排队任务或请求停止正在执行的长任务。已提交给游戏的操作以实际结算为准。\n"
        "确认卡有效五分钟。群聊中由发起人在原群 @ 机器人发送卡片上的命令，或点原卡按钮。"
        "「本次没有可执行项目」表示本次任务跳过，例如动力转换的领取间隔未到。"
        "若收到「结果待核对」或「结果未知」，先查看游戏内记录，再决定是否重试。"
    ),
}

MANUAL_INDEX = (
    f"{MANUAL_TITLE}\n"
    "涵盖扫码授权、账号查询、日常领取、每日托管、商店和任务回执。\n"
    f"{MANUAL_ACCESS_NOTE}"
)

QUICK_START = (
    ("/lucima 大厅", "打开操作菜单"),
    ("/lucima 登录", "获取网页登录入口"),
    ("/lucima 状态", "查看任务进度"),
)


def is_manual_command(text: str) -> bool:
    return " ".join(text.split()).casefold() in (MANUAL_COMMAND, *MANUAL_ALIASES)


def manual_qq_reply() -> str:
    site = os.environ.get("BOT_PUBLIC_SITE_URL", "").strip().rstrip("/") or "https://bot.oneweblog.cn"
    return f"{MANUAL_INDEX}\n完整版\n{site}/lucima/"


def manual_text(topic: str = "总览") -> str:
    if topic == "总览":
        commands = "\n".join(f"{command}　{description}" for command, description in QUICK_START)
        return f"{MANUAL_INDEX}\n\n{commands}\n\n章节为 " + "、".join(MANUAL_TOPICS) + "。"
    if isinstance(topic, str) and topic in MANUAL_TOPICS:
        return f"{MANUAL_TITLE} · {topic}\n\n{MANUAL_TOPICS[topic]}"
    return "没有这个章节。可选章节为 " + "、".join(MANUAL_TOPICS) + "。"


def command_row(command: str, description: str) -> str:
    return f'<p class="command"><code>{escape(command)}</code><span>{escape(description)}</span></p>'


def render_manual(*, stylesheet: str = "/manual.css") -> str:
    links, sections = [], []
    for number, (topic, body) in enumerate(MANUAL_TOPICS.items(), start=1):
        anchor = f"topic-{number}"
        heading = escape(topic)
        links.append(f'<a href="#{anchor}">{heading}</a>')
        paragraphs = []
        for line in body.splitlines():
            command, separator, description = line.partition("　")
            if separator and command.startswith("/lucima "):
                paragraphs.append(command_row(command, description))
            else:
                paragraphs.append(f"<p>{escape(line)}</p>")
        sections.append(
            f'<section id="{anchor}"><h2>{heading}</h2>{"".join(paragraphs)}</section>'
        )
    quick_start = "".join(command_row(command, description) for command, description in QUICK_START)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{MANUAL_TITLE}</title>
  <link rel="stylesheet" href="{escape(stylesheet)}">
</head>
<body>
  <a class="skip" href="#content">跳到手册正文</a>
  <header class="intro" id="top">
    <h1>{MANUAL_TITLE}</h1>
    <p>{escape(MANUAL_ACCESS_NOTE)}</p>
  </header>
  <div class="layout">
    <nav aria-label="手册目录">{"".join(links)}</nav>
    <main id="content">
      <section><h2>快速开始</h2>{quick_start}</section>
      {"".join(sections)}
      <a class="back" href="#top">返回顶部</a>
    </main>
  </div>
</body>
</html>
"""
