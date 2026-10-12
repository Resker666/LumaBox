# LumaBox 独立 P0 验证：2026-10-13

结论：独立 CLI 已完成实现，离线完整流程通过；**真实 P0 未通过**。当前 DNS 返回非公网地址，程序在请求作品页之前停止。不能把此前 Codex 浏览器的实际下载或本次合成页面测试算成本程序真实下载成功，暂不进入网站阶段。

## 环境和运行方式

- 当前 Windows 电脑，Python 3.11.9，Playwright 1.58.0，Chromium 145.0.7632.6。
- FFmpeg/FFprobe：2025-09-01-git-3ea6c2fe25 full_build。
- 正式 `python -m lumabox` 入口，默认有界面、临时未登录浏览器会话。
- 沙箱禁止浏览器进程和本地测试网络时，使用获准的执行环境运行；未改变 VPN、DNS 或网络设置。
- 真实有界面运行失败后，没有运行真实无头下载；Linux 云网络也未验证。

## 真实样本结果

| 样本 | 运行 ID | 退出码 | 结果 | 保留媒体 |
|---|---|---|---|---|
| 视频 7686430330920075249 | `2a70b95f00f34712ac4c76c3a570b0a9` | 1 | `UNSAFE_TARGET` | 0 |
| 图集短链 p6mTe8JN73M（此前浏览器确认作品 7695689642767235187） | `63eedbfe71154392b15d8c9e3e848fbf` | 1 | `UNSAFE_TARGET` | 0 |

两次都没有取得作品页 HTTP 状态，`http_status=null`、`metadata_parsed=false`、`full_decode=false`。图集在短链解析前失败，本次不能声称已识别作品 ID 或完整数量。

2026-10-13 07:51 左右（Asia/Shanghai）的独立 DNS 诊断：

| 域名 | 系统解析结果 | 公网判断 |
|---|---|---|
| `www.douyin.com` | `198.18.0.8` | false |
| `v.douyin.com` | `198.18.0.94` | false |

`198.18.0.0/15` 为 Benchmarking 地址段，IANA 标记 Globally Reachable=false：[IANA IPv4 特殊用途地址注册表](https://www.iana.org/assignments/iana-ipv4-special-registry)。这类映射与一些代理的 Fake-IP 模式相符，但本次没有读取代理配置，不能确认具体来源。浏览器能通过代理访问，并不意味着这些地址符合独立内核的公网连接策略。此错误发生在页面请求前，不能据此判断是否需要登录或 Cookie。

原始脱敏报告保留在：

- `Downloaded/p0-video/2a70b95f00f34712ac4c76c3a570b0a9-failed.json`
- `Downloaded/p0-gallery/63eedbfe71154392b15d8c9e3e848fbf-failed.json`

## 离线验收

测试使用新生成的 H.264/AAC MP4、JPEG、真实 Chromium、测试专用 HTTP 服务和真实 FFmpeg；默认不访问抖音。

- URL 和目的地址：单一分享链接、域名边界、HTTPS/端口、控制字符、混合 DNS、固定 IP+原域名 TLS、逐跳文档和媒体重定向。
- DOM：目标 ID、活动播放器、blob 自然变 HTTPS、轮播总数/顺序、背景和推荐排除、真实重复图片保留、资源缺失与访问限制停止。
- 文件：流式超限、空文件/断流/206/类型错误、空闲及总时限、MP4+音频和 JPEG 完整解码、损坏文件拒绝。
- 发布：CLI 授权标志、退出状态、图片编号/ZIP、失败无半组、中断清理、以前产物保留、报告脱敏。
- 端到端：真实 Chromium 加载合成作品页→真实字节下载→完整解码→发布，视频/图集成功；HTTP 403 失败不发布。

完整测试结果与最终代码审查在实施完成时补充。

## 复测命令

在能够将目标域名解析为实际公网地址的网络下，从仓库目录运行：

```powershell
.\.venv\Scripts\python.exe -m lumabox --authorized "https://www.douyin.com/video/7686430330920075249" --output Downloaded/p0-video
.\.venv\Scripts\python.exe -m lumabox --authorized "https://v.douyin.com/p6mTe8JN73M/" --output Downloaded/p0-gallery
```

代码没有放宽目的地址检查，没有导入账号会话，也没有重试绕过平台访问限制。下一阶段取决于上述独立命令实际生成并验证视频和全部 12 张图集图片。
