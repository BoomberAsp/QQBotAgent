"""Read-only role catalogue tool for Ark Re:Code."""

import asyncio
import json

from lib.wiki_catalog import RELEASE_SORTS, WikiCatalog


async def list_characters(q="", element=None, job=None, release_status=None,
                          sort="release_desc", offset=0, limit=20):
    if sort not in RELEASE_SORTS:
        return "[Error] 排序方式仅支持 release_desc 和 release_asc。"
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        return "[Error] offset 必须为非负整数，limit 须在 1 到 50 之间。"
    if not isinstance(q, str):
        return "[Error] 搜索词须为字符串。"

    def query():
        return WikiCatalog().query(
            q=q, element=element, job=job, release_status=release_status,
            sort=sort, offset=offset, limit=limit,
        )

    try:
        result = await asyncio.to_thread(query)
    except ValueError as exc:
        return f"[Error] {exc}"
    fields = ("id", "name_cn", "elements", "job", "stars", "release_status", "release_date")
    return json.dumps({
        "matched": result["matched"], "next_offset": result["next_offset"],
        "items": [{field: item[field] for field in fields} for item in result["items"]],
    }, ensure_ascii=False)
