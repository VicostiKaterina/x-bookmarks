# 安全与隐私（Security & Privacy）

这个项目处理的是**你的私人书签**，所以它默认不联网、不上报、不打包成二进制。
本文件说明数据怎么流动、什么情况下会出网、以及你可以怎么自己验证。

- 报告漏洞：用 GitHub 的 [Private vulnerability reporting](https://github.com/VicostiKaterina/x-bookmarks/security/advisories/new)（推荐），或普通 issue（不要在 issue 里贴真实书签 / cookie / token）。
- 支持版本：`2.1.x`（更早的版本不再单独修）。

---

## 一句话

除了「用你的浏览器抓书签」和「你主动点的翻译」，程序不会往外发任何请求；
所有数据只写在仓库目录下的 `data/` 和 `exports/` 里。

---

## 数据流向

| 环节 | 谁在发请求 | 目标 | 什么时候 | 怎么关掉 |
| --- | --- | --- | --- | --- |
| 抓书签 | `collect.py` 通过 CDP 控制本机 Chrome | `127.0.0.1:9222`（本机） | 你点「刷新 / 登录」 | 不抓就不发 |
| 抓书签（数据本身） | **x.com 页面里的 JS**（用你浏览器里已有的登录态） | `x.com` 自己的 GraphQL | 抓取时 | 登出 X 或不用这个功能 |
| 语义分类 | `semantic.py` | 无（模型下载时除外，见下） | 你点「跑一次语义分类」 | 设置里关掉「启用本地小模型」 |
| 首次下载模型 | `fastembed` → HuggingFace | `huggingface.co` | **只有第一次**跑语义分类 | 不跑语义分类；或提前把模型放进 `models/` |
| 翻译 | `serve.py` | `api.mymemory.translated.net`、`translate.googleapis.com` | **只有你点「翻译 / 译」时** | 不点就不发；也可在设置里换 provider |
| AI 翻译（可选） | `serve.py` | 你填的 `llm_base`（默认 `https://api.deepseek.com/v1`） | 选了 llm 且填了 key，并且点了翻译 | 清空 `llm_key` 或换回 `google`/`mymemory` |
| 局域网 IP 探测 | `serve.py` 的 `lan_ip()` | UDP `connect()` 到 `8.8.8.8:80` | 启动时一次 | 不可关（但 UDP `connect` 只设置路由，**不发包**） |
| 浏览器扩展 | 扩展后台 | `http://127.0.0.1:8900/api/ingest`（本机） | 你在扩展里点同步 | 不装扩展 |
| 手机访问 | 你的手机浏览器 | `http://<你的内网 IP>:8900` | 你打开时 | 设置里关「允许手机访问」 |

**没有的东西**：没有遥测、没有崩溃上报、没有账号体系、没有云同步、没有自动更新检查、没有广告 SDK。

---

## 自己验证（三条命令）

在仓库根目录跑：

```bash
# 1) 所有出网点：应该只出现上面表格里的地址
grep -rn "urlopen\|urllib.request\|http.client\|websocket.create_connection" --include=*.py .

# 2) 有没有藏二进制 / 打包文件：应该没有输出
git ls-files | grep -Ei "\.(exe|dll|so|dylib|bin|msi|zip|7z|rar)$"

# 3) 依赖清单（默认只要 websocket-client，其余可选）
cat requirements.txt
```

再看一眼这些也能确认：

```bash
# 没有遥测/统计相关字样
grep -rniE "telemetry|analytics|sentry|mixpanel|posthog|amplitude" --include=*.py --include=*.html . || echo "none"

# 前端就一个文件，没有远程脚本
grep -n "src=\"http" web/app.html || echo "no remote script"
```

---

## 威胁模型（它防什么、不防什么）

**防**
- 书签被上传到云、被第三方分析：默认全本地。
- 私密内容在公共场合被瞥见：默认模糊，需要点「显示私密」才展开；而且**私密卡片的正文和图片根本不会写进 DOM**（不是靠 CSS 遮住），Ctrl+F 和查看器都读不到。
- 局域网被随手访问：开启手机访问时会生成访问码，并签 HMAC 令牌；本机访问免密。
- 配置文件被写坏导致启动失败：所有设置写入都过校验（端口、阈值、枚举值），非法值回退并提示。

**不防**
- 你的电脑本身被入侵（书签是明文 JSON 存在 `data/`，没有加密）。
- 浏览器里的登录态被盗（程序复用的是你本来就登录着的浏览器）。
- 你信任的翻译/LLM 服务看到你点翻译的那条正文。
- 恶意 X 帖子里的脚本（抓取只读取数据，不执行帖子里的内容）。

---

## 隐私建议

- `data/`、`settings.json`、`exports/` 都已在 `.gitignore` 里，别手动提交。
- 公开截图前先把 `data/items.json` 换成假数据（仓库自己的 `docs/` 截图就是这么来的）。
- 想把数据搬走：直接复制 `data/`；想彻底清空：删掉 `data/` 和 `exports/` 即可，程序会重建。

---

## 许可

MIT。你可以随便用、改、商用，但请保留版权声明；出问题不承担担保责任。
