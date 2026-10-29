# -*- coding: utf-8 -*-
"""
BOSS 直聘岗位抓取（裸 CDP 版）

关键设计：
- 全程只发 Runtime.evaluate，不碰 Playwright —— Playwright 的 connect_over_cdp
  会被识别并导致页面被弹回首页。
- 导航用页面内 location.href，等效于点击站内链接。
- 结果是无限滚动，不是分页；靠反复滚到底把全部结果加载出来。
- 薪资还原：kanzhun-mix 混淆字体私用区码点 U+E031~U+E03A 顺序对应 0~9。
  若 BOSS 换字体：把新字体用页面 canvas 渲染成图、人工比对数字样张重新标定
  码点表；运行时的码点自检与降级见 check_font() 与 README「已知限制」。

用法：
    PYTHONUTF8=1 py fetch_jobs.py "NVH" 101020100 [--max-scrolls N]
"""
import json
import sys
import time
import urllib.parse
from pathlib import Path

from cdp_raw import evaluate, pick_page

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "data"
OUT_DIR.mkdir(exist_ok=True)

SEARCH_URL = "https://www.zhipin.com/web/geek/jobs?query={q}&city={c}&industry=&position="

EXTRACT_JS = """(() => {
  const decode = (s) => Array.from(s || "").map(c => {
    const cp = c.codePointAt(0);
    return (cp >= 0xE031 && cp <= 0xE03A) ? String(cp - 0xE031) : c;
  }).join("");
  const cards = Array.from(document.querySelectorAll(".job-card-box"));
  return cards.map(li => {
    const q = (sel) => li.querySelector(sel);
    const nameEl = q("a.job-name");
    const compEl = q("a.boss-info");
    const salEl = q(".job-salary");
    const locEl = q(".company-location");
    const compNameEl = q(".boss-name");
    return {
      title:      nameEl ? nameEl.textContent.trim() : "",
      jobUrl:     nameEl ? nameEl.getAttribute("href") : "",
      salary:     salEl  ? decode(salEl.textContent)  : "",
      tags:       Array.from(li.querySelectorAll("ul.tag-list li")).map(e => e.textContent.trim()),
      company:    compNameEl ? compNameEl.textContent.trim() : "",
      companyUrl: compEl ? compEl.getAttribute("href") : "",
      location:   locEl  ? locEl.textContent.trim() : "",
      isHeadhunter: !!li.querySelector('img.job-tag-icon[alt*="猎头"]'),
    };
  });
})()"""

COUNT_JS = '(() => document.querySelectorAll(".job-card-box").length)()'
SCROLL_JS = '(() => { window.scrollTo(0, document.body.scrollHeight); return document.body.scrollHeight; })()'

# 字体自检：抓原始码点（未解码）。已知映射只有 U+E031~U+E03A 十个码点，
# 私用区里出现其它码点 = BOSS 换了混淆字体文件，解码结果不可信。
# 局限（如实记录）：若新字体仍在同一区间但重排数字顺序，码点自检发现不了——
# 那种情况靠 hunt.py 的「全量薪资解析失败 → 降级」兜底。
FONT_PROBE_JS = """(() => {
  const els = Array.from(document.querySelectorAll(".job-salary")).slice(0, 12);
  return els.map(e => Array.from(e.textContent).map(c => c.codePointAt(0)));
})()"""


def check_font(ws):
    """字体混淆健康检查。True=码点都在已知区间；False=发现未知码点，应降级。"""
    codes = safe_eval(ws, FONT_PROBE_JS, retries=1)
    if not isinstance(codes, list) or not codes:
        return True  # 拿不到样张不误报，空结果由 scrape 原有路径兜底
    for seq in codes:
        if not isinstance(seq, list):
            continue
        for cp in seq:
            if 0xE000 <= cp <= 0xF8FF and not (0xE031 <= cp <= 0xE03A):
                return False
    return True


def get_ws():
    t = pick_page()
    if not t:
        raise RuntimeError("没有可用的 BOSS 页面")
    return t["webSocketDebuggerUrl"]


def safe_eval(ws, expr, retries=3):
    for _ in range(retries):
        try:
            res = evaluate(ws, expr)
            inner = (res.get("result") or {}).get("result") or {}
            if "value" in inner:
                return inner["value"]
        except Exception:
            pass
        time.sleep(1.5)
    return None


def wait_for_cards(ws, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        n = safe_eval(ws, COUNT_JS, retries=1)
        if isinstance(n, int) and n > 0:
            return n
        time.sleep(1.5)
    return 0


def goto(ws, url):
    try:
        evaluate(ws, f'(() => {{ location.href = "{url}"; return "go"; }})()')
    except Exception:
        pass
    time.sleep(2)


def scroll_all(ws, max_scrolls=25, settle=2.5, quiet_stop=3):
    """反复滚到底，直到卡片数连续 quiet_stop 次不再增长。"""
    prev = -1
    quiet = 0
    for i in range(max_scrolls):
        safe_eval(ws, SCROLL_JS, retries=1)
        time.sleep(settle)
        n = safe_eval(ws, COUNT_JS, retries=1)
        if not isinstance(n, int):
            continue
        print(f"    滚动 {i+1:>2}: {n} 条")
        if n == prev:
            quiet += 1
            if quiet >= quiet_stop:
                break
        else:
            quiet = 0
        prev = n
    return prev


def scrape(query, city, max_scrolls=25, verbose=True):
    url = SEARCH_URL.format(q=urllib.parse.quote(query), c=city)
    ws = get_ws()
    if verbose:
        print(f"[*] 「{query}」@ {city}")
    goto(ws, url)

    ws = get_ws()
    n = wait_for_cards(ws)
    if n == 0:
        print("[!] 没等到结果，可能无结果或加载失败")
        return []
    if verbose:
        print(f"    初次 {n} 条，开始滚动加载")
    total = scroll_all(ws, max_scrolls=max_scrolls)
    if verbose:
        print(f"    滚动结束，{total} 条")

    rows = safe_eval(ws, EXTRACT_JS)
    rows = [r for r in (rows if isinstance(rows, list) else []) if r.get("title")]
    if rows and not check_font(ws):
        print("[!] 字体自检未通过：薪资码点超出已知映射区间（疑似 BOSS 换了混淆字体）。"
              "本轮薪资不参与过滤与加分（降级运行）。")
        for r in rows:
            r["_font_warn"] = True
    return rows


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    max_scrolls = 25
    if "--max-scrolls" in sys.argv:
        max_scrolls = int(sys.argv[sys.argv.index("--max-scrolls") + 1])

    query = args[0] if args else "NVH"
    city = args[1] if len(args) > 1 else "101020100"

    rows = scrape(query, city, max_scrolls=max_scrolls)
    if not rows:
        return 1

    seen, uniq = set(), []
    for r in rows:
        key = r.get("jobUrl") or r.get("title")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    out = OUT_DIR / f"jobs_{query}_{city}.json"
    out.write_text(json.dumps(uniq, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[+] 抓到 {len(rows)} 条，去重后 {len(uniq)} 条 -> {out}")
    if uniq:
        print("样例:", json.dumps(uniq[0], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
