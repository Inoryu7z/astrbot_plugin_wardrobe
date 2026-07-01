### v2.9.1

🐛 修复：WebUI 无限滚动丢失最后一页图片

* 修复 `preloadNextPage` 在预加载到最后一页（< 24 张）时提前把 `state.allLoaded` 设为 true，导致 `loadImages(false)` 在滚动到底部时直接 return，`preloadedPage2` 里最后一批图片永远不会被渲染
* 现象：当图片总数不是 24 的倍数时，最后 1~23 张被静默丢弃；删除任意一张图触发 `loadImages(true)` 重置回第 1 页后，原本被藏起来的图被挤进可视页，表现为"删除后冒出没见过的图"
* 修复后：`allLoaded` 改由 `loadImages` 在真正把预加载页渲染进网格后，根据 `images.length < perPage` 再判断，确保最后一批图正确显示

---

### v2.9.0

✨ 新增：人格级风格池（补拍专用）

* 新增 `get_style_pool_for_persona(persona_name)` 接口：返回指定人格的自定义风格池，供 aiimg 补拍使用
* 新增 `save_persona_style_pool(persona_name, styles)` / `delete_persona_style_pool(persona_name)` 方法
* 新增 `persona_style_pools.json` 存储文件，独立于全局池子配置
* WebUI 标签分类管理 modal 新增「人格风格池」区域：选择人格后可管理其专属风格列表
* 支持从全局风格池点击快速添加到人格池
* 人格池留空时自动回退到全局风格池，不影响现有行为
* 新增 `GET /api/persona-style-pools` 和 `POST /api/persona-style-pools` API 端点

---

### v2.8.1

✨ 新增：批量全选 + 筛选导出 + 选择备份导出

* 批量模式新增「全选」按钮：根据当前筛选条件（人格/分类/风格等）一键全选所有匹配图片
* 批量模式新增「导出图片」：将选中图片打包为纯图片 ZIP（仅图片文件，无数据库）
* 批量模式新增「导出备份」：将选中图片打包为完整备份 ZIP（含数据库记录 + 图片 + 关联视频 + 视频设置），可导入其他服务器的 wardrobe 插件
* 新增 `GET /api/images/ids` 端点：按筛选条件返回所有匹配图片 ID
* 新增 `POST /api/images/export` 端点：按 ID 列表导出纯图片 ZIP
* 新增 `POST /api/backup/export-selected` 端点：按 ID 列表导出完整备份 ZIP

🐛 修复

* 修复 `send_file()` 使用 `download_name` 参数在旧版 Quart 中报错，改为 `attachment_filename`
* 修复备份恢复 413 错误：`MAX_CONTENT_LENGTH` 从 1GB 增至 5GB，`BODY_TIMEOUT` 从 600s 增至 1200s
* 修复 `main.py` 缺少 `import uuid`，自动存视频下载 HTTP URL 时 NameError
* 修复 `add_video()` 不接受 `video_url` 参数，视频发送 URL 回退无效
* 修复 `_save_video_to_wardrobe` 不传 `video_url`，自动保存的视频无法 URL 回退
* 修复 `import_records()` 不含 `daily_selfie_use_count`，恢复备份后衰减计数归零

---

### v2.8.0

✨ 新增：存储空间优化 — 视频保留策略、无感模式、批量标签

* 新增视频保留策略：自动保存的视频（DailySharing 等插件产生的）超过 7 天后自动删除文件和记录，每天凌晨 5 点清理
* WebUI 手动生成的视频不受视频保留策略影响
* 新增自动备份开关（`auto_backup_enabled`）：可关闭自动备份以节省存储空间，默认开启
* 新增无感模式（favorite=meh）：标记为"无感"的图片超过 30 天后自动彻底删除（文件 + 数据库 + 向量索引 + 关联视频），每天凌晨 5:30 清理
* 无感与"无标签"的区别：只有无感标签的图片才会被自动删除，无标签图片不会被自动清理
* 新增批量标签操作：批量模式下可一键将选中图片标记为收藏/喜欢/无感/取消标记
* WebUI 侧边栏、统计页面、右键菜单、详情弹窗均支持无感筛选和标记

---

### v2.7.0

♻️ 重构：备份系统优化，解决大量图片时服务器压力过大问题

* 自动备份从每日全量改为**每月全量 + 每日增量**，大幅降低日常备份压力
* 全量备份时间从凌晨1点改为凌晨4点，避开高峰时段
* 全量备份使用重度节流（每文件 sleep 0.5s），增量备份使用轻度节流（0.1s）
* 图片/视频文件使用 ZIP_STORED（不压缩），避免对已压缩格式做无效 CPU 消耗
* 备份直接写磁盘文件，不再使用 BytesIO 全量内存构建
* 新增 `backup_state.json` 追踪上次备份时间，增量备份仅包含新增记录
* 新增自动清理逻辑：保留30天内备份，始终保留最新全量备份
* 备份文件命名规范：`full_YYYY-MM-DD.zip`（全量）、`incr_YYYY-MM-DD.zip`（增量）
* 备份格式版本从 2.0 升级到 3.0，metadata 新增 `type` 字段区分全量/增量
* WebUI 恢复支持**多文件上传**，系统自动识别全量/增量并按时间顺序恢复
* 手动导出同样使用重度节流，避免导出时服务器过载
* database 新增 `get_records_since()` / `get_video_records_since()` 增量查询方法

---

### v2.6.5

✨ 新增：补拍衰减过滤机制

* images 表新增 `daily_selfie_use_count` 字段，仅统计补拍场景的选中次数
* `search()` 新增 `daily_selfie_mode` 参数：补拍时在向量检索后、取图模型前执行指数衰减过滤（0.6^n，权重低于0.05直接排除）
* `get_reference_image()` 新增 `daily_selfie_mode` 参数并透传；补拍选中后递增计数
* 新增每周衰减后台任务：每周一凌晨4点所有图片的 daily_selfie_use_count 减1（最低0）
* 仅影响补拍场景，手动自拍和手动取图不受影响

---

### v2.6.4

🐛 修复：视频发送 retcode=1200 terminated 体验优化

* 修复 `retcode=1200 message='terminated'` 被当作普通发送失败处理：NapCat 上传超时后返回此错误，但上传可能仍在后台进行
* 新增 terminated 错误专项检测（`_is_upload_terminated`），区分"上传被终止"和"发送异常"
* 新增 30 秒宽限期：terminated 错误后等待 NapCat 完成后台上传，再尝试回退方法
* URL 回退超时从 120 秒增至 300 秒，给大视频上传更多时间
* 修复 `send_video_by_id` 忽略 `_send_video_to_conversation` 返回值，始终返回 True 的 bug
* 新增 `VideoSendResult` 数据类，返回发送结果含 `success`/`terminated`/`message` 三个字段
* WebUI 视频发送按钮现在区分"发送成功"/"上传被终止（可能仍在后台）"/"发送失败"三种状态
* 增强日志：发送日志包含视频文件大小，terminated 错误给出 NapCat 配置建议

---

### v2.6.3

🐛 修复：视频发送失败（retcode=1200 terminated）+ 发送逻辑加固

* 修复 Python faststart 未修正 stco/co64 chunk 偏移量，导致 moov 前移后视频文件损坏，协议端上传终止
* 修复视频发送使用手动构造的 `file://` URI（Windows 路径不兼容），改用 `Video.fromFileSystem()` 确保格式正确
* 修复 `callback_api_base` 已配置时 base64 回退必然失败（FileTokenService 无法处理 base64 URI），改为条件跳过
* 新增发送超时保护（120 秒），防止 send_message 无限挂起
* 新增纯文本 URL 兜底：所有视频发送方式均失败时，发送视频链接作为纯文本消息
* 增强诊断日志：每步发送尝试记录路径/URL/错误类型，便于排查

---

### v2.6.2

🐛 修复：视频功能数据完整性 + 性能优化

* 修复删除图片时未清理关联视频记录和视频文件，导致视频成为孤儿数据（命令删除 + WebUI 单个/批量删除）
* 修复备份完全不包含视频数据：导出备份现在包含视频记录、视频文件和视频设置（UMO 配置 + 系统提示词），恢复备份时自动还原
* 修复视频文件端点缺少缓存头（Cache-Control + ETag），每次播放/拖拽都重新下载完整视频
* 修复 `_download_video` 一次性将整个视频加载到内存，改为流式下载（64KB 分块），大视频不再 OOM
* 修复自动存视频使用 30 秒超时下载视频（继承自图片下载函数），改为 300 秒 + 流式写入
* 修复视频重试时 `error_message=None` 导致数据库存 NULL 而非空字符串
* 修复 `list_images()` 缺少 `persona` 过滤参数，与 `list_images_lightweight()` 不一致
* 修复视频生成接口未即时校验提示词模型配置，未配置时返回即时错误而非后台静默失败
* 修复 `_check_moov_position` 对 free/skip/wide 填充 atom 误判需要 faststart，减少不必要的视频重写

---

### v2.6.1

🐛 修复：v2.6.0 引入的若干问题

* 修复 `_check_moov_position` 逻辑错误：moov 紧跟 ftyp 时误判为需要 faststart，导致不必要的视频重写
* 修复 `list_images()` 缺少 `sort_by=random` 支持，与 `list_images_lightweight()` 不一致
* 修复随机排序池耗尽后滚动哨兵仍可见的问题（图片总数 > 500 时）
* 后台任务改用 `_spawn_bg_task` 保留引用，防止被垃圾回收（video_service / server 中的视频生成/重试任务）

---

### v2.6.0

 新增：视频发送到会话 + 随机排序

* WebUI 视频设置中新增「视频发送会话」配置，填写 UMO 值后可将视频发送到指定会话
* WebUI 视频生成面板新增「生成后自动发送」选项，生成完成后自动发送到配置的会话
* 视频播放器弹窗新增「发送视频」按钮，可手动发送已完成的视频
* 图片排序新增「随机」选项，一次性加载 500 条随机数据并客户端分页，避免重复

---

### v2.5.9

🐛 修复：PC 端无限滚动重复加载问题

* 所有 SQL 查询的 ORDER BY 添加 `id DESC` 二级排序键，消除批量上传图片排序不确定导致的分页重复
* appendGrid 新增 gridImageIds 去重检查，防止同一张图片被重复追加到网格
* 修复 loadImages API 请求失败时 loading 状态未重置的 bug，避免无限滚动永久卡死

---

### v2.5.8

 修复：移动端/平板端瀑布流布局严重错位

* 移动端（480px 以下）从单列 Grid 伪瀑布流改为双列 CSS Columns 真瀑布流，图片不再挤成一条竖线
* 平板端（481-768px）同样改为 CSS Columns 瀑布流，消除 Grid span hack 导致的间距不均
* 修复 recalculateAllSpans() 在移动端/平板端无效计算的问题，768px 以下直接跳过
* 修复 gap 计算不一致的问题，桌面端统一使用 16px gap
* 同步 main.py 版本号至 2.5.8（此前 metadata.yaml 与 main.py 版本不一致）

---

### v2.5.7

🔧 新增：`_save_video_from_bytes` 接口供外部插件调用

* 新增 `_save_video_from_bytes` 方法，接受 video_bytes、persona、source_image_path、created_by 参数，不依赖 event 对象
* 新增 `_find_source_image_id_by_path` 方法，通过图片文件路径查找对应的衣橱图片 ID
* 修复 DailySharing 等插件通过 `context.send_message()` 主动发视频时，`after_message_sent` 钩子不触发导致视频无法自动保存的问题

---

### v2.5.6

🚀 性能优化：视频流式传输 + faststart，秒开播放

* 视频文件接口从手动 `_read_all()` 全量读取改为 `quart.send_file` 流式传输，30~40MB 视频不再一次性读入内存
* 浏览器 Range 请求（206 Partial Content）由 Quart 原生处理，视频可边下边播，无需等待全量下载
* 自动获得 ETag / 304 条件请求缓存，重复播放不重复传输
* 约 40 行手动 Range 处理代码替换为 1 行 `send_file` 调用
* 新增 MP4 faststart 后处理：下载视频后自动将 moov atom 移至文件开头，浏览器首次请求即可拿到索引，一次往返开始播放
* 优先使用 `ffmpeg -movflags +faststart -c copy`（不重编码，秒级完成），不可用时回退纯 Python 实现
* 自动存视频（AiImg/DailySharing）同样执行 faststart 后处理

### v2.5.5

🔧 修复：视频重试浪费LLM token + WriteTimeout

* 重试视频时复用已有的提示词（`generated_prompt`），不再重复调用 LLM 生成新提示词，节省 token 并加快重试速度
* 配合 aiimg 端增大视频后端 write timeout（30s→120s），解决大图 Base64 data URL 上传超时导致提供商后台收不到请求的问题

### v2.5.4

🚀 性能优化：视频库加载加速

* 数据库层新增 `list_videos_lightweight()` 轻量查询（仅 SELECT 必要字段）和 `count_videos()` 计数方法，减少不必要数据传输
* 服务端 `/api/videos` 接口改用轻量查询并返回 `total` 字段，配合耗时诊断日志（>1s 输出 WARNING）
* 前端视频库加载改为并行请求（filters + videos 同时发出，不再串行等待）
* 前端新增客户端缓存（同参数不重复请求），二次切换视频库秒开
* `createVideoCard` 从 innerHTML 改为 createElement + textContent 程序化构建，消除 XSS 风险并提升渲染性能
* `renderVideoGrid` 使用 DocumentFragment 批量插入 DOM 节点，减少页面重排
* 视频库新增独立加载指示器（加载中转圈提示）和页码指示器（X / Y 个视频）
* 修复 `style.css` 缓存版本号长期落后（v=2.5.1 vs v=2.5.3）的问题

### v2.5.3

🌟 新功能：无人格筛选

* 图片库和视频库新增「无人格」筛选选项，可筛选未绑定人格的图片/视频
* 后端 `/api/images`、`/api/videos`、`/api/search` 三处 API 支持 `persona=__none__` 参数，对接数据库已有的空 persona 查询
* 向量搜索器原生支持 `filter_no_persona`，向量语义检索也适用无人格筛选

### v2.5.2

🌟 新功能：自动存视频

* 新增 `auto_save_video_enabled` 配置项（继承 `auto_save_aiimg_enabled`），启用后 AiImg 生成的视频和 DailySharing 分享的视频自动存入 wardrobe 视频库
* 自动存视频通过 `after_message_sent` 钩子检测消息中的 Video 组件，无感知入库，无需手动命令
* 自动入库的视频继承源图片的 wardrobe ID（通过 aiimg 的 `_last_image_by_user` 反查文件 hash 匹配），关联到源图片记录
* 视频文件以 `auto_{md5}.mp4` 命名存入 wardrobe 视频目录，MD5 去重避免重复存储
* 统计命令 `/衣柜统计` 现在同时显示视频数量

🔄 改进

* `get_stats()` 返回新增 `video_count` 字段
* `on_after_message_sent` 钩子同时触发图片和视频的自动保存任务


### v2.5.1

**🐛 修复：视频提示词生成图片未传递**

* 修复 `provider.text_chat(image_urls=base64)` 实际不生效，导致提示词模型没收到图片，产出全为幻觉（门框、公园长椅等不存在元素）
* 新增直连 API 方式：配置 `video_prompt_base_url` + `video_prompt_api_key` + `video_prompt_model` 后，直接 HTTP 调用 OpenAI 兼容 Vision API，彻底绕过 Provider 传图不可靠问题。直连优先，Provider 为回退
* 图片分析描述（服装/风格/场景/姿势等 17 个字段）现在一同注入到提示词生成请求中，帮助模型理解图片内容

**✨ 改进：视频提示词输出格式**

* 默认系统提示词输出改为 JSON：`{"reasoning": "...", "prompt": "..."}`，将模型思考过程收纳到 reasoning 字段，prompt 字段保持纯净
* 新增 4 层 JSON 解析兜底（直接解析 → 去代码块 → 花括号匹配 → 全文兜底）
* 新增思维链特征检测日志，发现"我来分析""用户意图"等泄露时打印 WARNING

**🖥️ WebUI：视频库界面统一**

* 视频库顶栏不再隐藏，与图片库保持一致的搜索框和操作按钮
* 视频侧边栏从 `video-sidebar` 改为统一的 `sidebar` 样式，视觉完全一致
* 视频内容区左右边距对称修复（移除多余的 `margin-left`）
* 视频设置按钮从视频视图内部移至顶栏，仅在视频视图时显示

**🖥️ WebUI：视频播放器快速切换**

* 视频播放弹窗添加左右箭头按钮，支持切换到上一个/下一个视频
* 键盘导航：← → 切换视频，Esc 关闭播放器
* 打开视频时自动预加载相邻两个视频文件，提升切换速度

**🖥️ WebUI：失败视频管理**

* 失败视频卡片显示半透明遮罩 + ✗ 图标，底部提供 «↻ 重试» 和 «🗑 删除» 两个按钮
* 重试复用原图片 + 档位 + 想法，状态变回"生成中"
* 新增 `/api/videos/<video_id>/retry` POST 接口

**🐛 修复：视频生成重复提交**

* 提交成功后 3 秒冷却期内按钮保持禁用，防止重复点击导致同一图片生成多个视频任务

### v2.5.0

**✨ 新功能：图片转视频（WebUI）**

* 新增图片转视频独立板块，可在 WebUI 中为衣橱中任意图片生成视频
* 支持三档视频风格：正常、轻荤、重荤，每档可独立绑定不同的视频后端（复用 AiImg Provider Registry）
* 视频提示词自动生成：调用 Vision 模型根据图片内容和档位生成动态提示词
* 系统提示词可自定义：通过 WebUI 设置页面实时编辑，持久化到 video_system_prompt.txt
* 视频后台异步生成，不阻塞前端操作，状态实时更新（generating → done/failed）
* 视频列表按人格/档位/状态/源图片筛选，支持删除和管理
* 视频文件支持流式播放（支持 Accept-Ranges 分段请求）
* 新增 videos 数据库表，记录完整的生成历史（源图、提示词、后端、状态等）

**🔧 新增配置项**

* video_enabled：启用图片转视频功能（默认关闭）
* video_prompt_provider_id：视频提示词生成模型（需支持视觉能力）
* video_normal_default_backend：正常档默认视频后端（复用 AiImg Provider ID）
* video_light_spicy_default_backend：轻荤档默认视频后端
* video_heavy_spicy_default_backend：重荤档默认视频后端

**📦 新增文件**

* core/video_service.py：视频生成服务，负责提示词生成、后端调用、视频下载全流程

**🔗 依赖关系**

* 视频后端通过 AiImg 插件的 ProviderRegistry 调用，需确保 AiImg 已激活并配置了视频后端

---

### v2.4.1

** 优化：参考图接口新增相似度阈值参数**

* get_reference_image 新增 min_similarity 参数（float|None），透传至向量检索层
* searcher.search / _vector_search / _query_candidates 全链路支持 min_similarity
* 不传时行为不变（使用全局 vector_search_min_similarity 配置）；传入时覆盖全局阈值
* 供 AiImg 补拍等场景按需收紧搜图条件，不影响日常取图

---

### v2.4.0

**🔧 架构重构：向量检索优先 + 向量索引扩展**

* 向量检索可用时跳过意图解析模型，直接用自然语言 query 做语义搜索，减少一次 LLM 调用
* 意图解析模型降级为 LEGACY 路径：仅在向量检索不可用或无结果时作为 LIKE 回退使用
* 向量索引新增 style（风格）和 clothing_type（服装类型）字段，解决按风格搜图找不到的问题

**🐛 修复**

* 修复 main.py 版本号落后 metadata.yaml 一个版本的问题（2.3.8 → 2.4.0）

---

### v2.3.9

**🔧 优化：任务间隔拉长**

* 旧图重分析、ref_strength 回填的任务间隔从 2s 拉长到 30s，减轻 API 并发压力

---

### v2.3.8

**✨ 新功能**

* 全屏大图左右箭头：lightbox 支持 ← → 切换上一张/下一张，支持键盘导航，显示图片序号计数器

**🔧 优化**

* 大图优先加载：打开全屏大图时当前图片跳到原图加载队列最前面，不再排队等待

**🐛 修复**

* 修复 lightbox 左右箭头点击后退出问题：箭头按钮内文本节点无 `closest()` 方法导致事件处理崩溃

---

### v2.3.7

**✨ 新功能**

* 瀑布流布局：图片按原始宽高比展示，不再裁成统一正方形。紧凑模式5列，大图模式3列，移动端自适应（单/双列）
* 详情页左右箭头：弹窗查看图片时可用箭头键快速切换上一张/下一张，支持键盘 ←→

**🔧 优化**

* Grid + JS 预加载：根据缩略图实际尺寸精确计算卡片行跨度，保证加载更多时排序不乱
* 无限滚动：移除手动"加载更多"，滑到底部自动加载后续图片
* 视图切换实时重算：紧凑/大图切换时自动重算所有卡片比例，无缝适配
* 移动端双断点适配：≤480px 单列满宽，481-768px 双列紧凑

---

### v2.3.5

**✨ 新功能**

* 缩略图预生成：存图时自动生成缩略图，浏览时直接返回缓存，无需实时缩放
* 右键菜单：图片卡片支持右键快捷操作（收藏/喜欢/删除/重新分析/切换参考强度）
* 视图切换：工具栏新增紧凑/大图两种浏览模式，偏好自动记忆

---

### v2.3.4

**⚡ 性能优化**

* 已加载原图的卡片移除 content-visibility，保留渲染内容，滚动回来不再重新解码
* 图片添加 decoding="async"，异步解码不阻塞主线程，快速滑动更流畅

---

### v2.3.3

**⚡ 性能优化**

* HTTP 缓存头：图片端点添加 Cache-Control（7天缓存）+ ETag，二次访问浏览器直接使用缓存，网络请求减少 90%+
* IntersectionObserver 原图懒加载：替代批量预加载，仅加载视口内可见卡片原图，并发限制3个，带宽占用降低 70%+
* content-visibility: auto：CSS 跳过离屏卡片渲染，DOM 节点恒定开销，多图不再卡顿
* 详情数据缓存 + AbortController：已访问图片瞬间显示，快速导航自动取消前一个请求
* 批量预生成缩略图：插件启动时后台生成所有缺失缩略图，首次页面加载不再等待按需生成
* DocumentFragment 批量插入：卡片从逐个 append 改为 fragment 一次性插入，减少 reflow

---

### v2.3.2

**✨ 新功能**

* 缩略图系统：WebUI 图片卡片先加载缩略图（长边400px），后台自动加载原图替换，大幅提升首屏加载速度
* 渐进式预加载：首页加载后按顺序预加载（P1缩略图→P2缩略图→P1原图→P2原图），"加载更多"时直接使用预缓存数据
* 批量重分析失败图：批量操作栏新增「重分析失败图」按钮，自动查找所有分析失败的图片并批量重新分析
* 详情弹窗左右箭头导航：点击箭头或键盘←→切换上下张图片，Esc关闭弹窗

**🔧 优化**

* 批量上传间隔从5秒调整为20秒，缓解模型API并发超时问题

---

**✨ 新功能**

* 收藏/喜欢影响低热度优先排序：开启「优先低热度」时，收藏图虚拟降低3点热度、喜欢图降低1点热度，使它们更容易被返回（实际 use_count 不变）
* WebUI 详情弹窗支持编辑热度值（use_count），查看模式下显示有效热度折扣信息

---

### v2.3.0

**🐛 Bug 修复**

* 修复 `exclude_current_persona=True`（AiImg 参考图搜索）时忽略 `search_persona_mode` 配置的 bug：之前该路径直接排除当前人格后返回其他人格的图，完全绕过了 `no_persona_only` / `fallback_other` 策略。现在正确遵循策略：`no_persona_only` 只搜无人格图，`fallback_other` 优先搜无人格图再回退其他人格
* `get_reference_image` 现在显式传入 `persona_mode` 配置值，而非依赖默认参数
* 修复 Bot 自己取图时错误应用 `search_persona_mode` 策略的问题：`persona_mode` 仅影响 AiImg 参考图搜索，Bot 取图时 `self` scope 只搜当前人格、`other` scope 只搜非当前人格、`named` scope 只搜指定人格、`global` scope 搜所有图，搜不到直接返回空，不再回退
* 修复 `persona=""` 在数据库层和向量搜索层被当作"不过滤人格"处理的问题：统一语义 `persona=None` 不过滤，`persona=""` 只搜无人格图

**🔧 优化**

* `search_persona_mode` 配置项更名为"参考图人格搜索策略"，描述明确仅影响 AiImg 参考图搜索

**🔧 日志优化**

* 移除所有信息日志中的描述/提示词截断（`[:100]`/`[:200]`），输出完整内容便于排查

---

### v2.2.9

**🐛 Bug 修复**

* 修复取图人格搜索策略 `no_persona_only` 不生效的问题：`persona=""` 在数据库层和向量搜索层被当作"不过滤人格"处理，导致实际返回了所有人格的图而非仅无人格图。现统一语义：`persona=None` 表示不过滤，`persona=""` 表示只搜无人格图

---

### v2.2.8

**🐛 Bug 修复**

* 修复衣橱自动存图阻塞角色回复的问题：`on_llm_tool_respond` 和 `on_after_message_sent` 钩子改为后台异步执行，图片分析不再阻塞 LLM 工具调用链
* 修复向量检索可用时仍调用意图解析模型的问题：当 `exclude_current_persona=True` 且向量检索可用时，直接进行向量检索，跳过意图解析

**✨ 新功能**

* WebUI 新增「最近调用」排序：按图片最后被调用的时间倒序排列，最近调用的图排在最前
* 图片卡片显示 🕐 最近调用时间（相对时间格式化）

---

### v2.2.7

**✨ 新功能**

* WebUI 统计分析可视化页面：顶部导航栏「统计」按钮进入，支持按人格/分类/收藏筛选
  - 景别分布饼图、氛围分布环形图、风格/场景 Treemap（按系列分组）
  - 每个维度显示 Top 5 标签及占比
  - 点击图表区域可跳转图片视图并应用对应筛选
  - 冷门品类（占比<5%）自动合并为「其他」
  - 图表库使用 ECharts 5.5.1（CDN 加载）
* WebUI 存图趋势折线图：按天统计存图数量，渐变紫粉配色，支持筛选

**🔧 优化**

* 统计页面 Treemap 支持钻入模式：点击大类色块展开子项，面包屑导航返回
* 统计页面 Treemap 同组同色系配色，视觉分组更清晰
* 统计页面 UI 居中修复：隐藏侧边栏时内容区域不再偏左
* 修复统计页面点击子类跳转后筛选未重置的 bug

**✨ 新功能**

* 新增配置项「取图结果包含用户标注」(`search_include_user_tags`)，开启后取图返回给聊天模型的描述中会额外包含用户保存时添加的标注信息

**🔧 优化**

* 批量操作栏改为顶部固定定位（sticky），滚动时始终可见

**🐛 Bug 修复**

* 修复向量检索器在 Embedding Provider 延迟注册时无法自动初始化的问题：新增 `_ensure_vector_searcher()` 方法，搜索时自动检测并重试初始化
* 修复存图模型返回结果但 JSON 解析失败时不会尝试备用模型的问题

---

### v2.2.6

**🔧 优化**

* 取图人格搜索策略更名：`exclude_all` → `no_persona_only`（只搜无人格图，搜不到返回空），`self_first` → `fallback_other`（优先搜无人格图，搜不到回退其他人格图）
* 取图选择策略优先级调整：clothing_type/body_focus/description中的服装与姿势表述 提升为最高优先级，style/atmosphere 降级为辅助参考
* 取图匹配策略放宽：不完全不匹配就返回，宁可多返回不漏掉

**🐛 Bug 修复**

* 修复 WebUI 卡片左上角参考强度与喜爱程度标签重叠

---

### v2.2.5

**✨ 新功能**

* 每日凌晨1点自动备份衣橱数据（数据库记录+图片文件），备份文件位于 `backups/wardrobe_auto_backup.zip`，保留最近1份，可直接通过 WebUI 导入恢复

**🔧 优化**

* 备份 ZIP 构建逻辑从 server.py 提取到 main.py 的 `build_backup_zip()` 共享方法，WebUI 导出和自动备份共用

---

### v2.2.4

**🐛 Bug 修复**

* 修复版本号不一致：main.py 版本号与 metadata.yaml 对齐
* 修复 `search_count` 缺少 `ref_strength` 参数导致 WebUI 分页总数不准确
* 修复备份导出同步阻塞事件循环：大量图片时 WebUI 不再卡死
* 修复 `create_task` 无引用导致后台任务可能被 GC 回收
* 修复 `_wardrobe_plugin` 从未被设置导致自定义池子在搜索中永远不生效
* 修复 `vector_searcher.terminate` 不尝试持久化 FAISS 索引
* 修复 `analyzer.py` 重复 `import os`
* 修复 `metadata.yaml` 缺少 `dependencies` 字段
* 修复删除图片时未清理向量索引（命令删除 + WebUI 批量删除）
* 修复重分析时未更新 `ref_strength_reason` 字段（旧图重分析 + WebUI 重新分析）
* 修复 ref_strength 回填逻辑：模型返回 style 时不应算失败
* 修复 WebUI 编辑/切换参考强度后卡片不实时更新

**✨ 新功能**

* 新增 `ref_strength_reason` 字段：模型分析时输出评级理由，仅在日志和 WebUI 可见，取图时屏蔽
* `ref_strength` 按钮置顶：从字段列表底部移至详情弹窗底部操作栏，面板式三档选择
* 卡片显示参考强度标注：所有级别均显示（📸full / 🎨style / 🔄reimagine）
* 轻量列表 API 返回 `style` 字段

**🔧 优化**

* 备份导出改为异步线程执行（`asyncio.to_thread`），避免阻塞事件循环
* 后台任务改用 `_spawn_bg_task` 保存引用，防止被垃圾回收

---

### v2.2.3

**🔧 优化**

* `ref_strength` 评估标准重写：从"姿势好坏"改为"姿势与构图的参考价值"
  - `full`：姿势有强烈视觉表现力或身体魅力展示，剪影仍有看点
  - `style`：姿势有韵味但未达刻意设计，取其氛围和感觉
  - `reimagine`：纯功能性姿态，"人形衣架"，姿势无视觉叙事
* 评估标准与服装美观程度完全解耦，避免 LLM 因服装好看而给高姿势评分
* WebUI 新增 ref_strength 筛选器和标签显示

**✨ 新功能**

* 新增 `ref_strength` 字段：存图时自动评估姿势与构图的参考价值（三档：full/style/reimagine）
* `get_reference_image` 返回值新增 `ref_strength` 字段
* `aiimg_wardrobe_preview` 返回值新增参考强度指引

---

### v2.2.2

**✨ 新功能**

* 新增重排序模型集成：向量检索后可选 Rerank 精排，进一步提升搜索精度
* 新增 `rerank_provider_id`、`rerank_top_k`、`rerank_min_candidates` 三个配置项

**🐛 Bug 修复**

* 修复版本号不一致：metadata.yaml 与 @register 版本号对齐
* 修复 WebUI 搜索缺少 `exclude_persona` 参数
* 修复编辑后向量索引重建漏传 `allure_features` 和 `body_focus`
* 修复备份恢复后向量索引未重建

### v2.2.1

**✨ 新功能**

* 新增 `search_prioritize_unused` 配置项，开启后取图时优先返回使用次数少的图片

**🐛 Bug 修复**

* 修复 WebUI 404 日志噪音问题（浏览器请求 favicon.ico 等不再打印 ERROR 日志）

### v2.2.0

**✨ 新功能**

* 向量检索结果现在包含相似度分数（`_similarity` 字段），结果按相似度从高到低排序

**🔧 改进**

* 向量检索返回类型改为 `list[tuple[str, float]]`，包含 wardrobe_id 和相似度

### v2.1.9

**🐛 Bug 修复**

* 修复 WebUI 热重载失效：将 WebUI 启动从 `on_astrbot_loaded` 改为 `initialize()` 生命周期钩子，现在插件重载后 WebUI 会正确重启

**🔧 改进**

* `search_persona_mode` 配置改为下拉选择（exclude_all / self_first）

### v2.1.8

**🔧 改进**

* 向量检索相似度阈值改为可配置项 `vector_search_min_similarity`（默认 0.5），可在 WebUI 或配置文件中调整

### v2.1.7

**🐛 Bug 修复**

* 修复向量检索返回不相关结果：添加相似度阈值过滤（默认 0.5），低于阈值的结果会被过滤并回退到 LIKE 搜索

### v2.1.6

**🐛 Bug 修复**

* 修复向量检索结果重复：`_id_map` 为纯内存字典，重启后丢失导致 `index_existing_images()` 重复索引。现改为启动时从 `wardrobe_vec.db` 重建映射并自动清理重复条目；`search()` 增加 `seen` 集合去重

**🔧 改进**

* LIKE 回退策略增强：新增渐进式前缀截断（`jk服`→`jk`→命中`JK制服`）和字符级 AND 匹配（拆单字要求全部出现）
* 移除未使用的 WebUI API 端点：`batch-upload` 和 `batch-reanalyze`（前端均未调用）

### v2.1.5

**✨ 新功能**

* 新增取图人格搜索策略配置（`search_persona_mode`）：`exclude_all`（默认）优先搜无人格图，找不到再搜其他人格；`self_first` 保留旧逻辑先搜当前人格再回退全局

**🐛 Bug 修复**

* 修复批量操作面板按钮无效：`toggleBatchOpsPanel`/`clearBatchOps`/`retryBatchOps`/`retrySingleOp` 定义在 IIFE 内部，onclick 全局调用访问不到，现挂载到 window
* 修复向量检索器延迟初始化：`__init__` 阶段 provider 可能未注册导致向量检索器为 None，现改为 `_ensure_db` 中延迟重试
* 修复 WebUI 搜索不使用向量检索：`/api/search` 直接调用 `db.search_by_description()` 绕过了向量检索，现改为先向量检索再回退 LIKE
* 修复 WebUI 404 错误日志噪音：新增 404 错误处理器，不再触发全局异常日志
* 修复 `exclude_all` 人格搜索逻辑：原逻辑搜无人格图后错误过滤，现改为先搜无人格图→找不到再搜其他人格→再找不到返回空

**🔧 改进**

* 向量检索日志增强：检索不可用/开始/无结果/命中均打印日志，方便排查是否生效
* LIKE 回退策略优化：完整关键词搜不到时，自动 bigram 分解搜索（如"厚白丝"→搜"厚白"或"白丝"），提升中文模糊匹配能力
* 工具描述优化：query 参数明确要求使用自然语言完整表达，不要拆成关键词
* 提示词优化：值池改为"优先选用、允许池外填写"，尤其表情和姿势池不再限定死

### v2.1.4

**🐛 Bug 修复**

* 修复批量上传/重新分析5秒间隔逻辑：原逻辑等待上一张分析完成后才等5秒发下一张，现改为每5秒发起下一个请求，不等上一个完成
* 修复批量上传中新图片不可见：每张上传成功后立即刷新网格，无需等待全部完成

**🔧 改进**

* 提示词优化：值池改为"优先选用、允许池外填写"，尤其表情和姿势池不再限定死，模型可使用更准确的描述

### v2.1.3

**✨ 新功能**

* 批量操作进度面板：右下角浮动面板，实时显示批量上传/重新分析的逐张进度（✓/✗/⏳/○）
* 批量操作失败重试：进度面板中失败项支持单张重试（↻按钮）和一键重试全部
* 批量重新分析改为前端逐张调用：支持逐张进度跟踪，5秒间隔避免 API 限流

**🐛 Bug 修复**

* 修复登录后必须刷新才能进入：`auth_check` 改为 302 重定向到 `/login`
* 修复浏览器缓存旧版 JS 导致新功能无效：cache-busting 版本号从 `?v=1.9.0` 更新
* 修复批量上传静默中断：`api()` 返回错误对象时 `.json()` 崩溃 + for 循环缺少外层 try/catch
* 修复日志截断：重新分析日志不再截断 description

**🔧 改进**

* 批量上传/重新分析统一 5 秒间隔机制
* 批量上传添加分析结果日志
* WebUI 重新分析（单图/批量）添加完整日志

### v2.1.2

**🐛 Bug 修复**

* 修复批量重新分析 API 缺少外层 try/except，异常时无日志无响应
* 修复 `batchReanalyze()` 前端 `api()` 返回错误对象时 `.json()` 调用崩溃

**🔧 改进**

* WebUI 重新分析（单图/批量）添加完整日志：入口、分析结果摘要、失败原因
* 批量重新分析完成时打印汇总日志（成功/失败/总数）

### v2.1.1

**✨ 新功能**

* 批量重新分析：WebUI 批量模式新增"重新分析"按钮，支持批量选中图片后一键重新分析
* 批量上传非阻塞：批量上传点击后弹窗自动关闭，上传在后台继续，用户可继续浏览/操作 WebUI
* 批量上传进度指示器：批量操作栏实时显示上传进度（`上传中 3/10（✓2 ✗1）`）

**🔧 改进**

* 提示词优化：`allure_features` 扩展为三层结构（明确诱惑 / 姿态暗示 / 不要记录），覆盖"保守穿着+微妙姿态"的灰色地带
* 提示词优化：JSON 示例精简，消除与规则区的重复描述，减少歧义
* 提示词优化：`key_features` 示例通用化，去掉过于具体的示例
* 提示词优化：新增"不确定时留空"规则，减少模型猜测

### v2.1.0

**✨ 新功能**

* MD5 文件去重：存图时自动计算 MD5 哈希，检测到完全相同的图片时跳过保存并提示用户
* 旧图哈希回填：启动时自动扫描旧图，计算并回填 MD5 哈希值
* 排序模式扩展：WebUI 排序新增"喜爱优先"选项，现支持三种排序（最新上传 / 喜爱优先 / 热度优先）

**🔧 改进**

* 排序逻辑修复：默认"最新上传"排序不再优先展示收藏图片，改为纯时间排序
* `exposure_features` 描述示例更新：`乳沟/深V` → `乳沟`，`侧乳/侧胸露出` → `侧乳露出`，新增 `露肩` 等

**🐛 Bug 修复**

* 修复 `analyzer.py` 中 `user_description` 为 `None` 时 `.strip()` 崩溃
* 修复 WebUI 重新分析时 `user_description or None` 传 `None` 导致分析失败

### v2.0.0

**✨ 新功能**

* 新增 `body_focus` 字段：记录画面聚焦的视觉重点区域
* 新增 `allure_features` 字段：记录动作/表情/姿势带来的魅力感
* 新增三个特征字段：`exposure_features`、`key_features`、`prop_objects`，大幅提升搜索精准度
* 收藏/喜欢机制：双层标记（收藏>喜欢），取图时优先返回收藏图片；WebUI 侧边栏筛选、详情页快捷按钮
* 图片热度机制：取图/参考图时自动计数，支持按热度排序
* WebUI 批量上传：支持多文件选择，逐张上传并显示进度
* WebUI 详情页全面改造：所有字段可查看和编辑，支持重新分析
* WebUI 侧边栏新增收藏筛选和排序选择

**🔧 改进**

* 氛围池与姿势池扩展优化
* 暴露程度分级细化：5级制
* 多个特征字段提示词优化
* 关键词搜索扩展到 7 个字段
* 检索管线补全 pose_type/body_focus 支持
* 向量索引文本构建纳入新字段
* 旧图自动重分析：检测新字段为空时逐张重分析

**🐛 Bug 修复**

* 修复数据库初始化时索引创建顺序导致启动失败
* 修复 import_records 丢失 favorite/use_count 字段
* 修复 PUT 端点不处理 favorite 字段
* 修复搜索结果分页总数不准确
* 修复文本搜索缺少 favorite 参数

### v1.8.1

**✨ 新功能**

* 新增向量语义检索：基于 AstrBot 框架的 FaissVecDB + EmbeddingProvider，支持配置专用 Embedding 模型，向量模型失效时自动回退到本地关键词匹配
* 向量检索解决洛丽塔/JK等相似描述的精准匹配问题——LIKE 搜索无法区分"中华风甜系"和"蓝白蕾丝拼接"，向量检索可以捕捉语义差异
* 存图时自动生成 description + user_tags 的向量索引，首次启用时自动索引已有图片
* 新增 `embedding_provider_id` 配置项，允许指定专用 Embedding Provider

**🔧 改进**

* 搜索意图解析 prompt 注入值池（style/scene/atmosphere/clothing_type），解决存图-取图值池断层问题（审查 #5）
* 候选图片选择 prompt 增加属性优先级说明和空结果选项，提升选择质量（审查 #7）
* 选择 prompt 支持返回空列表，避免强行选择不匹配的图片

**📝 用户偏好记录**

* 第 9 点（用户反馈闭环）不做——用户明确表示太麻烦
* 第 10 点（自动存图上下文）不做——用户明确表示不做

### v1.7.0

**✨ 新功能**

* 新增备份导出功能：WebUI 一键导出所有图片元数据和图片文件为 zip 包，方便迁移到新服务器
* 新增备份恢复功能：上传 zip 备份文件一键恢复，已有数据按 ID 跳过不会被覆盖，安全可靠

**⚡ 性能优化**

* 图片列表加载优化：新增轻量列表 API（`/api/images?lightweight=1`），列表页只加载 `id/category/persona` 等核心字段，不再返回完整属性，大幅减少数据传输量
* 图片列表改用"加载更多"模式替代分页，首屏渲染更快，无需等待所有图片加载完毕
* 图片详情按需加载：点击查看详情时才请求完整属性数据，列表页不再预加载
* 上传限制提升至 512MB，备份恢复超时提升至 600 秒

### v1.6.8

**🐛 Bug 修复**

* 修复 WebUI 端口占用问题：将 ASGI 服务器从 hypercorn 切换为 uvicorn，参照 DayMind/LivingMemory 插件的成熟方案，使用 `server.started` 标志做可靠启动检测，`should_exit` 做干净关闭，彻底移除不合理的端口跳跃逻辑
* 修复关键词搜索遗漏 user_tags 字段：`search_by_description()` 现在同时搜索 `description` 和 `user_tags` 两个字段，用户提供的标签（如"杏花微雨"）不再被遗漏
* 修复搜索缺少原始查询回退：当 LLM 将查询解析为结构化字段但匹配失败时，现在始终将原始查询文本作为 keyword 回退搜索，避免搜不到明明存在的图片
* 修复搜索 category 过滤过于严格：当带 category 的搜索无结果时，自动尝试不带 category 的纯关键词搜索作为最终回退

**🔧 改进**

* 依赖更新：`hypercorn` 替换为 `uvicorn>=0.29.0`

### v1.6.7

**🐛 Bug 修复**

* 修复取图/存图时人格丢失：LLM 工具调用经常不传 persona 参数，导致搜索直接走全局而非当前人格。现在当 persona 为空时自动从对话上下文获取当前人格名

### v1.6.6

**🐛 Bug 修复**

* 修复 WebUI 上传图片报"网络错误"：`request.form` 未 `await` 导致服务端 500（Quart 框架要求 `request.form`/`request.files` 必须异步等待）
* 修复前端上传失败时显示"网络错误"而非真实错误信息：`api()` 返回 `null` 时直接调用 `.json()` 导致 TypeError
* 修复 Quart 默认配置导致上传失败：`MAX_CONTENT_LENGTH` 提升到 64MB，`BODY_TIMEOUT` 提升到 300 秒
* 新增 Quart 全局异常处理器和 413 错误处理器，确保异常信息输出到 AstrBot 日志并返回给前端

**🔧 改进**

* 日志增强：`_save_image_from_bytes` 分析结果日志新增朝向、动态程度、动作风格、色调、构图、背景、用户标签共 7 个字段，与 WebUI 详情页一致
* `/存图` 命令支持双参数：`/存图 人格名 描述`，第一个词匹配已配置人格则识别为人格，剩余部分为描述；向后兼容旧用法
* 前端 `api()` 函数改进：非 200 响应时解析后端返回的 JSON 错误信息并显示，不再笼统显示"网络错误"

### v1.6.5

**🐛 Bug 修复**

* 修复自动存图不区分生成模式的问题：自动存图现在仅保存自拍模式生成的图片，文生图/改图不再自动存入
* 修复自动存图配置描述与实际行为不一致的问题：hint 已修正为"仅保存自拍模式"
* 修复 `_last_image_by_user` 类型变更后的兼容问题：使用 `isinstance(entry, dict)` 判断新旧格式

**📝 文档**

* 更新 AIIMG_DEV_GUIDE.md 与当前代码同步

### v1.6.4

**🔧 修复与优化**

* 修复人格获取失败问题：`_get_current_persona_name` 增加 `persona_manager` 回退逻辑，当 `conversation_manager` 获取不到人格时，尝试从 `persona_manager.get_default_persona_v3()` 获取
* 修复 `/自拍` 命令不自动存图问题：新增 `after_message_sent` 钩子，捕获命令方式生成的图片并自动存图（原仅支持 LLM 工具调用路径）
* 增加人格获取调试日志，便于排查人格识别问题

### v1.6.3

**🔧 修复与优化**

* 简化自动存图配置：删除冗余的 `auto_save_aiimg_follow_conversation` 和 `auto_save_aiimg_default_persona`，自动存图直接使用当前对话人格
* 日志增强：存图日志增加「用户描述」字段，便于调试
* WebUI 显示用户标签：图片详情弹窗新增 `user_tags` 字段显示

### v1.6.2

**🔧 修复与优化**

* WebUI 端口占用自动解决：当默认端口被占用时，自动尝试递增端口（最多10个），并在日志中提示实际使用端口
* API 错误处理增强：`/api/filters` 和 `/api/pools` 增加异常捕获，防止损坏数据导致 500 错误
* 前端错误处理：`api()` 函数增加非 200 状态码检查，避免静默失败
* 数据库初始化优化：增加 `_db_initialized` 标志，避免每次请求重复执行 `ALTER TABLE`
* 文件 I/O 异步化：`_load_custom_pools` 和 `save_custom_pools` 改为异步，避免阻塞事件循环
* 数据校验：自定义池子 JSON 数据增加类型校验，防止非 list 值导致崩溃

### v1.6.1

**🔧 配置界面优化**

* 人格配置改为列表式界面：新增 `personas` 配置项，每个人格独立配置规范名和别名
* 多人格自动存图：新增 `auto_save_aiimg_follow_conversation` 开关，自动存图可跟随当前对话人格
* 向后兼容：旧版 `persona_names` 和 `auto_save_aiimg_persona` 配置仍可使用

### v1.6.0

**✨ 新功能：AiImg 双向联动**

* 参考图接口：新增 `get_reference_image(query, current_persona)` 公开方法，供 AiImg 插件在生图时调用获取参考图
  - 搜索时硬排除当前人格的图库，避免同质化
  - 返回图片路径 + 描述信息，AiImg 可用描述辅助生成提示词
* 自动存图：监听 `aiimg_generate` 工具调用，自动将 AiImg 生成的图片存入衣柜库
* 配置项命名统一：`auto_save_gitee_enabled` → `auto_save_aiimg_enabled`

**🔧 改进**

* `ImageSearcher.search` 新增 `exclude_current_persona` 参数
* 智能人格搜索策略：根据指代意图（self/other/named/global）智能决定搜索范围

### v1.5.0

**✨ 新功能：WebUI 管理界面**

* 全新 Web 管理界面，支持图片浏览、搜索、上传、删除等管理
* 图片网格浏览、关键词搜索、批量操作、图片详情弹窗
* 简单密码认证，默认端口 18921

**✨ 新功能：人格子目录机制**

* 存图时支持指定人格名，图片归入对应人格目录
* 取图时模型自动判断是否按人格过滤
* 新增「人格名称列表」配置项，支持别名格式

**🔧 其他改进**

* AiImg 插件兼容：自动存图同时支持新旧插件
* 用户描述原样保存到 `user_tags` 字段
* 池子管理：WebUI 新增值池管理弹窗

### v1.0.0

**👗 首次发布：图片衣柜管理插件**

* 智能存图：视觉模型自动分析图片内容，生成结构化属性标签
* 语义检索：自然语言检索图片，取图模型解析意图并匹配
* LLM 工具注册：`save_wardrobe_image` 和 `search_wardrobe_image`
* 双模型配置与 Fallback：存图模型和取图模型分别配置，支持主备切换
* 管理指令：`/存图`、`/删图`、`/衣柜统计`
