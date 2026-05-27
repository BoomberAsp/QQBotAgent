"""
Map & Location Tools — Powered by Amap (高德地图) Web Services API.

All tools are async and use the shared amap_client for HTTP calls.
"""
from lib.amap_client import _amap_get


# ── 1. Geocoding — address → coordinates ──────────────────────────

async def geocode(address: str, city: str = None) -> str:
    """Convert a text address into geographic coordinates.

    Args:
        address: Place name or address (e.g. "深圳南山科技园", "天安门").
        city: Optional city name to narrow the search scope.
    """
    if not address or not address.strip():
        return "[地图] 请提供要查询的地址。"

    params = {"address": address.strip()}
    if city and city.strip():
        params["city"] = city.strip()

    data, err = await _amap_get("/geocode/geo", params)
    if err:
        return err

    geocodes = data.get("geocodes", [])
    if not geocodes:
        return f"[地图] 未找到与 '{address}' 匹配的坐标。\n建议: 尝试更具体的地址或补充城市名。"

    lines = [f"'{address}' 的地理编码结果:"]
    for i, geo in enumerate(geocodes[:3]):
        loc = geo.get("location", "未知")
        formatted = geo.get("formatted_address", address)
        level = geo.get("level", "")
        level_hint = _level_label(level)
        lines.append(f"  {i + 1}. {formatted}\n     坐标: {loc}  {level_hint}")

    return "\n".join(lines)


# ── 2. Reverse Geocoding — coordinates → address ──────────────────

async def reverse_geocode(location: str) -> str:
    """Convert coordinates (longitude, latitude) into a human-readable address.

    Args:
        location: Coordinates in "lon,lat" format (e.g. "113.952,22.542").
    """
    if not location or not location.strip():
        return "[地图] 请提供坐标，格式为: 经度,纬度 (例如 113.952,22.542)"

    loc = location.strip()
    # Accept both "lon,lat" and "lon, lat" spacing
    if "," not in loc:
        return "[地图] 坐标格式错误。请使用 '经度,纬度' 格式 (例如 113.952,22.542)"

    params = {"location": loc.replace(" ", ""), "extensions": "base"}

    data, err = await _amap_get("/geocode/regeo", params)
    if err:
        return err

    regeo = data.get("regeocode", {})
    if not regeo:
        return f"[地图] 坐标 '{location}' 未匹配到地址。"

    formatted = regeo.get("formatted_address", "未知地址")
    addr_comp = regeo.get("addressComponent", {})

    # Build a readable response
    parts = [f"坐标 ({loc.replace(' ', '')}) 的逆地理编码结果:"]
    parts.append(f"  地址: {formatted}")

    # Nearby landmarks
    pois = regeo.get("pois", [])[:3]
    if pois:
        poi_names = "、".join(p.get("name", "") for p in pois if p.get("name"))
        if poi_names:
            parts.append(f"  附近: {poi_names}")

    # Administrative hierarchy
    comp_parts = []
    for key in ("province", "city", "district", "township"):
        val = addr_comp.get(key, "")
        if isinstance(val, str) and val and val != "[]":
            comp_parts.append(val)
    # streetNumber is a dict like {"street":"学苑大道","number":"1088号"}
    sn = addr_comp.get("streetNumber", {})
    if isinstance(sn, dict) and sn:
        street = sn.get("street", "")
        num = sn.get("number", "")
        if street or num:
            comp_parts.append(f"{street}{num}")
    if comp_parts:
        parts.append(f"  区域: {' '.join(comp_parts)}")

    return "\n".join(parts)


# ── 3. Weather — city weather (replaces search-based weather) ─────

async def get_weather(city: str, forecast: bool = False) -> str:
    """Query real-time or forecast weather for a city via Amap.

    Args:
        city: City name or adcode (e.g. "深圳", "440305", "北京").
        forecast: False = real-time (live), True = 4-day forecast.
    """
    if not city or not city.strip():
        return "[地图] 请提供城市名称或行政区划代码。"

    params = {
        "city": city.strip(),
        "extensions": "all" if forecast else "base",
    }

    data, err = await _amap_get("/weather/weatherInfo", params)
    if err:
        return err

    if forecast:
        forecasts = data.get("forecasts", [])
        if not forecasts:
            return f"[地图] 未找到 '{city}' 的天气预报。"

        lines = []
        for f in forecasts:
            lines.append(f"**{f.get('province', '')} {f.get('city', city)}** 天气预报:")
            casts = f.get("casts", [])
            for cast in casts[:4]:
                lines.append(
                    f"  {cast.get('date', '?')}  {cast.get('week', '?')}\n"
                    f"    白天: {cast.get('dayweather', '?')} {cast.get('daytemp', '?')}°C  "
                    f"{cast.get('daywind', '?')}风{cast.get('daypower', '?')}级\n"
                    f"    夜间: {cast.get('nightweather', '?')} {cast.get('nighttemp', '?')}°C  "
                    f"{cast.get('nightwind', '?')}风{cast.get('nightpower', '?')}级"
                )
        return "\n".join(lines)

    else:
        lives = data.get("lives", [])
        if not lives:
            return f"[地图] 未找到 '{city}' 的实时天气。"

        lines = []
        for live in lives:
            lines.append(
                f"**{live.get('province', '')} {live.get('city', city)}** 实时天气:\n"
                f"  天气: {live.get('weather', '?')}\n"
                f"  温度: {live.get('temperature', '?')}°C\n"
                f"  湿度: {live.get('humidity', '?')}%\n"
                f"  风向: {live.get('winddirection', '?')} "
                f"({live.get('windpower', '?')}级)\n"
                f"  发布时间: {live.get('reporttime', '?')}"
            )
        return "\n".join(lines)


# ── 4. POI Search — points of interest ────────────────────────────

async def search_poi(keywords: str, city: str = None, num_results: int = 5) -> str:
    """Search for places / POIs (restaurants, subway, banks, etc.).

    Args:
        keywords: Search keywords (e.g. "餐厅", "地铁站", "北京大学").
        city: Optional city name to limit the search scope.
        num_results: Number of results (max 10, default 5).
    """
    if not keywords or not keywords.strip():
        return "[地图] 请提供搜索关键词。"

    num = min(max(num_results, 1), 10)

    params = {
        "keywords": keywords.strip(),
        "offset": str(num),
        "extensions": "base",
    }
    if city and city.strip():
        params["city"] = city.strip()

    data, err = await _amap_get("/place/text", params)
    if err:
        return err

    pois = data.get("pois", [])
    if not pois:
        city_hint = f" ({city})" if city else ""
        return f"[地图] 未在{city_hint}找到与 '{keywords}' 相关的 POI。"

    lines = [f"搜索 '{keywords}' 的结果 ({len(pois)} 条):"]
    for i, poi in enumerate(pois[:num]):
        name = poi.get("name", "未知")
        addr = poi.get("address", "")
        loc = poi.get("location", "")
        tel = poi.get("tel", "")
        dist = poi.get("distance", "")
        biz_type = poi.get("type", "")

        extra = []
        if tel:
            extra.append(f"电话: {tel}")
        if dist:
            extra.append(f"距离: {dist}米")
        if biz_type:
            type_label = biz_type.split(";")[-1] if ";" in biz_type else biz_type
            extra.append(f"类型: {type_label}")

        extra_str = " | ".join(extra) if extra else ""
        lines.append(
            f"  {i + 1}. **{name}**\n"
            f"     地址: {addr or '未知'}\n"
            f"     坐标: {loc or '未知'}"
            + (f"\n     {extra_str}" if extra_str else "")
        )

    return "\n".join(lines)


# ── 5. Route Planning — driving / walking / transit ────────────────

async def plan_route(
    origin: str,
    destination: str,
    mode: str = "driving",
) -> str:
    """Calculate a route between two points.

    Args:
        origin: Starting point. Can be coordinates ("113.95,22.54") or address.
        destination: End point. Same format as origin.
        mode: Travel mode — "driving", "walking", or "transit" (公交).
    """
    if not origin or not origin.strip():
        return "[地图] 请提供起点坐标或地址。"
    if not destination or not destination.strip():
        return "[地图] 请提供终点坐标或地址。"

    orig = origin.strip()
    dest = destination.strip()

    mode_map = {
        "driving": "/direction/driving",
        "walking": "/direction/walking",
        "transit": "/direction/transit/integrated",
    }

    endpoint = mode_map.get(mode)
    if not endpoint:
        return f"[地图] 不支持的出行方式: {mode}。可选: driving, walking, transit"

    params = {
        "origin": orig.replace(" ", ""),
        "destination": dest.replace(" ", ""),
        "extensions": "base",
    }

    data, err = await _amap_get(endpoint, params)
    if err:
        return err

    route = data.get("route", {})
    if not route:
        return "[地图] 未找到可行路线。"

    paths = route.get("paths", [])
    if not paths:
        return f"[地图] 从 '{orig}' 到 '{dest}' 未找到可行路线。\n建议: 检查起终点是否在可达区域内。"

    mode_label = {"driving": "驾车", "walking": "步行", "transit": "公交"}.get(mode, mode)
    lines = [f"从 {orig} 到 {dest} 的{mode_label}路线:"]

    for i, path in enumerate(paths[:2]):
        dist_m = int(path.get("distance", 0))
        dur_s = int(path.get("duration", 0))

        if dist_m >= 1000:
            dist_str = f"{dist_m / 1000:.1f} 公里"
        else:
            dist_str = f"{int(dist_m)} 米"

        if dur_s >= 3600:
            dur_str = f"{int(dur_s // 3600)}小时{int((dur_s % 3600) // 60)}分钟"
        elif dur_s >= 60:
            dur_str = f"{int(dur_s // 60)}分钟"
        else:
            dur_str = f"{int(dur_s)}秒"

        lines.append(f"  路线 {i + 1}: {dist_str} / {dur_str}")

        # Steps summary (first 4)
        steps = path.get("steps", [])
        for step in steps[:4]:
            instruction = step.get("instruction", "").strip()
            step_dist = int(step.get("distance", 0))
            if instruction:
                if step_dist >= 1000:
                    lines.append(f"      {instruction} ({step_dist / 1000:.1f}km)")
                else:
                    lines.append(f"      {instruction}")

        if len(steps) > 4:
            lines.append(f"      ... 共 {len(steps)} 步")

        # Transit-specific: fare info
        if mode == "transit":
            cost = path.get("cost", 0)
            if cost:
                lines.append(f"    票价: {cost}元")

    return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────

def _level_label(level: str) -> str:
    """Human-readable label for geocode match level."""
    labels = {
        "country": "国家",
        "province": "省/直辖市",
        "city": "城市",
        "district": "区县",
        "street": "街道",
        "interest": "兴趣点",
        "business_area": "商圈",
    }
    label = labels.get(level, level)
    return f"[{label}]" if label else ""
