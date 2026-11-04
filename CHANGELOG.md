# Changelog

## [2.1.3] — 2026-11-04

### 修复
- **扩展同步必然失败**：`/api/ingest` 自己在锁里把状态置成「正在刷新」，紧接着调用的 `reclassify()` 又检查这个标记，
  于是永远返回 500「App 正在刷新，等它跑完再改分类」——数据其实已经落盘，但扩展只能看到失败。
  现在 `reclassify(force=True)`：调用方已经持锁时不再自锁。这条是扩展桥真正跑不通的根因之一。
- 补一条回归测试（断言 ingest 以 `force=True` 调 `reclassify`），测试总数 45。

## [2.1.2] — 2026-11-04

扩展桥和同步入口的可靠性修复。如果你装了浏览器扩展，**必须到 chrome://extensions 点一次 ↻ 重新加载**，
弹窗右上角会显示 `v1.2.1` —— 看不到版本号就说明还在跑旧代码。

### 修复
- **同步失败却显示「已捕获 ✓」**：popup 在同步返回后无条件把捕获状态设成成功，失败时界面自相矛盾。
  现在按实际结果走，失败会重新探测页面状态。
- **导航后捕获状态被重置导致空抓**：页面重新加载会把桥接状态清空，此时同步必然报「没抓到书签请求」。
  后台现在会自动重开书签页再抓一次，并在启动抓取失败（页面里没有桥接脚本）时先 reload 注入再重试。
- **扩展只抓到一部分时会覆盖掉本地全量书签**：`/api/ingest` 过去是整份覆盖 `raw_bookmarks.json`。
  现在按 id 与本地已有数据**合并**，扩展抓到多少都不会让已有的书签变少；返回里多一个 `received` 表示本次收到多少条。
- 失败提示带上诊断信息（桥接是否在、有没有捕获、hits 数、当前页面），方便定位问题。

### 新增
- `tests/test_ingest.py`：10 条测试覆盖 id 提取、合并顺序、部分抓取不回退、坏文件容错、likes 模式。
- 扩展 popup 右上角显示扩展版本，方便确认是否 reload 成功。

## [2.1.1] — 2026-11-04

### 修复
- **扩展桥重写导航逻辑**：改为直接打开 `https://x.com/i/history/bookmarks`（书签标签预选中，页面加载时自己就会请求），不再依赖"点中书签标签"这一步 —— 那是之前「还没捕获」的主因。
- 导航后先确认页面里有没有桥接脚本，没有就自动 reload 一次（装扩展之前就开着的标签页不会注入脚本）。
- 失败时自动 reload 重试，最多 3 轮；弹窗现在会显示具体卡点（没注入脚本 / 没找到书签标签 / 没发出请求）。
- `manifest.json` 加 `minimum_chrome_version: "111"`（`world:"MAIN"` 需要 111+），`host_permissions` 补 `127.0.0.1` / `localhost`。
- 桌面端实测：直接打开 `x.com/i/history/bookmarks` 会触发 `/Bookmarks` 请求，页面侧脚本一次拿到 94 条（1 页）。

## [2.1.0] — 2026-11-04

### 新增

- **批量改分类**：工具栏「选择」（快捷键 `S`）→ 点选多条 → 底部操作条一键改；后端 `POST /api/item_batch`，
  一次请求、只重算一遍分类，支持「全选当前」「取消选择」「恢复自动」。
- **快捷键**：`/` 搜索、`J/K` 上下、`Enter/O` 开原帖、`D` 详情、`S` 多选、`X` 选当前、
  `1`–`5`/`0` 切分类、`E` 打开导出、`?` 快捷键面板、`Esc` 关闭。光标用 `outline` 标出，不抢焦点。
- **导出**：
  - `Markdown / Obsidian` — zip，每类一个文件夹、一条书签一个 `.md`，YAML frontmatter（url/author/name/date/category/media/sensitive）
  - `Raindrop` — `url,title,folder,tags,created,notes`，可直接 Raindrop.io → Import
  - `Notion` — `Name,URL,Category,Author,Date,Text`
  - CSV / Excel 保留；所有导出默认**不含私密**，抽屉里勾「导出时包含私密」才带（`?private=1`）
- 设置抽屉「数据」一节重排：四个导出按钮 + 包含私密开关 + Markdown 说明。

### 变更

- `VERSION` → `2.1.0`；`serve.py` 新增 `CAT_LABEL`、`_slug()`、`_yaml_str()`、`export_items()`、
  `build_markdown_zip()`、`build_table_csv()`、`Handler.send_bytes()`。
- `web/app.html`：「批量条」「快捷键面板」的 DOM 放在 `<script>` 之前（脚本执行时就能绑事件，避免首屏中断）。
- README 中英版补「批量操作与快捷键」「导出」两节与新截图（`docs/batch.png`、`docs/export.png`、`docs/help.png`）。

## [2.0.1] - 2026-11-04

一次以「别带 bug 发布」为目标的加固：三路只读审计（后端 / 扩展 / 前端）+ 运行时冒烟，修掉 6 个真问题。

### 修复

- **隐私（高）**：私密卡片之前只是 CSS 遮住，正文和图片仍在 DOM 里，Ctrl+F / 查看器能读到 → 锁定状态下不再渲染正文和 `<img>`
- **安全（高）**：`/api/settings` 不校验类型与范围，`{"port": "abc"}` 这类值会落盘并让下次启动崩溃 → 全部字段先过 `sanitize_settings()`，非法值回退并返回 `warn`
- **安全（高）**：`settings.json` 非原子写 + 解析失败静默退回默认 → `access_code` 可能变空，而在 `0.0.0.0` 监听时等于全网无鉴权 → 原子写 + `.bak` 恢复 + 空码不再放行局域网
- **安全（高）**：`/api/unlock` 无退避、cookie 里存的是明文访问码 → 按 IP 指数退避（429）、cookie 改 HMAC 签名令牌、`/api/state` 只下发白名单字段（不再吐 `llm_key`、`chrome_path`）
- **数据（高）**：`items.json` 直接 `open("w")` 流式写，崩溃/断电会留下半截文件，下次读到空数据再覆盖一次 → 临时文件 + `os.replace` + `.bak`；读不出来时报错而不是显示 0 条
- **数据（高）**：Excel 正开着 `exports/bookmarks.xlsx` 时 `wb.save` 抛 PermissionError，整轮分类白跑 → 写 `.tmp` 再替换，失败只告警
- **中**：`reclassify` 与刷新无互斥（并发写同一个 `items.json`）、icon 路由可路径穿越、子进程按 UTF-8 读 cp936 输出导致中文乱码、`kill_our_chrome` 双反斜杠匹配恒失败、`body_json` / unlock 无体积上限、`allow_reuse_address` 设置无效、慢连接拖死线程（`Handler.timeout=15`）
- **中**：`organize.py` 忽略 `settings.json` 里的 `data_dir` / `export_dir`
- **中**：私密关键词含单字 `乳` `喷` 等，`乳制品真好吃` 会被判成私密 → 改为 ≥2 字词，并补 `骚货 / 淫荡 / 淫秽 / 乳首 / 喷水`
- **中**：`media_only_smooth` 用的是全局敏感率（708/1028 恒过闸），会把普通账号的纯图帖也拖进去 → 改为按作者统计；`author_smooth` 要求同一作者 ≥2 条敏感帖才画像
- **低**：`cdp.py` 的 `drain()` 把 socket 超时改成 2s 不恢复、事件无限堆积；`semantic.py` 模型缺失时直接抛 traceback、`semantic.json` 非原子写
- **扩展（重写）**：等标签页 `complete` 再注入（原来盲打 `executeScript` 并吞掉异常）、按 `[role="tab"][href*="/bookmarks"]` 点书签页（原来只认中英文字）、结果按 200 条分块搬回（原来一次性 structured-clone 几十 MB）、校验响应体的 `ok`（原来 202 也当成功）、并发锁、`minimum_chrome_version: 111`、剔除 `content-length` 等重放头

### 新增测试

- `tests/test_organize.py` 14 → 20 条：私密词表不含单字、日常词不误判私密、`write_items` 原子写并留 `.bak`、`data_dir` 生效、`sanitize_settings` 夹取与丢弃未知键、`_code_token` 加盐稳定、跨账号不被连带

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
