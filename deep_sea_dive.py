#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deep_sea_dive.py — 深海潜航状态模拟与错误报告工具（纯 Python 标准库，单文件）

用法:
    python3 deep_sea_dive.py              # 运行内置示例（可直接验证全部规则）
    python3 deep_sea_dive.py input.json   # 从 JSON 文件读取输入
    cat input.json | python3 deep_sea_dive.py -   # 从标准输入读取

输入 JSON 结构:
{
  "submersible": {"name": "蛟龙-X", "max_depth": 7000, "battery_capacity": 1000, "ballast_tanks": 4},
  "missions":    [{"name": "M1", "target_depth": 3000}, ...],
  "operations":  [{"type": "下潜|上浮|悬停", "depth": 米, "battery": 耗电量, "mission": "可选"}, ...],
  "monitoring":  [{"depth": 米, "battery": 剩余电量, "status": "正常|报警", "after": 操作序号(可选,缺省为全程结束后)}, ...]
}

自定义判定规则（及理由）:
1. 电池耗尽: 任一操作消耗后电量 <= 0 即判定耗尽。电量为负在物理上不可行，
   耗尽意味着推进/维生系统失电，必须立即级联强制上浮。
2. 低电量阈值: 剩余电量 < 20% 容量。此时若继续向更深处下潜，判定为
   "深度与电池矛盾"（深潜低电量继续下潜），返程余量已不足。
3. 任务放弃阈值: 剩余电量 < 30% 容量且任务目标深度未达成，判定电池不足以
   完成任务并保留返程余量，级联放弃任务并触发强制上浮。
4. 监测一致性容差: 深度 ±50m、电量 ±5% 容量，超出即报告监测与模拟状态矛盾。
"""

import json
import sys

LOW_BATTERY_RATIO = 0.20                 # 低电量阈值：容量的 20%
MISSION_ABORT_RATIO = 0.30               # 任务放弃阈值：容量的 30%
MONITOR_DEPTH_TOLERANCE = 50.0           # 监测深度容差（米）
MONITOR_BATTERY_TOLERANCE_RATIO = 0.05   # 监测电量容差（容量比例）


class Simulator:
    def __init__(self, data):
        sub = data["submersible"]
        self.name = sub["name"]
        self.max_depth = float(sub["max_depth"])
        self.battery_capacity = float(sub["battery_capacity"])
        self.ballast_tanks = int(sub["ballast_tanks"])
        self.missions = {}
        for m in data.get("missions", []):
            self.missions[m["name"]] = {
                "target_depth": float(m["target_depth"]),
                "status": "未开始",
            }
        self.operations = data.get("operations", [])
        self.monitoring = data.get("monitoring", [])
        # 跨操作延续的潜航状态
        self.depth = 0.0
        self.battery = self.battery_capacity
        self.forced_ascend = False
        self.errors = []
        self.events = []

    # ---------- 主流程 ----------
    def run(self):
        monitors_by_op = {}
        tail_monitors = []
        for mon in self.monitoring:
            after = mon.get("after")
            if after is None:
                tail_monitors.append(mon)
            else:
                monitors_by_op.setdefault(int(after), []).append(mon)
        for idx, op in enumerate(self.operations, 1):
            self.exec_op(idx, op)
            for mon in monitors_by_op.get(idx, []):
                self.check_monitor(idx, mon)
            self.check_mission_abort(idx)
        for mon in tail_monitors:
            self.check_monitor(len(self.operations), mon)

    # ---------- 操作执行 ----------
    def exec_op(self, idx, op):
        otype = op.get("type")
        target = float(op.get("depth", 0))
        cost = float(op.get("battery", 0))
        label = "操作#%d(%s 目标%gm 耗电%g)" % (idx, otype, target, cost)

        mission = None
        mission_name = op.get("mission")
        if mission_name is not None:
            mission = self.missions.get(mission_name)
            if mission is None:
                self.errors.append("[%s] 引用了不存在的任务 '%s'" % (label, mission_name))
            elif mission["status"] == "未开始":
                mission["status"] = "进行中"
                self.events.append("操作#%d: 任务 '%s' 开始执行" % (idx, mission_name))

        if otype == "下潜":
            self.do_dive(idx, label, target, cost, mission_name, mission)
        elif otype == "上浮":
            self.do_ascend(idx, label, target, cost)
        elif otype == "悬停":
            self.do_hover(idx, label, target, cost)
        else:
            self.errors.append("[%s] 未知操作类型 '%s'，操作被跳过" % (label, otype))

    def do_dive(self, idx, label, target, cost, mission_name, mission):
        if self.forced_ascend:
            self.errors.append("[%s] 强制上浮程序进行中，级联禁止下潜，操作被跳过" % label)
            return
        if target > self.max_depth:
            self.errors.append(
                "[%s] 下潜目标 %gm 超过深度上限 %gm，操作被拒绝并级联强制上浮"
                % (label, target, self.max_depth))
            self.forced_ascend = True
            return
        if target == self.depth:
            self.errors.append("[%s] 重复下潜到同深度 %gm" % (label, target))
            self.consume(cost, label)  # 操作实际发生，耗电照计，深度不变
            return
        if target < self.depth:
            self.errors.append(
                "[%s] 下潜目标 %gm 浅于当前深度 %gm，属于深度矛盾，按实际移动处理"
                % (label, target, self.depth))
        low_threshold = LOW_BATTERY_RATIO * self.battery_capacity
        if self.battery < low_threshold and target > self.depth:
            self.errors.append(
                "[%s] 深度与电池矛盾：剩余电量 %g 低于低电量阈值 %g（容量20%%），"
                "仍继续向 %gm 深处下潜" % (label, self.battery, low_threshold, target))
        self.depth = target
        self.consume(cost, label)
        self.events.append("操作#%d: 下潜至 %gm，剩余电量 %g" % (idx, self.depth, self.battery))
        if mission is not None and mission["status"] == "进行中" \
                and self.depth >= mission["target_depth"]:
            mission["status"] = "已完成"
            self.events.append("操作#%d: 任务 '%s' 到达目标深度 %gm，标记完成"
                               % (idx, mission_name, mission["target_depth"]))

    def do_ascend(self, idx, label, target, cost):
        if target > self.depth:
            self.errors.append(
                "[%s] 上浮目标 %gm 深于当前深度 %gm，属于深度矛盾" % (label, target, self.depth))
        self.depth = max(0.0, target)
        self.consume(cost, label)
        self.events.append("操作#%d: 上浮至 %gm，剩余电量 %g" % (idx, self.depth, self.battery))
        if self.depth == 0.0 and self.forced_ascend:
            self.forced_ascend = False
            self.events.append(
                "操作#%d: 已上浮至水面，强制上浮状态解除，各系统状态级联恢复正常" % idx)

    def do_hover(self, idx, label, target, cost):
        if target != self.depth:
            self.errors.append(
                "[%s] 悬停深度 %gm 与当前深度 %gm 不一致，以当前深度为准"
                % (label, target, self.depth))
        self.consume(cost, label)  # 悬停期间电池持续消耗，深度不变
        self.events.append("操作#%d: 在 %gm 处悬停，耗电 %g，剩余电量 %g"
                           % (idx, self.depth, cost, self.battery))

    # ---------- 公共判定 ----------
    def consume(self, cost, label):
        if cost <= 0:
            return
        self.battery -= cost
        if self.battery <= 0:
            self.battery = 0.0
            self.errors.append(
                "[%s] 电池耗尽（电量 <= 0），潜航器失去动力，级联强制上浮" % label)
            self.forced_ascend = True

    def check_mission_abort(self, idx):
        threshold = MISSION_ABORT_RATIO * self.battery_capacity
        for name, m in self.missions.items():
            if m["status"] == "进行中" and self.battery < threshold \
                    and self.depth < m["target_depth"]:
                m["status"] = "已放弃"
                self.forced_ascend = True
                self.errors.append(
                    "[操作#%d后] 任务 '%s' 目标 %gm 未达成，剩余电量 %g 低于放弃阈值 %g"
                    "（容量30%%），电池不足以完成任务，级联放弃并强制上浮"
                    % (idx, name, m["target_depth"], self.battery, threshold))

    def check_monitor(self, idx, mon):
        label = "监测(操作#%d后)" % idx
        depth = float(mon.get("depth", 0))
        battery = float(mon.get("battery", 0))
        status = mon.get("status")
        if status == "报警":
            self.errors.append("[%s] 监测状态为报警（深度 %gm，电量 %g）" % (label, depth, battery))
        elif status != "正常":
            self.errors.append("[%s] 监测状态 '%s' 非法，应为 正常/报警" % (label, status))
        if depth > self.max_depth:
            self.errors.append("[%s] 监测深度 %gm 超过深度上限 %gm" % (label, depth, self.max_depth))
        if battery <= 0:
            self.errors.append("[%s] 监测显示电池耗尽（电量 %g），级联强制上浮" % (label, battery))
            self.forced_ascend = True
        if abs(depth - self.depth) > MONITOR_DEPTH_TOLERANCE:
            self.errors.append(
                "[%s] 监测深度 %gm 与模拟状态 %gm 矛盾（容差 %gm）"
                % (label, depth, self.depth, MONITOR_DEPTH_TOLERANCE))
        if abs(battery - self.battery) > MONITOR_BATTERY_TOLERANCE_RATIO * self.battery_capacity:
            self.errors.append(
                "[%s] 监测电量 %g 与模拟状态 %g 矛盾（容差 %g）"
                % (label, battery, self.battery,
                   MONITOR_BATTERY_TOLERANCE_RATIO * self.battery_capacity))
        if 0 < depth and 0 < battery < LOW_BATTERY_RATIO * self.battery_capacity:
            self.errors.append(
                "[%s] 监测显示深水低电量（深度 %gm，电量 %g），存在深度与电池矛盾风险"
                % (label, depth, battery))

    # ---------- 报告 ----------
    def report(self):
        lines = []
        lines.append("=" * 60)
        lines.append("潜航状态")
        lines.append("=" * 60)
        lines.append("潜航器: %s（深度上限 %gm，电池容量 %g，压载仓 %d 个）"
                     % (self.name, self.max_depth, self.battery_capacity, self.ballast_tanks))
        pct = 100.0 * self.battery / self.battery_capacity if self.battery_capacity else 0.0
        lines.append("当前深度: %gm" % self.depth)
        lines.append("剩余电池: %g / %g（%.1f%%）" % (self.battery, self.battery_capacity, pct))
        lines.append("强制上浮程序: %s" % ("激活中" if self.forced_ascend else "未激活"))
        lines.append("任务状态:")
        if self.missions:
            for name, m in self.missions.items():
                lines.append("  - %s: %s（目标深度 %gm）" % (name, m["status"], m["target_depth"]))
        else:
            lines.append("  （无任务）")
        lines.append("")
        lines.append("事件日志（跨操作状态延续）:")
        for e in self.events:
            lines.append("  " + e)
        lines.append("")
        lines.append("=" * 60)
        lines.append("错误清单（共 %d 条）" % len(self.errors))
        lines.append("=" * 60)
        if self.errors:
            for i, e in enumerate(self.errors, 1):
                lines.append("%2d. %s" % (i, e))
        else:
            lines.append("无错误。")
        return "\n".join(lines)


def demo_input():
    """内置示例：覆盖全部规则，便于直接验证。"""
    return {
        "submersible": {"name": "蛟龙-X", "max_depth": 7000,
                        "battery_capacity": 1000, "ballast_tanks": 4},
        "missions": [
            {"name": "M1", "target_depth": 3000},
            {"name": "M2", "target_depth": 6500},
            {"name": "M3", "target_depth": 4000},
        ],
        "operations": [
            {"type": "下潜", "mission": "M1", "depth": 3000, "battery": 100},
            {"type": "下潜", "mission": "M1", "depth": 3000, "battery": 10},
            {"type": "悬停", "depth": 3000, "battery": 50},
            {"type": "下潜", "mission": "M2", "depth": 6500, "battery": 200},
            {"type": "下潜", "mission": "M2", "depth": 7500, "battery": 100},
            {"type": "下潜", "mission": "M1", "depth": 1000, "battery": 50},
            {"type": "上浮", "depth": 0, "battery": 30},
            {"type": "下潜", "mission": "M9", "depth": 500, "battery": 20},
            {"type": "上浮", "depth": 0, "battery": 20},
            {"type": "下潜", "mission": "M3", "depth": 1000, "battery": 400},
            {"type": "上浮", "depth": 0, "battery": 50},
            {"type": "下潜", "mission": "M1", "depth": 2000, "battery": 200},
        ],
        "monitoring": [
            {"after": 3, "depth": 3000, "battery": 840, "status": "正常"},
            {"after": 4, "depth": 6600, "battery": 640, "status": "报警"},
            {"after": 10, "depth": 1000, "battery": 100, "status": "正常"},
            {"depth": 2000, "battery": 0, "status": "报警"},
        ],
    }


def main(argv):
    if len(argv) > 1 and argv[1] == "-":
        data = json.load(sys.stdin)
    elif len(argv) > 1 and argv[1] != "--demo":
        with open(argv[1], encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = demo_input()
        print("未提供输入文件，运行内置示例。示例输入 JSON：")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print()
    sim = Simulator(data)
    sim.run()
    print(sim.report())


if __name__ == "__main__":
    main(sys.argv)
