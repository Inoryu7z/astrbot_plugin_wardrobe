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
