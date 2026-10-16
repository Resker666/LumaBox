# 待办事项

- [ ] **验收：iPhone Safari 动态片段预览。** 云端实际图片、MP4 与 ZIP 下载已通过，Chromium 实际播放通过；Windows WebKit 的公网真实片段播放未通过，需要在用户实际 iPhone Safari 上确认，不能用桌面 WebKit 夹具替代。见[验证记录](docs/validation/2026-10-16-single-live-note.md)。

- [ ] **低优先级：VPN / 代理兼容。** 验证系统代理、TUN 与 Fake-IP DNS 等模式的实际表现，考虑显式配置可信上游代理。目标是在保留目标链接和连接边界检查的前提下，支持用户日常开启 VPN 时下载。当前没有统一禁用 VPN，但非公网 DNS 映射会被拒绝，程序也不自动继承系统 HTTP/SOCKS 代理；此次只完善失败诊断，不实现代理接入或放宽检查。
