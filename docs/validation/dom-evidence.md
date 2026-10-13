# DOM 观察依据

2026-10-12/13，使用正常未登录浏览器只读检查用户授权的两个作品。此记录用于选择器依据，不计作独立程序真实下载成功。

- 图文 7695689642767235187：唯一 `.note-detail-container[data-e2e="player-container"]` 下有唯一 `.basePlayerContainer` 和 12 个 `.dySwiperSlide`。每个位置前景图片 `img.o0KibnKo` 的父节点直接位于 slide；另一个图片在更深背景容器中。作品根节点中计数文本为三个 span 组成的 `当前位置/12`。
- 视频页面会自动切换作品并变为 `/jingxuan?modal_id=<id>`。播放器祖先 `data-e2e="feed-active-video"` 同时带 `data-e2e-vid=<id>`；其他预加载播放器为 `feed-video`，另有直播预创建播放器。URL 与活动播放器 ID 必须共同匹配目标。
- 不读取页面内部应用状态，不记录完整媒体地址或会话令牌。未观察虚拟化图集的可操作下一张语义入口；当前内核仅在全部位置和计数一致时接受，结构不明明确失败。

最终审查补充，2026-10-13 08:00 左右通过 Codex 浏览器只读观察同一授权图集（仅用于 DOM 依据，不计作独立 P0）：

- 图集根区域的最近作品祖先是 `[data-e2e="feed-active-video"][data-e2e-vid="7695689642767235187"]`。图集和视频都必须使用 DOM 中实际的作品 ID，不能将调用参数复制为验证结果。
- 12 个 slide 是同一个水平 `display:flex; flex-direction:row; direction:ltr` 轨道的直接子节点。slide 的 `order:0`、`transform:none`，尺寸与 `.basePlayerContainer` 视口相同，播放器 `overflow-x:hidden`。
- 一次原子 DOM 观察中计数器为 `9/12`，视口 x=160、宽746；slide x 从 -5808 按746递增到2398，第9个恰好位于x=160。因此用 `当前计数 + round((slide.left - viewport.left) / viewport.width)` 确定序号，并验证位置集合严格为1～总数。
- 实现仅接受这类可由计数器和几何位置共同确认的完整线性布局；过渡动画、非水平布局、循环错位、显式克隆标记、声明序号冲突或缺失位置均拒绝。未验证所有未来轮播实现；不能仅凭节点数量宣称兼容。
