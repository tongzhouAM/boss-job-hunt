# -*- coding: utf-8 -*-
"""
BOSS 直聘通用求职流水线：关键词 → 搜索 → 打分 → JD 核验 → 自动投递

引擎与配置分离：所有与人设/偏好相关的参数都在配置文件里（默认 config.json，
不存在则自动生成默认配置）。换求职方向或换人，只需换配置，不改代码。

用法：
    # 计划模式（默认）：搜索+打分+核验，列出「会投哪些」，不点击
    PYTHONUTF8=1 py hunt.py "解决方案工程师,AI产品经理" --city 上海

    # 全自动投递（仍受闸门+上限保护）
    PYTHONUTF8=1 py hunt.py "售前工程师" --city all --apply --yes

    # 使用其他配置（不同人设/方向）
    PYTHONUTF8=1 py hunt.py "测试开发" --config config.demo.json

人设从哪来：resume_wizard.py 从简历生成（--dry-run 可让 Claude 代生成），
详见该文件头注释与 README。

参数：
    --config FILE        配置文件（默认 config.json，缺失时自动生成默认配置）
    --city NAME|all      城市，覆盖配置里的 cities 键名（默认取配置第一个城市）
    --scrolls N          每关键词滚动轮数（默认 3）
    --top N              打分后取前 N 条抓 JD 核验（默认 25）
    --limit N            单次最多投 N 条（默认 10）
    --daily-cap N        每日投递上限（默认 20）
    --min-mid K          薪资中位数下限（默认取配置值）
    --loose              追加放行配置里的 loose_roles_extra
    --apply              真正投递（缺省只出计划）
    --yes                跳过投递前确认停顿

配置文件键（均可缺省，缺省用内置默认）：
    search_keywords      默认搜索关键词列表（命令行不给关键词时使用）
    cities               {城市名: 城市码}
    salary_min_mid       薪资中位数下限（K）
    allowed_roles        放行的职能 tag 列表
    loose_roles_extra    --loose 时追加放行的 tag
    experience_max_years 本人经验上限（JD 要求年限超过它则硬拦截，如本人9年→拦「10年以上」）
    degrees_reject       硬拦截的学历要求（如 ["博士"]）
    edu_warn_words       JD 正文出现 → 软警告⚠不拦截（如 ["统招","全日制"]）
    hard_skill_blacklist JD 正文出现即拦截的硬技能词
    role_tiers           职能打分档 [{keywords, points, tag}]
    industry             {high, mid, low} 行业词表
    ai_keywords          AI 加分词
    agency_keywords      猎头/外包公司识别词
    limits               {daily_cap, run_limit, scrolls, top, interval}
"""
import json
import random
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path

from cdp_raw import evaluate, pick_page
from fetch_jobs import scrape
from fetch_jd import get_ws as jd_get_ws, safe_eval as jd_safe_eval, goto as jd_goto, wait_loaded

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
APPLIED_LOG = DATA / "applied_log.json"

SALARY_RE = re.compile(r"(\d+)-(\d+)K(?:·(\d+)薪)?")
EXP_RE = re.compile(r"(\d+)\s*[-~到至]?\s*(\d+)?\s*年")

# 中性兜底配置：只保证引擎能跑、宁缺勿错，不携带任何人的人设。
# 正式使用先跑 resume_wizard.py 从简历生成，或参照 config.demo.json 手工填写。
# （历史注：这里曾内置「售前/技术销售」人设，2026-10-29 起人设全部外置到各自
#  config 文件——新用户零配置直接跑会全量过滤，属于故意的安全失败。）
DEFAULT_CONFIG = {
    "cities": {"北京": "101010100", "上海": "101020100", "深圳": "101280600", "杭州": "101210100"},
    "salary_min_mid": 0,
    "search_keywords": [],
    "allowed_roles": [],
    "loose_roles_extra": [],
    "experience_max_years": 10,
    "degrees_reject": ["博士"],
    "edu_warn_words": ["统招", "全日制"],
    "title_blacklist": ["校招", "应届", "实习", "兼职", "保险"],
    "domain_bonus": {"keywords": [], "points": 30},
    "hard_skill_blacklist": [],
    "role_tiers": [],
    "industry": {"high": [], "mid": [], "low": []},
    "ai_keywords": ["AI", "ai", "人工智能", "大模型", "智能体", "Agent"],
    "agency_keywords": ["人力资源", "劳务", "派遣", "外包", "人才服务"],
    "limits": {"daily_cap": 15, "run_limit": 10, "scrolls": 3, "top": 25, "interval": [8, 15]},
}

AFTER_CLICK_JS = """(() => {
  const btn = document.querySelector("a.btn-startchat");
  const txt = document.body.innerText.slice(0, 4000);
  const m = txt.match(/发送成功|招呼已发送|已成功发送/);
  return { btn: btn ? btn.innerText.trim() : "(gone)", toast: m ? m[0] : null };
})()"""


# ── 配置 ─────────────────────────────────────────────────

def deep_merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path_arg=None):
    path = Path(path_arg) if path_arg else BASE / "config.json"
    if not path.exists():
        if path_arg:
            print(f"[!] 配置文件不存在: {path}")
            sys.exit(1)
        path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"[*] 未找到配置，已生成默认配置 -> {path}")
        return DEFAULT_CONFIG
    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = deep_merge(DEFAULT_CONFIG, raw)
    if "cities" in raw:
        # 城市是集合语义：整表替换，不做递归合并（否则删掉的城市会被默认表并回）
        cfg["cities"] = raw["cities"]
    print(f"[*] 配置: {path.name}  （职能白名单: {','.join(cfg['allowed_roles']) or '(空)'}，"
          f"薪资下限 {cfg['salary_min_mid']}K）")
    if not cfg["allowed_roles"] or not cfg["role_tiers"]:
        print("[!] 当前配置没有人设（职能白名单/打分档为空）——直接跑会全量过滤。")
        print("    先生成人设: PYTHONUTF8=1 py resume_wizard.py --resume 你的简历.md --city 上海")
        print("    或参照 config.demo.json 手工填写。")
    return cfg


# ── 打分（配置驱动） ─────────────────────────────────────

def parse_salary(s):
    m = SALARY_RE.search(s or "")
    if not m:
        return None
    lo, hi = int(m.group(1)), int(m.group(2))
    months = int(m.group(3)) if m.group(3) else 12
    return {"lo": lo, "hi": hi, "months": months,
            "annual_wan": round((lo + hi) / 2 * months / 10, 1)}


def score_with(cfg, row):
    t = row.get("title", "") or ""
    ind_cfg = cfg["industry"]

    role_s, role_tag = 0, ""
    best = None
    for tier in cfg["role_tiers"]:
        for k in tier["keywords"]:
            if k in t:
                if best is None or tier["points"] > best[0]:
                    best = (tier["points"], tier["tag"])
                break
    if best:
        role_s, role_tag = best

    ind_s, ind_tag = 0, ""
    for words, pts, tag in ((ind_cfg["high"], 30, "工业相关"),
                            (ind_cfg["mid"], 10, "企业服务"),
                            (ind_cfg["low"], -30, "无关行业")):
        for k in words:
            if k in t:
                ind_s, ind_tag = pts, tag
                break
        if ind_tag:
            break

    ai = 8 if any(k in t for k in cfg["ai_keywords"]) else 0
    dom = cfg.get("domain_bonus") or {}
    dom_s = dom.get("points", 0) if any(k in t for k in dom.get("keywords", [])) else 0
    sal = parse_salary(row.get("salary"))
    sal_s = 0
    if sal:
        mid = (sal["lo"] + sal["hi"]) / 2
        if 20 <= mid <= 30:
            sal_s = 50
        elif 30 < mid <= 42:
            sal_s = 38
        elif mid > 42:
            sal_s = 25
        elif 17 <= mid < 20:
            sal_s = 5
        elif 13 <= mid < 17:
            sal_s = -25
        else:
            sal_s = -55
    else:
        sal_s = -10
    return role_s + ind_s + ai + dom_s + sal_s, sal, role_tag, ind_tag


# ── 闸门：硬拦截 + 软警告 ─────────────────────────────────
# 哲学（用户定调）：统招/全日制这类"差一点"的学历门槛不要自我审查，
# 照投让对面 judge；但经验年限差档（如要求10年以上）和博士、硬技能不匹配 → 硬拦。

def gates_with(cfg, item):
    """硬拦截：投了基本必拒的。"""
    r = []
    jd = item.get("jdText", "") or ""
    if (item.get("degree") or "") in cfg["degrees_reject"]:
        r.append(f"学历({item['degree']})")
    m = EXP_RE.search(item.get("experience") or "")
    if m and int(m.group(1)) > cfg["experience_max_years"]:
        r.append(f"要求{m.group(1)}年经验")
    hard = [s for s in cfg["hard_skill_blacklist"] if s in jd]
    if hard:
        r.append("硬技能:" + "/".join(hard[:2]))
    return r


def warns_with(cfg, item):
    """软警告：不拦截，计划里标注出来。"""
    w = []
    jd = item.get("jdText", "") or ""
    for k in cfg.get("edu_warn_words", []):
        if k in jd:
            w.append(k + "门槛")
            break
    return w


# ── 已投清单 / 预检 ───────────────────────────────────────

def load_applied():
    if APPLIED_LOG.exists():
        return json.loads(APPLIED_LOG.read_text(encoding="utf-8"))
    APPLIED_LOG.write_text("[]", encoding="utf-8")
    return []


def applied_today(log):
    today = date.today().isoformat()
    return sum(1 for x in log if str(x.get("at", ""))[:10] == today)


def ensure_edge(max_wait=30):
    import subprocess
    if pick_page():
        return True
    print("[*] 调试浏览器未启动，自动拉起 Edge……")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(BASE / "start_edge.ps1")], capture_output=True)
    for _ in range(max_wait // 3):
        time.sleep(3)
        if pick_page():
            time.sleep(4)
            return True
    return False


def preflight(cfg, do_apply):
    if not ensure_edge():
        print("[!] 预检失败：Edge 拉起后仍无 BOSS 页面")
        return False
    t = pick_page()
    # 登录检测轮询：Edge 刚拉起时页面未加载完会误报，最多等 ~20s（10-12 三次实踩）
    st = None
    for _ in range(6):
        st = jd_safe_eval(t["webSocketDebuggerUrl"],
                          """(() => ({
                              url: location.href,
                              login: !!document.querySelector('a[href*="/web/geek/resume"]')
                          }))()""", retries=1, delay=1.0)
        if st and st.get("login"):
            break
        time.sleep(3)
    if not st or not st.get("login"):
        print("[!] 预检失败：未检测到登录态，请先在浏览器扫码登录")
        return False
    print(f"[*] 预检通过：已登录")

    if do_apply:
        try:
            jd_goto(t["webSocketDebuggerUrl"],
                    "https://www.zhipin.com/web/geek/notify-set?type=greetSet")
            t = pick_page()
            # 等开关真正渲染出来（页面加载慢时会误报“未启用”），最多 10 秒
            # 注意：False 不能提前 break——开关元素先渲染、checked 类晚一拍挂上（React），
            # 早退会把"还没渲染完"误读成"确认关闭"。轮询到底取最后一次读数。
            v = None
            for _ in range(10):
                v = jd_safe_eval(t["webSocketDebuggerUrl"],
                                 '''(() => {
                                     const sws = document.querySelectorAll('.ui-switch');
                                     if (!sws.length) return null;
                                     // 只认外层 .ui-switch（[class*=ui-switch] 会误抓 ui-switch-inner 内层滑块）
                                     for (const el of sws) {
                                         let p = el.parentElement;
                                         for (let k = 0; k < 4 && p; k++) {
                                             if ((p.innerText || '').includes('启用招呼语')) {
                                                 return String(el.className).includes('ui-switch-checked');
                                             }
                                             p = p.parentElement;
                                         }
                                     }
                                     return String(sws[0].className).includes('ui-switch-checked');
                                 })()''',
                                 retries=1)
                if v is True:
                    break
                time.sleep(1)
            if v is True:
                print("[*] 招呼语开关：已启用")
            elif v is False:
                print("[!] 警告：招呼语开关确认处于关闭状态，投出的将是默认「你好」！")
                if "--yes" in sys.argv:
                    print("[!] 中止。请先到 APP 确认招呼语。")
                    return False
                print("    15 秒内 Ctrl+C 中止，否则继续……")
                time.sleep(15)
            else:
                print("[!] 招呼语开关状态未知，继续（请自行确认已在 APP 启用）")
        except Exception:
            print("[!] 招呼语开关状态未知，继续（请自行确认已在 APP 启用）")
    return True


# ── 主流程 ───────────────────────────────────────────────

def argval(flag, default=None, cast=str):
    a = sys.argv
    return cast(a[a.index(flag) + 1]) if flag in a else default


def main() -> int:
    # 位置参数 = 搜索关键词；带值 flag 的值不能被误认成关键词（如 --city all 的 "all"）
    VALUE_FLAGS = {"--config", "--city", "--limit", "--top", "--scrolls",
                   "--daily-cap", "--min-mid", "--from-plan"}
    pos, skip = [], False
    for a in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if a in VALUE_FLAGS:
            skip = True
            continue
        if a.startswith("--"):
            continue
        pos.append(a)
    keywords = [k.strip() for k in pos[0].split(",") if k.strip()] if pos else []

    cfg = load_config(argval("--config"))
    if not keywords and "--from-plan" not in sys.argv:
        # 无命令行关键词时退回配置里的默认搜索词
        keywords = [k.strip() for k in cfg.get("search_keywords", []) if k.strip()]
        if not keywords:
            print(__doc__)
            return 1
    lim = cfg["limits"]
    city_arg = argval("--city")
    cities = {city_arg: cfg["cities"][city_arg]} if city_arg and city_arg != "all" and city_arg in cfg["cities"] \
        else (cfg["cities"] if (city_arg == "all" or not city_arg) else None)
    if cities is None:
        print(f"[!] 未知城市 {city_arg}，配置里有: {list(cfg['cities'])}")
        return 1
    scrolls = argval("--scrolls", lim["scrolls"], int)
    top_n = argval("--top", lim["top"], int)
    limit = argval("--limit", lim["run_limit"], int)
    daily_cap = argval("--daily-cap", lim["daily_cap"], int)
    min_mid = argval("--min-mid", cfg["salary_min_mid"], float)
    lo, hi = lim["interval"]
    do_apply = "--apply" in sys.argv

    allowed = set(cfg["allowed_roles"]) | (set(cfg["loose_roles_extra"]) if "--loose" in sys.argv else set())
    applied = load_applied()
    applied_urls = {x["url"] for x in applied}
    # BOSS 的 job_detail 加密 URL 跨会话会变（同一岗位出现两个 URL），URL 去重不可靠。
    # 补充「标题内 J 编码」与「归一化标题+公司」两道去重（2026-09-20 补丁）
    _jc_re = re.compile(r"[（(]([A-Za-z]{1,6}\d{2,8})[)）]")

    def _jcode(t):
        m = _jc_re.search(t or "")
        return m.group(1).upper() if m else None

    def _ntitle(t):
        return "".join((t or "").split())

    def _co(x):
        c = x.get("company")
        if isinstance(c, list):
            c = c[0] if c else ""
        return (c or "").strip()

    applied_jcodes = {j for j in (_jcode(x.get("title")) for x in applied) if j}
    applied_tc = {(_ntitle(x.get("title")), _co(x)) for x in applied if _co(x)}

    if not preflight(cfg, do_apply):
        return 2

    plan_file = argval("--from-plan")
    if plan_file:
        final = json.loads(Path(plan_file).read_text(encoding="utf-8"))
        print(f"[*] 载入计划文件 {plan_file}：{len(final)} 条（跳过搜索/打分/核验）")
        if not do_apply:
            print("[计划模式] 计划文件已存在，无需重复出计划；加 --apply 执行投递。")
            return 0
    if not plan_file:
        # 1) 搜索
        rows, seen = [], set()
        for cname, ccode in cities.items():
            for q in keywords:
                print(f"\n=== 搜索 [{cname}] 「{q}」 ===")
                try:
                    got = scrape(q, ccode, max_scrolls=scrolls)
                except Exception as e:
                    print(f"[!] 抓取异常: {e}")
                    got = []
                for r in got:
                    key = r.get("jobUrl") or r.get("title")
                    if key in seen:
                        continue
                    seen.add(key)
                    r["_query"] = q
                    r["_city"] = cname
                    rows.append(r)
                print(f"    -> {len(got)} 条")
                time.sleep(2)

        # 2) 打分 + 过滤（带原因统计）
        # 薪资降级检测：列表页薪资依赖混淆字体解码，若 BOSS 换字体导致全量解析失败，
        # 跳过薪资过滤而不是把所有岗位误杀成「薪资过低」（配合 fetch_jobs 的码点自检）
        salary_degraded = bool(rows) and not any(parse_salary(r.get("salary")) for r in rows)
        if salary_degraded:
            print("[!] 薪资解析降级：本轮列表页拿不到可解析薪资（疑似混淆字体变更）。"
                  "跳过薪资过滤，薪资加分失效，其余流程照常。")
        cand, reasons = [], {}
        def bump(k):
            reasons[k] = reasons.get(k, 0) + 1
        for r in rows:
            _u = ("https://www.zhipin.com" + r["jobUrl"]) if r.get("jobUrl") else None
            _jc = _jcode(r.get("title"))
            _tc = (_ntitle(r.get("title")), (r.get("company") or "").strip())
            if ((_u and _u in applied_urls) or (_jc and _jc in applied_jcodes)
                    or (_tc[1] and _tc in applied_tc)):
                bump("已投过"); continue
            bl = [w for w in cfg.get("title_blacklist", []) if w in (r.get("title") or "")]
            if bl:
                bump("黑名单[" + "/".join(bl[:2]) + "]"); continue
            if r.get("isHeadhunter"):
                bump("猎头岗位"); continue
            agency = [w for w in cfg.get("agency_keywords", [])
                      if w in ((r.get("title") or "") + (r.get("company") or ""))]
            if agency:
                bump("人力机构[" + "/".join(agency[:2]) + "]"); continue
            sc, sal, role, ind = score_with(cfg, r)
            r.update({"_score": sc, "_sal": sal, "_role": role, "_ind": ind})
            if role not in allowed:
                bump(f"职能[{role or '无'}]"); continue
            if ind == "无关行业":
                bump("行业不符"); continue
            if not any(c in (r.get("location") or "") for c in cities):
                bump("城市不符"); continue
            if not salary_degraded and (not sal or (sal["lo"] + sal["hi"]) / 2 < min_mid):
                bump("薪资过低"); continue
            cand.append(r)
        cand.sort(key=lambda r: (-r["_score"], -(r["_sal"]["annual_wan"] if r["_sal"] else 0)))
        why = "  ".join(f"{k}:{v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
        print(f"\n[*] 抓取 {len(rows)} 条 → 通过 {len(cand)} 条（{why or '无过滤'}）")
        if not cand:
            print(f"[!] 提示：没有岗位通过基础过滤。当前白名单职能: {sorted(allowed)}；"
                  f"可加 --loose 放行 {cfg['loose_roles_extra']}，或 --min-mid 调低薪资线")
            return 1

        # 3) JD 核验
        targets = cand[:top_n]
        print(f"[*] 对前 {len(targets)} 条抓取 JD 做硬门槛核验……\n")
        final = []
        for i, r in enumerate(targets, 1):
            ws = jd_get_ws()
            jd_goto(ws, "https://www.zhipin.com" + r["jobUrl"])
            ws = jd_get_ws()
            item = wait_loaded(ws)
            if not item:
                print(f"  [{i}] {r['title'][:26]:<28} 加载失败，跳过")
                continue
            item["_company_list"] = r.get("company")
            item["_query"] = r.get("_query")
            item["_score"] = r["_score"]
            item["_sal"] = r["_sal"]
            item["_location"] = r.get("location")
            item["_role"] = r["_role"]
            g = gates_with(cfg, item)
            w = warns_with(cfg, item)
            item["_warn"] = w
            mark = "拦截(" + ";".join(g) + ")" if g else ("通过" + (" ⚠" + "/".join(w) if w else ""))
            print(f"  [{i}] [{r['_score']}] {r['title'][:26]:<28} {r['salary']:<12} {mark}")
            if not g:
                final.append(item)
            time.sleep(1.2)

        if not final:
            print("\n[!] 没有通过核验的岗位")
            return 1

        # 4) 计划
        print(f"\n{'='*70}\n通过核验 {len(final)} 条：")
        for i, it in enumerate(final[:limit], 1):
            print(f"  {i}. [{it['_score']}] {it['title'][:30]} | {it['salary']} | {it['_company_list']}")
        rest = len(final) - limit
        if rest > 0:
            print(f"  （其余 {rest} 条超出单次上限，下轮再投）")

        if not do_apply:
            print("\n[计划模式] 未投递。确认无误后加 --apply 执行投递。")
            out = DATA / f"hunt_plan_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
            out.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"计划已存 -> {out}")
            return 0

    # 5) 投递
    used = applied_today(applied)
    quota = min(limit, daily_cap - used)
    if quota <= 0:
        print(f"\n[!] 今日已投 {used} 条，达到上限 {daily_cap}")
        return 1
    print(f"\n[*] 今日已投 {used}/{daily_cap}，本次额度 {quota}")
    if "--yes" not in sys.argv:
        print("    10 秒后开始投递，Ctrl+C 中止……")
        time.sleep(10)

    sent = 0
    for it in final[:quota]:
        url, title = it["url"], it.get("title", "")
        ws = jd_get_ws()
        jd_goto(ws, url)
        # 全程锁死同一连接：原先 goto 后重取 ws，多标签页时会拿到另一个 tab，
        # 轮询在错误页面上找按钮 → 连环误判"无投递按钮"（10-14 实锤）
        st = None
        for _ in range(10):
            st = jd_safe_eval(ws, """(() => {
            const b = document.querySelector("a.btn-startchat");
            return {btn: b ? b.innerText.trim() : ""};
        })()""", retries=1, delay=1.0)
            if st and st.get("btn"):
                break
            time.sleep(2)
        st = None
        for _ in range(10):
            st = jd_safe_eval(ws, """(() => {
            const b = document.querySelector("a.btn-startchat");
            return {btn: b ? b.innerText.trim() : ""};
        })()""", retries=1, delay=1.0)
            if st and st.get("btn"):
                break
            time.sleep(2)
        if not st or not st.get("btn"):
            print(f"  [-] {title[:26]} 无投递按钮，跳过"); continue
        if "继续沟通" in st["btn"]:
            print(f"  [=] {title[:26]} 已沟通过，跳过"); continue
        jd_safe_eval(ws, '(() => { document.querySelector("a.btn-startchat").click(); return 1; })()',
                     retries=1)
        time.sleep(4)
        chk = jd_safe_eval(ws, AFTER_CLICK_JS, retries=2) or {}
        ok = bool(chk.get("toast")) or chk.get("btn") != "立即沟通"
        print(f"  [{'✓' if ok else '?'}] {title[:30]} | {it['salary']}")
        if ok:
            sent += 1
        applied.append({"url": url, "title": title,
                        "company": it.get("_company_list"), "at": datetime.now().isoformat()})
        APPLIED_LOG.write_text(json.dumps(applied, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(random.uniform(lo, hi))

    print(f"\n{'='*70}\n完成：本次投出 {sent}/{min(len(final), quota)} 条")
    print(f"已投清单 -> {APPLIED_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
