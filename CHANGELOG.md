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
