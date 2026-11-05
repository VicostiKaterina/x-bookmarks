# 参与开发

欢迎 issue / PR。这个项目刻意保持「零构建、纯 Python 标准库 + 一点 JS」的形态，改动时请尽量别引入构建步骤。

## 本地跑起来

```bash
pip install -r requirements.txt      # 只有 websocket-client 是必须的
python serve.py --open
```

语义分类是可选的，不装 `fastembed` 也能跑（只是「待定」不会被模型兜底）：

```bash
pip install fastembed onnxruntime
python semantic.py
```

## 自测

```bash
python -m unittest discover -s tests -v
```

改动分类逻辑（`organize.py` 的 `classify` / 平滑 / 覆盖）或 `norm()` 解析时，**请顺手补一条测试**；CI 会在 Ubuntu + Windows、Python 3.9 / 3.12 上跑。

## 想让它分类更准

最简单的贡献方式是改 `rules.json`：

- `categories.<cat>.keywords`：大小写不敏感的关键词
- `categories.<cat>.cs_keywords`：大小写敏感（`AI`、`GPT` 这类短词）
- `categories.<cat>.regex`：正则，命中 +2
- `thresholds.min_score`：低于这个分数归「待定」

改完跑 `python organize.py` 看分布，把「待定」里明显该归某一类的词补进去即可。

## 代码地图

| 文件 | 干什么 |
| --- | --- |
| `serve.py` | HTTP 服务、设置读写、刷新/登录/语义/局域网 API、访问码鉴权 |
| `collect.py` | CDP 采集：复用登录态，翻页 GraphQL，写 `data/raw_bookmarks.json` |
| `cdp.py` | 极简 DevTools Protocol 客户端（WebSocket） |
| `organize.py` | 归一化 → 分类 → 平滑 → 覆盖 → 导出 |
| `semantic.py` | 本地 embedding 语义分类（可选依赖） |
| `web/app.html` | 全部前端逻辑与样式（单文件） |
| `extension/` | MV3 扩展桥：旁听页面请求，POST 给本机 |

## 提交前

1. `python -m unittest discover -s tests -v` 全绿
2. `git status` 里不该有 `data/`、`settings.json`、`profile*/`、`models/` —— 那些是个人数据
3. 如果你的改动改了界面，附一张截图（`docs/` 下）

## 隐私约定

这个项目处理的是个人书签，可能包含私密内容。请**不要**在 issue / PR 里贴真实的书签内容、cookie、`x-csrf-token` 或任何账号数据；需要复现时用 `tests/` 里的假数据。
