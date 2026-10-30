"""Shadow 对照：同一张截图分别用「手工图标模板」与「解包数据源模板」检测状态图标，
把差异落盘，供切换默认模板源之前积累样本。

用法（在仓库根目录执行）：
    # 指定图片
    python scripts/shadow_status_compare.py --out shadow/2026-10-31.json img1.jpg img2.jpg
    # 指定目录（递归 .jpg/.png）
    python scripts/shadow_status_compare.py --dir ~/screenshots --out shadow/run.json
    # 用仓库内已有 fixture
    python scripts/shadow_status_compare.py --fixtures

产出 JSON 结构：
    {"generated_at": "...", "templates": {"manual": N, "gamedata": M},
     "summary": {"images": .., "rows": .., "agree": .., "manual_only": .., "gamedata_only": ..},
     "images": [{"file": .., "frame": [..], "rows": [
         {"name": .., "side": ..,
          "manual": {"ids": [..], "scores": {..}},
          "gamedata": {"ids": [..], "scores": {..}},
          "agree": bool,
          "expected_blocking": [..], "expected_advisory": [..],
          "blocking_ok": bool|None}]}]}
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "QQBot"))

ENV_FLAG = "GAMEDATA_STATUS_ICONS"
FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "QQBot", "images", "cal-speed-data")


def _load_deps():
    import cv2  # noqa: F401
    import numpy as np  # noqa: F401
    from PIL import Image  # noqa: F401
    return cv2, np, Image


def _detect(image_path: str, use_gamedata: bool) -> dict:
    """跑一遍检测，返回 {frame, rows:[{name?, side, bbox, ids, scores}]}。"""
    cv2, np, Image = _load_deps()
    from lib.ocr_engine import BuffDetector, detect_frame
    import tools.battle_parser as bp

    if use_gamedata:
        os.environ[ENV_FLAG] = "1"
    else:
        os.environ.pop(ENV_FLAG, None)

    img = Image.open(image_path).convert("RGB")
    frame = detect_frame(img)
    left, top, right, bottom = frame
    bands = bp._detect_row_bands(img, frame)
    rows, rows_for = [], []
    for i, bd in enumerate(bands):
        bbox = [[left + int((right - left) * 0.12), bd["y0"] + 4],
                [left + int((right - left) * 0.30), bd["y0"] + 4],
                [left + int((right - left) * 0.30), bd["y1"] - 4],
                [left + int((right - left) * 0.12), bd["y1"] - 4]]
        rows_for.append([{"bbox": bbox}])
        rows.append({"side": bd["side"], "band": [bd["y0"], bd["y1"]]})

    det = BuffDetector()
    img_bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    filters = bp._row_template_filters(
        [{"name": "", "bbox": r["band"] and rows_for[i][0]["bbox"]}
         for i, r in enumerate(rows)], det)
    out = det.detect_in_rows(img_bgr, rows_for, frame, template_filters=filters)
    for r, hits in zip(rows, out):
        r["raw"] = dict(hits or {})
    return {"frame": list(frame), "rows": rows, "source": det.source,
            "templates": len(det.template_names)}


def _to_ids(raw: dict) -> dict:
    try:
        from lib.gamedata_source import get_source
        src = get_source()
        if src.available:
            return {(src.resolve(k) or k): round(v, 3) for k, v in raw.items()}
    except Exception:
        pass
    return {k: round(v, 3) for k, v in raw.items()}


def _expected(name_hint: str) -> tuple[list, list]:
    """按行序猜不到角色名时返回空；真正期望值需要 OCR 角色名，这里仅在有线索时给。"""
    return [], []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="shadow_status_compare")
    ap.add_argument("images", nargs="*", help="截图路径（jpg/png）")
    ap.add_argument("--dir", help="递归扫描该目录下的 jpg/png")
    ap.add_argument("--fixtures", action="store_true", help="用仓库内 cal-speed-data 截图")
    ap.add_argument("--out", default=os.path.join("shadow", "shadow_report.json"))
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少张（0=不限）")
    args = ap.parse_args(argv)

    files = list(args.images)
    if args.dir:
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            files += glob.glob(os.path.join(args.dir, "**", ext), recursive=True)
    if args.fixtures:
        files += [p for p in glob.glob(os.path.join(FIXTURE_DIR, "*.jpg"))
                  + glob.glob(os.path.join(FIXTURE_DIR, "*.png"))
                  if "图标" not in os.path.basename(p)]
    files = sorted({os.path.abspath(f) for f in files if os.path.exists(f)})
    if args.limit > 0:
        files = files[: args.limit]
    if not files:
        print("没有可处理的图片（用 --images / --dir / --fixtures 指定）")
        return 2

    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "env_flag": f"{ENV_FLAG}=1 表示数据源模板",
              "summary": {"images": 0, "rows": 0, "agree": 0,
                          "manual_only": 0, "gamedata_only": 0, "both_empty": 0},
              "images": []}

    for path in files:
        try:
            man = _detect(path, use_gamedata=False)
            gd = _detect(path, use_gamedata=True)
        except Exception as e:
            print(f"[skip] {os.path.basename(path)}: {type(e).__name__}: {e}")
            continue
        entry = {"file": path, "frame": man["frame"],
                 "templates": {"manual": man["templates"], "gamedata": gd["templates"]},
                 "rows": []}
        for i, (rm, rg) in enumerate(zip(man["rows"], gd["rows"])):
            a, b = _to_ids(rm["raw"]), _to_ids(rg["raw"])
            sa, sb = set(a), set(b)
            entry["rows"].append({
                "index": i,
                "side": rm["side"],
                "band": rm["band"],
                "manual": {"ids": sorted(sa), "scores": a},
                "gamedata": {"ids": sorted(sb), "scores": b},
                "agree": sa == sb,
                "manual_only": sorted(sa - sb),
                "gamedata_only": sorted(sb - sa),
            })
            s = report["summary"]
            s["rows"] += 1
            if sa == sb:
                s["agree"] += 1
            if not sa and not sb:
                s["both_empty"] += 1
            s["manual_only"] += len(sa - sb)
            s["gamedata_only"] += len(sb - sa)
        report["summary"]["images"] += 1
        report["images"].append(entry)
        print(f"  {os.path.basename(path)}: {len(entry['rows'])} 行，"
              f"一致 {sum(1 for r in entry['rows'] if r['agree'])} 行")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    s = report["summary"]
    print(f"\n== 汇总 == 图片 {s['images']} / 行 {s['rows']} / 一致 {s['agree']} / "
          f"仅手工 {s['manual_only']} / 仅数据源 {s['gamedata_only']} / 双空 {s['both_empty']}")
    print(f"报告: {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
