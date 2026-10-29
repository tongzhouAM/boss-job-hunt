# -*- coding: utf-8 -*-
"""
离线冒烟测试：不连浏览器、不调 API、不碰网络，验证引擎纯逻辑。

每次改完引擎/换配置先跑这个：
    PYTHONUTF8=1 py smoke_test.py
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import hunt  # noqa: E402
import resume_wizard  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [✓] {name}")
    else:
        FAIL += 1
        print(f"  [✗] {name}  {detail}")


print("== 1. 配置加载 ==")
demo = hunt.load_config(str(BASE / "config.demo.json"))
check("demo 人设独立生效", demo["allowed_roles"] == ["测试/检测", "研发"],
      f"got {demo['allowed_roles']}")
check("demo 职能档非空", len(demo["role_tiers"]) >= 4)
mine = hunt.load_config(str(BASE / "config.json"))
# 我的人设必须自包含：内置默认已中性化，role_tiers 全靠 config.json 自己带
check("config.json 自包含（role_tiers 已外置）", len(mine["role_tiers"]) >= 5,
      "role_tiers 需 >=5 档，否则打分退回中性默认")
check("config.json 售前档分数正确",
      any(t["tag"] == "售前/方案" and t["points"] == 35 for t in mine["role_tiers"]))

print("\n== 2. 薪资解析 ==")
s = hunt.parse_salary("25-35K·14薪")
check("区间+薪数", s and s["lo"] == 25 and s["hi"] == 35 and s["months"] == 14
      and s["annual_wan"] == 42.0, json.dumps(s, ensure_ascii=False))
check("无薪返回 None", hunt.parse_salary("面议") is None)

print("\n== 3. 打分排序（demo 人设） ==")
good = {"title": "测试开发工程师", "salary": "25-35K·14薪", "location": "上海·浦东"}
bad = {"title": "电话销售专员", "salary": "8-12K", "location": "上海·松江"}
sg, _, rg, ig = hunt.score_with(demo, good)
sb, _, rb, ib = hunt.score_with(demo, bad)
check("测试开发 > 电话销售", sg > sb, f"{sg} vs {sb}")
check("职能档命中", rg == "测试/检测", f"got {rg}")
check("低端销售负分档", rb == "销售类" and sb <= -70, f"got {rb}/{sb}")

print("\n== 4. 闸门（demo 人设：经验上限 5 年） ==")
item = {"degree": "博士", "experience": "3-5年", "jdText": "负责接口自动化测试"}
check("博士硬拦", hunt.gates_with(demo, item) == ["学历(博士)"],
      str(hunt.gates_with(demo, item)))
item2 = {"degree": "本科", "experience": "10-15年", "jdText": "需要 Verilog 经验"}
g2 = hunt.gates_with(demo, item2)
check("经验超档硬拦", any("10年经验" in x for x in g2), str(g2))
check("黑名单技能硬拦", any("Verilog" in x for x in g2), str(g2))
check("软警告：统招", hunt.warns_with(demo, {"jdText": "统招本科及以上学历"})
      == ["统招门槛"])
check("软警告不误触", hunt.warns_with(demo, {"jdText": "本科及以上学历"}) == [])

print("\n== 5. 薪资降级判定 ==")
rows_degraded = [{"title": "A", "salary": "㌅-㌇K"}, {"title": "B", "salary": ""}]
rows_ok = [{"title": "A", "salary": "20-30K"}]
check("全量解析失败 → 降级",
      bool(rows_degraded) and not any(hunt.parse_salary(r.get("salary")) for r in rows_degraded))
check("正常解析 → 不降级",
      not (bool(rows_ok) and not any(hunt.parse_salary(r.get("salary")) for r in rows_ok)))

print("\n== 6. 向导：校验与 prompt 组装 ==")
good_persona = {
    "allowed_roles": ["测试/检测"], "search_keywords": ["测试开发"],
    "experience_max_years": 5, "degrees_reject": ["博士"],
    "role_tiers": [{"keywords": ["测试"], "points": 30, "tag": "测试/检测"}],
    "industry": {"high": ["软件"], "mid": ["制造"], "low": ["金融"]},
}
check("合规人设通过", resume_wizard.validate_persona(good_persona) == [],
      str(resume_wizard.validate_persona(good_persona)))
bad_persona = dict(good_persona, allowed_roles=[], experience_max_years=99)
errs = resume_wizard.validate_persona(bad_persona)
check("空白名单/年限非法被拦", len(errs) == 2, str(errs))
prompt = resume_wizard.build_prompt("我会 Python 自动化测试", {"上海": "101020100"}, 15, "测试开发")
check("prompt 含简历/城市/薪资/Schema",
      all(k in prompt for k in ("Python 自动化测试", "上海", "15K", "role_tiers")))

print(f"\n{'='*46}\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
