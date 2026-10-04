#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大坝泄洪调度监控工具（纯 Python 标准库，单文件）

用法：
    python3 dam_dispatch.py            # 运行内置自测样例
    python3 dam_dispatch.py input.json # 从 JSON 文件读取输入

输入 JSON 格式：
{
  "dam": {"name": "青云大坝", "normal_level": 100.0, "warning_level": 105.0,
          "gates": [{"name": "闸A", "capacity": 120.0}, ...]},
  "monitors":  [{"step": 1, "water_level": 101.0, "displacement": 1.0}, ...],
  "schedules": [{"step": 3, "gate": "闸A", "opening": "全开"}, ...]
}
opening 取值：全开 / 半开 / 关闭（兼容 full/half/closed）。step 可省略，默认按顺序编号。

自定规则（理由）：
1. 水位 > 警戒水位：报告并自动触发泄洪。泄洪需求 = (水位 - 正常水位) * 50 (m³/s)，
   理由：超出正常蓄水的每米水位需要对应泄量才能回落，系数 50 为示例库容-泄量比。
2. 自动开闸：按闸门能力从大到小贪心全开（已半开的按需提升全开）。
   理由：优先大闸可最小化开闸数量，降低操作与故障风险。
3. 有效泄量 = Σ(闸门能力 × 开度系数[全开1.0/半开0.5/关闭0])；有效泄量 < 需求时报缺口。
4. 坝体位移 > 10mm：报告并级联提升为“紧急”，全部闸门强制全开。
   理由：位移异常预示结构风险，按最高等级处置。
5. 恢复常规：水位 ≤ 正常水位 且 位移不超限 时，状态复位“正常”并自动关闭全部闸门。
   理由：以正常水位（而非警戒水位）作回落条件形成滞回，避免水位在警戒线附近抖动
   导致状态频繁切换。
6. 监测跳变：相邻两次水位差 > 2.0m 或位移差 > 5mm 报告异常（传感器故障或险情征兆）。
7. 调度引用不存在的闸门、同一步同一闸重复调度：报告错误并忽略该指令。
8. 监测流与调度流按 step 归并处理（同步先监测后调度），引擎状态跨流延续。
"""

import json
import sys
from dataclasses import dataclass, field

LEVEL_JUMP_THRESHOLD = 2.0    # 水位跳变阈值 (m)
DISP_JUMP_THRESHOLD = 5.0     # 位移跳变阈值 (mm)
DISP_ALARM_THRESHOLD = 10.0   # 位移报警阈值 (mm)
DEMAND_COEF = 50.0            # 泄洪需求系数 (m³/s 每超蓄米数)

OPENING_FRACTION = {"全开": 1.0, "半开": 0.5, "关闭": 0.0}
OPENING_ALIASES = {"full": "全开", "half": "半开", "closed": "关闭",
                   "全开": "全开", "半开": "半开", "关闭": "关闭"}

STATE_NORMAL = "正常"
STATE_DISCHARGING = "警戒泄洪"
STATE_CRITICAL = "紧急"


@dataclass
class Gate:
    name: str
    capacity: float
    opening: str = "关闭"

    @property
    def effective(self):
        return self.capacity * OPENING_FRACTION[self.opening]


@dataclass
class DamEngine:
    name: str
    normal_level: float
    warning_level: float
    gates: dict
    state: str = STATE_NORMAL
    level: float = None
    disp: float = None
    errors: list = field(default_factory=list)
    log: list = field(default_factory=list)
    _scheduled: dict = field(default_factory=dict)  # step -> {gate names}

    # ---------- 基础工具 ----------
    def report(self, step, etype, msg):
        self.errors.append({"step": step, "type": etype, "message": msg})

    def demand(self):
        if self.level is not None and self.level > self.warning_level:
            return (self.level - self.normal_level) * DEMAND_COEF
        return 0.0

    def capacity(self):
        return sum(g.effective for g in self.gates.values())

    def snapshot(self, step, event):
        gates_desc = " ".join(
            f"{g.name}:{g.opening}({g.effective:.0f})" for g in self.gates.values())
        lvl = f"{self.level:.1f}" if self.level is not None else "-"
        dsp = f"{self.disp:.1f}" if self.disp is not None else "-"
        self.log.append(
            f"[步骤{step}] {event}\n"
            f"    状态={self.state} 水位={lvl}m 位移={dsp}mm "
            f"泄洪需求={self.demand():.0f} 有效泄量={self.capacity():.0f}\n"
            f"    闸门: {gates_desc}")

    # ---------- 泄洪控制 ----------
    def ensure_discharge(self, step):
        """水位超警戒时保证有效泄量满足需求，不足先自动开闸，再报缺口。"""
        need = self.demand()
        if need <= 0:
            return
        deficit = need - self.capacity()
        if deficit > 0:
            # 贪心：可按需提升的闸门（非全开）按剩余能力从大到小全开
            candidates = sorted(
                (g for g in self.gates.values() if g.opening != "全开"),
                key=lambda g: g.capacity * (1.0 - OPENING_FRACTION[g.opening]),
                reverse=True)
            opened = []
            for g in candidates:
                if deficit <= 0:
                    break
                g.opening = "全开"
                opened.append(g.name)
                deficit = need - self.capacity()
            if opened:
                self.report(step, "自动泄洪",
                            f"水位{self.level:.1f}m超警戒{self.warning_level:.1f}m，"
                            f"级联自动全开闸门: {', '.join(opened)}")
        gap = need - self.capacity()
        if gap > 0:
            self.report(step, "泄量缺口",
                        f"需求{need:.0f}m³/s，有效泄量{self.capacity():.0f}m³/s，"
                        f"缺口{gap:.0f}m³/s，全部闸门已开仍不足！")

    def escalate_critical(self, step, reason):
        if self.state != STATE_CRITICAL:
            self.state = STATE_CRITICAL
            self.report(step, "级联提升警戒", reason + "，状态提升为【紧急】")
        forced = [g.name for g in self.gates.values() if g.opening != "全开"]
        for g in self.gates.values():
            g.opening = "全开"
        if forced:
            self.report(step, "强制泄洪",
                        f"紧急状态级联强制全开闸门: {', '.join(forced)}")

    def check_recovery(self, step):
        if (self.state != STATE_NORMAL and self.level is not None
                and self.level <= self.normal_level
                and (self.disp is None or self.disp <= DISP_ALARM_THRESHOLD)):
            closed = [g.name for g in self.gates.values() if g.opening != "关闭"]
            for g in self.gates.values():
                g.opening = "关闭"
            self.state = STATE_NORMAL
            self.report(step, "恢复常规",
                        f"水位回落至{self.level:.1f}m(≤正常{self.normal_level:.1f}m)，"
                        f"状态复位【正常】，级联关闭闸门: "
                        f"{', '.join(closed) if closed else '无'}")

    # ---------- 监测流 ----------
    def on_monitor(self, step, level, disp):
        if self.level is not None and abs(level - self.level) > LEVEL_JUMP_THRESHOLD:
            self.report(step, "监测跳变",
                        f"水位{self.level:.1f}→{level:.1f}m，"
                        f"跳变{abs(level - self.level):.1f}m超阈值{LEVEL_JUMP_THRESHOLD}m")
        if self.disp is not None and abs(disp - self.disp) > DISP_JUMP_THRESHOLD:
            self.report(step, "监测跳变",
                        f"位移{self.disp:.1f}→{disp:.1f}mm，"
                        f"跳变{abs(disp - self.disp):.1f}mm超阈值{DISP_JUMP_THRESHOLD}mm")
        self.level, self.disp = level, disp

        if disp > DISP_ALARM_THRESHOLD:
            self.report(step, "位移超限",
                        f"坝体位移{disp:.1f}mm超阈值{DISP_ALARM_THRESHOLD}mm")
            self.escalate_critical(step, f"坝体位移{disp:.1f}mm超限")

        if level > self.warning_level:
            if self.state == STATE_NORMAL:
                self.state = STATE_DISCHARGING
                self.report(step, "超警戒",
                            f"水位{level:.1f}m超警戒{self.warning_level:.1f}m，"
                            f"触发泄洪，状态提升为【警戒泄洪】")
            self.ensure_discharge(step)
        elif self.state == STATE_CRITICAL:
            self.ensure_discharge(step)  # 紧急状态下持续校验泄量

        self.check_recovery(step)
        self.snapshot(step, f"监测 水位={level:.1f}m 位移={disp:.1f}mm")

    # ---------- 调度流 ----------
    def on_schedule(self, step, gate_name, opening):
        if gate_name not in self.gates:
            self.report(step, "闸门不存在",
                        f"调度引用不存在的闸门【{gate_name}】，指令忽略")
            self.snapshot(step, f"调度 {gate_name}={opening}（被拒绝）")
            return
        done = self._scheduled.setdefault(step, set())
        if gate_name in done:
            self.report(step, "重复调度",
                        f"闸门【{gate_name}】在步骤{step}被重复调度，指令忽略")
            self.snapshot(step, f"调度 {gate_name}={opening}（重复，被拒绝）")
            return
        opening = OPENING_ALIASES.get(str(opening).strip().lower(),
                                      OPENING_ALIASES.get(str(opening).strip()))
        if opening is None:
            self.report(step, "非法开度", f"开度【{opening}】非法，指令忽略")
            return
        done.add(gate_name)
        self.gates[gate_name].opening = opening
        self.ensure_discharge(step)  # 手动调度后重新校验泄量是否仍满足需求
        self.snapshot(step, f"调度 {gate_name}={opening}")


def run(data):
    dam = data["dam"]
    engine = DamEngine(
        name=dam["name"],
        normal_level=float(dam["normal_level"]),
        warning_level=float(dam["warning_level"]),
        gates={g["name"]: Gate(g["name"], float(g["capacity"]))
               for g in dam["gates"]})
    events = []
    for i, m in enumerate(data.get("monitors", []), 1):
        events.append((m.get("step", i), 0, m))
    for i, s in enumerate(data.get("schedules", []), 1):
        events.append((s.get("step", i), 1, s))
    events.sort(key=lambda e: (e[0], e[1]))  # 同步：先监测后调度

    for step, kind, ev in events:
        if kind == 0:
            engine.on_monitor(step, float(ev["water_level"]),
                              float(ev["displacement"]))
        else:
            engine.on_schedule(step, ev["gate"], ev["opening"])
    return engine


def print_report(engine):
    print("=" * 72)
    print(f"大坝【{engine.name}】运行过程  (正常水位={engine.normal_level}m "
          f"警戒水位={engine.warning_level}m)")
    print("=" * 72)
    for line in engine.log:
        print(line)
    print("=" * 72)
    print(f"最终状态: {engine.state}  水位={engine.level:.1f}m  "
          f"位移={engine.disp:.1f}mm")
    for g in engine.gates.values():
        print(f"  {g.name}: {g.opening} (能力{g.capacity:.0f}m³/s)")
    print("-" * 72)
    print(f"错误/事件报告（共{len(engine.errors)}条）:")
    for e in engine.errors:
        print(f"  [步骤{e['step']}][{e['type']}] {e['message']}")
    print("=" * 72)


DEMO = {
    "dam": {"name": "青云大坝", "normal_level": 100.0, "warning_level": 105.0,
            "gates": [{"name": "闸A", "capacity": 120.0},
                      {"name": "闸B", "capacity": 100.0},
                      {"name": "闸C", "capacity": 80.0}]},
    "monitors": [
        {"step": 1, "water_level": 101.0, "displacement": 1.0},
        {"step": 2, "water_level": 106.5, "displacement": 2.0},
        {"step": 4, "water_level": 104.0, "displacement": 12.0},
        {"step": 5, "water_level": 101.5, "displacement": 3.0},
        {"step": 6, "water_level": 99.8, "displacement": 2.0},
        {"step": 7, "water_level": 99.5, "displacement": 1.5},
    ],
    "schedules": [
        {"step": 3, "gate": "闸X", "opening": "全开"},   # 不存在的闸门
        {"step": 3, "gate": "闸A", "opening": "半开"},   # 手动压低开度→缺口
        {"step": 3, "gate": "闸A", "opening": "全开"},   # 同步重复调度
        {"step": 7, "gate": "闸B", "opening": "全开"},   # 常规下手动调度
    ],
}


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            data = json.load(f)
    else:
        print("（未提供输入文件，运行内置自测样例）\n")
        data = DEMO
    print_report(run(data))


if __name__ == "__main__":
    main()
