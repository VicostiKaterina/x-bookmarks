# -*- coding: utf-8 -*-
"""代理选择逻辑的单元测试（v2.5.1）。

真实教训：settings.json 里存着 http://127.0.0.1:7897，用户的代理软件早换成 7888，
采集浏览器被塞了个死代理，x.com 打不开，每次刷新白等到超时。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve


class ProxyPick(unittest.TestCase):
    def test_alive_detects_closed_port(self):
        # 用 1 号端口（几乎必然没人监听）当作「死代理」
        self.assertFalse(serve._proxy_alive("http://127.0.0.1:1", timeout=0.2))

    def test_alive_accepts_bare_hostport(self):
        # 不带 scheme 也要能解析，不抛出
        self.assertIn(serve._proxy_alive("127.0.0.1:1", timeout=0.2), (True, False))

    def test_dead_setting_falls_back_to_system(self):
        with mock.patch.object(serve, "_proxy_alive", side_effect=lambda p, timeout=0.4: str(p).endswith(":7888")), \
             mock.patch.object(serve, "_system_proxy", return_value="http://127.0.0.1:7888"):
            self.assertEqual(serve._effective_proxy({"proxy": "http://127.0.0.1:7897"}),
                             "http://127.0.0.1:7888")

    def test_empty_setting_uses_system(self):
        with mock.patch.object(serve, "_proxy_alive", return_value=True), \
             mock.patch.object(serve, "_system_proxy", return_value="http://127.0.0.1:7888"):
            self.assertEqual(serve._effective_proxy({"proxy": ""}), "http://127.0.0.1:7888")

    def test_good_setting_wins(self):
        with mock.patch.object(serve, "_proxy_alive", return_value=True), \
             mock.patch.object(serve, "_system_proxy", return_value="http://127.0.0.1:7888"):
            self.assertEqual(serve._effective_proxy({"proxy": "http://127.0.0.1:7890"}),
                             "http://127.0.0.1:7890")

    def test_nothing_usable_returns_empty(self):
        with mock.patch.object(serve, "_proxy_alive", return_value=False), \
             mock.patch.object(serve, "_system_proxy", return_value=""):
            self.assertEqual(serve._effective_proxy({"proxy": "http://127.0.0.1:1"}),
                             "", "两个都不通就别塞死代理，让 Chrome 自己决定")

    def test_system_proxy_parses_scheme_list(self):
        # http=127.0.0.1:7888;https=127.0.0.1:7888 这种写法要能挑出 https
        import winreg
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        with mock.patch("winreg.OpenKey", return_value=fake), \
             mock.patch("winreg.QueryValueEx", side_effect=[(1, 0), ("http=1.1.1.1:1;https=2.2.2.2:2", 0)]):
            self.assertEqual(serve._system_proxy(), "http://2.2.2.2:2")


if __name__ == "__main__":
    unittest.main()
