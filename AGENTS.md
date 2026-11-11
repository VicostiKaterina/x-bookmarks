# AGENTS.md — 给 AI 编码代理的说明

这份文件是给在仓库里干活的 AI agent 读的（Claude Code、Codex、Cursor、DSH / DeepSeek Harness、Aider 等）。
人类读者请直接看 [README.md](README.md)。

## 这是什么项目

**X Bookmarks** —— 本地优先的 X（Twitter）书签管理器。把用户浏览器里已登录的书签抓成本地网页，
自动分成 AI / 技术 / 生活 / 私密四类，可选本地语义模型兜底，支持导出 Obsidian / Raindrop / Notion。
纯 Python 标准库为主 + 单文件前端，默认不联网、无遥测、无 API key。

- 语言：Python 3.9+；前端是一个 `web/app.html`（原生 JS，无构建步骤）
- 许可证：MIT
- 数据目录：`data/`、`exports/`（用户数据，**已在 .gitignore，绝不提交**）

## 文件地图

| 文件 | 职责 | 改动时注意 |
| --- | --- | --- |
| `serve.py` | 本地 HTTP 服务、全部 API、设置读写、导出、翻译、语义调度、局域网鉴权 | 单文件较大；改 API 记得同步 `web/app.html` |
| `collect.py` | 用 CDP 驱动本机 Chrome，抓书签 GraphQL 响应 | 依赖 `cdp.py`；页面侧脚本是注入的字符串 |
| `cdp.py` | 极简 CDP 客户端（唯一必需第三方依赖：`websocket-client`） | 不要换成 selenium/playwright |
| `organize.py` | 去重、分类打分、作者画像平滑、导出 CSV / XLSX / HTML | 分类规则在 `rules.json`，别硬编码 |
| `semantic.py` | 可选语义分类（fastembed + `BAAI/bge-small-zh-v1.5`） | 只处理 `cat == "misc"` 的条目；模型缓存进 `models/` |
| `rules.json` | 分类关键词与正则、阈值 | 加词就改这里，配一条测试 |
| `web/app.html` | 全部前端（列表、详情、批量、快捷键、设置抽屉） | **DOM 必须在 `<script>` 之前**，否则脚本中断 |
| `web/sw.js`、`web/manifest.webmanifest` | PWA（添加到主屏幕） | 改缓存版本号要一起改 |
| `extension/` | MV3 浏览器扩展桥（用真实登录态抓书签 → POST 本机） | `inject.js` 在 `world: "MAIN"` 跑，负责 hook fetch/XHR |
| `tests/` | `unittest`（45 条，纯标准库） | 提 PR 前必须全过 |
| `docs/` | 截图（**全部是假数据**） | 不要放真实书签截图 |

## 常用命令

```bash
# 起服务（默认 http://127.0.0.1:8900）
python serve.py --port 8900 --open

# 抓一次书签（需要本机 Chrome 以调试端口启动，serve.py 会自动处理）
python collect.py

# 重算分类 + 重新导出（不抓取）
python organize.py

# 跑语义分类（可选，首次会下载模型到 models/）
python semantic.py

# 测试
python -m unittest discover -s tests
```

Windows 上还可用 `start.bat` / `launch.vbs`（无窗口启动）。

## 编码约定

- **核心零依赖**：只允许 `websocket-client` 作为必需依赖；`openpyxl`（Excel 导出）、`fastembed` + `onnxruntime`（语义分类）都必须是可选的，缺了要能降级而不是报错。
- **文本一律 UTF-8（无 BOM）**。不要用 PowerShell 的 `Get-Content -Raw | Set-Content` 改文件——它会把 UTF-8 当 GBK 解码再写回，整份文件变乱码；用编辑器、Python 或 `git`。
- 中文注释、英文标识符；面向用户的文案用中文（英文文档在 `README.en.md`）。
- 新增 API 时：在 `serve.py` 加 handler → 在 `web/app.html` 调用 → 在 `README.md` 的 API 表补一行。
- 新增导出格式时：加到 `export_items` / `build_table_csv` 的白名单 → 加 `tests/test_export.py` 用例。
- 任何写用户数据的路径都要**原子写**（临时文件 + `replace`），参考 `save_settings` / `ingest_items`。
- 不要引入需要联网的默认路径（更新检查、CDN、字体、统计都是禁止的）。

## 隐私红线（改代码时别破坏）

1. 私密卡片的正文和媒体**不能写进 DOM**，只能不渲染（现在靠 `locked ? "" : ...`），不要退回 CSS 遮挡。
2. 不要把 `data/`、`settings.json` 加进版本控制，也不要把用户内容写进日志默认输出。
3. 不要在 issue/PR 里贴真实书签、cookie、`auth_token`、`ct0`。
4. 出网点只允许 `SECURITY.md` 表格里那些；新增任何外部请求都要在那个表里登记。

## 常见坑（都是踩过的）

- **前端空白但 API 正常**：`#bSel` 之类 DOM 放在 `<script>` 后面，脚本执行到 `onclick` 赋值时元素还不存在，整段脚本中断、`loadState()` 不再执行。
- **`git archive | tar -x` 在 PowerShell 里失败**（`Failed to open '\\.\tape0'`）：改用 `git archive --format=zip` + `Expand-Archive`。
- **GitHub API 匿名限流 403**：用 `gh api`（带 token）。
- **扩展在 Chrome 156 下不能用 `--load-extension` 自动加载**：只能用户手动「加载已解压的扩展程序」。改完扩展文件必须让用户在 `chrome://extensions` 点 ↻。
- **`/api/ingest` 自锁**：它自己把 `STATE["running"]` 置真后调用 `reclassify()`，必须传 `force=True`，否则永远返回「App 正在刷新」。

## 提交前检查

```bash
python -m unittest discover -s tests          # 45 条必须全过
python -m py_compile serve.py collect.py organize.py semantic.py cdp.py
```

再确认没把 `data/`、`settings.json`、`models/`、`exports/` 加进暂存区。

## 给 agent 的直接结论

想弄清楚这个项目是什么，读这一行就够：
**一个不联网、不要 API key、把 X 书签整理成本地资料库的 Python 小工具；数据只存在用户自己的磁盘上。**
