"""Vocabulary shared by sticker intake, review and reply selection."""
import re

USE_LABELS = {
    "comfort": "安慰", "celebrate": "庆祝", "bad_luck": "吐槽运气",
    "greeting": "问候", "thanks": "感谢", "surprise": "惊讶",
    "speechless": "无语", "surrender": "认输", "teasing": "接梗",
}
MOOD_USES = {
    "comfort": "comfort", "happy": "celebrate", "sad": "comfort",
    "greeting": "greeting", "thanks": "thanks", "surprised": "surprise",
    "tsundere": "teasing",
}
_PATTERNS = (
    ("bad_luck", r"又歪|歪了|抽卡翻车|沉船|保底吃满|全歪|非酋|吐槽运气"),
    ("comfort", r"拍拍|抱抱|安慰|难过|委屈|辛苦了|难受|不开心"),
    ("celebrate", r"终于过了|通关|恭喜|庆祝|拿下|赢了|出货|出金|上岸"),
    ("greeting", r"早上好|早安|晚安|晚上好|你好|问候"),
    ("thanks", r"感谢|谢谢|多谢|谢啦"),
    ("surprise", r"震惊|惊讶|真的假的|吓一跳"),
    ("speechless", r"无语|离谱|绷不住|无话可说"),
    ("surrender", r"认输|投降|甘拜下风|服了你"),
    ("teasing", r"接梗|玩梗|好耶|笑死|调侃"),
)


def infer_uses(text):
    return [use for use, cue in _PATTERNS if re.search(cue, text or "")]


def parse_uses(value):
    if isinstance(value, str):
        value = re.split(r"[,，、\s]+", value.strip()) if value.strip() else []
    if not isinstance(value, list):
        raise ValueError("用途标签格式错误")
    by_label = {label: key for key, label in USE_LABELS.items()}
    uses = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("用途标签格式错误")
        key = by_label.get(item, item)
        if key not in USE_LABELS:
            raise ValueError("可选用途：" + "、".join(USE_LABELS.values()))
        if key not in uses:
            uses.append(key)
    return uses


def asset_uses(asset):
    from_text = infer_uses(str(asset.get("description", "")))
    return set(asset.get("uses") or ()) | set(from_text) | {
        MOOD_USES[mood] for mood in (asset.get("mood") or ()) if mood in MOOD_USES
    }
