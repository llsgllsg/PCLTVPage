#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
uapi_api.py — UAPI (uapis.cn) 电影收视排行接口客户端。

完整 API 地址: https://uapis.cn/api/v1/misc/movie-rating-rank
鉴权: 免费档无需密钥; 付费档在请求头 Authorization: Bearer <KEY> 中携带密钥,
      KEY 以 uapi- 开头, 从环境变量 UAPI_API_KEY 读取, 不硬编码、不拼进 URL。
      UAPI_API_KEY 未设置时, 以免费档调用(本接口实测无需密钥即可访问)。

用法(供 main.py 调用):
    data = uapi_api.get_movie_rating_rank(channel="all", limit=12, period="realtime")

返回结构(已归一化, 兼容线上真实返回与文档示例):
    {
      "period": "realtime",
      "date": "2026-08-09" | None,
      "groups": [
        {
          "channel": "tv",            # all/tv/web/cinema
          "channel_desc": "电视收视",
          "metric_label": "收视率",
          "items": [ { "rank": 1, "name": "...", "channel": "CCTV-6",
                       "metric": "17.7019%", "metric_rate": "2.8226%",
                       "detail_url": None } ],
        },
        ...
      ],
    }

边界处理:
    * 参数校验: channel/period 白名单、limit 范围、date 格式与必填
    * 请求超时 + 网络异常重试(指数退避)
    * 非 2xx: 解析 {"code","message"}; 400 INVALID_PARAMETER 直接报错,
      404 SNAPSHOT_NOT_FOUND 直接报错, 429/5xx 带退避重试(尊重 Retry-After)
    * 限流: 按接口建议平均请求保持在 40 次/分钟以内; 本模块默认带 30 分钟文件缓存,
      大幅降低调用频率
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

import requests

API_BASE = "https://uapis.cn/api/v1"
# 完整 API 地址整条照用(含 /api/v1 版本前缀), 不自行拼接
ENDPOINT = API_BASE + "/misc/movie-rating-rank"

DEFAULT_TIMEOUT = 60        # 秒(历史快照查询可能较慢)
DEFAULT_MAX_RETRIES = 3     # 失败重试次数(不含首次请求)
DEFAULT_TTL = 30 * 60       # 响应缓存 30 分钟
CACHE_DIR = "cache"

CHANNELS = ("all", "tv", "web", "cinema")
PERIODS = ("realtime", "day", "week", "month")
DATE_FMT = "%Y-%m-%d"
MIN_LIMIT, MAX_LIMIT = 1, 100

# 可重试的状态码: 限流与服务器瞬时错误
_RETRY_STATUS = {429, 500, 502, 503, 504}

HEADERS = {
    "User-Agent": "TVPage/1.0 (PCL2 homepage; +https://uapis.cn)",
}


class UapiError(Exception):
    """接口调用错误。code/message 来自接口返回体, status 为 HTTP 状态码。"""

    def __init__(self, code, message, status):
        super().__init__(message or f"HTTP {status}")
        self.code = code
        self.message = message
        self.status = status


def get_api_key() -> str | None:
    """从环境变量读取 API Key。未设置或为空时返回 None(以免费档调用)。"""
    key = os.environ.get("UAPI_API_KEY", "").strip()
    return key or None


def validate_params(channel: str = "all", platform: str | None = None,
                    limit: int = 10, period: str = "realtime",
                    date: str | None = None):
    """参数校验。非法参数抛出 ValueError, 否则返回规整后的元组。"""
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
    """把接口返回统一为固定结构。

    线上真实返回是 groups[].list[]; 若接口按文档示例改为 channels[].items[],
    这里也能兼容解析(文档示例字段: rank/title/score/hot_value)。
    """
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


# ----------------------------- 缓存 -----------------------------

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


# --------------------------- 请求与重试 ---------------------------

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
    """查询电影收视/热度/票房排行。返回归一化 dict(见模块 docstring)。

    参数与接口文档一一对应:
        channel  all/tv/web/cinema(默认 all)
        platform 按渠道或平台关键字过滤(如 卫视 / 爱奇艺)
        limit    每个渠道仅返回前 N 条(1~100)
        period   realtime/day/week/month(默认 realtime)
        date     历史快照日期 YYYY-MM-DD, period 为 day/week/month 时必填

    异常: ValueError(参数非法), UapiError(接口错误, 含 code/message/status)。
    """
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
            # 限流(429)或服务器瞬时错误(5xx): 指数退避, 尊重 Retry-After
            delay = _retry_after(resp) or backoff
            time.sleep(delay)
            backoff *= 2
            continue

        # 终态错误: 4xx(参数/快照不存在)或重试耗尽
        code, message = _extract_error(resp)
        raise UapiError(code, message, resp.status_code)

    raise UapiError(None, f"多次重试后仍失败: {last_exc}", None)


if __name__ == "__main__":
    # 直接运行: 打印一次真实返回, 方便调试/验证接口
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    d = get_movie_rating_rank(limit=5)
    print(json.dumps(d, ensure_ascii=False, indent=2))
