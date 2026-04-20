### v1.6.5

**🐛 Bug 修复**

* 修复自动存图不区分生成模式的问题：自动存图现在仅保存自拍模式生成的图片，文生图/改图不再自动存入
* 修复自动存图配置描述与实际行为不一致的问题：hint 已修正为"仅保存自拍模式"
* 修复 `_last_image_by_user` 类型变更后的兼容问题：使用 `isinstance(entry, dict)` 判断新旧格式

**📝 文档**

* 更新 AIIMG_DEV_GUIDE.md 与当前代码同步
* 新增 HANDOFF.md 交接文档（本地维护，不上传 GitHub）

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
