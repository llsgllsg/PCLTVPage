#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import time
from datetime import datetime

import requests

API_BASE = "https://uapis.cn/api/v1"
ENDPOINT = API_BASE + "/misc/movie-rating-rank"

DEFAULT_TIMEOUT = 60        
DEFAULT_MAX_RETRIES = 3     
DEFAULT_TTL = 30 * 60       
CACHE_DIR = "cache"

CHANNELS = ("all", "tv", "web", "cinema")
PERIODS = ("realtime", "day", "week", "month")
DATE_FMT = "%Y-%m-%d"
MIN_LIMIT, MAX_LIMIT = 1, 100

_RETRY_STATUS = {429, 500, 502, 503, 504}

HEADERS = {
    "User-Agent": "TVPage/1.0 (PCL2 homepage; +https://uapis.cn)",
}


class UapiError(Exception):

    def __init__(self, code, message, status):
        super().__init__(message or f"HTTP {status}")
        self.code = code
        self.message = message
        self.status = status


def get_api_key() -> str | None:
    key = os.environ.get("UAPI_API_KEY", "").strip()
    return key or None


def validate_params(channel: str = "all", platform: str | None = None,
                    limit: int = 10, period: str = "realtime",
                    date: str | None = None):
    if channel not in CHANNELS:
        raise ValueError(f"channel 必须是 {list(CHANNELS)} 之一, 收到: {channel!r}")
    if period not in PERIODS:
        raise ValueError(f"period 必须是 {list(PERIODS)} 之一, 收到: {period!r}")

    if isinstance(limit, bool) or not isinstance(limit, int):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ValueError(f"limit 必须是整数, 收到: {limit!r}")
    if not (MIN_LIMIT <= limit <= MAX_LIMIT):
        raise ValueError(f"limit 必须在 {MIN_LIMIT}~{MAX_LIMIT} 之间, 收到: {limit}")

    if date is not None:
        try:
            datetime.strptime(date, DATE_FMT)
        except ValueError:
            raise ValueError(f"date 格式必须是 YYYY-MM-DD, 收到: {date!r}")
    if period != "realtime" and not date:
        raise ValueError(f"period={period} 时必须提供 date(YYYY-MM-DD)")

    return channel, platform, limit, period, date


def _normalize(data: dict) -> dict:

    groups = []

    if isinstance(data.get("groups"), list):
        for g in data["groups"]:
            items = g.get("list")
            if not isinstance(items, list):
                items = []
            groups.append({
                "channel": g.get("channel"),
                "channel_desc": g.get("channel_desc"),
                "metric_label": g.get("metric_label") or g.get("metric_type"),
                "items": [dict(it) for it in items],
            })
    elif isinstance(data.get("channels"), list):
        for c in data["channels"]:
            items = c.get("items")
            if not isinstance(items, list):
                items = []
            normalized_items = []
            for it in items:
                normalized_items.append({
                    "rank": it.get("rank"),
                    "name": it.get("title") or it.get("name"),
                    "channel": it.get("platform") or c.get("platform"),
                    "metric": it.get("score"),
                    "metric_rate": it.get("hot_value"),
                    "detail_url": it.get("detail_url"),
                })
            groups.append({
                "channel": c.get("channel"),
                "channel_desc": c.get("platform"),
                "metric_label": "评分",
                "items": normalized_items,
            })

    return {
        "period": data.get("period") or "realtime",
        "date": data.get("date"),
        "groups": groups,
    }




def _cache_file(query: dict) -> str:
    key = "_".join(f"{k}:{v}" for k, v in query.items())
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in key)
    return os.path.join(CACHE_DIR, f"rank_{safe}.json")


def _load_cache(path: str, ttl: int):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)
        if time.time() - entry.get("ts", 0) < ttl:
            return entry.get("data")
    except Exception:
        pass
    return None


def _save_cache(path: str, data: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "data": data}, f, ensure_ascii=False)
    except Exception:
        pass



def _retry_after(resp) -> float | None:
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return min(max(float(ra), 0.0), 30.0)
        except ValueError:
            pass
    return None


def _extract_error(resp) -> tuple[str, str]:
    try:
        body = resp.json()
        code = body.get("code") or f"HTTP_{resp.status_code}"
        return code, body.get("message") or ""
    except Exception:
        text = (resp.text or "").strip().replace("\n", " ")
        return f"HTTP_{resp.status_code}", text[:200] or resp.reason


def get_movie_rating_rank(channel: str = "all", platform: str | None = None,
                          limit: int = 10, period: str = "realtime",
                          date: str | None = None, api_key: str | None = None,
                          timeout: float = DEFAULT_TIMEOUT,
                          max_retries: int = DEFAULT_MAX_RETRIES,
                          use_cache: bool = True,
                          ttl: float = DEFAULT_TTL) -> dict:

    channel, platform, limit, period, date = validate_params(
        channel=channel, platform=platform, limit=limit, period=period, date=date)

    query = {"channel": channel, "limit": limit, "period": period}
    if platform:
        query["platform"] = platform
    if date:
        query["date"] = date

    if use_cache:
        path = _cache_file(query)
        cached = _load_cache(path, ttl)
        if cached is not None:
            return cached

    key = api_key if api_key is not None else get_api_key()
    headers = dict(HEADERS)
    if key:
        headers["Authorization"] = f"Bearer {key}"

    backoff = 1.0
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(ENDPOINT, params=query, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < max_retries:  # 网络异常: 退避后重试
                time.sleep(backoff)
                backoff *= 2
                continue
            raise UapiError(None, f"网络请求失败: {exc}", None) from exc

        if resp.status_code == 200:
            data = _normalize(resp.json())
            if use_cache:
                _save_cache(path, data)
            return data

        if resp.status_code in _RETRY_STATUS and attempt < max_retries:
            delay = _retry_after(resp) or backoff
            time.sleep(delay)
            backoff *= 2
            continue

        code, message = _extract_error(resp)
        raise UapiError(code, message, resp.status_code)

    raise UapiError(None, f"多次重试后仍失败: {last_exc}", None)


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    d = get_movie_rating_rank(limit=5)
    print(json.dumps(d, ensure_ascii=False, indent=2))
