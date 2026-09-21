"""电阻容差包络分析（Resistor tolerance envelope）。

请求体与 ``/api/solve`` 的网络 JSON 完全一致，另带一个 ``envelope`` 段：

    {
      ...网络...,
      "envelope": {
        "quantity": "voltage",          // 或 "current"
        "target": "2",                  // 节点编号 / 支路（元件）编号
        "resistors": [                  // 1～8 个电阻，各自给出对称容差
          {"id": "R1", "tolerance": 0.05},   // 0.0001（0.01%）～ 0.5（50%）
          {"id": "R2", "tolerance": 0.1}
        ]
      }
    }

先对**标称网络**完整复用 :func:`app.solver.solve` 求解一次，取目标量
（节点电压或支路电流）的标称值 F₀；随后枚举所选 N 个电阻上下限的
**全部 2^N 个角点**：第 j 个电阻取 ``R_j·(1−tol_j)``（下限，``low``）
或 ``R_j·(1+tol_j)``（上限，``high``），每个角点都在网络的**深拷贝**
上替换阻值后复用一次 solve，记录目标值。

枚举完成后返回：

- 目标的**最小值 / 最大值**及取得极值的**角点**（角点编号、各电阻
  上 / 下限取向与取值）；
- **标称值 F₀**，以及最小 / 最大角点相对标称值的**绝对偏差与相对偏差**
  （F₀ = 0 时相对偏差为 ``null``）；
- 全部 2^N 个角点的明细，供前端展示包络。

失败约定：

- 容差参数本身（缺少 envelope 段、电阻数量不在 1～8、电阻不存在 / 重复 /
  不是 R、容差越界、目标非法等）无法开工，直接抛出
  ``envelope_validation``；
- **标称网络无法求解**时整体抛出 :class:`CircuitError`，错误分组带
  ``stage: "baseline"``；
- **任一角点不可解**（结构矛盾 / 奇异 / 目标值非有限）时不返回半截结果，
  整体抛出 :class:`CircuitError`，每个失败角点的错误分组都带上
  ``stage: "corner"`` 以及 ``corner / corner_id / sides / resistors``
  以标出角点；枚举会继续完成，一次返回全部失败角点。
"""

from __future__ import annotations

import copy
import math
from typing import Any

from .solver import CircuitError, solve

ENVELOPE_MIN_RESISTORS = 1
ENVELOPE_MAX_RESISTORS = 8
ENVELOPE_MIN_TOLERANCE = 1e-4    # 0.01%
ENVELOPE_MAX_TOLERANCE = 5e-1    # 50%
_TINY = 1e-12

QUANTITY_VOLTAGE = "voltage"
QUANTITY_CURRENT = "current"

STAGE_BASELINE = "baseline"
STAGE_CORNER = "corner"
SIDE_LOW = "low"     # R·(1−tol)
SIDE_HIGH = "high"   # R·(1+tol)


def _reject(items: list[dict[str, str]]) -> CircuitError:
    return CircuitError(
        [{"code": "envelope_validation", "message": "容差包络参数未通过校验", "items": items}]
    )


def _with_stage(exc: CircuitError, stage: str) -> CircuitError:
    """给既有错误分组补上阶段标记（浅拷贝分组，不动 items 内容）。"""
    return CircuitError([{**group, "stage": stage} for group in exc.errors])


def _with_corner(
    exc: CircuitError, index: int, sides: list[str], chosen: list[dict[str, Any]]
) -> CircuitError:
    """给角点求解的错误分组补上角点标记（编号 / 各电阻取向与取值）。"""
    resistor_values = [
        {"id": r["id"], "side": side, "value": r["value_low"] if side == SIDE_LOW else r["value_high"]}
        for r, side in zip(chosen, sides)
    ]
    tagged = [
        {
            **group,
            "stage": STAGE_CORNER,
            "corner": index,
            "corner_id": f"C{index}",
            "sides": sides,
            "resistors": resistor_values,
        }
        for group in exc.errors
    ]
    return CircuitError(tagged)


def _node_names(payload: dict[str, Any]) -> list[str]:
    """按请求体中的出现顺序兜底提取节点表中的非空节点名（去重）。"""
    names: list[str] = []
    seen: set[str] = set()
    raw_nodes = payload.get("nodes")
    if isinstance(raw_nodes, list):
        for raw in raw_nodes:
            name = str(raw).strip() if raw is not None else ""
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _element_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """按请求体中的顺序提取元件表中“看起来是对象”的行（合法性由 solve 兜底）。"""
    raw_elements = payload.get("elements")
    if not isinstance(raw_elements, list):
        return []
    return [raw for raw in raw_elements if isinstance(raw, dict)]


def _snap(value: float) -> float:
    return 0.0 if abs(value) < _TINY else value


def envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """枚举所选电阻上下限的全部 2^N 个角点，求目标量的容差包络。"""
    problems: list[dict[str, str]] = []

    if not isinstance(payload, dict):
        raise _reject([{"ref": "-", "detail": "请求体应为对象"}])

    spec = payload.get("envelope")
    if not isinstance(spec, dict):
        raise _reject([{"ref": "-", "detail": "缺少 envelope 容差包络参数对象"}])

    # ------------------------------------------------------------- 目标类型
    quantity = str(spec.get("quantity") or "").strip()
    if quantity not in (QUANTITY_VOLTAGE, QUANTITY_CURRENT):
        problems.append(
            {
                "ref": "目标类型",
                "detail": f"目标类型“{spec.get('quantity')}”非法，应为 voltage（节点电压）或 current（支路电流）",
            }
        )

    # ------------------------------------------------------------- 目标对象
    target = str(spec.get("target") or "").strip()
    if not target:
        problems.append({"ref": "分析目标", "detail": "未指定分析目标（节点编号或支路编号）"})

    node_names = _node_names(payload)
    element_rows = _element_rows(payload)
    index_by_id: dict[str, list[int]] = {}
    for i, raw in enumerate(element_rows):
        eid = str(raw.get("id") or "").strip()
        if eid:
            index_by_id.setdefault(eid, []).append(i)

    if target and quantity == QUANTITY_VOLTAGE and target not in node_names:
        problems.append({"ref": target, "detail": f"目标节点“{target}”不在节点表中"})
    if target and quantity == QUANTITY_CURRENT and target not in index_by_id:
        problems.append({"ref": target, "detail": f"目标支路（元件）“{target}”不在元件表中"})

    # ------------------------------------------------------------- 电阻选择
    raw_resistors = spec.get("resistors")
    if not isinstance(raw_resistors, list):
        problems.append(
            {"ref": "容差电阻", "detail": "resistors 必须是数组（每项含电阻 id 与对称容差）"}
        )
        raw_resistors = []
    elif not (ENVELOPE_MIN_RESISTORS <= len(raw_resistors) <= ENVELOPE_MAX_RESISTORS):
        problems.append(
            {
                "ref": "容差电阻",
                "detail": (
                    f"参与容差包络的电阻必须为 {ENVELOPE_MIN_RESISTORS}～"
                    f"{ENVELOPE_MAX_RESISTORS} 个（当前：{len(raw_resistors)} 个）"
                ),
            }
        )

    chosen: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(raw_resistors):
        ref = f"容差电阻行 {i + 1}"
        if not isinstance(raw, dict):
            problems.append({"ref": ref, "detail": "电阻配置必须是对象（含 id 与 tolerance）"})
            continue

        rid = str(raw.get("id") or "").strip()
        if not rid:
            problems.append({"ref": ref, "detail": "未选择电阻编号"})
        elif rid in seen_ids:
            problems.append({"ref": rid, "detail": f"电阻“{rid}”在容差配置中重复出现，每个电阻只能选择一次"})
        else:
            matches = index_by_id.get(rid, [])
            if not matches:
                problems.append({"ref": rid, "detail": f"容差电阻“{rid}”不在元件表中"})
            elif len(matches) > 1:
                problems.append(
                    {"ref": rid, "detail": f"电阻“{rid}”编号在元件表中重复，无法唯一确定，请先修改元件表"}
                )
            else:
                row = element_rows[matches[0]]
                etype = str(row.get("type") or "").strip().upper()
                if etype != "R":
                    problems.append(
                        {"ref": rid, "detail": f"元件“{rid}”类型为 {etype}，容差包络仅支持电阻（R）"}
                    )
                else:
                    seen_ids.add(rid)

        raw_tol = raw.get("tolerance")
        if isinstance(raw_tol, bool) or not isinstance(raw_tol, (int, float)):
            problems.append({"ref": rid or ref, "detail": f"对称容差“{raw_tol}”必须是数字"})
        else:
            tol = float(raw_tol)
            if not math.isfinite(tol):
                problems.append({"ref": rid or ref, "detail": f"对称容差“{raw_tol}”必须是有限实数"})
            elif not (ENVELOPE_MIN_TOLERANCE <= tol <= ENVELOPE_MAX_TOLERANCE):
                problems.append(
                    {
                        "ref": rid or ref,
                        "detail": (
                            "对称容差必须在 "
                            f"{ENVELOPE_MIN_TOLERANCE * 100:g}%～{ENVELOPE_MAX_TOLERANCE * 100:g}%"
                            f" 之间（当前：{tol * 100:g}%）"
                        ),
                    }
                )

        # 仅当电阻与容差都初步可用时才登记，供后续角点枚举
        if rid and rid in seen_ids and isinstance(raw_tol, (int, float)) and not isinstance(raw_tol, bool):
            tol = float(raw_tol)
            if math.isfinite(tol) and ENVELOPE_MIN_TOLERANCE <= tol <= ENVELOPE_MAX_TOLERANCE:
                row = element_rows[index_by_id[rid][0]]
                base_value = float(row["value"])  # solve 成功后必然为正的有限数
                chosen.append(
                    {
                        "id": rid,
                        "index": index_by_id[rid][0],
                        "node_p": str(row.get("node_p") or ""),
                        "node_n": str(row.get("node_n") or ""),
                        "base_value": base_value,
                        "tolerance": tol,
                        "value_low": base_value * (1.0 - tol),
                        "value_high": base_value * (1.0 + tol),
                    }
                )

    if problems:
        raise _reject(problems)

    # ------------------------------------------------------------- 标称求解
    try:
        base = solve(copy.deepcopy(payload))
    except CircuitError as exc:
        raise _with_stage(exc, STAGE_BASELINE)

    def target_value(result: dict[str, Any]) -> float:
        if quantity == QUANTITY_VOLTAGE:
            table = {str(row["id"]): float(row["voltage"]) for row in result["node_voltages"]}
        else:
            table = {str(br["id"]): float(br["current"]) for br in result["branches"]}
        return table[target]

    if quantity == QUANTITY_VOLTAGE:
        f0 = _snap(target_value(base))
        target_unit = "V"
        target_name = f"节点 {target} 电压"
    else:
        f0 = _snap(target_value(base))
        target_row = element_rows[index_by_id[target][0]]
        target_unit = "A"
        target_name = (
            f"支路 {target} 电流（{target_row.get('node_p', '')}→"
            f"{target_row.get('node_n', '')}）"
        )

    # ----------------------------------------------- 2^N 角点枚举（深拷贝）
    n = len(chosen)
    corner_count = 1 << n
    corners: list[dict[str, Any]] = []
    corner_failures: list[dict[str, Any]] = []

    for mask in range(corner_count):
        # bit j = 0 → 下限 low（R·(1−tol)），bit j = 1 → 上限 high（R·(1+tol)）
        sides = [SIDE_HIGH if (mask >> j) & 1 else SIDE_LOW for j in range(n)]

        corner_payload = copy.deepcopy(payload)
        resistor_values: list[dict[str, Any]] = []
        for r, side in zip(chosen, sides):
            value = r["value_high"] if side == SIDE_HIGH else r["value_low"]
            corner_payload["elements"][r["index"]]["value"] = value
            resistor_values.append({"id": r["id"], "side": side, "value": value})

        try:
            result = solve(corner_payload)
        except CircuitError as exc:
            corner_failures.extend(_with_corner(exc, mask, sides, chosen).errors)
            continue

        value = target_value(result)
        if not math.isfinite(value):
            group = {
                "code": "non_finite_target",
                "message": f"角点 C{mask} 的求解结果中目标值不是有限实数，包络无法闭合",
                "items": [],
                "stage": STAGE_CORNER,
                "corner": mask,
                "corner_id": f"C{mask}",
                "sides": sides,
                "resistors": resistor_values,
            }
            corner_failures.append(group)
            continue

        corners.append(
            {
                "index": mask,
                "corner_id": f"C{mask}",
                "sides": sides,
                "resistor_values": resistor_values,
                "value": _snap(float(value)),
            }
        )

    if corner_failures:
        # 任一角点不可解：不返回任何半截包络，整体 422 并标出全部失败角点
        raise CircuitError(corner_failures)

    # ------------------------------------------------------------- 极值归纳
    min_idx = min(range(corner_count), key=lambda k: corners[k]["value"])
    max_idx = max(range(corner_count), key=lambda k: corners[k]["value"])
    f_min = corners[min_idx]["value"]
    f_max = corners[max_idx]["value"]

    delta_min = _snap(f_min - f0)
    delta_max = _snap(f_max - f0)
    rel_min = None if f0 == 0.0 else _snap((f_min - f0) / f0)
    rel_max = None if f0 == 0.0 else _snap((f_max - f0) / f0)

    return {
        "ok": True,
        "envelope": {
            "quantity": quantity,
            "target": target,
            "target_name": target_name,
            "target_unit": target_unit,
            "resistors": [
                {
                    "id": r["id"],
                    "type": "R",
                    "node_p": r["node_p"],
                    "node_n": r["node_n"],
                    "index": r["index"],
                    "base_value": r["base_value"],
                    "tolerance": r["tolerance"],
                    "value_low": r["value_low"],
                    "value_high": r["value_high"],
                }
                for r in chosen
            ],
            "corner_count": corner_count,
            "nominal_value": f0,
            "min_value": f_min,
            "max_value": f_max,
            "deviation_abs_min": delta_min,
            "deviation_abs_max": delta_max,
            "deviation_rel_min": rel_min,
            "deviation_rel_max": rel_max,
        },
        "min_corner": corners[min_idx],
        "max_corner": corners[max_idx],
        "corners": corners,
        "warnings": base.get("warnings", []),
    }
