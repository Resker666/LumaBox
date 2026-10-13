# 持续 blob 视频：正常详情响应与真实下载

日期：2026-10-13（Asia/Shanghai）。以下区分默认网络运行与临时公网 DNS 诊断，不将不同条件的结果混为一次验收。

## 最新验收：正常网络、默认有界面 CLI 已通过

2026-10-13 22:27（Asia/Shanghai），用户在其说明的正常网络下运行默认命令，取得成功报告 `7f6c551d1fcc4f7180a68d6483babe83`。本轮使用最终大小检查修补后的正式程序，没有启用临时公网 DNS 诊断解析器。`metadata_parsed=true`、`expected_count=actual_count=1`、`full_decode=true`、`headless=false`、HTTP 200。

本会话读取本地报告及新产物，重新运行 FFprobe、FFmpeg 完整音视频解码与 SHA-256 检查，确认 3,316,797 字节、1920×1080、124.6 秒、H.264/AAC，哈希与下文样本一致。成功目录为 `Downloaded/p0-video/7686430330920075249-7f6c551d1fcc4f7180a68d6483babe83/`。

同时复核先前默认 CLI 图集成功目录：12 张图片逐张完整解码，实际文件的字节数、尺寸和哈希与报告一致；编号为 01～12，ZIP CRC、成员顺序、每张图片哈希及内置报告全部一致。复核命令退出 0。这是对已下载产物的再次检查，不算新增联网下载次数。

用户两次粘贴的是同一个运行 ID，计为一次成功。Windows `_ProactorBasePipeTransport._call_connection_lost` 的 10054 回调仍出现，但本次没有阻止文件验证及成功发布；该回调问题未修复。此结果只确认当前 Windows 正常网络的有界面视频和图集样本，无头、Linux/云网络及其他作品尚未验收。网站阶段尚未开始，可据此制定后续阶段计划。

## 真实文件

授权视频：`7686430330920075249`。

临时匿名 Playwright 探测取得正常页面发出的 `/aweme/v1/web/aweme/detail/` 响应。请求的 `aweme_id`、响应的 `aweme_detail.aweme_id` 和可见播放器的作品 ID 一致；播放器仍为 blob。响应的 `video.play_addr_h264.url_list` 提供完整 MP4 地址。

没有伪造详情请求、生成签名、导入账号 Cookie、登录或处理验证码。第一次包含匿名会话 Cookie 转发的探测被自动审批拒绝，随后移除 Cookie 转发，使用无会话凭据的媒体请求完成下载。

新下载的文件验证结果：

- 字节数：3,316,797。
- 分辨率：1920×1080；时长：124.6 秒。
- 视频 H.264、音频 AAC；FFmpeg 完整音视频解码通过。
- SHA-256：`226a444d446103f423eb09cb38333a7ecbf7e9cdebfe773a99d5b6e25b54edb2`。

哈希与之前 Codex 浏览器下载的样本一致。本次重新取得页面提供的地址并实际下载，没有把旧媒体复制为新结果。

## 正式内核与网络条件

默认 CLI 运行 `e22b335236c54fd9a48aa694b9f785f7` 在 `dns_resolution` 失败：`UNSAFE_TARGET`、`DNS returned a non-public destination`，未到页面，HTTP 状态为空。

临时诊断进程将解析函数替换为 Cloudflare 公网 DoH 查询，连接固定公网地址 `1.1.1.1` 并保留 TLS 主机名证书验证；仅接受所查询主机的有效公网 A 记录，实际连接仍固定到校验后的公网地址。未修改系统 DNS、VPN、默认 CLI 配置或地址校验规则。该解析代码留在忽略的 `Downloaded/video-probe/`，不作为产品配置发布。查询格式参考 [Cloudflare 官方 JSON DoH 文档](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/)。

使用同一正式 CLI，仅覆盖上述诊断解析函数：

- `191a8c432db1455ebb70f3f98e7490d2`：HTTP 200，45 秒内未确认作品区域，失败，未发布文件。
- `ea328ed1b0a144c994f46694f4796c58`：大小检查审查修正前，成功发布一份新的完整 MP4；字节数、尺寸、时长、哈希与完整解码结果同上。报告在 `Downloaded/video-probe/formal-core/7686430330920075249-ea328ed1b0a144c994f46694f4796c58/report.json`。另外直接对产物运行 FFprobe 和 SHA-256，再次核对了 H.264/AAC、尺寸、时长、字节数和哈希。
- `580db38b8fb24a50a3062ebf28c1e901`：大小检查审查修正后，HTTP 200，但 45 秒内未确认作品区域，`detail_response_count=0`、`detail_metadata_parsed=false`，未到详情正文读取，失败且没有发布文件。
- `5e217d7f9d6c40ff9668b9ac355e214e`：修正后的最后一次有界复测仍在同一前端渲染阶段失败，HTTP 200、目标区域未确认、详情响应数 0，无成功产物。未再继续重试，也没有放宽校验或延长时限。

截至这组诊断结束，正文大小修补后的真实下载未复现，失败发生在未出现目标详情响应之前；默认 VPN DNS 条件仍会被拒绝。匿名页面渲染出现多次时限内无法确认目标的失败，不能宣称每次会话可靠成功，也不能把修补前成功当作最终版本默认 CLI 已验收。随后用户正常网络的默认 CLI 成功及产物复核见本文件“最新验收”；无头真实抖音、Linux 和云网络仍未通过验收。

## 接入的修补

- 同时支持已确认作品 DOM 的普通 HTTPS 视频来源，以及同一主框架正常发出的目标详情响应；仅提取已验证的完整 H.264 播放地址。
- 同时核对请求 ID、响应作品 ID、作品类型与可见作品播放器。推荐接口、子框架、其他作品、图文图片、封面、音轨和 DASH 分片不能充当目标 MP4。
- 新增的 CDN 只允许精确主机 `v5-hl-mly-ov.zjcdn.com`、`v5-dy-ov-experiment.zjcdn.com`，不放开整个 `zjcdn.com`；这两个主机的媒体请求不读取或发送 Cookie。逐跳 HTTPS、固定公网 IP、大小和完整解码检查继续生效。
- 浏览器正常重新导航时，临时销毁的 DOM 上下文在原有 45 秒时限内继续观察；没有增加等待时限或访问拒绝重试。
- 最多观察四份目标 JSON。以同一 CDP 会话的请求 ID 关联响应及解压字节计数，超过 2 MiB 的正文不再读取到 Python；该会话的每资源缓存上限 2 MiB，总缓存 8 MiB。读取失败、缓存淘汰及畸形 JSON 不成为成功资源。依据 [Chromium Network 协议](https://chromedevtools.github.io/devtools-protocol/tot/Network/)，未通过额外接口获取作品。
- 失败诊断增加 `detail_response_count` 和 `detail_metadata_parsed`，仍不记录正文、Cookie、请求查询或完整签名地址。

## 回归与审查

新增本地合成 Chromium 测试先重现旧实现的持续 blob、未知 CDN、详情访问拒绝、导航上下文更换和完整 CLI 失败。修补后首次完整回归 **178 passed in 88.49s**。

独立审查发现原先 `response.body()` 在大小检查之前复制完整解压正文。新增真实 Chromium 合成响应测试，以已解压的 3,146,109 字节 JSON 和很小的传输长度复现，旧实现测试失败；修正后不再使用该完整读取接口，相关 **28 passed in 36.48s**。另以 Chromium 观察确认，2 MiB 的诊断缓存会淘汰超大正文；不能只凭很小的 Content-Length 认定解压后正文也很小。

修正后的最终完整命令 `python -m pytest -q --tb=short`：**179 passed in 94.71s**，退出 0。`pip check` 无依赖冲突，`git diff --check` 通过。测试使用合成响应和本地媒体，不能代替真实抖音网络验收。

修正后的独立代码审查未发现剩余可操作问题，确认正文复制问题已解决，同意提交本轮有限范围修补。审查未重新执行测试或外部下载；默认网络验收、最终页面渲染稳定性、无头/云环境及 Windows 10054 回调仍按上述限制处理。

## 已完成的图集验证

用户正常网络运行 `7731a88238664cdaa9c084158ec9f126`：作品 `7695689642767235187`，`expected_count=actual_count=12`、`full_decode=true`，3,303,822 字节，11 张 WebP 和 1 张 JPEG。已读取本地成功报告核对数量、编码扩展名和尺寸。

网站阶段仍未开始。下一次无头模式检查命令见 `2026-10-13-next-check.md`。Windows 10054 连接关闭回调不是本轮修补对象；临时探测和用户最新成功运行中仍观察到该输出，不能宣称它已修复。
