# -*- coding: utf-8 -*-
"""
抓取岗位详情页（JD 全文）

用于对打分靠前的岗位逐条拉取完整 JD，便于判断真实匹配度 ——
列表页只有标题和薪资，看不出「要会 Hypermesh」「需 5 年车企经验」这类硬门槛。

详情页的薪资是明文（未使用字体混淆），但仍套用解码以防万一。

用法：
    PYTHONUTF8=1 py fetch_jd.py --min-score 115 [--limit 40]
    PYTHONUTF8=1 py fetch_jd.py --file data/ranked_jobs_all_上海.json --min-score 100
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from cdp_raw import evaluate, pick_page

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"

DETAIL_JS = """(() => {
  const decode = (s) => Array.from(s || "").map(c => {
    const cp = c.codePointAt(0);
    return (cp >= 0xE031 && cp <= 0xE03A) ? String(cp - 0xE031) : c;
  }).join("");
  const q = (s) => document.querySelector(s);
  const t = (s) => { const e = q(s); return e ? e.innerText.trim() : ""; };
  const secs = Array.from(document.querySelectorAll(".job-sec-text"));
  return {
    url: location.href,
    title: t(".info-primary h1"),
    salary: decode(t(".info-primary .salary")),
    city: t(".text-city"),
    experience: t(".text-experiece"),
    degree: t(".text-degree"),
    jdText: secs.length ? secs[0].innerText.trim() : "",
    companyBlurb: secs.length > 1 ? secs[1].innerText.trim() : "",
    sidebar: t(".sider-company"),
    loaded: !!q(".job-sec-text"),
  };
})()"""


def get_ws():
    t = pick_page()
    if t:
        return t["webSocketDebuggerUrl"]
    # 自愈：调试浏览器被关掉时自动拉起（今天 Edge 多次中途消失的教训）
    import subprocess
    ps = Path(__file__).resolve().parent / "start_edge.ps1"
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(ps)], capture_output=True, timeout=30)
    except Exception:
        pass
    for _ in range(10):
        time.sleep(3)
        t = pick_page()
        if t:
            time.sleep(4)  # 等页面加载完
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("没有可用的 BOSS 页面（自动拉起 Edge 也失败）")


def safe_eval(ws, expr, retries=3, delay=1.5):
    for _ in range(retries):
        try:
            res = evaluate(ws, expr)
            inner = (res.get("result") or {}).get("result") or {}
            if "value" in inner:
                return inner["value"]
        except Exception:
            pass
        time.sleep(delay)
    return None


def goto(ws, url):
    try:
        evaluate(ws, f'(() => {{ location.href = "{url}"; return "go"; }})()')
    except Exception:
        pass
    time.sleep(1.5)


def wait_loaded(ws, timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = safe_eval(ws, DETAIL_JS, retries=1)
        if isinstance(v, dict) and v.get("loaded"):
            return v
        time.sleep(1.5)
    return None


def main() -> int:
    args = sys.argv[1:]
    min_score = 115
    limit = 60
    src = None
    if "--min-score" in args:
        min_score = int(args[args.index("--min-score") + 1])
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    if "--file" in args:
        src = Path(args[args.index("--file") + 1])
    if src is None:
        cands = sorted(DATA.glob("ranked_*.json"), key=lambda p: p.stat().st_mtime)
        if not cands:
            print("[!] 没有 ranked_*.json")
            return 1
        src = cands[-1]

    rows = json.loads(src.read_text(encoding="utf-8"))

    if "--titles" in args:
        kws = args[args.index("--titles") + 1].split("|")
        targets = []
        for k in kws:
            hit = next((r for r in rows if k in (r.get("title") or "")), None)
            if hit:
                targets.append(hit)
            else:
                print(f"[!] 未找到: {k}")
    else:
        targets = [r for r in rows if r.get("_score", 0) >= min_score][:limit]

    print(f"数据源: {src.name}")
    print(f"待抓 {len(targets)} 条（分数 >= {min_score}）\n")

    out_rows = []
    for i, r in enumerate(targets, 1):
        url = "https://www.zhipin.com" + r["jobUrl"]
        ws = get_ws()
        print(f"[{i}/{len(targets)}] {r['title'][:30]}", flush=True)
        goto(ws, url)

        ws = get_ws()
        detail = wait_loaded(ws)
        if not detail:
            print("      [!] 加载超时，跳过", flush=True)
            continue

        detail["_score"] = r.get("_score")
        detail["_company_list"] = r.get("company")
        detail["_location_list"] = r.get("location")
        detail["_query"] = r.get("_query")
        out_rows.append(detail)
        print(f"      JD {len(detail['jdText'])} 字 | {detail['salary']} | "
              f"{detail['experience']} {detail['degree']}", flush=True)
        time.sleep(1.5)

    suffix = (f"batch_{datetime.now().strftime('%H%M%S')}"
              if "--titles" in args else f"score{min_score}")
    out = DATA / f"jd_{src.stem.replace('ranked_', '')}_{suffix}.json"
    out.write_text(json.dumps(out_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[+] 抓到 {len(out_rows)} 条 JD -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
