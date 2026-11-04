# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.0.0] - 2026-11-04

第一个可发布版本：从「一个 Python 脚本」变成「本地 App」。

### 新增

- **本地 App 化**：`serve.py` 提供 `/api/state`、`/api/refresh`、`/api/login`、`/api/settings`、`/api/item`、`/api/translate`、`/api/ingest`、`/api/net`、`/api/semantic` 等接口；前端 `web/app.html` 单文件，无构建
- **采集**：`collect.py` 用 CDP 复用已登录 Chrome，走 X 网页 GraphQL 翻页（无 800 条上限）
- **分类**：`organize.py` 关键词/正则打分 + 敏感标记加权 + 作者画像平滑 + 纯媒体帖平滑；`rules.json` 可自行加词
- **手动改分类**：卡片「分类」按钮与详情弹层，存 `data/overrides.json`，优先级最高
- **判定理由**：详情里显示为什么归这一类（命中词 / X 标记敏感 / 语义模型 p 值 / 手动指定）
- **语义分类**：`semantic.py` 本地 embedding（`BAAI/bge-small-zh-v1.5`），给「待定」兜底，向量缓存 + 留一自检
- **翻译**：MyMemory / Google / OpenAI 兼容接口三级降级，带缓存与分片并发
- **隐私**：私密卡折叠、待定图模糊、「显示私密」只影响当前会话
- **手机 / 局域网**：绑定 `0.0.0.0` + 访问码解锁页 + cookie 记忆；PWA（manifest + service worker + 图标），手机端响应式布局
- **浏览器扩展桥**（实验性）：MV3 扩展用你日常 Chrome 的登录态抓书签，POST 到 `/api/ingest`
- **设置抽屉**：默认排序、模糊开关、自动刷新间隔、语义开关/阈值、局域网、代理、导出 CSV/Excel
- **空状态引导**、排序（时间/浏览/点赞/转发/收藏/回复）、「加载更多」分页、键盘 `/` 聚焦搜索
- 文档：README（截图、FAQ、配置表、发布说明）、`settings.example.json`、`LICENSE`(MIT)、`.gitignore`、`.gitattributes`

### 已知问题

- 扩展桥偶发「还没捕获书签请求」：需先刷新页面再抓；App 内 CDP 采集更稳
- Chrome 137+ 禁用了 `--load-extension`，扩展只能手动加载
- 依赖 X 网页 GraphQL 签名头，X 改接口时需要跟进

## [1.0.0] - 2026-11-03

- 初版脚本：导出 JSON → 关键词分类 → CSV / Excel / 单页 HTML
- 首次验证：104 条书签跑通全流程；随后扩到 1028 条
