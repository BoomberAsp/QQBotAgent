"""
Amap (高德地图) Web Services API Client.

Provides geocoding, reverse geocoding, weather, POI search, and route planning.
Free tier: 5000 requests/day — more than enough for a personal QQ bot.

Docs: https://lbs.amap.com/api/webservice/summary
"""

import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import httpx

# Environment variable name for the API key
ENV_AMAP_KEY = "AMAP_API_KEY"
BASE_URL = "https://restapi.amap.com/v3"


def _get_api_key() -> str:
    """Read the Amap API key from NoneBot config, with fallbacks.

    NoneBot2 loads .env into its config object but does NOT push values
    to os.environ. We try config first (same approach as deepseek_client.py),
    then os.environ (for non-NoneBot contexts e.g. testing).
    """
    # 1. NoneBot config (primary — this is where .env values actually live)
    try:
        from nonebot import get_driver
        key = getattr(get_driver().config, ENV_AMAP_KEY, "").strip()
        if key:
            return key
    except Exception:
        pass

    # 2. os.environ (non-NoneBot contexts — testing / direct invocation)
    key = os.environ.get(ENV_AMAP_KEY, "").strip()
    if key:
        return key

    return ""


async def _amap_get(
    endpoint: str,
    params: Dict[str, str],
    timeout: float = 10.0,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Call an Amap API endpoint and return (data, error).

    Args:
        endpoint: API path, e.g. "/geocode/geo".
        params: Query parameters (api key is added automatically).
        timeout: Request timeout in seconds.

    Returns:
        (data_dict, None) on success, or (None, error_message) on failure.
    """
    api_key = _get_api_key()
    if not api_key:
        return None, (
            "[地图] 高德地图 API Key 未配置。\n"
            "请在 QQBot/.env 中添加: AMAP_API_KEY=<你的Key>\n"
            "注册地址: https://lbs.amap.com"
        )

    params["key"] = api_key

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}{endpoint}",
                params=params,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "1":
                info = data.get("info", "未知错误")
                return None, f"[地图] API 返回错误: {info}"

            return data, None

        except httpx.ConnectTimeout:
            return None, f"[地图] 连接超时 ({timeout}秒)"
        except httpx.ReadTimeout:
            return None, f"[地图] 响应超时 ({timeout}秒)"
        except httpx.HTTPStatusError as e:
            return None, f"[地图] HTTP 错误 ({e.response.status_code})"
        except Exception as e:
            return None, f"[地图] 请求异常: {e}"
