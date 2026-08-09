#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — 生成 PCL2 影视排行主页 TVPage.xaml (数据源: UAPI 电影收视排行)。

调用: python main.py                # 默认实时榜, 每渠道前 12 条, 2 列
      python main.py --period week --date 2026-08-02   # 历史周榜
      python main.py --channel tv --limit 20           # 只看电视收视
      python main.py --fresh                          # 跳过缓存重新拉取
      python main.py --dry-run                        # 只打印数据, 不生成文件

密钥: 免费档无需密钥; 如需付费档, 设置环境变量 UAPI_API_KEY=<uapi-开头的密钥>,
      脚本会自动放入 Authorization: Bearer 请求头(见 uapi_api.py)。
"""
from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import argparse
import os
import time

import uapi_api

LABEL = "影视排行"
COLUMNS = 2          # 卡片列数
LIMIT = 12           # 每个渠道返回前 N 条
TEMPLATE_DIR = "templates"
OUTPUT_XAML = "TVPage.xaml"
OUTPUT_INI = "TVPage.xaml.ini"   # PCL2 用于判断页面是否更新的版本号

# channel → 中文描述(接口 channel_desc 缺失时的兜底)
CHANNEL_DESC = {
    "tv": "电视收视",
    "web": "网络平台",
    "cinema": "院线票房",
    "all": "全网",
}


def escape_xaml(text) -> str:
    if text is None:
        return ""
    text = str(text)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    text = text.replace("'", "&apos;")
    return text


def replaces(template: str, data: dict, no_escape_keys=None) -> str:
    if no_escape_keys is None:
        no_escape_keys = []
    for key, value in data.items():
        if key in no_escape_keys:
            template = template.replace("{" + key + "}", str(value))
        else:
            template = template.replace("{" + key + "}", escape_xaml(value))
    return template


def build_meta(group: dict, item: dict) -> str:
    """卡片副标题: 频道/平台 · 指标值 · 份额 · 上映信息。"""
    parts = []
    if item.get("channel"):
        parts.append(str(item["channel"]))
    label = group.get("metric_label")
    if label and item.get("metric") is not None:
        parts.append(f"{label} {item['metric']}")
    if item.get("metric_rate") is not None:
        parts.append(f"份额 {item['metric_rate']}")
    if item.get("release_info"):
        parts.append(str(item["release_info"]))
    return " · ".join(parts)


def build_buttons_xaml(item: dict) -> str:
    """卡片按钮: 有详情链接时加一个"查看详情"(打开网页)。"""
    url = item.get("detail_url")
    if not url:
        return ""
    return (
        f'<local:MyIconTextButton Height="30" HorizontalAlignment="Left" Margin="0,6,0,0" '
        f'Text="查看详情" EventType="打开网页" EventData="{escape_xaml(url)}" '
        f'LogoScale="0.9" '
        f'Logo="M14,3V5H17.59L7.76,14.83L9.17,16.24L19,6.41V10H21V3M19,19H5V5H12V3H5C3.89,3 3,3.9 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19V12H19V19Z" />'
    )


def build_message_xaml(text: str) -> str:
    """整页提示卡片(无数据 / 无快照时占位, 保证主页不是空白)。"""
    return (
        f'<local:MyCard Margin="5,10,5,5" UseAnimation="False">\n'
        f'    <Grid Margin="14,10">\n'
        f'        <TextBlock TextWrapping="Wrap" TextAlignment="Center" HorizontalAlignment="Center"\n'
        f'                   VerticalAlignment="Center" FontSize="14" FontWeight="Bold"\n'
        f'                   Foreground="{{DynamicResource ColorBrush6}}" Text="{escape_xaml(text)}" />\n'
        f'    </Grid>\n'
        f'</local:MyCard>'
    )


def build_group_grid(items: list, columns: int, rank_tpl: str, group: dict) -> str:
    """把一个渠道的若干条目渲染成 Grid 卡片阵列。"""
    if not items:
        return ""
    rows = (len(items) + columns - 1) // columns
    grid_columns = "".join(f'<ColumnDefinition Width="1*" />\n        ' for _ in range(columns))
    grid_rows = "".join(f'<RowDefinition Height="Auto" />\n        ' for _ in range(rows))

    cards = []
    for index, it in enumerate(items):
        data = {
            "row": index // columns,
            "column": index % columns,
            "rank": it.get("rank") or index + 1,
            "name": it.get("name") or "未知",
            "meta": build_meta(group, it),
            "buttons": build_buttons_xaml(it),
        }
        cards.append(replaces(rank_tpl, data, no_escape_keys=["buttons"]))

    block = "\n    ".join(cards)
    return (
        f"    <Grid>\n"
        f"        <Grid.RowDefinitions>\n"
        f"        {grid_rows}</Grid.RowDefinitions>\n"
        f"        <Grid.ColumnDefinitions>\n"
        f"        {grid_columns}</Grid.ColumnDefinitions>\n"
        f"        {block}\n"
        f"    </Grid>"
    )


def render_page(data: dict, args) -> str:
    """把归一化数据拼成完整 XAML 页面。"""
    def read_tpl(name: str) -> str:
        with open(os.path.join(TEMPLATE_DIR, name), "r", encoding="utf-8") as f:
            return f.read()

    header = read_tpl("header.xaml")
    label_tpl = read_tpl("label.xaml")
    sublabel_tpl = read_tpl("sublabel.xaml")
    rank_tpl = read_tpl("rank.xaml")
    footer = read_tpl("footer.xaml")

    main_label = replaces(label_tpl, {"label": LABEL})

    groups = data.get("groups") or []
    body_parts = []
    period_note = "实时榜" if data.get("period") == "realtime" else f"{data.get('period')}榜"
    if data.get("date"):
        period_note += f" {data['date']}"

    if not groups:
        body_parts.append(build_message_xaml(f"{period_note}暂无排行数据，请稍后刷新重试。"))
    else:
        for group in groups:
            items = group.get("items") or []
            if not items:
                continue
            desc = group.get("channel_desc") or CHANNEL_DESC.get(group.get("channel"), "排行")
            sub = replaces(sublabel_tpl, {"label": f"{desc} · {period_note} TOP{len(items)}"})
            grid = build_group_grid(items, args.columns, rank_tpl, group)
            body_parts.append(sub)
            body_parts.append(grid)

    return header + "\n" + main_label + "\n" + "\n".join(body_parts) + "\n" + footer


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="生成 PCL2 影视排行主页 TVPage.xaml (UAPI 电影收视排行)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例: python main.py --period week --date 2026-08-02",
    )
    p.add_argument("--limit", type=int, default=None, help=f"每个渠道仅返回前 N 条 (默认 {LIMIT})")
    p.add_argument("--columns", type=int, default=COLUMNS, help=f"卡片列数 (默认 {COLUMNS})")
    p.add_argument("--channel", choices=list(uapi_api.CHANNELS), default="all",
                   help="渠道: all/tv/web/cinema (默认 all)")
    p.add_argument("--platform", default=None, help="按渠道或平台关键字过滤, 如 卫视/爱奇艺")
    p.add_argument("--period", choices=list(uapi_api.PERIODS), default="realtime",
                   help="周期: realtime/day/week/month (默认 realtime)")
    p.add_argument("--date", default=None, help="历史快照日期 YYYY-MM-DD, period 为 day/week/month 时必填")
    p.add_argument("--fresh", action="store_true", help="跳过缓存, 强制重新请求接口")
    p.add_argument("--dry-run", action="store_true", help="只打印解析后的数据, 不生成文件")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    limit = args.limit or LIMIT

    try:
        data = uapi_api.get_movie_rating_rank(
            channel=args.channel, platform=args.platform, limit=limit,
            period=args.period, date=args.date, use_cache=not args.fresh,
        )
    except ValueError as exc:
        print(f"[错误] 参数不合法: {exc}")
        return 1
    except uapi_api.UapiError as exc:
        if exc.status == 404 or exc.code == "SNAPSHOT_NOT_FOUND":
            # 该日期/周期暂无历史快照: 生成带提示的占位页, 页面仍可正常打开
            print(f"[提示] {exc}")
            page = render_page({"period": args.period, "date": args.date, "groups": []}, args)
            write_output(page, dry=args.dry_run)
            print("[成功] 已生成占位页(指定日期/周期暂无历史快照)。")
            return 0
        print(f"[错误] 接口调用失败 (HTTP {exc.status} / {exc.code}): {exc.message}")
        print("  请检查网络、API Key(UAPI_API_KEY) 或稍后重试; 未生成新页面, 保留上次结果。")
        return 1

    if args.dry_run:
        print(f"period={data['period']} date={data.get('date')}")
        for group in data["groups"]:
            desc = group.get("channel_desc") or CHANNEL_DESC.get(group.get("channel"), "?")
            print(f"\n[{group.get('channel')}] {desc} ({len(group.get('items') or [])} 条)")
            for it in (group.get("items") or [])[:limit]:
                print(f"  #{it.get('rank'):>3} {it.get('name')} | {build_meta(group, it)}"
                      + (f" | {it.get('detail_url')}" if it.get("detail_url") else ""))
        print(f"\n[dry-run] 共 {len(data['groups'])} 个渠道, 未生成文件。")
        return 0

    page = render_page(data, args)
    write_output(page)
    count = sum(len(g.get("items") or []) for g in data["groups"])
    print(f"[成功] 已生成 {OUTPUT_XAML}, 渠道数: {len(data['groups'])}, 卡片数: {count}。")
    if not uapi_api.get_api_key():
        print("  (未设置 UAPI_API_KEY, 以免费档调用; 如需付费档请设置该环境变量)")
    return 0


def write_output(page: str, dry: bool = False) -> None:
    if dry:
        return
    with open(OUTPUT_XAML, "w", encoding="utf-8") as f:
        f.write(page)
    with open(OUTPUT_INI, "w", encoding="utf-8") as f:
        f.write(str(int(time.time())))


if __name__ == "__main__":
    sys.exit(main())
