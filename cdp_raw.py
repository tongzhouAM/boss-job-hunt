# -*- coding: utf-8 -*-
"""
裸 CDP 客户端 —— 绕开 Playwright 的自动化痕迹

动机：Playwright 的 connect_over_cdp 会发 Page.enable / Runtime.enable /
Target.setAutoAttach 等一串命令，BOSS 疑似据此识别自动化并把页面弹回首页。
本脚本只发一条 Runtime.evaluate，不做任何 enable、不注入、不设监听。

同时打印「连接前 / 连接后」的页面 URL，用来判断一次连接是否触发了跳转。

用法：
    PYTHONUTF8=1 py cdp_raw.py                 # 读取当前页面状态
    PYTHONUTF8=1 py cdp_raw.py "JS表达式"       # 自定义求值
"""
import json
import sys
import urllib.request

import websocket

ENDPOINT = "http://127.0.0.1:9222"

DEFAULT_EXPR = """(() => ({
  url: location.href,
  title: document.title,
  cards: document.querySelectorAll('[class*="job-card-wrapper"]').length,
  text: document.body.innerText.slice(0, 300)
}))()"""


def http_json(path):
    with urllib.request.urlopen(ENDPOINT + path, timeout=8) as r:
        return json.load(r)


def pick_page():
    try:
        targets = http_json("/json/list")
    except Exception:
        # 调试端口不可达（Edge 未启动/已关闭）——返回 None 而非抛异常，
        # 让调用方有机会自行拉起浏览器
        return None
    pages = [t for t in targets
             if t.get("type") == "page"
             and "zhipin.com" in (t.get("url") or "")
             and "socket-worker" not in (t.get("url") or "")]
    return pages[0] if pages else None


def evaluate(ws_url, expr):
    """建立 ws、发一条 Runtime.evaluate、收回结果、断开。"""
    ws = websocket.create_connection(ws_url, timeout=25, suppress_origin=True)
    try:
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True},
        }))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return msg
    finally:
        ws.close()


def main() -> int:
    expr = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXPR

    t = pick_page()
    if not t:
        print("[!] 没找到 BOSS 页面")
        return 1
    print(f"[连接前] {t['url']}")

    res = evaluate(t["webSocketDebuggerUrl"], expr)

    inner = (res.get("result") or {}).get("result") or {}
    if "value" in inner:
        val = inner["value"]
        if isinstance(val, (dict, list)):
            print(json.dumps(val, ensure_ascii=False, indent=2))
        else:
            print(val)
    else:
        print("[原始响应]", json.dumps(res, ensure_ascii=False)[:700])

    t2 = pick_page()
    print(f"[连接后] {t2['url'] if t2 else '(页面不复存在)'}")
    if t2 and t and t2["url"] != t["url"]:
        print("[!!] URL 在本次连接期间发生了变化")
    return 0


if __name__ == "__main__":
    sys.exit(main())
