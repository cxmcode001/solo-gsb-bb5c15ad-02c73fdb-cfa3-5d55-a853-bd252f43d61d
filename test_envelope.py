"""电阻容差包络分析冒烟测试（开发用，不打进镜像）。

运行：python3 test_envelope.py
"""
import math
import sys

import app.envelope as env
from app.envelope import (
    TOLERANCE_MAX_PCT,
    TOLERANCE_MAX_RESISTORS,
    TOLERANCE_MIN_PCT,
    envelope,
)
from app.solver import CircuitError

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def expect_rejected(payload, *detail_parts):
    try:
        envelope(payload)
        return False, "应当被拒绝但包络分析成功"
    except CircuitError as e:
        codes = [g["code"] for g in e.errors]
        text = " ".join(
            [g["message"] for g in e.errors]
            + [it.get("detail", "") for g in e.errors for it in g.get("items", [])]
        )
        ok = codes == ["envelope_validation"] and all(p in text for p in detail_parts)
        return ok, f"{codes} / {text}"


def divider(t):
    print(f"\n----- {t} -----")


# 分压器：V1=10, R1=10(1→2), R2=20(2→0)，V(2)=20/3，I(R1)=1/3
BASE = {
    "ground": "0",
    "nodes": ["0", "1", "2"],
    "elements": [
        {"id": "V1", "type": "V", "node_p": "1", "node_n": "0", "value": 10},
        {"id": "R1", "type": "R", "node_p": "1", "node_n": "2", "value": 10},
        {"id": "R2", "type": "R", "node_p": "2", "node_n": "0", "value": 20},
    ],
}
SPEC2 = {"quantity": "voltage", "target": "2",
         "resistors": [{"id": "R1", "tolerance": 10}, {"id": "R2", "tolerance": 10}]}

# ---------- 1. 双电阻包络：角点解析解 ----------
divider("1. 目标 V(2)，R1/R2 各 ±10%：min=180/29（+-），max=220/31（-+）")
r = envelope({**BASE, "envelope": SPEC2})
check("返回 ok", r["ok"] is True)
check("标称值 20/3", math.isclose(r["envelope"]["target_nominal"], 20 / 3, abs_tol=1e-12))
check("角点数 2^2 = 4", r["counts"] == {"resistors": 2, "corners": 4}
      and len(r["corners"]) == 4)
check("最小值 180/29 且角点 +-",
      math.isclose(r["minimum"]["value"], 180 / 29, rel_tol=1e-12)
      and r["minimum"]["corner"]["pattern"] == "+-"
      and r["minimum"]["corner"]["index"] == 1,
      str(r["minimum"]))
check("最大值 220/31 且角点 -+",
      math.isclose(r["maximum"]["value"], 220 / 31, rel_tol=1e-12)
      and r["maximum"]["corner"]["pattern"] == "-+",
      str(r["maximum"]))
check("极值角点列出两电阻取值与方向",
      {(x["id"], x["side"]): x["value"] for x in r["minimum"]["corner"]["resistors"]}
      == {("R1", "plus"): 11.0, ("R2", "minus"): 18.0})
check("min 相对偏差",
      math.isclose(r["minimum"]["deviation_rel"], (180 / 29 - 20 / 3) / (20 / 3), rel_tol=1e-12))
check("max 绝对偏差", math.isclose(
    r["maximum"]["deviation_abs"], 220 / 31 - 20 / 3, rel_tol=1e-12))
check("目标名 / 单位",
      r["envelope"]["target_name"] == "节点 2 电压" and r["envelope"]["target_unit"] == "V")
check("角点按 mask 顺序且模式完整",
      [c["pattern"] for c in r["corners"]] == ["--", "+-", "-+", "++"]
      and [c["index"] for c in r["corners"]] == [0, 1, 2, 3])
check("全部角点值有限", all(math.isfinite(c["value"]) for c in r["corners"]))
check("电阻上下限回显",
      r["envelope"]["resistors"][0]["value_minus"] == 9.0
      and r["envelope"]["resistors"][0]["value_plus"] == 11.0)

# ---------- 2. 支路电流目标 ----------
divider("2. 目标 I(R1)：I = 10/(R1+R2)，min=10/33（++），max=10/27（--）")
r = envelope({**BASE, "envelope": {"quantity": "current", "target": "R1",
             "resistors": [{"id": "R1", "tolerance": 10}, {"id": "R2", "tolerance": 10}]}})
check("标称电流 1/3", math.isclose(r["envelope"]["target_nominal"], 1 / 3, abs_tol=1e-12))
check("min=10/33 @ ++",
      math.isclose(r["minimum"]["value"], 10 / 33, rel_tol=1e-12)
      and r["minimum"]["corner"]["pattern"] == "++", str(r["minimum"]))
check("max=10/27 @ --",
      math.isclose(r["maximum"]["value"], 10 / 27, rel_tol=1e-12)
      and r["maximum"]["corner"]["pattern"] == "--", str(r["maximum"]))
check("目标单位为 A", r["envelope"]["target_unit"] == "A")

# ---------- 3. 单电阻 / 边界容差 / 满 8 电阻 ----------
divider("3. 单电阻与容差边界 0.01% / 50%")
for tol in (TOLERANCE_MIN_PCT, TOLERANCE_MAX_PCT):
    rr = envelope({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                  "resistors": [{"id": "R2", "tolerance": tol}]}})
    check(f"tol={tol:g}% 两个角点", rr["counts"] == {"resistors": 1, "corners": 2}
          and [c["pattern"] for c in rr["corners"]] == ["-", "+"])
# R2 ±50%：V2 min = 10*10/(10+10)=5（-），max=10*30/(10+30)=7.5（+）
rr = envelope({**BASE, "envelope": {"quantity": "voltage", "target": "2",
              "resistors": [{"id": "R2", "tolerance": 50}]}})
check("±50% 极值", math.isclose(rr["minimum"]["value"], 5.0)
      and math.isclose(rr["maximum"]["value"], 7.5),
      f"{rr['minimum']['value']} / {rr['maximum']['value']}")

# 8 个电阻：2^8 = 256
net8 = {
    "ground": "0",
    "nodes": ["0", "1"],
    "elements": [
        {"id": "V1", "type": "V", "node_p": "1", "node_n": "0", "value": 5},
        *[{"id": f"R{i}", "type": "R", "node_p": "1", "node_n": "0", "value": 100}
          for i in range(1, 9)],
    ],
}
rr = envelope({**net8, "envelope": {"quantity": "voltage", "target": "1",
              "resistors": [{"id": f"R{i}", "tolerance": 1} for i in range(1, 9)]}})
check("8 电阻 → 256 角点", rr["counts"] == {"resistors": 8, "corners": 256}
      and len(rr["corners"]) == 256)
check("理想电压源节点电压不随电阻变化",
      all(c["value"] == 5.0 for c in rr["corners"])
      and rr["minimum"]["deviation_abs"] == 0.0 and rr["maximum"]["deviation_abs"] == 0.0)

# ---------- 4. 参数校验 ----------
divider("4. 容差包络参数校验")
ok, detail = expect_rejected({**BASE}, "缺少 envelope")
check("缺少 envelope 段被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {}}, "resistors")
check("缺少 resistors 被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "power", "target": "2",
                             "resistors": [{"id": "R1", "tolerance": 1}]}}, "voltage")
check("非法目标类型被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "9",
                             "resistors": [{"id": "R1", "tolerance": 1}]}}, "不在节点表")
check("目标节点不存在被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "current", "target": "Rx",
                             "resistors": [{"id": "R1", "tolerance": 1}]}}, "不在元件表")
check("目标支路不存在被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                             "resistors": []}}, "1～8")
check("0 个电阻被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                             "resistors": [{"id": f"R{i}", "tolerance": 1} for i in range(1, 10)]
                             if False else []}}, "1～8")  # 占位，下面用网表构造 9 个
nine = {"ground": "0", "nodes": ["0", "1"],
        "elements": [{"id": "V1", "type": "V", "node_p": "1", "node_n": "0", "value": 1}]
        + [{"id": f"R{i}", "type": "R", "node_p": "1", "node_n": "0", "value": 1}
           for i in range(1, 10)]}
ok, detail = expect_rejected({**nine, "envelope": {"quantity": "voltage", "target": "1",
                             "resistors": [{"id": f"R{i}", "tolerance": 1} for i in range(1, 10)]}},
                             "1～8")
check("9 个电阻被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                             "resistors": [{"id": "Rx", "tolerance": 1}]}}, "不在元件表")
check("容差电阻不存在被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                             "resistors": [{"id": "V1", "tolerance": 1}]}}, "只允许选择电阻")
check("选择非电阻被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                             "resistors": [{"id": "R1", "tolerance": 1},
                                           {"id": "R1", "tolerance": 2}]}}, "重复")
check("同一电阻重复被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                             "resistors": [{"id": "R1", "tolerance": 0.001}]}}, "0.01%～50%")
check("容差低于 0.01% 被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                             "resistors": [{"id": "R1", "tolerance": 51}]}}, "0.01%～50%")
check("容差高于 50% 被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                             "resistors": [{"id": "R1", "tolerance": "x"}]}}, "数字")
check("容差非数字被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                             "resistors": [{"id": "R1", "tolerance": True}]}}, "数字")
check("容差为布尔值被拒", ok, detail)
check("容差常量", TOLERANCE_MIN_PCT == 0.01 and TOLERANCE_MAX_PCT == 50.0
      and TOLERANCE_MAX_RESISTORS == 8)

# ---------- 5. 基准网络失败：整体 422 + baseline 阶段标记 ----------
divider("5. 基准网络无唯一解：baseline 阶段错误")
try:
    envelope({**BASE, "ground": "", "envelope": SPEC2})
    check("无接地整体失败", False)
except CircuitError as e:
    check("无接地 → no_ground@baseline",
          [(g["code"], g.get("stage")) for g in e.errors] == [("no_ground", "baseline")],
          str(e.errors))

# ---------- 6. 任一角点不可解：整体结构化错误并标出角点 ----------
divider("6. 角点不可解：envelope_corner_unsolvable@corner（桩件模拟）")
orig_solve = env.solve
calls = {"n": 0}


def flaky(payload_):
    calls["n"] += 1
    if calls["n"] == 3:  # 第 1 次为基准，随后是角点 0、角点 1…
        raise CircuitError([{"code": "singular", "message": "模拟角点奇异", "items": []}])
    return orig_solve(payload_)


env.solve = flaky
try:
    envelope({**BASE, "envelope": SPEC2})
    check("角点失败应整体报错", False)
except CircuitError as e:
    g = e.errors[0]
    item = g["items"][0]
    check("错误码 / 阶段", g["code"] == "envelope_corner_unsolvable"
          and g.get("stage") == "corner", str(g))
    check("标出失败角点编号与模式", item["corner"]["index"] == 1
          and item["corner"]["pattern"] == "+-"
          and [x["side"] for x in item["corner"]["resistors"]] == ["plus", "minus"],
          str(item.get("corner")))
    check("内联原始错误分组 causes", item["causes"][0]["code"] == "singular")
finally:
    env.solve = orig_solve

# ---------- 7. 标称值为 0（接地节点）：相对偏差为 null ----------
divider("7. 接地节点目标 F₀=0：deviation_rel 为 null")
r = envelope({**BASE, "envelope": {"quantity": "voltage", "target": "0",
             "resistors": [{"id": "R1", "tolerance": 10}]}})
check("标称 0 且相对偏差为 null",
      r["envelope"]["target_nominal"] == 0.0
      and r["minimum"]["deviation_rel"] is None
      and r["maximum"]["deviation_rel"] is None)

# ---------- 8. 深拷贝隔离 ----------
divider("8. 深拷贝隔离：不改写调用方电路表")
payload = {**BASE, "envelope": SPEC2}
snapshot = [(x["id"], x["value"]) for x in payload["elements"]]
envelope(payload)
check("元件参数不被改写",
      [(x["id"], x["value"]) for x in payload["elements"]] == snapshot)

# ---------- 9. 元件值非法：基准求解兜底抛结构化错误（而非 500） ----------
divider("9. 非法元件值由基准求解兜底（不抛 Python 异常）")
bad_val = {**BASE}
bad_val["elements"][1]["value"] = "abc"
try:
    envelope({**bad_val, "envelope": SPEC2})
    check("非法阻值应整体失败", False)
except CircuitError as e:
    check("非法阻值 → validation@baseline",
          e.errors[0]["code"] == "validation"
          and e.errors[0].get("stage") == "baseline", str(e.errors))

print()
if failures:
    print(f"{len(failures)} 个用例失败: {failures}")
    sys.exit(1)
print("全部用例通过 ✔")
