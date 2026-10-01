#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deep_dive_monitor.py — 深海潜航状态模拟与错误报告工具（纯 Python 标准库，单文件）

输入（JSON，文件路径或标准输入）：
{
  "submersible": {"name": "蛟龙号", "max_depth": 7000, "battery_capacity": 100, "ballast_tanks": 4},
  "tasks":       [{"name": "T1", "target_depth": 3500}],
  "operations":  [{"type": "下潜", "task": "T1", "depth": 3500, "battery": 15},
                  {"type": "悬停", "depth": 3500, "battery": 5},
                  {"type": "上浮", "depth": 0,    "battery": 10}],
  "monitoring":  [{"depth": 3500, "battery": 80, "status": "正常"}]
}
  - 操作 type ∈ {下潜, 上浮, 悬停}；task 可选；battery 为本操作耗电量
  - 监测 status ∈ {正常, 报警}

自定规则（理由）：
  1. 电池耗尽判定：操作耗电后剩余电量 <= 0 即判定耗尽。
     理由：电量归零即失去全部动力，必须立即报告并级联强制上浮。
  2. 电池不足阈值：容量的 20%（LOW_BATTERY_RATIO）。
     理由：上浮与应急机动本身耗电，需预留安全余量；低于该值视为
     “电池不足”，未完成任务级联放弃。
  3. 深潜区判定：深度 >= 上限的 50%（DEEP_DIVE_RATIO）。
     理由：超过半量程后上浮耗时长、风险高，此区低电继续下潜属
     “深度与电池矛盾”。

用法：
  python3 deep_dive_monitor.py input.json     # 从文件读取
  cat input.json | python3 deep_dive_monitor.py   # 从标准输入读取
  python3 deep_dive_monitor.py --demo         # 运行内置示例（触发全部错误类型）
  python3 deep_dive_monitor.py --json input.json  # JSON 格式输出

退出码：0 = 无“错误”级报告；1 = 存在“错误”级报告；2 = 输入非法。
"""
from __future__ import annotations

import argparse
import json
import sys

LOW_BATTERY_RATIO = 0.20   # 低电阈值：容量的 20%
DEEP_DIVE_RATIO = 0.50     # 深潜区阈值：上限的 50%

REQUIRED_SUB_KEYS = ("name", "max_depth", "battery_capacity", "ballast_tanks")
OP_TYPES = ("下潜", "上浮", "悬停")
MON_STATUS = ("正常", "报警")


class SpecError(Exception):
    """输入定义非法。"""


def num(value):
    """整数形式的浮点显示为整数，便于阅读。"""
    return int(value) if float(value).is_integer() else round(float(value), 4)


def simulate(spec):
    if not isinstance(spec, dict):
        raise SpecError("输入必须是 JSON 对象")
    sub = spec.get("submersible")
    if not isinstance(sub, dict):
        raise SpecError("缺少 submersible 定义")
    missing = [k for k in REQUIRED_SUB_KEYS if k not in sub]
    if missing:
        raise SpecError("submersible 缺少字段: " + ", ".join(missing))

    name = str(sub["name"])
    max_depth = float(sub["max_depth"])
    capacity = float(sub["battery_capacity"])
    tanks = int(sub["ballast_tanks"])
    if max_depth <= 0:
        raise SpecError("max_depth 必须为正数")
    if capacity <= 0:
        raise SpecError("battery_capacity 必须为正数")
    if tanks < 0:
        raise SpecError("ballast_tanks 不能为负")

    low_threshold = capacity * LOW_BATTERY_RATIO
    deep_threshold = max_depth * DEEP_DIVE_RATIO

    errors = []

    def report(where, message, level="错误"):
        errors.append({"where": where, "level": level, "message": message})

    # ---- 任务定义 ----
    tasks = {}
    for t in spec.get("tasks", []):
        tname = str(t["name"])
        target = float(t["target_depth"])
        if tname in tasks:
            report("任务定义", f"任务 {tname} 重复定义，后者覆盖前者", "警告")
        if target > max_depth:
            report("任务定义", f"任务 {tname} 目标深度 {num(target)}m 超过深度上限 {num(max_depth)}m")
        if target <= 0:
            report("任务定义", f"任务 {tname} 目标深度非法: {num(target)}m")
        tasks[tname] = {"target": target, "status": "待执行"}

    # ---- 操作流模拟（状态跨操作延续） ----
    depth = 0.0
    battery = capacity
    forced_ascent = False
    exhausted = False
    total_consumed = 0.0
    operations = spec.get("operations", [])

    def consume(where, cost):
        nonlocal battery, total_consumed, forced_ascent, exhausted
        battery -= cost
        total_consumed += cost
        if battery <= 0 and not exhausted:
            battery = 0.0
            exhausted = True
            report(where, "电池耗尽（耗电后剩余电量 ≤ 0），潜航器失去动力，级联强制上浮")
            forced_ascent = True
        elif battery < 0:
            battery = 0.0

    def update_tasks(where):
        for tname, t in tasks.items():
            if t["status"] in ("待执行", "执行中") and depth >= t["target"]:
                t["status"] = "已完成"
        for tname, t in tasks.items():
            if t["status"] in ("待执行", "执行中") and battery < low_threshold:
                t["status"] = "已放弃(电池不足)"
                report(where, f"任务 {tname} 未完成且电池 {num(battery)} 低于低电阈值 "
                              f"{num(low_threshold)}，级联放弃该任务")

    for idx, op in enumerate(operations, 1):
        where = f"操作#{idx}"
        typ = op.get("type")
        tname = op.get("task")
        cost = float(op.get("battery", 0) or 0)
        if cost < 0:
            report(where, f"电池消耗为负（{num(cost)}），按 0 处理", "警告")
            cost = 0.0

        if tname is not None:
            tname = str(tname)
            if tname not in tasks:
                report(where, f"操作引用了不存在的任务 {tname}")
                tname = None
            elif tasks[tname]["status"] in ("待执行", "执行中"):
                tasks[tname]["status"] = "执行中"

        if typ not in OP_TYPES:
            report(where, f"未知操作类型 {typ!r}，已跳过")
            continue

        if typ == "悬停":
            # 悬停：深度不变，仅消耗电池
            d = op.get("depth")
            if d is not None and abs(float(d) - depth) > 1e-9:
                report(where, f"悬停深度 {num(float(d))}m 与当前深度 {num(depth)}m 不一致，"
                              f"悬停不改变深度，以当前深度为准", "警告")
            consume(where, cost)
            update_tasks(where)
            continue

        if "depth" not in op:
            report(where, f"{typ} 操作缺少目标深度，已跳过")
            continue
        target = float(op["depth"])

        if typ == "下潜":
            if forced_ascent:
                report(where, "强制上浮期间禁止下潜，已忽略该操作")
                continue
            if exhausted:
                report(where, "电池已耗尽，禁止下潜，已忽略该操作")
                continue
            if target > max_depth:
                report(where, f"下潜目标 {num(target)}m 超过深度上限 {num(max_depth)}m，"
                              f"级联强制上浮")
                depth = max_depth
                forced_ascent = True
                if tname and tasks[tname]["status"] in ("待执行", "执行中"):
                    tasks[tname]["status"] = "已放弃(超深度上限)"
                    report(where, f"任务 {tname} 因超深度上限级联放弃")
                consume(where, cost)
                update_tasks(where)
                continue
            if abs(target - depth) < 1e-9 and depth > 0:
                report(where, f"重复下潜到同深度 {num(target)}m（当前已位于该深度）")
            if target < depth:
                report(where, f"下潜目标 {num(target)}m 浅于当前深度 {num(depth)}m，"
                              f"应使用上浮操作", "警告")
            if depth >= deep_threshold and battery - cost < low_threshold:
                report(where, f"深度与电池矛盾：当前深度 {num(depth)}m 属深潜区，"
                              f"本操作后电池将降至 {num(battery - cost)}"
                              f"（低于低电阈值 {num(low_threshold)}）仍继续下潜")
            depth = target
        else:  # 上浮
            if target > depth:
                report(where, f"上浮目标 {num(target)}m 深于当前深度 {num(depth)}m，"
                              f"应使用下潜操作")
            depth = max(target, 0.0)
            if depth == 0.0 and forced_ascent:
                report(where, "已上浮至水面，强制上浮状态级联解除，潜航状态恢复正常", "提示")
                forced_ascent = False

        consume(where, cost)
        update_tasks(where)

    # ---- 监测流检查 ----
    for j, m in enumerate(spec.get("monitoring", []), 1):
        where = f"监测#{j}"
        if "depth" not in m or "battery" not in m:
            report(where, "监测记录缺少深度或电池读数")
            continue
        d = float(m["depth"])
        b = float(m["battery"])
        s = m.get("status")
        if s not in MON_STATUS:
            report(where, f"未知监测状态 {s!r}")
        if d < 0:
            report(where, f"监测深度为负（{num(d)}m）")
        if d > max_depth:
            report(where, f"监测深度 {num(d)}m 超过深度上限 {num(max_depth)}m")
        if b < 0 or b > capacity:
            report(where, f"监测电池读数 {num(b)} 非法（容量 {num(capacity)}）")
        if d >= deep_threshold and b <= low_threshold:
            if s == "正常":
                report(where, f"深度与电池矛盾：深度 {num(d)}m 且电池 {num(b)} 低于低电阈值 "
                              f"{num(low_threshold)}，监测状态却为正常（应为报警）")
            if any(o.get("type") == "下潜" for o in operations):
                report(where, f"深潜低电量（{num(d)}m / 电池 {num(b)}）情况下操作流仍包含"
                              f"下潜操作，存在继续下潜风险", "警告")

    return {
        "submersible": {
            "name": name,
            "max_depth": num(max_depth),
            "battery_capacity": num(capacity),
            "ballast_tanks": tanks,
        },
        "rules": {
            "low_battery_threshold": num(low_threshold),
            "deep_dive_threshold": num(deep_threshold),
        },
        "final_state": {
            "depth": num(depth),
            "battery": num(battery),
            "battery_pct": round(battery / capacity * 100, 1),
            "total_consumed": num(total_consumed),
            "forced_ascent": forced_ascent,
            "surfaced": depth == 0,
        },
        "tasks": {n: {"target_depth": num(t["target"]), "status": t["status"]}
                  for n, t in tasks.items()},
        "errors": errors,
    }


def render_text(result):
    lines = []
    sub = result["submersible"]
    rules = result["rules"]
    st = result["final_state"]
    lines.append("========== 潜航状态 ==========")
    lines.append(f"潜航器: {sub['name']}（深度上限 {sub['max_depth']}m，"
                 f"电池容量 {sub['battery_capacity']}，压载仓 {sub['ballast_tanks']} 个）")
    lines.append(f"判定规则: 低电阈值 {rules['low_battery_threshold']}（容量 20%），"
                 f"深潜区 ≥ {rules['deep_dive_threshold']}m（上限 50%），耗尽 = 电量 ≤ 0")
    lines.append(f"当前深度: {st['depth']}m")
    lines.append(f"剩余电池: {st['battery']}（{st['battery_pct']}%），累计消耗 {st['total_consumed']}")
    lines.append(f"强制上浮: {'是' if st['forced_ascent'] else '否'}；"
                 f"水面状态: {'已上浮至水面' if st['surfaced'] else '潜航中'}")
    lines.append("任务状态:")
    if result["tasks"]:
        for tname, t in result["tasks"].items():
            lines.append(f"  - {tname}: 目标 {t['target_depth']}m，状态 {t['status']}")
    else:
        lines.append("  （无任务）")
    lines.append("========== 错误清单 ==========")
    if result["errors"]:
        for i, e in enumerate(result["errors"], 1):
            lines.append(f"[{i}] ({e['level']}) {e['where']}: {e['message']}")
    else:
        lines.append("无错误。")
    return "\n".join(lines)


DEMO = {
    "submersible": {"name": "蛟龙号", "max_depth": 7000, "battery_capacity": 100,
                    "ballast_tanks": 4},
    "tasks": [
        {"name": "T1", "target_depth": 3500},
        {"name": "T2", "target_depth": 6800},
        {"name": "T3", "target_depth": 7500},
    ],
    "operations": [
        {"type": "下潜", "task": "T1", "depth": 3500, "battery": 15},
        {"type": "悬停", "depth": 3500, "battery": 5},
        {"type": "下潜", "task": "T1", "depth": 3500, "battery": 0},
        {"type": "下潜", "task": "T9", "depth": 4000, "battery": 10},
        {"type": "下潜", "task": "T2", "depth": 7200, "battery": 20},
        {"type": "下潜", "task": "T2", "depth": 6800, "battery": 5},
        {"type": "上浮", "depth": 0, "battery": 10},
        {"type": "下潜", "task": "T2", "depth": 6800, "battery": 25},
        {"type": "悬停", "depth": 6800, "battery": 10},
        {"type": "下潜", "depth": 6900, "battery": 10},
        {"type": "上浮", "depth": 0, "battery": 0},
    ],
    "monitoring": [
        {"depth": 6800, "battery": 10, "status": "正常"},
        {"depth": 7200, "battery": 30, "status": "报警"},
        {"depth": 100, "battery": 150, "status": "正常"},
        {"depth": 500, "battery": 60, "status": "正常"},
    ],
}


def main(argv=None):
    parser = argparse.ArgumentParser(description="深海潜航状态模拟与错误报告工具")
    parser.add_argument("input", nargs="?", default="-",
                        help="JSON 输入文件路径，省略或 - 表示从标准输入读取")
    parser.add_argument("--demo", action="store_true", help="运行内置示例")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
    args = parser.parse_args(argv)

    if args.demo:
        spec = DEMO
    else:
        try:
            text = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8").read()
            spec = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"输入读取/解析失败: {exc}", file=sys.stderr)
            return 2

    try:
        result = simulate(spec)
    except (SpecError, KeyError, TypeError, ValueError) as exc:
        print(f"输入定义非法: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result))
    return 1 if any(e["level"] == "错误" for e in result["errors"]) else 0


if __name__ == "__main__":
    sys.exit(main())
