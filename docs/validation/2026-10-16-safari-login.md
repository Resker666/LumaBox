# iPhone Safari 登录兼容修复

开发版本 0.4.4，运行提交 b2ae2e8。用户报告 iPhone Safari 登录显示服务暂时不可用，桌面浏览器正常。

## 原因与真实复现

旧页面全局使用 Referrer-Policy: no-referrer，登录和任务 API 的 fetch 又设置 mode: same-origin。WebKit 在这组设置下将 POST 的 Origin 序列化为 null，被现有来源校验拒绝。

新增真实 WebKit 测试先观察到 `/auth/login` 返回 403，实际请求 Origin 为 null。随后通过同一个公网 IP 入口再次复现：Nginx 返回 HTML 403，旧页面将 JSON 解析失败统一显示为服务暂时不可用，与用户手机症状一致。来源处理规则见 [Fetch Standard](https://fetch.spec.whatwg.org/#append-a-request-origin-header)。

原先 Chromium 测试覆盖了手机尺寸，没有覆盖 Safari 引擎行为，因此漏过此问题。

## 修复

登录和工作台 API 的同源 fetch 均明确指定 referrerPolicy: same-origin，让合法请求保留真实 Origin；保留 mode: same-origin、全局 no-referrer 及原有严格来源校验。继续拒绝 Origin: null 和外部来源，未通过放宽 Nginx 或应用边界规避错误。

登录接口遇到代理 HTML 403 时显示请求校验提示，不再统一误报服务不可用。测试夹具支持按引擎运行，新增 WebKit 登录、提交真实受控下载任务及退出的完整流程；未安装 WebKit 时该项单独跳过，可先运行 playwright install webkit。

## 验证与上线

- 专项测试 15 项通过，包含空 Origin 继续被拒绝、代理错误提示和 WebKit 写操作。
- 完整回归 **369 项通过、1 项跳过、1 条已有警告，232.66 秒**；报告在本地忽略目录 `.superpowers/cloud-deploy/safari-full.xml`。
- 更新前确认无活动任务并备份云端数据库，保留原密码、Nginx 配置及下载目录；更新后服务 active、以 dev 运行、NRestarts=0。
- 通过真实公网入口使用 WebKit 验证：登录从 403 / null Origin 恢复为 200 / 正确公网 Origin；返回 0.4.4，原密码可用，收藏可读取，真实 MP4 的 SHA-256 与原样本一致。
- WebKit 退出请求同样携带正确 Origin，退出后收藏 API 返回 401。

公网修复前后证据分别在 `.superpowers/cloud-deploy/safari-before.json` 和 safari-after.json，均不含密码或 Cookie。验证使用真实 WebKit 和手机视口，未宣称已经替用户在其 iPhone 真机上复测。
