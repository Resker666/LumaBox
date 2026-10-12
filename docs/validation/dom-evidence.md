# DOM 观察依据

2026-10-12/13，使用正常未登录浏览器只读检查用户授权的两个作品。此记录用于选择器依据，不计作独立程序真实下载成功。

- 图文 7695689642767235187：唯一 `.note-detail-container[data-e2e="player-container"]` 下有唯一 `.basePlayerContainer` 和 12 个 `.dySwiperSlide`。每个位置前景图片 `img.o0KibnKo` 的父节点直接位于 slide；另一个图片在更深背景容器中。作品根节点中计数文本为三个 span 组成的 `当前位置/12`。
- 视频页面会自动切换作品并变为 `/jingxuan?modal_id=<id>`。播放器祖先 `data-e2e="feed-active-video"` 同时带 `data-e2e-vid=<id>`；其他预加载播放器为 `feed-video`，另有直播预创建播放器。URL 与活动播放器 ID 必须共同匹配目标。
- 不读取页面内部应用状态，不记录完整媒体地址或会话令牌。未观察虚拟化图集的可操作下一张语义入口；当前内核仅在全部位置和计数一致时接受，结构不明明确失败。
