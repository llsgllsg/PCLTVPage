# PCLTVPage — PCL2 影视排行主页生成器

一个 PCL2 自定义主页: 展示 UAPI(uapis.cn) **电影收视排行**接口的实时/历史榜单(电视收视率、院线票房等)。

## 使用

```bash
python main.py                # 默认实时榜, 每个渠道前 12 条, 2 列
python main.py --period week --date 2026-08-02   # 历史周榜
python main.py --channel tv --limit 20           # 只看电视收视
python main.py --fresh                           # 跳过缓存强制拉取
python main.py --dry-run                         # 只打印数据不生成文件
```

运行后生成 `TVPage.xaml`。在 PCL2 的「启动页设置」里把主页地址指向该文件(或部署后指向在线 URL)即可。

## API Key 填在哪

- **免费档**: 本接口实测无需密钥, 直接运行即可。
- **付费档**: 设置环境变量 `UAPI_API_KEY` 为以 `uapi-` 开头的密钥, 脚本会自动以
  `Authorization: Bearer <KEY>` 请求头发送(见 `uapi_api.py`)。

```bash
# Windows PowerShell
$env:UAPI_API_KEY = "uapi-你的密钥"
python main.py
```

不要在代码里硬编码密钥。

## 接口说明

- 完整 API 地址: `https://uapis.cn/api/v1/misc/movie-rating-rank` (GET)
- 参数: `channel`(all/tv/web/cinema)、`platform`、`limit`、`period`(realtime/day/week/month)、`date`(YYYY-MM-DD)
- 返回: 按渠道分组 `groups[].list[]`, 每项含 `rank/name/channel/metric/metric_rate` 等
- 文档示例的 `channels[].items[]` 结构在代码中也做了兼容解析

**实际返回结构与文档示例不一致**: 文档示例为 `channels[].items[]`(title/score/hot_value),
线上真实返回为 `groups[].list[]`(name/channel/metric/metric_rate)。代码以线上真实结构为准解析。
另外 `channel=web`(网络平台)数据源当前可能返回 503 `SERVICE_UNAVAILABLE`, 此时该渠道会被跳过,
其余渠道(如电视收视、院线票房)正常展示。

## 边界处理(已在 uapi_api.py 实现)

- 参数校验: channel/period 白名单、limit 1~100、date 格式 `YYYY-MM-DD` 且 day/week/month 必填
- 超时(默认 60s)与网络异常指数退避重试
- 限流: 429 时尊重 `Retry-After` 并退避重试; 平时带 30 分钟文件缓存(接口建议平均 ≤40 次/分)
- 错误码: 400 `INVALID_PARAMETER`、404 `SNAPSHOT_NOT_FOUND`(生成占位页)、503 `SERVICE_UNAVAILABLE`

## 自动更新(可选)

`.github/workflows/main.yml` 定时(每 6 小时)在 GitHub Actions 里拉取数据并 scp 部署到服务器。
需要配置仓库 Secrets: `SSH_HOST / SSH_USER / SSH_PASSWORD / SSH_PORT / SSH_PATH`(与 SteamPage/ModPage 一致),
以及可选的 `UAPI_API_KEY`(免费档可不配)。
