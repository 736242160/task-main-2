#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
供水管网分区计量漏损监测与泵站联动工具（纯 Python 标准库，单文件）

用法:
    python3 water_leak_monitor.py [输入文件]     # 缺省从标准输入读取
    python3 water_leak_monitor.py --demo         # 运行内置示例（含各类错误定位）

输入格式（逐行，# 开头为注释，字段以空白分隔，五类流可任意交错，状态跨流延续）:
    ZONE    <分区名> <进水计量点> <出水计量点>
    PUMP    <泵站名> <服务分区> <增压档位:正整数>
    METER   <计量点> <流量:m³/h>
    MONITOR <分区名> <压力:MPa>
    EVENT   <爆管|恢复> <分区名>

输出:
    1) 供水状态：每个分区的进/出流量、压力、运行状态；每个泵站的服务分区与增压能力
    2) 错误与告警报告：每条均带输入行号，可定位到具体输入行

自定规则与阈值（理由）:
    * 漏水判定: 进水-出水 > max(5 m³/h, 进水的 8%)。
      理由: 电磁/超声流量计自身误差约 2%~5%，取 8% 相对裕量避免误报；
      小流量分区相对误差放大，故设 5 m³/h 绝对下限（对应 DMA 夜间最小流量分析经验值）。
    * 漏水压降: 压力 *= (1 - 0.6 * 漏损率)，漏损率=(进-出)/进。
      理由: 漏点泄流导致沿程水头损失增大，压降与漏损率近似线性，0.6 为经验系数。
    * 计量跳变: 同一计量点相邻读数 |Δ| > max(20 m³/h, 前值的 30%)。
      理由: 正常需水波动一般在 ±30% 以内，超出则疑似表计故障或突发工况。
    * 爆管级联关断: 关闭本区进水阀（进水置 0、压力归零、标记关断）；
      下游分区（其进水计量点 == 本区出水计量点）逐级失去来水，
      每级联一级压力按 40% 残余衰减（管道余压与局部蓄水）。
    * 恢复联动: 恢复事件后重新开阀，泵站按档位增压（每档 +0.04 MPa），
      由恢复分区向下游级联恢复压力，每级水头损失 10%（压力 *= 0.9^级数）。
"""

import argparse
import sys
from dataclasses import dataclass, field

# ---------------- 阈值与模型参数（理由见模块 docstring） ----------------
LEAK_ABS_THRESHOLD = 5.0      # m³/h，漏水绝对阈值
LEAK_REL_THRESHOLD = 0.08     # 漏水相对阈值（占进水比例）
LEAK_DROP_COEF = 0.6          # 漏水压降系数
JUMP_ABS_THRESHOLD = 20.0     # m³/h，计量跳变绝对阈值
JUMP_REL_THRESHOLD = 0.30     # 计量跳变相对阈值
BASE_PRESSURE = 0.30          # MPa，分区基准压力（未被监测流修正前）
CASCADE_DECAY = 0.4           # 爆管关断后下游每级残余压力系数
PUMP_BOOST_PER_LEVEL = 0.04   # MPa/档，泵站每档增压
RESTORE_DECAY = 0.9           # 恢复时向下游每级水头保留系数


@dataclass
class Zone:
    name: str
    in_meter: str
    out_meter: str
    in_flow: float = None
    out_flow: float = None
    pressure: float = BASE_PRESSURE
    base_pressure: float = BASE_PRESSURE
    leaking: bool = False
    burst: bool = False
    shutoff: bool = False
    affected: bool = False     # 因上游关断而级联受压

    def status(self):
        flags = []
        if self.burst:
            flags.append("爆管")
        if self.shutoff:
            flags.append("已关断进水")
        if self.leaking and not self.burst:
            flags.append("漏水")
        if self.affected:
            flags.append("下游级联受压")
        return "/".join(flags) if flags else "正常"


@dataclass
class Pump:
    name: str
    zone: str
    level: int


class Network:
    def __init__(self):
        self.zones = {}          # 分区名 -> Zone
        self.pumps = {}          # 泵站名 -> Pump
        self.known_meters = set()
        self.meter_last = {}     # 计量点 -> 上一次流量（跨流延续）
        self.report = []         # (行号, 级别, 消息)

    # ---- 报告 ----
    def emit(self, line_no, level, msg):
        self.report.append((line_no, level, msg))

    # ---- 拓扑：B 的进水计量点 == A 的出水计量点 => B 是 A 的下游 ----
    def downstreams(self, zone):
        return [z for z in self.zones.values()
                if z is not zone and z.in_meter == zone.out_meter]

    def cascade_depths(self, root):
        """从 root 出发 BFS，返回 {分区名: 级联深度}（root 深度为 0）。"""
        depths = {root.name: 0}
        queue = [root]
        while queue:
            cur = queue.pop(0)
            for d in self.downstreams(cur):
                if d.name not in depths:
                    depths[d.name] = depths[cur.name] + 1
                    queue.append(d)
        return depths

    def boost_of(self, zone_name):
        return sum(p.level for p in self.pumps.values() if p.zone == zone_name)

    # ---- 定义流 ----
    def add_zone(self, line_no, name, in_m, out_m):
        if name in self.zones:
            self.emit(line_no, "错误", f"分区重复定义: {name}")
            return
        z = Zone(name, in_m, out_m)
        self.zones[name] = z
        self.known_meters.update([in_m, out_m])

    def add_pump(self, line_no, name, zone_name, level):
        if zone_name not in self.zones:
            self.emit(line_no, "错误",
                      f"泵站 {name} 引用了不存在的分区: {zone_name}")
            return
        if name in self.pumps:
            self.emit(line_no, "错误", f"泵站重复定义: {name}")
            return
        self.pumps[name] = Pump(name, zone_name, level)

    # ---- 计量流 ----
    def add_meter(self, line_no, meter, flow):
        if meter not in self.known_meters:
            self.emit(line_no, "错误", f"计量记录引用了不存在的计量点: {meter}")
            return
        prev = self.meter_last.get(meter)
        if prev is not None:
            jump = abs(flow - prev)
            if jump > max(JUMP_ABS_THRESHOLD, JUMP_REL_THRESHOLD * abs(prev)):
                users = [z.name for z in self.zones.values()
                         if meter in (z.in_meter, z.out_meter)]
                self.emit(line_no, "告警",
                          f"计量点 {meter} 流量跳变: {prev:.1f} -> {flow:.1f} m³/h"
                          f"（|Δ|={jump:.1f}，关联分区: {', '.join(users)}）")
        self.meter_last[meter] = flow
        for z in self.zones.values():
            if z.in_meter == meter:
                z.in_flow = flow
            if z.out_meter == meter:
                z.out_flow = flow
        for z in self.zones.values():
            if meter in (z.in_meter, z.out_meter):
                self.check_leak(line_no, z)

    def check_leak(self, line_no, z):
        if z.in_flow is None or z.out_flow is None or z.shutoff:
            return
        diff = z.in_flow - z.out_flow
        threshold = max(LEAK_ABS_THRESHOLD, LEAK_REL_THRESHOLD * z.in_flow)
        if diff > threshold:
            ratio = diff / z.in_flow if z.in_flow > 0 else 1.0
            z.pressure = max(0.0, z.pressure * (1 - LEAK_DROP_COEF * ratio))
            if not z.leaking:
                z.leaking = True
                self.emit(line_no, "告警",
                          f"分区 {z.name} 检出漏水: 进水 {z.in_flow:.1f} - "
                          f"出水 {z.out_flow:.1f} = 差 {diff:.1f} m³/h "
                          f"> 阈值 {threshold:.1f}，漏损率 {ratio:.1%}，"
                          f"压力降至 {z.pressure:.3f} MPa")
        else:
            z.leaking = False

    # ---- 监测流 ----
    def add_monitor(self, line_no, zone_name, pressure):
        z = self.zones.get(zone_name)
        if z is None:
            self.emit(line_no, "错误", f"监测记录引用了不存在的分区: {zone_name}")
            return
        z.pressure = pressure
        if not (z.leaking or z.burst or z.affected):
            z.base_pressure = pressure   # 正常工况下用实测值修正基准压力

    # ---- 事件流 ----
    def add_event(self, line_no, kind, zone_name):
        z = self.zones.get(zone_name)
        if z is None:
            self.emit(line_no, "错误", f"事件引用了不存在的分区: {zone_name}")
            return
        if kind == "爆管":
            self.apply_burst(line_no, z)
        elif kind == "恢复":
            self.apply_recovery(line_no, z)
        else:
            self.emit(line_no, "错误", f"未知事件类型: {kind}（仅支持 爆管/恢复）")

    def apply_burst(self, line_no, z):
        if z.burst:
            self.emit(line_no, "错误",
                      f"分区 {z.name} 重复爆管：此前爆管尚未恢复")
            return
        z.burst = z.shutoff = z.leaking = True
        z.in_flow = 0.0          # 级联关断：关闭本区进水阀
        z.pressure = 0.0
        depths = self.cascade_depths(z)
        for name, depth in depths.items():
            if depth == 0:
                continue
            d = self.zones[name]
            d.pressure *= CASCADE_DECAY ** depth
            d.affected = True
        downstream = [n for n, d in depths.items() if d > 0]
        msg = (f"分区 {z.name} 爆管：已级联关断进水阀，本区压力归零")
        if downstream:
            msg += (f"；下游 {', '.join(downstream)} 失去来水，"
                    f"压力按 {CASCADE_DECAY:.0%}/级 级联重算")
        self.emit(line_no, "告警", msg)

    def apply_recovery(self, line_no, z):
        if not z.burst:
            self.emit(line_no, "错误",
                      f"分区 {z.name} 收到恢复事件，但无未恢复的爆管记录")
            return
        z.burst = z.shutoff = z.leaking = False
        depths = self.cascade_depths(z)
        for name, depth in depths.items():
            d = self.zones[name]
            d.affected = False
            boost = PUMP_BOOST_PER_LEVEL * self.boost_of(name)
            d.pressure = (d.base_pressure + boost) * (RESTORE_DECAY ** depth)
        self.emit(line_no, "告警",
                  f"分区 {z.name} 恢复：进水阀重新开启，泵站增压联动，"
                  f"压力级联恢复（每档 +{PUMP_BOOST_PER_LEVEL} MPa，"
                  f"下游每级保留 {RESTORE_DECAY:.0%}）")

    # ---- 输出 ----
    def render(self):
        out = ["======== 供水状态 ========", "【分区】"]
        for z in self.zones.values():
            inf = f"{z.in_flow:.1f}" if z.in_flow is not None else "--"
            outf = f"{z.out_flow:.1f}" if z.out_flow is not None else "--"
            out.append(f"  {z.name}: 进水={inf} m³/h 出水={outf} m³/h "
                       f"压力={z.pressure:.3f} MPa 状态={z.status()}")
        out.append("【泵站】")
        for p in self.pumps.values():
            out.append(f"  {p.name}: 服务分区={p.zone} 档位={p.level} "
                       f"增压能力={p.level * PUMP_BOOST_PER_LEVEL:.2f} MPa")
        out.append("======== 错误与告警报告 ========")
        if not self.report:
            out.append("  （无）")
        for line_no, level, msg in self.report:
            out.append(f"  [行 {line_no:>3}] {level}: {msg}")
        return "\n".join(out)


def process(net, lines):
    for line_no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        kind, args = parts[0], parts[1:]
        try:
            if kind == "ZONE" and len(args) == 3:
                net.add_zone(line_no, *args)
            elif kind == "PUMP" and len(args) == 3:
                net.add_pump(line_no, args[0], args[1], int(args[2]))
            elif kind == "METER" and len(args) == 2:
                net.add_meter(line_no, args[0], float(args[1]))
            elif kind == "MONITOR" and len(args) == 2:
                net.add_monitor(line_no, args[0], float(args[1]))
            elif kind == "EVENT" and len(args) == 2:
                net.add_event(line_no, args[0], args[1])
            elif kind in ("ZONE", "PUMP", "METER", "MONITOR", "EVENT"):
                net.emit(line_no, "错误", f"{kind} 记录字段个数不正确: {line}")
            else:
                net.emit(line_no, "错误", f"未知记录类型: {kind}")
        except ValueError:
            net.emit(line_no, "错误", f"数值解析失败: {line}")


DEMO_INPUT = """\
# ===== 分区定义：名称 进水计量点 出水计量点 =====
ZONE 东区 IN_E OUT_E
ZONE 西区 IN_W OUT_W
ZONE 南区 OUT_E OUT_S
# 南区进水计量点=东区出水计量点 => 南区是东区下游
# ===== 泵站定义：名称 服务分区 增压档位 =====
PUMP 泵站甲 东区 2
PUMP 泵站乙 南区 1
PUMP 泵站丙 幽灵区 1
# ===== 计量流 =====
METER IN_E 120.0
METER OUT_E 118.0
METER IN_W 80.0
METER OUT_W 79.0
METER IN_E 121.0
METER OUT_E 118.5
METER IN_E 200.0
METER OUT_E 150.0
# ===== 监测流 =====
MONITOR 东区 0.28
MONITOR 幽灵区 0.30
METER NO_SUCH 10.0
# ===== 事件流 =====
EVENT 爆管 东区
EVENT 爆管 东区
EVENT 爆管 不存在区
EVENT 恢复 东区
EVENT 恢复 西区
# ===== 恢复后计量回归正常 =====
METER IN_E 122.0
METER OUT_E 120.0
"""


def main():
    ap = argparse.ArgumentParser(description="供水管网漏损监测与泵站联动工具")
    ap.add_argument("input", nargs="?", help="输入文件（缺省读标准输入）")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    args = ap.parse_args()

    if args.demo:
        print("-------- 示例输入 --------")
        print(DEMO_INPUT)
        lines = DEMO_INPUT.splitlines()
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()

    net = Network()
    process(net, lines)
    print(net.render())


if __name__ == "__main__":
    main()
