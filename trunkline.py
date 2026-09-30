#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
干线调度校验工具（纯 Python 标准库，单文件）。

输入为带关键字前缀的行文本（文件或标准输入），行序即时序：
    LINE   线名   计划发车时刻   运行时长(分钟)   装载上限(件)
    VEHICLE 车辆编号   状态(available|maintenance)
    LOAD   线名   车辆编号   包裹量
    DEPART 线名   车辆编号   实际发车时刻
以 # 开头或空行被忽略。时刻格式 HH:MM，允许 24:00 之后（如 25:30）。

示例：
    LINE 京广 08:00 240 1000
    VEHICLE V1 available
    LOAD 京广 V1 1200
    DEPART 京广 V1 09:05

用法：
    python3 trunkline.py data.txt
    python3 trunkline.py --demo
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 规则常量（可在此集中调整）
# ---------------------------------------------------------------------------
PENALTY_LATE_MINUTES = 60   # 晚点达到该分钟数触发赔付标记


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def parse_hhmm(text: str) -> int:
    """把 HH:MM 解析为“分钟数”，支持超过 24 点（如 26:10）。"""
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"时刻格式应为 HH:MM：{text!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= minute < 60) or hour < 0:
        raise ValueError(f"非法时刻：{text!r}")
    return hour * 60 + minute


def fmt_hhmm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def fmt_late(minutes: int) -> str:
    sign = "-" if minutes < 0 else ""
    minutes = abs(minutes)
    return f"{sign}{minutes // 60}小时{minutes % 60}分"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Line:
    name: str
    base_depart: int          # 首班计划发车时刻（分钟）
    duration: int             # 运行时长（分钟）
    capacity: int             # 装载上限（件）
    next_planned: int = 0     # 下一班计划发车时刻（随晚点级联顺延）
    total_delay: int = 0      # 累计晚点分钟
    departures: int = 0       # 已完成班次数

    def __post_init__(self) -> None:
        if self.duration <= 0:
            raise ValueError(f"线路 {self.name} 运行时长必须为正数")
        if self.capacity < 0:
            raise ValueError(f"线路 {self.name} 装载上限不能为负")
        self.next_planned = self.base_depart


@dataclass
class Vehicle:
    code: str
    maintenance: bool = False
    available_at: int = -1    # 运输中时：预计再次可用的时刻；否则为 -1
    last_depart: int = -1     # 最近一次实际发车时刻，用于同时刻冲突判定


@dataclass
class Issue:
    kind: str                 # 错误类别
    detail: str               # 人读描述
    ref: Tuple = ()           # 结构化定位（线、车等），便于程序消费


@dataclass
class Dispatcher:
    lines: Dict[str, Line] = field(default_factory=dict)
    vehicles: Dict[str, Vehicle] = field(default_factory=dict)
    # (线名, 车号) -> 当前班次已装载件数；发车后清空，进入下一班
    loads: Dict[Tuple[str, str], int] = field(default_factory=dict)
    issues: List[Issue] = field(default_factory=list)

    # -- 报告辅助 ----------------------------------------------------------
    def report(self, kind: str, detail: str, ref: Tuple = ()) -> None:
        self.issues.append(Issue(kind, detail, ref))

    # -- 状态惰性结算 ------------------------------------------------------
    def settle_vehicle(self, vehicle: Vehicle, now: int) -> None:
        """时刻推进到 now：若车辆运输已结束，恢复为可用。"""
        if vehicle.available_at >= 0 and now >= vehicle.available_at:
            vehicle.available_at = -1

    # -- 装载 --------------------------------------------------------------
    def handle_load(self, line_name: str, vehicle_code: str, qty: int) -> None:
        line = self.lines.get(line_name)
        vehicle = self.vehicles.get(vehicle_code)
        if line is None:
            self.report(
                "引用不存在的线路",
                f"装载引用了不存在的线路 {line_name!r}（车 {vehicle_code}，{qty} 件）",
                ("LOAD", line_name, vehicle_code),
            )
            return
        if vehicle is None:
            self.report(
                "引用不存在的车辆",
                f"装载引用了不存在的车辆 {vehicle_code!r}（线 {line_name}，{qty} 件）",
                ("LOAD", line_name, vehicle_code),
            )
            return
        if qty < 0:
            self.report(
                "非法装载量",
                f"线路 {line_name} 车辆 {vehicle_code} 装载量为负（{qty}）",
                ("LOAD", line_name, vehicle_code),
            )
            return

        key = (line_name, vehicle_code)
        if key in self.loads:
            self.report(
                "重复装载",
                f"车辆 {vehicle_code} 在线路 {line_name} 当前班次已装载 "
                f"{self.loads[key]} 件，重复装载 {qty} 件被忽略",
                ("DUP_LOAD", line_name, vehicle_code),
            )
            return

        self.loads[key] = qty
        if qty > line.capacity:
            excess = qty - line.capacity
            self.report(
                "装载超上限",
                f"线路 {line_name} 车辆 {vehicle_code} 装载 {qty} 件，"
                f"超出上限 {line.capacity} 件，超装 {excess} 件",
                ("OVERLOAD", line_name, vehicle_code, excess),
            )

    # -- 发车 --------------------------------------------------------------
    def handle_depart(self, line_name: str, vehicle_code: str, actual: int) -> None:
        line = self.lines.get(line_name)
        vehicle = self.vehicles.get(vehicle_code)
        if line is None:
            self.report(
                "引用不存在的线路",
                f"发车引用了不存在的线路 {line_name!r}（车 {vehicle_code}，"
                f"时刻 {fmt_hhmm(actual)}）",
                ("DEPART", line_name, vehicle_code),
            )
            return
        if vehicle is None:
            self.report(
                "引用不存在的车辆",
                f"发车引用了不存在的车辆 {vehicle_code!r}（线 {line_name}，"
                f"时刻 {fmt_hhmm(actual)}）",
                ("DEPART", line_name, vehicle_code),
            )
            return

        # 1) 检修车不得发车：仅报告，不发生任何状态变更（该车本班不执行）。
        if vehicle.maintenance:
            self.report(
                "检修车辆发车",
                f"检修车辆 {vehicle_code} 不得在 {fmt_hhmm(actual)} "
                f"执行线路 {line_name}，发车无效",
                ("MAINTENANCE", line_name, vehicle_code),
            )
            return

        self.settle_vehicle(vehicle, actual)

        # 2a) 同一时刻冲突：同一车辆在同一时刻被安排了两笔发车（通常分属不同线路）。
        if actual == vehicle.last_depart:
            self.report(
                "同一时刻多线路发车冲突",
                f"车辆 {vehicle_code} 在 {fmt_hhmm(actual)} 同时刻已被安排另一线路，"
                f"本次线路 {line_name} 发车无效",
                ("CONFLICT", line_name, vehicle_code),
            )
            return

        # 2b) 占用冲突：车辆尚未结束上一班运输。
        if vehicle.available_at >= 0:
            self.report(
                "车辆占用冲突",
                f"车辆 {vehicle_code} 在 {fmt_hhmm(actual)} 被安排执行线路 "
                f"{line_name}，但该车 {fmt_hhmm(vehicle.available_at)} 前仍在"
                f"上一班运输中，发车无效",
                ("CONFLICT", line_name, vehicle_code),
            )
            return

        # 3) 晚点判定：实际晚于“当前班计划时刻”即为晚点（计划时刻已含历史晚点顺延）。
        #    自定阈值：晚点 >= 60 分钟额外标记触发赔付——题目约定“晚点一小时就赔钱”。
        late = actual - line.next_planned
        if late > 0:
            penalty = "，已达 60 分钟赔付线" if late >= PENALTY_LATE_MINUTES else ""
            self.report(
                "发车晚点",
                f"线路 {line_name} 车辆 {vehicle_code} 计划 {fmt_hhmm(line.next_planned)} "
                f"实际 {fmt_hhmm(actual)}，晚点 {fmt_late(late)}{penalty}",
                ("LATE", line_name, vehicle_code, late),
            )

        # 4) 级联更新（仅在发车合法时发生）。
        #    车辆：进入运输中，到 actual + duration 才恢复可用。
        #    线路：下一班计划 = max(实际, 本班计划) + 运行时长。
        #    即早点不回拨、晚点全额顺延，后续班次继承全部延误。
        vehicle.available_at = actual + line.duration
        vehicle.last_depart = actual
        line.total_delay += max(0, late)
        line.next_planned = max(actual, line.next_planned) + line.duration
        line.departures += 1
        self.loads.pop((line_name, vehicle_code), None)  # 随车发走，下一班重新装载


# ---------------------------------------------------------------------------
# 输入解析
# ---------------------------------------------------------------------------
def parse(lines) -> Dispatcher:
    d = Dispatcher()
    for lineno, raw in enumerate(lines, 1):
        text = raw.split("#", 1)[0].strip()
        if not text:
            continue
        parts = text.split()
        cmd = parts[0].upper()
        try:
            if cmd == "LINE" and len(parts) == 5:
                name, depart, duration, cap = parts[1], parts[2], parts[3], parts[4]
                if name in d.lines:
                    d.report("重复定义", f"第 {lineno} 行：线路 {name} 重复定义，后者忽略",
                             ("DUP_DEF", name))
                    continue
                d.lines[name] = Line(name, parse_hhmm(depart), int(duration), int(cap))
            elif cmd == "VEHICLE" and len(parts) == 3:
                code, status = parts[1], parts[2].lower()
                if code in d.vehicles:
                    d.report("重复定义", f"第 {lineno} 行：车辆 {code} 重复定义，后者忽略",
                             ("DUP_DEF", code))
                    continue
                if status not in ("available", "maintenance"):
                    raise ValueError(f"车辆状态须为 available/maintenance：{parts[2]}")
                d.vehicles[code] = Vehicle(code, status == "maintenance")
            elif cmd == "LOAD" and len(parts) == 4:
                d.handle_load(parts[1], parts[2], int(parts[3]))
            elif cmd == "DEPART" and len(parts) == 4:
                d.handle_depart(parts[1], parts[2], parse_hhmm(parts[3]))
            else:
                raise ValueError(f"无法识别的记录：{text!r}")
        except ValueError as exc:
            d.report("输入格式错误", f"第 {lineno} 行：{exc}（该行已跳过）", ("SYNTAX", lineno))
    return d


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------
def render(d: Dispatcher) -> str:
    out: List[str] = []
    out.append("=" * 64)
    out.append("干线状态")
    out.append("=" * 64)
    out.append("[线路]")
    for line in d.lines.values():
        out.append(
            f"  {line.name}: 首班 {fmt_hhmm(line.base_depart)} | 运行 {line.duration} 分钟 "
            f"| 上限 {line.capacity} 件 | 已发 {line.departures} 班"
        )
        out.append(
            f"    下一班计划 {fmt_hhmm(line.next_planned)}，"
            f"累计晚点 {fmt_late(line.total_delay)}"
        )
    out.append("[车辆]")
    for vehicle in d.vehicles.values():
        if vehicle.maintenance:
            state = "检修中（禁止发车）"
        elif vehicle.available_at >= 0:
            state = f"运输中，{fmt_hhmm(vehicle.available_at)} 恢复可用"
        else:
            state = "可用"
        carrying = [f"{ln}:{qty}件" for (ln, vc), qty in sorted(d.loads.items())
                    if vc == vehicle.code]
        tail = f"，待发装载 {', '.join(carrying)}" if carrying else ""
        out.append(f"  {vehicle.code}: {state}{tail}")

    out.append("")
    out.append("=" * 64)
    out.append(f"错误清单（共 {len(d.issues)} 条）")
    out.append("=" * 64)
    if not d.issues:
        out.append("  无错误")
    else:
        for idx, issue in enumerate(d.issues, 1):
            out.append(f"  {idx:>2}. [{issue.kind}] {issue.detail}")
    return "\n".join(out)


DEMO_DATA = """\
# 线路：名称 首班时刻 运行分钟 装载上限
LINE 京广 08:00 240 1000
LINE 沪深 09:00 300 800
# 车辆：编号 状态
VEHICLE V1 available
VEHICLE V2 available
VEHICLE V3 maintenance

# 跨流延续：装载可在发车之前，也可在事件流任意位置出现
LOAD 京广 V1 950
LOAD 京广 V2 1300          # 超装 300 件
LOAD 京广 V2 20            # 重复装载，应报告
LOAD 京广 VX 10            # 车辆不存在
LOAD 幽灵线 V1 10          # 线路不存在

DEPART 京广 V1 08:40       # 晚点 40 分钟，不赔付
DEPART 沪深 V3 09:10       # 检修车发车，无效
DEPART 沪深 V2 09:10       # 正常，V2 14:10 才回
DEPART 沪深 V2 10:00       # 占用冲突，无效
DEPART 京广 V1 13:05       # 计划已顺延至 12:40，晚点 25 分钟
DEPART 沪深 V1 13:05       # 与上一笔同车同刻、不同线路，冲突
DEPART 京广 V2 16:30       # V2 上一班 14:10 已结束，合法；晚于顺延计划
DEPART 京广 V4 17:00       # 车辆不存在
"""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="干线调度状态与错误报告工具")
    parser.add_argument("input", nargs="?", help="输入文件；缺省读取标准输入")
    parser.add_argument("--demo", action="store_true", help="运行内置演示数据")
    args = parser.parse_args(argv)

    if args.demo:
        lines = DEMO_DATA.splitlines()
    elif args.input:
        with open(args.input, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    else:
        lines = sys.stdin.readlines()

    dispatcher = parse(lines)
    print(render(dispatcher))
    return 1 if dispatcher.issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
