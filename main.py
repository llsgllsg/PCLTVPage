#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — 生成 PCL2 影视排行主页 TVPage.xaml (数据源: UAPI 电影收视排行)。

布局:
    * 一个"影视占比"卡片: 电视收视 / 院线票房 两个环形图(头部 TOP5 相对占比)。
      环形图用 WPF Path 的 SVG arc 命令绘制(Data="M.. A.. Z"), 不需要额外命名空间。
    * 每个渠道一个"微博热搜"式排行卡片: 排名 + 剧集名 + 热度值, 挤在一个卡片里,
      不再每个剧集单独一张卡片。
    * 支持 SVG 是因为 PCL2 主页基于 WPF, Path 控件的 Data 接受 SVG path 语法。

调用: python main.py                # 默认实时榜, 每渠道前 12 条
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
import math
import os
import time
import urllib.parse

import uapi_api

LABEL = "影视排行"
LIMIT = 12
TEMPLATE_DIR = "templates"
OUTPUT_XAML = "TVPage.xaml"
OUTPUT_INI = "TVPage.xaml.ini"   # PCL2 用于判断页面是否更新的版本号

# ---------- 环形图参数 ----------
DONUT_TOP_N = 5          # 每张饼图展示头部 N 条(份额为归一化相对占比)
DONUT_SIZE = 150         # 画布边长
DONUT_R = 48             # 环半径(中线)
DONUT_STROKE = 22        # 环粗细
# 分类色: 校验过的 8 槽调色板, 固定顺序不循环(第 9 项起并入"其他")
PALETTE = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]

# channel → 中文描述(接口 channel_desc 缺失时的兜底)
CHANNEL_DESC = {
    "tv": "电视收视",
    "web": "网络平台",
    "cinema": "院线票房",
    "all": "全网",
}

# period → 中文(用于卡片标题, 如 "实时榜"/"日榜"/"周榜"/"月榜")
PERIOD_CN = {
    "realtime": "实时",
    "day": "日",
    "week": "周",
    "month": "月",
}

# 每行"搜索"按钮的搜索引擎(可改): 百度 / cn.bing / google 等
SEARCH_ENGINE = "https://www.baidu.com/s?wd="
# 搜索(放大镜)与链接图标(用于 MyIconButton)
SEARCH_ICON = "M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"
LINK_ICON = "M14,3V5H17.59L7.76,14.83L9.17,16.24L19,6.41V10H21V3M19,19H5V5H12V3H5C3.89,3 3,3.9 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19V12H19V19Z"


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


# ----------------------------- 环形图(饼图) -----------------------------

def parse_pct(value) -> float:
    """把 '2.8226%' / 2.8 解析成 float; 解析失败返回 0.0。"""
    if value is None:
        return 0.0
    s = str(value).strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return 0.0


def donut_slices(group: dict, top_n: int = DONUT_TOP_N) -> list[dict]:
    """取头部 N 条, 按 metric_rate(份额)归一化到 100%, 分配调色板颜色。"""
    items = group.get("items") or []
    chosen = items[:top_n]
    shares = [parse_pct(it.get("metric_rate")) for it in chosen]
    total = sum(shares)
    if total <= 0:
        return []
    slices = []
    for i, it in enumerate(chosen):
        slices.append({
            "name": it.get("name") or "未知",
            "pct": shares[i] / total * 100,
            "color": PALETTE[i % len(PALETTE)],
        })
    return slices


def arc_path(cx: float, cy: float, r: float, a1: float, a2: float) -> str:
    """从 a1 顺时针画到 a2(角度, 12 点方向为 0)的 WPF arc 路径数据(SVG 语法)。"""
    def pt(deg):
        rad = math.radians(deg)
        return (cx + r * math.sin(rad), cy - r * math.cos(rad))
    x1, y1 = pt(a1)
    x2, y2 = pt(a2)
    large = 1 if (a2 - a1) > 180 else 0
    return f"M {x1:.2f},{y1:.2f} A {r:.2f},{r:.2f} 0 {large},1 {x2:.2f},{y2:.2f}"


def build_donut_canvas(slices: list[dict]) -> str:
    """把若干扇区画成 Canvas 里叠放的 Path 弧段(环形图)。"""
    cx = cy = DONUT_SIZE / 2
    cur = 0.0
    gap = 1.5  # 扇区之间的间隔角度, 避免相邻弧段粘连
    paths = []
    for s in slices:
        angle = s["pct"] / 100 * 360
        if angle - gap < 0.5:
            a1, a2 = cur, cur + angle
        else:
            a1, a2 = cur + gap / 2, cur + angle - gap / 2
        data = arc_path(cx, cy, DONUT_R, a1, a2)
        if data:
            paths.append(
                f'<Path Data="{data}" Stroke="{s["color"]}" StrokeThickness="{DONUT_STROKE}" '
                f'StrokeStartLineCap="Flat" StrokeEndLineCap="Flat" Canvas.Left="0" Canvas.Top="0" />'
            )
        cur += angle
    if not paths:
        return ""
    return (
        f'<Canvas Width="{DONUT_SIZE}" Height="{DONUT_SIZE}">\n'
        + "\n".join(paths)
        + "\n</Canvas>"
    )


def build_donut_legend(group: dict, slices: list[dict]) -> str:
    """环形图右侧图例: 色块 + 剧集名 + 相对占比。文字用主题色, 不用系列色。"""
    desc = group.get("channel_desc") or CHANNEL_DESC.get(group.get("channel"), "排行")
    rows = []
    for s in slices:
        rows.append(
            '<StackPanel Orientation="Horizontal" Margin="0,3,0,3">\n'
            f'    <Rectangle Width="10" Height="10" RadiusX="2" RadiusY="2" Fill="{s["color"]}" VerticalAlignment="Center" />\n'
            f'    <TextBlock Text="{escape_xaml(s["name"])}" FontSize="12" '
            f'Foreground="{{DynamicResource ColorBrush2}}" Margin="6,0,0,0" '
            f'MaxWidth="110" TextTrimming="CharacterEllipsis" VerticalAlignment="Center" />\n'
            f'    <TextBlock Text="{s["pct"]:.1f}%" FontSize="12" FontWeight="Bold" '
            f'Foreground="{{DynamicResource ColorBrush4}}" Margin="8,0,0,0" VerticalAlignment="Center" />\n'
            "</StackPanel>"
        )
    return (
        '<StackPanel Margin="0,12,0,0" HorizontalAlignment="Center">\n'
        f'    <TextBlock Text="{escape_xaml(desc + " · 头部5名相对占比")}" FontSize="13" FontWeight="Bold" '
        f'Foreground="{{DynamicResource ColorBrush2}}" HorizontalAlignment="Center" Margin="0,0,0,6" />\n'
        + "\n".join(rows)
        + "\n</StackPanel>"
    )


def build_donut_block(group: dict, column: int) -> str:
    """一个渠道的"环形图 + 图例"块(图例竖排在环形图下方, 放在第 column 列)。

    采用教程「进阶: Grid 布局」的自动缩放方式: 外层 Grid 用星号列(1*),
    宽度随窗口自适应, 块内宽度 = 环形图与图例的较宽者, 小屏也不会溢出被裁剪。
    """
    slices = donut_slices(group)
    if not slices:
        return ""
    canvas = build_donut_canvas(slices)
    legend = build_donut_legend(group, slices)
    return (
        f'<StackPanel Grid.Column="{column}" HorizontalAlignment="Center" VerticalAlignment="Center">\n'
        + canvas + "\n"
        + legend + "\n"
        + "</StackPanel>"
    )


def build_donut_card(groups: list[dict]) -> str:
    """"影视占比"卡片: 各渠道环形图放两列星号 Grid, 随窗口宽度自适应。"""
    blocks = [build_donut_block(g, i) for i, g in enumerate(groups)]
    blocks = [b for b in blocks if b]
    if not blocks:
        return ""
    columns = "".join('<ColumnDefinition Width="1*" />\n            ' for _ in blocks)
    grid = (
        "    <Grid>\n"
        f"        <Grid.ColumnDefinitions>\n        {columns}</Grid.ColumnDefinitions>\n"
        + "\n".join("        " + b for b in blocks)
        + "\n    </Grid>"
    )
    return (
        '<local:MyCard Title="影视占比 · 头部TOP5" Margin="0,0,0,12" CanSwap="True" IsSwapped="False">\n'
        "    <StackPanel Margin=\"25,30,23,15\">\n"
        + grid + "\n"
        + "    </StackPanel>\n"
        + "</local:MyCard>"
    )


# --------------------------- 微博热搜式排行 ---------------------------

def format_metric(metric) -> str:
    """热度值显示: '13.5995%' → '13.60%', '2816.31万' 保持原样。"""
    if metric is None:
        return ""
    s = str(metric)
    if s.endswith("%"):
        try:
            return f"{float(s.rstrip('%')):.2f}%"
        except ValueError:
            return s
    return s


def build_row_buttons_xaml(item: dict) -> str:
    """行末按钮: 每行一个"搜索"(用搜索引擎查该剧集), 院线行额外加"猫眼详情"。"""
    title = item.get("name") or ""
    search_url = SEARCH_ENGINE + urllib.parse.quote(title)
    parts = [
        f'<local:MyIconButton Width="20" Height="20" Margin="10,0,0,0" Theme="Color" '
        f'VerticalAlignment="Center" ToolTip="搜索相关资讯" EventType="打开网页" '
        f'EventData="{escape_xaml(search_url)}" LogoScale="0.9" Logo="{SEARCH_ICON}" />'
    ]
    detail = item.get("detail_url")
    if detail:
        parts.append(
            f'<local:MyIconButton Width="20" Height="20" Margin="4,0,0,0" Theme="Color" '
            f'VerticalAlignment="Center" ToolTip="猫眼详情" EventType="打开网页" '
            f'EventData="{escape_xaml(detail)}" LogoScale="0.9" Logo="{LINK_ICON}" />'
        )
    return (
        '<StackPanel Orientation="Horizontal" Grid.Column="3" VerticalAlignment="Center">\n'
        + "\n".join(parts)
        + "\n</StackPanel>"
    )


def build_weibo_rows(items: list[dict]) -> str:
    """微博热搜式行: 排名 | 剧集名 | 热度值 | [搜索][详情]。前 3 名排名用主题强调色。"""
    rows = []
    for index, it in enumerate(items):
        rank = it.get("rank") or index + 1
        brush = "ColorBrush3" if rank <= 3 else "ColorBrush4"
        buttons = build_row_buttons_xaml(it)
        rows.append(
            '<Grid Margin="0,5,0,5">\n'
            "    <Grid.ColumnDefinitions>\n"
            '        <ColumnDefinition Width="36" />\n'
            '        <ColumnDefinition Width="*" />\n'
            '        <ColumnDefinition Width="Auto" />\n'
            '        <ColumnDefinition Width="Auto" />\n'
            "    </Grid.ColumnDefinitions>\n"
            f'    <TextBlock Grid.Column="0" Text="{rank}" FontSize="13" FontWeight="Bold" '
            f'Foreground="{{DynamicResource {brush}}}" VerticalAlignment="Center" />\n'
            f'    <TextBlock Grid.Column="1" Text="{escape_xaml(it.get("name") or "")}" FontSize="14" FontWeight="Bold" '
            f'Foreground="{{DynamicResource ColorBrush1}}" TextTrimming="CharacterEllipsis" '
            f'VerticalAlignment="Center" />\n'
            f'    <TextBlock Grid.Column="2" Text="{escape_xaml(format_metric(it.get("metric")))}" FontSize="12" FontWeight="Bold" '
            f'Foreground="{{DynamicResource ColorBrush4}}" VerticalAlignment="Center" />\n'
            f"    {buttons}\n"
            "</Grid>"
        )
    return "\n".join(rows)


def build_list_card(group: dict, period: str) -> str:
    """一个渠道的"微博热搜"式排行卡片(单个卡片内排满所有条目)。"""
    items = group.get("items") or []
    if not items:
        return ""
    desc = group.get("channel_desc") or CHANNEL_DESC.get(group.get("channel"), "排行")
    title = f"{desc} · {PERIOD_CN.get(period, period)}榜 TOP{len(items)}"
    body = build_weibo_rows(items)
    return (
        f'<local:MyCard Title="{escape_xaml(title)}" Margin="0,0,0,12" CanSwap="True" IsSwapped="False">\n'
        "    <StackPanel Margin=\"25,30,23,15\">\n"
        f"        {body}\n"
        "    </StackPanel>\n"
        + "</local:MyCard>"
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


def render_page(data: dict, args) -> str:
    """把归一化数据拼成完整 XAML 页面。"""
    def read_tpl(name: str) -> str:
        with open(os.path.join(TEMPLATE_DIR, name), "r", encoding="utf-8") as f:
            return f.read()

    header = read_tpl("header.xaml")
    label_tpl = read_tpl("label.xaml")
    footer = read_tpl("footer.xaml")

    main_label = replaces(label_tpl, {"label": LABEL})

    groups = data.get("groups") or []
    groups = [g for g in groups if g.get("items")]
    body_parts = []

    if not groups:
        period_note = "实时榜" if data.get("period") == "realtime" else f"{data.get('period')}榜"
        if data.get("date"):
            period_note += f" {data['date']}"
        body_parts.append(build_message_xaml(f"{period_note}暂无排行数据，请稍后刷新重试。"))
    else:
        body_parts.append(build_donut_card(groups))
        for g in groups:
            body_parts.append(build_list_card(g, data.get("period") or "realtime"))

    return header + "\n" + main_label + "\n" + "\n".join(body_parts) + "\n" + footer


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="生成 PCL2 影视排行主页 TVPage.xaml (UAPI 电影收视排行)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例: python main.py --period week --date 2026-08-02",
    )
    p.add_argument("--limit", type=int, default=None, help=f"每个渠道返回前 N 条 (默认 {LIMIT})")
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
                print(f"  #{it.get('rank'):>3} {it.get('name')} | {format_metric(it.get('metric'))} | 份额 {it.get('metric_rate')}"
                      + f" | 搜 {SEARCH_ENGINE}{urllib.parse.quote(str(it.get('name') or ''))}"
                      + (f" | 详情 {it.get('detail_url')}" if it.get("detail_url") else ""))
            slices = donut_slices(group)
            if slices:
                print("  饼图(头部5名相对占比): " + "  ".join(
                    f"{s['name']} {s['pct']:.1f}%" for s in slices))
        print(f"\n[dry-run] 共 {len(data['groups'])} 个渠道, 未生成文件。")
        return 0

    page = render_page(data, args)
    write_output(page)
    count = sum(len(g.get("items") or []) for g in data["groups"])
    print(f"[成功] 已生成 {OUTPUT_XAML}, 渠道数: {len(data['groups'])}, 条目数: {count}。")
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
