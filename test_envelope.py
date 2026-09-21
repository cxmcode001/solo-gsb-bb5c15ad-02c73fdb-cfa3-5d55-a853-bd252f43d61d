"""电阻容差包络分析冒烟测试（开发用，不打进镜像）。

运行：python3 test_envelope.py
"""
import math
import sys

from app import envelope as env_mod
from app.envelope import (
    ENVELOPE_MAX_RESISTORS,
    ENVELOPE_MAX_TOLERANCE,
    ENVELOPE_MIN_TOLERANCE,
    envelope,
)
from app.solver import CircuitError, solve

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


def spec(quantity="voltage", target="2", resistors=("R1", "R2"), tol=0.1):
    return {
        **BASE,
        "envelope": {
            "quantity": quantity,
            "target": target,
            "resistors": [{"id": r, "tolerance": tol} for r in resistors],
        },
    }


# ---------- 1. 分压器 V(2)，R1/R2 各 ±10%：解析解角点 ----------
divider("1. V(2) 包络：2^2=4 角点，min/max 与角点取向")
r = envelope(spec(tol=0.1))
check("返回 ok", r["ok"] is True)
m = r["envelope"]
check("角点数 4", m["corner_count"] == 4 and len(r["corners"]) == 4)
check("电阻元信息（上下限）",
      math.isclose(m["resistors"][0]["value_low"], 9.0)
      and math.isclose(m["resistors"][0]["value_high"], 11.0)
      and math.isclose(m["resistors"][1]["value_low"], 18.0)
      and math.isclose(m["resistors"][1]["value_high"], 22.0))
check("标称值 20/3", math.isclose(m["nominal_value"], 20 / 3, abs_tol=1e-10),
      str(m["nominal_value"]))
# V2 = 10*R2/(R1+R2)：最小=R1 高、R2 低 → 10*18/(11+18)=180/29；
# 最大=R1 低、R2 高 → 10*22/(9+22)=220/31
check("最小值 180/29", math.isclose(m["min_value"], 180 / 29, rel_tol=1e-12), str(m["min_value"]))
check("最大值 220/31", math.isclose(m["max_value"], 220 / 31, rel_tol=1e-12), str(m["max_value"]))
check("最小角点 sides=[high, low]（C1）",
      r["min_corner"]["corner_id"] == "C1" and r["min_corner"]["sides"] == ["high", "low"],
      str(r["min_corner"]))
check("最大角点 sides=[low, high]（C2）",
      r["max_corner"]["corner_id"] == "C2" and r["max_corner"]["sides"] == ["low", "high"],
      str(r["max_corner"]))
check("角点取值与取向一致",
      math.isclose(r["min_corner"]["resistor_values"][0]["value"], 11.0)
      and math.isclose(r["min_corner"]["resistor_values"][1]["value"], 18.0))
check("相对偏差 = (min-F0)/F0",
      math.isclose(m["deviation_rel_min"], (180 / 29 - 20 / 3) / (20 / 3), rel_tol=1e-12)
      and math.isclose(m["deviation_rel_max"], (220 / 31 - 20 / 3) / (20 / 3), rel_tol=1e-12))
check("绝对偏差符号", m["deviation_abs_min"] < 0 < m["deviation_abs_max"])
check("全部角点编号与索引 0..3",
      sorted(c["index"] for c in r["corners"]) == [0, 1, 2, 3]
      and [c["corner_id"] for c in sorted(r["corners"], key=lambda x: x["index"])]
      == ["C0", "C1", "C2", "C3"])

# ---------- 2. 支路电流目标 ----------
divider("2. 目标 I(R1)：目标名与单位、单调性一致")
r = envelope(spec(quantity="current", target="R1", tol=0.1))
m = r["envelope"]
check("目标单位 A / 目标名", m["target_unit"] == "A" and "支路 R1" in m["target_name"],
      str(m["target_name"]))
check("标称电流 1/3", math.isclose(m["nominal_value"], 1 / 3, abs_tol=1e-10))
# I = 10/(R1+R2)：R1、R2 都取高时最小，都取低时最大
check("最小角点 C3=[high, high]",
      r["min_corner"]["sides"] == ["high", "high"]
      and math.isclose(m["min_value"], 10 / (11 + 22), rel_tol=1e-12))
check("最大角点 C0=[low, low]",
      r["max_corner"]["sides"] == ["low", "low"]
      and math.isclose(m["max_value"], 10 / (9 + 18), rel_tol=1e-12))

# ---------- 3. 单电阻 / 8 电阻上限与容差边界 ----------
divider("3. 角点数 2^N 与容差边界")
for n in (1, 3, 8):
    rids = [f"R{i}" for i in range(n)]
    net = {
        "ground": "0",
        "nodes": ["0", "1"],
        "elements": [
            {"id": "V1", "type": "V", "node_p": "1", "node_n": "0", "value": 10},
        ] + [
            {"id": f"R{i}", "type": "R", "node_p": "1", "node_n": "0", "value": 1}
            for i in range(n)
        ],
    }
    rr = envelope({
        **net,
        "envelope": {
            "quantity": "voltage", "target": "0",
            "resistors": [{"id": rid, "tolerance": ENVELOPE_MAX_TOLERANCE} for rid in rids],
        },
    })
    check(f"N={n} → 2^N={1 << n} 个角点",
          rr["envelope"]["corner_count"] == 1 << n
          and len(rr["corners"]) == 1 << n,
          str(rr["envelope"]["corner_count"]))
    check(f"N={n} 容差回显 50%", all(x["tolerance"] == ENVELOPE_MAX_TOLERANCE
                                     for x in rr["envelope"]["resistors"]))

# 容差下界 0.01% 可解
r = envelope(spec(tol=ENVELOPE_MIN_TOLERANCE))
check("0.01% 容差可解且包络宽度很小",
      r["envelope"]["max_value"] > r["envelope"]["min_value"])

# ---------- 4. 标称值为 0：相对偏差为 None ----------
divider("4. 接地节点目标 F₀=0：相对偏差 null，绝对偏差保留")
r = envelope(spec(quantity="voltage", target="0", tol=0.1))
m = r["envelope"]
check("标称 0、min/max 也是 0",
      m["nominal_value"] == 0.0 and m["min_value"] == 0.0 and m["max_value"] == 0.0)
check("相对偏差字段为 None",
      m["deviation_rel_min"] is None and m["deviation_rel_max"] is None)

# ---------- 5. 参数校验 ----------
divider("5. 容差包络参数校验")
ok, detail = expect_rejected({**BASE}, "缺少 envelope")
check("缺少 envelope 段被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "power", "target": "2",
                                                    "resistors": [{"id": "R1", "tolerance": 0.1}]}},
                             "voltage")
check("非法目标类型被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "9",
                                                    "resistors": [{"id": "R1", "tolerance": 0.1}]}},
                             "不在节点表")
check("目标节点不存在被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "current", "target": "Rx",
                                                    "resistors": [{"id": "R1", "tolerance": 0.1}]}},
                             "不在元件表")
check("目标支路不存在被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                                                    "resistors": []}},
                             "1～8")
check("空电阻列表被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                                                    "resistors": "x"}},
                             "resistors 必须是数组")
check("resistors 非数组被拒", ok, detail)

nine = [{"id": "R9", "tolerance": 0.1}] * 9
# 上面的 R9 引用必然不存在；构造 9 个真实电阻的网络
net9 = {
    "ground": "0",
    "nodes": ["0", "1"],
    "elements": [
        {"id": "V1", "type": "V", "node_p": "1", "node_n": "0", "value": 10},
    ] + [
        {"id": f"R{i}", "type": "R", "node_p": "1", "node_n": "0", "value": 1}
        for i in range(9)
    ],
}
ok, detail = expect_rejected({
    **net9,
    "envelope": {
        "quantity": "voltage", "target": "1",
        "resistors": [{"id": f"R{i}", "tolerance": 0.1} for i in range(9)],
    },
}, "1～8", "9")
check("超过 8 个电阻被拒", ok, detail)

ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                                                    "resistors": [{"id": "V1", "tolerance": 0.1}]}},
                             "仅支持电阻")
check("选择非 R 元件被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                                                    "resistors": [
                                                        {"id": "R1", "tolerance": 0.1},
                                                        {"id": "R1", "tolerance": 0.05},
                                                    ]}},
                             "重复")
check("同一电阻重复选择被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                                                    "resistors": [{"id": "Rx", "tolerance": 0.1}]}},
                             "不在元件表")
check("电阻不存在被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                                                    "resistors": [{"id": "R1", "tolerance": 0.0}]}},
                             "0.01%")
check("容差低于 0.01% 被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                                                    "resistors": [{"id": "R1", "tolerance": 0.6}]}},
                             "50%")
check("容差高于 50% 被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                                                    "resistors": [{"id": "R1", "tolerance": "x"}]}},
                             "必须是数字")
check("容差非数字被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                                                    "resistors": [{"id": "R1", "tolerance": True}]}},
                             "必须是数字")
check("容差为布尔值被拒", ok, detail)
ok, detail = expect_rejected({**BASE, "envelope": {"quantity": "voltage", "target": "2",
                                                    "resistors": [{"tolerance": 0.1}]}},
                             "未选择电阻")
check("未给电阻 id 被拒", ok, detail)

# ---------- 6. 标称网络无唯一解：整体 422 + baseline ----------
divider("6. 标称网络无唯一解：baseline 阶段错误")
try:
    envelope({**BASE, "ground": "",
              "envelope": {"quantity": "voltage", "target": "2",
                           "resistors": [{"id": "R1", "tolerance": 0.1}]}})
    check("无接地 → no_ground@baseline", False)
except CircuitError as e:
    got = [(g["code"], g.get("stage")) for g in e.errors]
    check("无接地 → no_ground@baseline", got == [("no_ground", "baseline")], str(got))

# ---------- 7. 任一角点不可解：结构化错误并标出角点 ----------
divider("7. 角点求解失败：stage=corner + 角点标记（白盒打桩）")
real_solve = env_mod.solve
calls = {"n": 0}


def flaky_solve(payload):
    # 标称调用（R1 仍为 10）正常；角点 C1（R1=11 高、R2=18 低）抛奇异
    calls["n"] += 1
    values = {x["id"]: x["value"] for x in payload.get("elements", [])}
    if values.get("R1") == 11 and values.get("R2") == 18:
        raise CircuitError([{"code": "singular", "message": "桩：矩阵奇异", "items": []}])
    return real_solve(payload)


env_mod.solve = flaky_solve
try:
    try:
        envelope(spec(tol=0.1))
        check("角点失败时整体报错", False)
    except CircuitError as e:
        check("错误码原样透传 singular", [g["code"] for g in e.errors] == ["singular"],
              str([g["code"] for g in e.errors]))
        g = e.errors[0]
        check("stage=corner", g.get("stage") == "corner")
        check("标出角点编号 C1", g.get("corner") == 1 and g.get("corner_id") == "C1",
              str({k: g.get(k) for k in ("corner", "corner_id")}))
        check("带 sides 与各电阻取值",
              g.get("sides") == ["high", "low"]
              and [x["id"] for x in g.get("resistors", [])] == ["R1", "R2"]
              and math.isclose(g["resistors"][0]["value"], 11.0),
              str(g.get("sides")) + " " + str(g.get("resistors")))
finally:
    env_mod.solve = real_solve

# ---------- 8. 角点目标值非有限：non_finite_target ----------
divider("8. 角点目标非有限：non_finite_target 角点错误")

real_solve2 = env_mod.solve


def nonfinite_solve(payload):
    result = real_solve2(payload)
    values = {x["id"]: x["value"] for x in payload.get("elements", [])}
    # 角点（R1 被替换为 9/11）：把 R1 支路电流伪造成 inf
    if values.get("R1") in (9.0, 11.0):
        for br in result["branches"]:
            if br["id"] == "R1":
                br["current"] = float("inf")
    return result


env_mod.solve = nonfinite_solve
try:
    try:
        envelope(spec(quantity="current", target="R1", tol=0.1))
        check("非有限角点目标被拒", False)
    except CircuitError as e:
        codes = {g["code"] for g in e.errors}
        check("错误码 non_finite_target", codes == {"non_finite_target"}, str(codes))
        check("全部 4 个角点都被标出",
              [g.get("corner") for g in e.errors] == [0, 1, 2, 3],
              str([g.get("corner") for g in e.errors]))
finally:
    env_mod.solve = real_solve2

# ---------- 9. 深拷贝隔离 ----------
divider("9. 深拷贝隔离：不改写调用方电路")
payload = spec(tol=0.1)
snapshot = [(x["id"], x["value"]) for x in payload["elements"]]
envelope(payload)
check("元件参数不被改写",
      [(x["id"], x["value"]) for x in payload["elements"]] == snapshot)
check("envelope 段仍在请求体中", "envelope" in payload)

# ---------- 10. 枚举完整性：每角点阻值 = R·(1±tol) ----------
divider("10. 角点阻值与取向逐位对应")
r = envelope(spec(tol=0.1))
for c in r["corners"]:
    for j, rv in enumerate(c["resistor_values"]):
        base = m_base = BASE["elements"][j + 1]["value"]
        tol = 0.1
        expect = base * (1 + tol) if c["sides"][j] == "high" else base * (1 - tol)
        check(f"{c['corner_id']} {rv['id']}={c['sides'][j]} 阻值",
              math.isclose(rv["value"], expect))

# ---------- 11. 警告透传 ----------
divider("11. 标称网络警告透传")
check("正常网络无警告", r.get("warnings", []) == [])

# ---------- 12. 纯电流源 + 单电阻：包络覆盖标称值 ----------
divider("12. 电流源网络：包络与角点")
i_net = {
    "ground": "0",
    "nodes": ["0", "1"],
    "elements": [
        {"id": "I1", "type": "I", "node_p": "0", "node_n": "1", "value": 2},
        {"id": "R1", "type": "R", "node_p": "1", "node_n": "0", "value": 5},
    ],
}
r = envelope({
    **i_net,
    "envelope": {"quantity": "voltage", "target": "1",
                 "resistors": [{"id": "R1", "tolerance": 0.5}]},
})
m = r["envelope"]
# V(1) = I1*R1（I 元件 p=0,n=1 表示电流 0→1，注入节点 1）→ 5*2=10 V
check("标称 10 V", math.isclose(m["nominal_value"], 10.0, abs_tol=1e-9))
check("min=5 V（R 低）/ max=15 V（R 高）",
      math.isclose(m["min_value"], 5.0) and math.isclose(m["max_value"], 15.0),
      f"{m['min_value']} / {m['max_value']}")
check("相对偏差 −50% / +50%",
      math.isclose(m["deviation_rel_min"], -0.5) and math.isclose(m["deviation_rel_max"], 0.5))

# solve 本身仍可直接使用（打桩已还原）
check("solve 打桩已还原", solve(BASE)["ok"] is True)

print()
if failures:
    print(f"{len(failures)} 个用例失败: {failures}")
    sys.exit(1)
print("全部用例通过 ✔")
