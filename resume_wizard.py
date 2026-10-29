# -*- coding: utf-8 -*-
"""
简历 → BOSS 人设配置向导（BYOK：用户自己的 LLM API key，本脚本不经手任何服务端）

把一份简历 + 三个答案（城市/薪资底线/目标方向）交给 LLM，产出 hunt.py 可直接
使用的配置 JSON。人设哲学与 hunt.py 一致：
  - 职能 + 行业双维度打分（漏维度的打分会高估匹配度，2026-09-14 的教训）
  - 软硬分级：「统招/全日制」这类差一点的学历门槛 → 软警告照投，让对面 judge；
    经验年限超档 / 博士门槛 / 简历没有的硬技能 → 硬拦
  - 限额保守：daily_cap 默认 ≤20，投递间隔 ≥8s

用法：
    # 全自动（需要 LLM_API_KEY）
    set LLM_API_KEY=sk-xxx
    PYTHONUTF8=1 py resume_wizard.py --resume 简历.md --city 上海,杭州 --salary-min 15

    # 干跑：只打印 prompt 不调 API —— 给 Claude/skill 模式用，
    # Claude 看完 prompt 规格后自己生成配置，再交给 --validate 校验
    PYTHONUTF8=1 py resume_wizard.py --resume 简历.md --city 上海 --dry-run > prompt.txt
    PYTHONUTF8=1 py resume_wizard.py --validate 生成的配置.json

环境变量：
    LLM_API_KEY    必填（--dry-run / --validate 除外）
    LLM_BASE_URL   默认 https://api.deepseek.com（任何 OpenAI 兼容端点均可）
    LLM_MODEL      默认 deepseek-chat

输出：config.<名字>.json + 使用说明。**生成后务必人工过目再用**——LLM 生成的人设
决定投出去什么岗位，这一步不值得省。
"""
import json
import os
import re
import sys
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent

CITY_CODES = {
    "北京": "101010100", "上海": "101020100", "广州": "101280100", "深圳": "101280600",
    "杭州": "101210100", "南京": "101190100", "苏州": "101190400", "成都": "101270100",
    "重庆": "101040100", "武汉": "101200100", "西安": "101110100", "天津": "101030100",
    "长沙": "101250100", "郑州": "101180100", "合肥": "101220100", "青岛": "101120200",
    "宁波": "101210400", "东莞": "101281600", "佛山": "101281000", "无锡": "101190200",
}
SEED_TITLE_BLACKLIST = ["校招", "应届", "实习", "兼职", "保险"]

SCHEMA_DOC = """配置键说明（全部要产出）：
{
  "search_keywords":    ["用于搜索页的岗位关键词，10~20 个，覆盖目标职能的常见叫法"],
  "cities":             {"城市名": "城市码"}        ← 不用你生成，向导按 --city 参数填
  "salary_min_mid":     薪资中位数下限（K），来自 --salary-min
  "allowed_roles":      [放行的职能 tag，即下面 role_tiers 里最好的 2~4 个 tag]
  "loose_roles_extra":  [--loose 时才放行的边缘 tag，1~2 个]
  "experience_max_years": 简历工作年限上限取整。JD 要求年限 > 它 → 硬拦截
  "degrees_reject":     [高于本人最高学历的学历要求，如本人本科 → ["硕士","博士"]]
  "edu_warn_words":     默认 ["统招","全日制"]，软警告不拦截，一般不改
  "title_blacklist":    [岗位标题黑名单：与目标无关的热门词。必含 __SEED__，
                         再按目标方向补充（如不做云的加"云原生"，不做数据的加"大数据"）]
  "domain_bonus":       {"keywords": [本人深耕领域的词，出现在标题加分], "points": 30}
  "hard_skill_blacklist": [相邻岗位的招牌技能但简历完全没有的 —— 出现在 JD 即硬拦。
                         例：做测试的人看到 JD 要"Verilog"说明是芯片岗，投了必拒]
  "role_tiers":         [4~7 档，每档 {"keywords":[标题关键词], "points":分数, "tag":档名}]。
                         打分原则：目标职能 30~40 分 > 相邻可接受 20~30 > 仅沾边 5~15 >
                         明确不投的 -20 以下。关键词必须覆盖该职能在 BOSS 上的真实叫法
                         （如"售前"岗也叫"解决方案""FAE""应用工程师"）]
  "industry":           {"high":[本人行业背景词，标题命中 +30],
                         "mid":[可接受行业 +10], "low":[无关/减分行当 -30]}
  "ai_keywords":        默认 ["AI","ai","人工智能","大模型","智能体","Agent"]
  "agency_keywords":    默认 ["人力资源","劳务","派遣","外包","人才服务"]
  "limits":             默认 {"daily_cap":15, "run_limit":10, "scrolls":3, "top":25,
                         "interval":[8,15]}，只允许更保守
  "meta":               {"profile_summary":"人设一句话", "advice":["给用户的建议 x3"]}
}
只输出一个 JSON 对象，不要 markdown 代码围栏，不要解释文字。"""
# __SEED__ 占位符替换成真实种子黑名单（普通字符串，不做 format，避免花括号转义问题）
SCHEMA_DOC = SCHEMA_DOC.replace("__SEED__", "、".join(SEED_TITLE_BLACKLIST))


def read_resume(path: Path) -> str:
    suf = path.suffix.lower()
    if suf in (".md", ".txt", ".text"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suf == ".docx":
        # 零依赖抽取：docx 就是 zip，正文在 word/document.xml
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml)
    if suf == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            sys.exit("[!] PDF 需要 pypdf：pip install pypdf；或先把简历转成 md/txt")
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    sys.exit(f"[!] 不支持的简历格式 {suf}（支持 .md/.txt/.docx/.pdf）")


def argval(flag, default=None, cast=str):
    a = sys.argv
    return cast(a[a.index(flag) + 1]) if flag in a else default


def build_prompt(resume: str, cities: dict, salary_min: float, direction: str) -> str:
    example_path = BASE / "config.demo.json"
    example = (example_path.read_text(encoding="utf-8") if example_path.exists()
               else "(示例缺失)")
    return f"""你在帮一位求职者配置「BOSS 直聘自动投递引擎」的人设。引擎拿这份配置去
搜索岗位、打分排名、硬门槛核验，最后投出岗位。配置质量 = 投递质量，请严格按规则生成。

{SCHEMA_DOC}

## 参考示例（另一个方向的人设，仅供格式与粒度参照，禁止照抄内容）
```json
{example}
```

## 求职者信息
### 目标方向（用户口述，优先级最高）
{direction or "（未指定——从简历推断最合理的 1~2 个方向，宁窄勿宽）"}

### 目标城市
{("、".join(cities)) or "（未指定，默认上海）"}　薪资中位数下限：{salary_min}K

### 简历全文
<resume>
{resume[:12000]}
</resume>

现在输出配置 JSON。再次检查：role_tiers 关键词要覆盖目标职能在招聘网站的真实叫法；
hard_skill_blacklist 填「相邻岗位招牌技能但简历没有」的词，不是随机技能；只输出 JSON。"""


def validate_persona(cfg: dict) -> list:
    """返回问题列表；空列表 = 通过。只做结构与类型校验，内容质量靠人过目。"""
    errs = []

    def _is_str_list(v):
        return isinstance(v, list) and all(isinstance(x, str) for x in v)

    if not _is_str_list(cfg.get("allowed_roles")) or not cfg["allowed_roles"]:
        errs.append("allowed_roles 必须是非空字符串列表")
    if not _is_str_list(cfg.get("search_keywords")) or not cfg["search_keywords"]:
        errs.append("search_keywords 必须是非空字符串列表")
    tiers = cfg.get("role_tiers")
    if not isinstance(tiers, list) or not tiers:
        errs.append("role_tiers 必须是非空列表")
    else:
        for i, t in enumerate(tiers):
            if not (isinstance(t, dict) and isinstance(t.get("keywords"), list)
                    and t["keywords"] and isinstance(t.get("points"), (int, float))
                    and isinstance(t.get("tag"), str)):
                errs.append(f"role_tiers[{i}] 缺 keywords/points/tag 或类型不对")
    ind = cfg.get("industry")
    if not (isinstance(ind, dict) and all(_is_str_list(ind.get(k)) for k in ("high", "mid", "low"))):
        errs.append("industry 必须是 high/mid/low 三个字符串列表")
    y = cfg.get("experience_max_years")
    if not (isinstance(y, int) and 0 < y <= 40):
        errs.append("experience_max_years 必须是 1~40 的整数")
    for k in ("degrees_reject", "title_blacklist", "hard_skill_blacklist", "edu_warn_words"):
        if k in cfg and not _is_str_list(cfg[k]):
            errs.append(f"{k} 必须是字符串列表")
    dom = cfg.get("domain_bonus")
    if dom and not (isinstance(dom, dict) and _is_str_list(dom.get("keywords", []))
                    and isinstance(dom.get("points", 30), (int, float))):
        errs.append("domain_bonus 结构应为 {keywords:[], points:n}")
    lim = cfg.get("limits")
    if lim and not isinstance(lim, dict):
        errs.append("limits 必须是对象")
    return errs


def call_llm(prompt: str) -> str:
    key = os.environ.get("LLM_API_KEY")
    if not key:
        sys.exit("[!] 缺 LLM_API_KEY。设一下：set LLM_API_KEY=sk-xxx\n"
                 "    （没有 key？用 --dry-run 打印 prompt，让 Claude 直接生成）")
    base = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("LLM_MODEL", "deepseek-chat")
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps({"model": model, "temperature": 0.3,
                         "messages": [{"role": "user", "content": prompt}]}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def extract_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?|```", "", text)
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("响应里没找到 JSON 对象")
    return json.loads(text[i:j + 1])


def main() -> int:
    if "--validate" in sys.argv:
        p = Path(sys.argv[sys.argv.index("--validate") + 1])
        cfg = json.loads(p.read_text(encoding="utf-8"))
        errs = validate_persona(cfg)
        print("\n".join(errs) if errs else f"[✓] 配置结构校验通过：{p.name}")
        return 1 if errs else 0

    resume_arg = argval("--resume")
    if not resume_arg:
        print(__doc__)
        return 1
    resume_path = Path(resume_arg)
    if not resume_path.exists():
        sys.exit(f"[!] 简历不存在: {resume_path}")
    resume = read_resume(resume_path)

    city_arg = argval("--city", "上海")
    cities, unknown = {}, []
    for c in (x.strip() for x in city_arg.split(",") if x.strip()):
        if c in CITY_CODES:
            cities[c] = CITY_CODES[c]
        elif ":" in c:  # 逃生口：--city 我家乡:101XXXXXX
            name, code = c.split(":", 1)
            cities[name] = code
        else:
            unknown.append(c)
    if unknown:
        sys.exit(f"[!] 不认识的城市 {unknown}；支持的：{sorted(CITY_CODES)}，"
                 f"或用「名称:城市码」格式自带")
    salary_min = argval("--salary-min", "0", float)
    direction = argval("--direction", "")
    out = Path(argval("--out", f"config.{datetime.now().strftime('%Y%m%d_%H%M')}.json"))

    prompt = build_prompt(resume, cities, salary_min, direction)
    if "--dry-run" in sys.argv:
        print(prompt)
        return 0

    print(f"[*] 调用 {os.environ.get('LLM_MODEL', 'deepseek-chat')} 生成人设……")
    raw = call_llm(prompt)
    cfg = extract_json(raw)
    cfg["cities"] = cities            # 集合语义：整表替换，不信 LLM 的城市码
    cfg["salary_min_mid"] = salary_min
    cfg["title_blacklist"] = sorted(set(SEED_TITLE_BLACKLIST)
                                    | set(cfg.get("title_blacklist") or []))
    errs = validate_persona(cfg)
    if errs:
        print("[!] LLM 产出未过校验：")
        for e in errs:
            print("    -", e)
        out.with_suffix(".raw.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
        print(f"    原始产出已存 {out.with_suffix('.raw.json')}，修好后用 --validate 复检")
        return 2
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = cfg.get("meta") or {}
    print(f"\n[+] 人设已生成 -> {out}")
    if meta.get("profile_summary"):
        print(f"    人设：{meta['profile_summary']}")
    for a in (meta.get("advice") or [])[:3]:
        print(f"    建议：{a}")
    print(f"\n[!] 人工过目 config 后，试跑（计划模式，不会真投）：")
    print(f"    PYTHONUTF8=1 py hunt.py --config {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
