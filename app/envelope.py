"""电阻容差包络分析（Resistor tolerance envelope）。

请求体与 ``/api/solve`` 的网络 JSON 完全一致，另带一个 ``envelope`` 段：

    {
      ...网络...,
      "envelope": {
        "quantity": "voltage",                 // 或 "current"
        "target": "2",                         // 节点编号 / 支路（元件）编号
        "resistors": [                         // 1～8 个电阻
          {"id": "R1", "tolerance": 5.0},      // 对称容差，百分数 0.01～50（%）
          {"id": "R2", "tolerance": 1.0}
        ]
      }
    }

先对**基准网络**完整复用 :func:`app.solver.solve` 求解一次，取出目标量
（节点电压或支路电流）的**标称值**；随后枚举所选电阻各自处于下限
``R·(1−t)`` 与上限 ``R·(1+t)`` 的全部 ``2^N`` 个角点组合（N ≤ 8，
最多 256 个角点），在网络的**深拷贝**上替换阻值后各复用一次 solve。

线性直流网络中，目标量在容差盒上的极值必然出现在角点上，因此比较全部
角点的目标值即可得到**最小值 / 最大值**、对应的极值角点以及相对标称值
的偏差：

    deviation_abs = F_extreme − F_nominal
    deviation_rel = (F_extreme − F_nominal) / F_nominal   （F_nominal = 0 时为 null）

- **包络参数本身**（缺少 envelope 段、电阻数量不在 1～8、所选元件不是
  电阻或不在表中、容差不是 0.01%～50% 的有限数、目标类型 / 目标非法等）
  无法开工，直接抛出 ``envelope_validation``。
- **基准网络无法求解**时整体失败，错误分组带 ``stage: "baseline"``。
- **任一角点不可解**时不返回半截包络，整体返回结构化错误
  （``envelope_corner_unsolvable``、``stage: "corner"``），错误条目内联
  标出失败角点（编号 / 角点模式 / 各电阻取值与方向）以及该角点 solve
  抛出的原始错误分组（``causes``）。
"""

from __future__ import annotations

import copy
import math
from typing import Any

from .solver import CircuitError, solve

TOLERANCE_MIN_RESISTORS = 1
TOLERANCE_MAX_RESISTORS = 8
# 容差以百分数给出：0.01% ～ 50%
TOLERANCE_MIN_PCT = 0.01
TOLERANCE_MAX_PCT = 50.0
_TINY = 1e-12

QUANTITY_VOLTAGE = "voltage"
QUANTITY_CURRENT = "current"

STAGE_BASELINE = "baseline"
STAGE_CORNER = "corner"
SIDE_MINUS = "minus"
SIDE_PLUS = "plus"


def _reject(items: list[dict[str, str]]) -> CircuitError:
    return CircuitError(
        [{"code": "envelope_validation", "message": "容差包络参数未通过校验", "items": items}]
    )


def _with_stage(exc: CircuitError, stage: str) -> CircuitError:
    """给既有错误分组补上阶段标记（浅拷贝分组，不动 items 内容）。"""
    return CircuitError([{**group, "stage": stage} for group in exc.errors])


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


def _corner_resistors(
    selected: list[dict[str, Any]], sides: list[str]
) -> list[dict[str, Any]]:
    """按角点方向序列生成各电阻在该角点的取值描述。"""
    rows: list[dict[str, Any]] = []
    for spec, side in zip(selected, sides):
        value = spec["value_plus"] if side == SIDE_PLUS else spec["value_minus"]
        rows.append({"id": spec["id"], "value": value, "side": side})
    return rows


def _unsolvable_corner(
    mask: int,
    sides: list[str],
    selected: list[dict[str, Any]],
    cause: CircuitError,
) -> CircuitError:
    """把某个不可解角点连同原始错误分组包装成整体结构化错误。"""
    pattern = "".join("+" if s == SIDE_PLUS else "-" for s in sides)
    corner = {
        "index": mask,
        "pattern": pattern,
        "sides": sides,
        "resistors": _corner_resistors(selected, sides),
    }
    return CircuitError(
        [
            {
                "code": "envelope_corner_unsolvable",
                "stage": STAGE_CORNER,
                "message": (
                    f"电阻容差角点 {mask}（{pattern}）处网络无唯一解，"
                    "无法确定完整的容差包络（极值必须在全部角点可解时才有意义）"
                ),
                "items": [
                    {
                        "ref": f"角点 {mask}（{pattern}）",
                        "detail": "该角点对应的电阻取值组合无法求解，请查看原始错误分组",
                        "corner": corner,
                        "causes": cause.errors,
                    }
                ],
            }
        ]
    )


def envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """枚举所选电阻容差上下限的全部角点，求目标量的最小/最大包络。"""
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
    index_by_id = {
        str(raw.get("id") or "").strip(): i
        for i, raw in enumerate(element_rows)
        if str(raw.get("id") or "").strip()
    }
    if target and quantity == QUANTITY_VOLTAGE and target not in node_names:
        problems.append({"ref": target, "detail": f"目标节点“{target}”不在节点表中"})
    if target and quantity == QUANTITY_CURRENT and target not in index_by_id:
        problems.append({"ref": target, "detail": f"目标支路（元件）“{target}”不在元件表中"})

    # ------------------------------------------------------------- 电阻选择
    raw_resistors = spec.get("resistors")
    if not isinstance(raw_resistors, list):
        problems.append(
            {"ref": "容差电阻", "detail": "resistors 必须为数组，每项含电阻编号 id 与对称容差 tolerance(%)"}
        )
        raw_resistors = []

    if not (TOLERANCE_MIN_RESISTORS <= len(raw_resistors) <= TOLERANCE_MAX_RESISTORS):
        problems.append(
            {
                "ref": "容差电阻",
                "detail": (
                    f"参与容差包络的电阻必须为 {TOLERANCE_MIN_RESISTORS}～"
                    f"{TOLERANCE_MAX_RESISTORS} 个（当前：{len(raw_resistors)}）"
                ),
            }
        )

    chosen_ids: set[str] = set()
    selected: list[dict[str, Any]] = []
    for i, raw in enumerate(raw_resistors):
        ref = f"容差电阻行 {i + 1}"
        if not isinstance(raw, dict):
            problems.append({"ref": ref, "detail": "电阻选择项格式错误，应为 {id, tolerance} 对象"})
            continue

        rid = str(raw.get("id") or "").strip()
        if not rid:
            problems.append({"ref": ref, "detail": "未选择电阻（缺少 id）"})
            continue
        if rid in chosen_ids:
            problems.append({"ref": rid, "detail": "同一电阻在容差列表中重复选择，每个电阻只能出现一次"})
            continue

        raw_tol = raw.get("tolerance")
        if isinstance(raw_tol, bool) or not isinstance(raw_tol, (int, float)):
            problems.append(
                {"ref": rid, "detail": f"电阻 {rid} 的容差“{raw_tol}”必须是数字（百分数，如 5 表示 5%）"}
            )
            continue
        tolerance_pct = float(raw_tol)
        if not math.isfinite(tolerance_pct):
            problems.append({"ref": rid, "detail": f"电阻 {rid} 的容差必须是有限实数"})
            continue
        if not (TOLERANCE_MIN_PCT <= tolerance_pct <= TOLERANCE_MAX_PCT):
            problems.append(
                {
                    "ref": rid,
                    "detail": (
                        f"电阻 {rid} 的对称容差必须在 {TOLERANCE_MIN_PCT:g}%～"
                        f"{TOLERANCE_MAX_PCT:g}% 之间（当前：{tolerance_pct:g}%）"
                    ),
                }
            )
            continue

        match = index_by_id.get(rid)
        if rid not in index_by_id:
            problems.append({"ref": rid, "detail": f"电阻“{rid}”不在元件表中"})
            continue
        etype = str(element_rows[match].get("type") or "").strip().upper()
        if etype != "R":
            problems.append(
                {"ref": rid, "detail": f"元件“{rid}”类型为 {etype}，容差包络只允许选择电阻（R）"}
            )
            continue

        chosen_ids.add(rid)
        # 基准值在 solve 成功后从结果表回填（此处不做 float 转换，非法值交给基准求解报错）
        selected.append(
            {
                "id": rid,
                "index": match,
                "base_value": 0.0,
                "tolerance_pct": tolerance_pct,
                "value_minus": 0.0,
                "value_plus": 0.0,
            }
        )

    if problems:
        raise _reject(problems)

    # ------------------------------------------------------------- 基准求解
    try:
        base = solve(copy.deepcopy(payload))
    except CircuitError as exc:
        raise _with_stage(exc, STAGE_BASELINE)

    # 基准求解通过后，所选电阻值必然为正，这里以基准结果中的参数值为准重算角点阻值
    base_value_by_id = {str(br["id"]): float(br["value"]) for br in base["branches"]}
    for spec_row in selected:
        base_value = base_value_by_id[spec_row["id"]]
        ratio = spec_row["tolerance_pct"] / 100.0
        spec_row["base_value"] = base_value
        spec_row["value_minus"] = base_value * (1.0 - ratio)
        spec_row["value_plus"] = base_value * (1.0 + ratio)

    if quantity == QUANTITY_VOLTAGE:
        voltage_by_id = {str(row["id"]): float(row["voltage"]) for row in base["node_voltages"]}
        f_nominal = voltage_by_id[target]
        target_unit = "V"
        target_name = f"节点 {target} 电压"
    else:
        current_by_id = {str(br["id"]): float(br["current"]) for br in base["branches"]}
        f_nominal = current_by_id[target]
        target_row = element_rows[index_by_id[target]]
        target_unit = "A"
        target_name = (
            f"支路 {target} 电流（{target_row.get('node_p', '')}→"
            f"{target_row.get('node_n', '')}）"
        )

    def target_value(result: dict[str, Any]) -> float:
        if quantity == QUANTITY_VOLTAGE:
            table = {str(row["id"]): float(row["voltage"]) for row in result["node_voltages"]}
        else:
            table = {str(br["id"]): float(br["current"]) for br in result["branches"]}
        return table[target]

    # --------------------------------------------- 枚举 2^N 个上下限角点
    n = len(selected)
    total = 1 << n
    corners: list[dict[str, Any]] = []
    min_record: dict[str, Any] | None = None
    max_record: dict[str, Any] | None = None
    warnings: list[str] = list(base.get("warnings", []))

    for mask in range(total):
        sides = [
            SIDE_PLUS if (mask >> i) & 1 else SIDE_MINUS
            for i in range(n)
        ]

        probe = copy.deepcopy(payload)
        for spec_row, side in zip(selected, sides):
            probe["elements"][spec_row["index"]]["value"] = (
                spec_row["value_plus"] if side == SIDE_PLUS else spec_row["value_minus"]
            )

        try:
            result = solve(probe)
        except CircuitError as exc:
            raise _unsolvable_corner(mask, sides, selected, exc)

        value = target_value(result)
        if not math.isfinite(value):
            raise CircuitError(
                [
                    {
                        "code": "envelope_corner_unsolvable",
                        "stage": STAGE_CORNER,
                        "message": f"电阻容差角点 {mask} 的目标值不是有限实数，无法确定包络",
                        "items": [
                            {
                                "ref": f"角点 {mask}",
                                "detail": "该角点求解结果中目标量为非有限值",
                                "corner": {
                                    "index": mask,
                                    "pattern": "".join(
                                        "+" if s == SIDE_PLUS else "-" for s in sides
                                    ),
                                    "sides": sides,
                                    "resistors": _corner_resistors(selected, sides),
                                },
                                "causes": [],
                            }
                        ],
                    }
                ]
            )

        for w in result.get("warnings", []):
            if w not in warnings:
                warnings.append(w)

        pattern = "".join("+" if s == SIDE_PLUS else "-" for s in sides)
        corner = {
            "index": mask,
            "pattern": pattern,
            "sides": sides,
            "resistors": _corner_resistors(selected, sides),
            "value": value,
        }
        corners.append(corner)

        # 严格比较：并列时保留编号更小（更早出现）的角点
        if min_record is None or value < min_record["value"]:
            min_record = corner
        if max_record is None or value > max_record["value"]:
            max_record = corner

    assert min_record is not None and max_record is not None  # n ≥ 1，至少有 2 个角点

    def extreme_record(corner: dict[str, Any]) -> dict[str, Any]:
        deviation_abs = corner["value"] - f_nominal
        if abs(deviation_abs) < _TINY:
            deviation_abs = 0.0
        return {
            "value": corner["value"],
            "deviation_abs": deviation_abs,
            "deviation_rel": None if abs(f_nominal) < _TINY else deviation_abs / f_nominal,
            "corner": {
                "index": corner["index"],
                "pattern": corner["pattern"],
                "sides": corner["sides"],
                "resistors": corner["resistors"],
            },
        }

    return {
        "ok": True,
        "envelope": {
            "quantity": quantity,
            "target": target,
            "target_name": target_name,
            "target_unit": target_unit,
            "target_nominal": f_nominal,
            "resistors": [
                {
                    "id": spec_row["id"],
                    "index": spec_row["index"],
                    "base_value": spec_row["base_value"],
                    "tolerance_pct": spec_row["tolerance_pct"],
                    "value_minus": spec_row["value_minus"],
                    "value_plus": spec_row["value_plus"],
                }
                for spec_row in selected
            ],
        },
        "minimum": extreme_record(min_record),
        "maximum": extreme_record(max_record),
        "corners": corners,
        "counts": {"resistors": n, "corners": total},
        "warnings": warnings,
    }
