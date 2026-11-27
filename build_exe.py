# -*- coding: utf-8 -*-
"""打包 Windows 免安装版：dist/XBookmarks/XBookmarks.exe，双击即用（不需要装 Python）。

用法（在仓库根目录）：
    pip install pyinstaller
    python build_exe.py

产物：dist/XBookmarks/ 整个文件夹就是免安装版（约 23 MB），连同 使用说明.txt 一起发出去即可。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "XBookmarks"
BUILD = ROOT / "build"
DIST = ROOT / "dist"


def main():
    for d in (BUILD, DIST):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--noconsole",
        "--name", NAME,
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--add-data", "%s;web" % (ROOT / "web"),
        "--add-data", "%s;." % (ROOT / "rules.json"),
        "--hidden-import", "websocket",
        "--hidden-import", "collect",
        "--hidden-import", "organize",
        "--hidden-import", "semantic",
        "--hidden-import", "cdp",
        "--collect-submodules", "websocket",
        "--exclude-module", "tkinter",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
        # 语义分类是可选功能，打进来要多 120 MB（numpy + onnxruntime + tokenizers），
        # 免安装版不带：点「语义分类」会明确提示改用 Python 版。
        "--exclude-module", "numpy",
        "--exclude-module", "fastembed",
        "--exclude-module", "onnxruntime",
        "--exclude-module", "tokenizers",
        "--exclude-module", "hf_xet",
        "--exclude-module", "transformers",
        "--exclude-module", "scipy",
        "--exclude-module", "sklearn",
        "--exclude-module", "pandas",
        "--exclude-module", "PIL",
        "--exclude-module", "lxml",
        "--exclude-module", "torch",
        str(ROOT / "serve.py"),
    ]
    print(" ".join(args))
    r = subprocess.run(args, cwd=str(ROOT))
    if r.returncode != 0:
        print("打包失败", r.returncode, file=sys.stderr)
        return r.returncode
    app = DIST / NAME
    exe = app / (NAME + ".exe")
    print("exe:", exe, exe.exists(), "%.1f MB" % (exe.stat().st_size / 1e6))
    # 附一份说明书 + 示例设置，方便直接发给别人
    readme = app / "使用说明.txt"
    readme.write_text(
        "X 书签 · 免安装版\n"
        "==================\n\n"
        "1. 双击 XBookmarks.exe，浏览器会打开 http://127.0.0.1:8900\n"
        "2. 右上角点「登录 / 换账号」，在弹出的窗口里登录 X\n"
        "3. 回到页面点「刷新」抓取书签\n\n"
        "所有数据都在这个文件夹里：\n"
        "  data\\        书签、分类结果、日志\n"
        "  settings.json 设置（端口、代理、访问码…）\n"
        "  .profile\\    登录状态（删掉就要重新登录）\n\n"
        "备份：整个文件夹复制走即可。\n"
        "卸载：删掉整个文件夹。\n\n"
        "需要 Chrome（或 Edge / Chromium 内核浏览器）。\n\n"
        "说明：免安装版没打包「语义分类」用的本地模型（会让体积涨到 150 MB），\n"
        "      点它只会提示一句。规则分类、搜索、排序、导出都正常。\n"
        "      要语义分类就用 Python 版：pip install fastembed onnxruntime\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
