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

08:30～08:49 的专用详情页复查补充（原祖先布局不是唯一布局）：

- 视频详情区使用 `[data-e2e="video-detail"]`，实际观察到 `div` 标签。唯一 `.video-detail-container[data-e2e="player-container"]` 的 `video_<id>` 语义类提供作品 ID，没有活动 feed 祖先。内部唯一视频来源持续为 blob，readyState=4、时长124.6秒，没有 source 子元素提供 HTTPS。
- 图集详情区使用 `[data-e2e="note-detail"]`，唯一播放器与 `.noteSideBarContainer` 为同区域不同分支；侧栏唯一 `[data-e2e="feed-active-video"][data-e2e-vid]` 提供实际作品 ID，不是播放器祖先。
- 新定位方式只读复查得到图集 ID 7695689642767235187、12个前景 HTTPS 来源及完整几何序号1～12；视频 ID 7686430330920075249、单个 blob 来源。仅用于 DOM 依据，不能替代独立 CLI 下载验收。详见[本轮诊断](2026-10-13-detail-page-extraction.md)。

用户19:49/19:50复测后的加载阶段补充：重新加载同一授权图集，12个前景 img 最初全部引用静态默认图 `lf-douyin-pc-web.douyinstatic.com/obj/douyin-pc-web/ies/douyin_web/media/douyinDefault.ee9da4401be3dae0.png`，complete=true，尺寸2416×1430；随后替换为 douyinpic.com 的12张作品图片。因此图片 complete、节点总数和HTTPS不能单独证明作品图片已经准备好。当前CLI会在这个阶段提前拒绝默认图域名，详见[持续blob和图集默认图分析](2026-10-13-blob-and-gallery-placeholder.md)。
