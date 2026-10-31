"""按 gamedata 重渲染 Ark Re:Code 角色技能卡（方案甲：gamedata 优先，wiki 补 gamedata 没有的字段）。

字段归属：
  来自 gamedata：name_cn/title/id/stars/element/class_cn/constellation/desc(签名)/
                 birthday/height/weight/skills[].name/des/cd/stats(全部 7 维)
  保留 wiki   ：skills[].multi/soul/focus/burst*、discs、team_pot/self_pot
  新增        ：gamedata 有、wiki 无的角色也会出卡（缺的字段留空/隐藏）

用法：
    python scripts/render_skill_cards.py --dry-run          # 只报告将重画/新建哪些
    python scripts/render_skill_cards.py --limit 3          # 只渲染前 3 张（抽查）
    python scripts/render_skill_cards.py                    # 全量
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

QQBOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, QQBOT)

CACHE = os.path.join(QQBOT, "data", "wiki_cache")
DETAILS = os.path.join(CACHE, "character_details.json")

# 元素/职业/星座：英文枚举 → 中文（由 wiki 与 gamedata 的 join 反推，不硬编码）
ELEMENT_MAP_HINT = {}
JOB_MAP_HINT = {}
CONST_HINT = {}


def _gamedata_dir() -> str:
    env = (os.environ.get("GAMEDATA_DIR") or "").strip()
    if env:
        return env
    root = (os.environ.get("USER_DATA_ROOT") or "").strip()
    if root:
        return os.path.join(root, "gamedata")
    return os.path.join(QQBOT, "..", "data", "gamedata")


def load_gamedata(gd_dir: str) -> dict:
    with open(os.path.join(gd_dir, "roles.json"), encoding="utf-8") as f:
        roles = json.load(f).get("roles") or {}
    skills = {}
    with open(os.path.join(gd_dir, "skills.json"), encoding="utf-8") as f:
        skills = json.load(f).get("skills") or {}
    grants = {}
    p = os.path.join(gd_dir, "skill_grants.json")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            grants = json.load(f) or {}
    stats_base = {}
    p = os.path.join(gd_dir, "stats_base.json")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            stats_base = (json.load(f) or {}).get("levels") or {}
    return {"roles": roles, "skills": skills, "grants": grants,
            "stats_base": stats_base}


def build_maps(roles: dict, wiki_entries: dict) -> None:
    """从 wiki 与 gamedata 的 join 反推「英文枚举 → 中文」，避免硬编码。"""
    for name, e in wiki_entries.items():
        rid = (e.get("id") or "").strip()
        g = roles.get(rid)
        if not g:
            continue
        if g.get("element") and e.get("element"):
            ELEMENT_MAP_HINT.setdefault(g["element"], e["element"])
        if g.get("job") and e.get("class_cn"):
            JOB_MAP_HINT.setdefault(g["job"], e["class_cn"])
        if g.get("constellation") and e.get("constellation"):
            CONST_HINT.setdefault(g["constellation"], e["constellation"])


def skill_des(gs: dict, sid: str) -> str:
    sk = gs.get(sid) or {}
    descs = sk.get("descriptions") or {}
    return descs.get("zh_hans") or descs.get("zh_hant") or descs.get("en") or ""


def build_entry(rid: str, g: dict, wiki: dict | None, gd: dict) -> dict:
    """组装 card_renderer 需要的 entry（字段名与 wiki 版保持一致）。"""
    names = g.get("names") or {}
    card = g.get("card") or {}
    entry = dict(wiki or {})                      # 以 wiki 为骨架（保留 multi/discs/pot）
    entry["id"] = rid
    entry["title"] = names.get("en") or (wiki or {}).get("title") or rid
    entry["name_cn"] = names.get("zh_hans") or (wiki or {}).get("name_cn") or rid
    entry["stars"] = g.get("star") or (wiki or {}).get("stars")
    entry["element"] = ELEMENT_MAP_HINT.get(g.get("element") or "", (wiki or {}).get("element"))
    entry["element_en"] = g.get("element") or (wiki or {}).get("element_en")
    entry["class_cn"] = JOB_MAP_HINT.get(g.get("job") or "", (wiki or {}).get("class_cn"))
    entry["class_en"] = g.get("job") or (wiki or {}).get("class_en")
    entry["constellation"] = CONST_HINT.get(g.get("constellation") or "",
                                            (wiki or {}).get("constellation"))
    # 简介：签名优先；没有就留空（卡面会隐藏该区块）
    entry["desc"] = card.get("signature") or ""
    entry["desc_source"] = "signature" if card.get("signature") else "none"
    entry["quote"] = card.get("quote") or ""
    for k in ("birthday", "height", "weight"):
        if card.get(k):
            entry[k] = card[k]
    # 数值：方案甲 —— gamedata 全 7 维；卡面沿用 wiki 的 4 键版式，多余 3 维进 stats_ext
    if g.get("stats"):
        s = g["stats"]

        def _fmt(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return v

        entry["stats"] = {
            "HP": _fmt(s.get("HP", "")), "ATK": _fmt(s.get("Attack", "")),
            "DEF": _fmt(s.get("Defence", "")), "SPD": _fmt(s.get("Speed", "")),
        }
        entry["stats_ext"] = {k: _fmt(s.get(k)) for k in
                              ("CriticalRate", "CriticalDamageRate", "EffectHitRate")
                              if k in s}
        # 面板(Lv.60) = 等级基础值 × 角色系数（渲染器读 stats_max，缺了就打印系数）
        base = (gd.get("stats_base") or {}).get("60") or {}
        if base:
            pairs = (("HP", "hp"), ("ATK", "atk"), ("DEF", "def"), ("SPD", "spd"))
            mx = {}
            for key, bkey in pairs:
                try:
                    mx[key] = round(float(base[bkey]) * float(s.get(
                        {"HP": "HP", "ATK": "Attack", "DEF": "Defence", "SPD": "Speed"}[key])))
                except (KeyError, TypeError, ValueError):
                    continue
            if mx:
                entry["stats_max"] = mx
    # 技能：gamedata 覆盖 名字/描述/CD，保留 wiki 的 multi/soul/focus/burst*
    gskills = g.get("skills") or []
    wskills = (wiki or {}).get("skills") or []
    merged = []
    for i, gsk in enumerate(gskills):
        sid = gsk.get("id") or ""
        w = wskills[i] if i < len(wskills) else {}
        item = dict(w)
        item["name"] = (gsk.get("name") or {}).get("zh_hans") or w.get("name") or ""
        item["name_en"] = (gsk.get("name") or {}).get("en") or w.get("name_en") or ""
        item["des"] = skill_des(gd["skills"], sid) or w.get("des") or ""
        cd = (gd["grants"].get(sid) or {}).get("cooldown")
        item["cd"] = str(cd) if cd is not None else w.get("cd", "")
        item["slot"] = gsk.get("slot")
        merged.append(item)
    if merged:
        entry["skills"] = merged
    # 指纹：数据源参与 → 已有卡会一次性重画
    payload = {k: v for k, v in entry.items() if k != "_card_hash"}
    payload["_renderer_source"] = "gamedata"
    entry["_card_hash"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return entry


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略指纹强制重画（换了立绘时用）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--gamedata", default="")
    args = ap.parse_args(argv)

    gd_dir = args.gamedata or _gamedata_dir()
    if not os.path.isdir(gd_dir):
        print(f"[错误] 找不到 gamedata 目录：{gd_dir}（设 GAMEDATA_DIR）")
        return 2
    gd = load_gamedata(gd_dir)
    roles = gd["roles"]
    with open(DETAILS, encoding="utf-8") as f:
        wiki_entries = (json.load(f) or {}).get("data") or {}
    wiki_by_id = {(e.get("id") or "").strip(): e for e in wiki_entries.values()}
    build_maps(roles, wiki_entries)

    print(f"gamedata: {len(roles)} 角色 / wiki: {len(wiki_by_id)} 角色")
    print(f"枚举映射: element {len(ELEMENT_MAP_HINT)} 项, job {len(JOB_MAP_HINT)} 项, "
          f"constellation {len(CONST_HINT)} 项")
    bad = [(k, v) for k, v in list(ELEMENT_MAP_HINT.items())]
    if len(JOB_MAP_HINT) and len(set(JOB_MAP_HINT.values())) != len(JOB_MAP_HINT):
        print("  [警告] job 枚举存在一对多映射，需人工裁决")

    from tools import card_renderer as cr

    todo = []
    for rid in sorted(roles):
        w = wiki_by_id.get(rid)
        et = build_entry(rid, roles[rid], w, gd)
        todo.append((rid, et, w is None))
    new_cards = [t for t in todo if t[2]]
    print(f"待处理 {len(todo)} 张：已有卡重画 {len(todo) - len(new_cards)} + 新建 {len(new_cards)}")
    print(f"  新建（gamedata 有、wiki 无）: {[t[0] for t in new_cards]}")

    if args.limit:
        todo = todo[: args.limit]
    renders, skipped, errors = 0, 0, []
    for rid, et, is_new in todo:
        out = cr.character_card_path(et)
        if args.dry_run:
            print(f"  [dry] {rid} {et['name_cn']} → {os.path.basename(out)}"
                  f"{'  (新建)' if is_new else ''}")
            continue
        try:
            before = os.path.isfile(out)
            if args.force:
                cr.render_character_card(et)
            else:
                cr.render_character_card_if_stale(et)
            if os.path.isfile(out) and (not before or os.path.getmtime(out) > 0):
                renders += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001
            errors.append((rid, f"{type(exc).__name__}: {exc}"))

    if not args.dry_run:
        print(f"渲染 {renders} 张，跳过 {skipped} 张，异常 {len(errors)} 个")
        for rid, err in errors[:8]:
            print(f"  [失败] {rid}: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
