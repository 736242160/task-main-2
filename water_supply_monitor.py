#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
供水管网分区计量(DMA) × 泵站联动 漏水检测工具（纯 Python 标准库，单文件）

用法:
    python3 water_supply_monitor.py [输入文件]      # 不给文件则读 stdin

输入行格式（# 开头为注释，空行忽略；各类流可任意交错，状态跨行延续）:
    ZONE    <分区名> <流入计量点> <流出计量点>
    PUMP    <泵站名> <服务分区> <增压档位(整数)>
    METER   <计量点> <流量 m3/h>
    MONITOR <分区>   <实测压力 MPa>
    EVENT   BURST   <分区>          # 爆管
    EVENT   RECOVER <分区>          # 恢复
    SNAPSHOT                        # 立即输出当前供水状态快照（可多次）

分区拓扑由计量点自动推出: 若甲区“流出计量点” == 乙区“流入计量点”，则乙是甲的下游。

判定与联动规则（阈值均可在下面常量处调整）:
1. 漏水: 分区 (流入-流出) > max(5 m3/h, 流入×10%) 判漏水并定位到该分区。
   理由: 参照 IWA 分区计量最小夜间流量法，扣除不可避免背景漏损后，10% 进水
   且不低于 5 m3/h 的绝对门槛可避免小流量分区被噪声误报。
   漏失比 r=差/流入(封顶0.7)，模型压力 = 0.30×(1-r) MPa（漏失越大压力越低）。
2. 计量跳变: 同一计量点相邻两次读数变化 > max(10 m3/h, 上次×30%) 即报告
   （水表故障/非法用水的典型征兆；30% 远大于正常用水波动）。
3. 爆管级联关断: 爆管分区立即关进水（有效流入置0）、服务该区的泵站停机(档位0)、
   该区压力置 0；下游按拓扑级联重算压力：第 d 级 = max(0, 0.30 - 0.06×d) MPa。
   （每经过一个分区按 0.06 MPa 沿程水头损失估算。）
4. 恢复级联增压: 恢复后服务泵站按原档位重新投运，爆管区压力 =
   min(0.30, 0.20 + 0.05×总档位) MPa；下游第 d 级获得增压
   0.05×总档位×0.5^(d-1) MPa（增压沿程衰减），封顶 0.30 MPa。
5. 引用完整性: ZONE/PUMP 重名、PUMP 引用不存在分区、METER 引用不存在计量点、
   MONITOR/EVENT 引用不存在分区，均进入错误报告（带物理行号）。
6. 重复爆管未恢复、无爆管却收到恢复，均报告；MONITOR 实测压力与模型压力偏差
   > 0.05 MPa 时给出警告。
"""

import sys

# ---- 阈值 / 联动参数（含理由，见模块 docstring） ----
P_NOMINAL = 0.30        # MPa，常规市政服务压力
LEAK_ABS = 5.0          # m3/h，漏失绝对门槛
LEAK_REL = 0.10         # 漏失相对门槛（占流入比）
JUMP_ABS = 10.0         # m3/h，计量跳变绝对门槛
JUMP_REL = 0.30         # 计量跳变相对门槛
CASCADE_DROP = 0.06     # MPa/级，关断后下游每级压降
PUMP_STEP = 0.05        # MPa/档，恢复时每档增压
WARN_PRESSURE_DEV = 0.05  # MPa，实测与模型压力偏差警告线


class Zone:
    __slots__ = ("name", "in_meter", "out_meter", "burst", "shut",
                 "leak", "pressure", "measured", "suppressed", "recovering")

    def __init__(self, name, in_meter, out_meter):
        self.name = name
        self.in_meter = in_meter
        self.out_meter = out_meter
        self.burst = False       # 爆管中
        self.shut = False        # 进水已关断
        self.leak = 0.0          # 漏失流量 m3/h（0 表示无漏水）
        self.pressure = P_NOMINAL
        self.measured = None     # MONITOR 流最近实测压力
        self.suppressed = False  # 被上游关断级联波及
        self.recovering = False  # 增压恢复尚未达到额定压力


class Pump:
    __slots__ = ("name", "zone", "level", "orig_level", "active")

    def __init__(self, name, zone, level):
        self.name = name
        self.zone = zone
        self.level = level
        self.orig_level = level
        self.active = True


def run(stream):
    zones = {}          # 分区名 -> Zone（保序）
    pumps = {}          # 泵站名 -> Pump
    meters = {}         # 计量点 -> 最近一次流量
    known_meters = set()  # 所有 ZONE 声明过的计量点
    errors = []         # (行号, 信息)

    def err(lineno, msg):
        errors.append((lineno, msg))

    def downstream_of(zone):
        return [z for z in zones.values()
                if z is not zone and z.in_meter == zone.out_meter]

    def evaluate_zone(zone, lineno):
        """用最新的进/出计量值重算某分区的漏失与压力。"""
        if zone.burst or zone.shut:
            return  # 爆管关断态由事件联动负责，计量不再改判
        if zone.in_meter not in meters or zone.out_meter not in meters:
            return
        fin, fout = meters[zone.in_meter], meters[zone.out_meter]
        diff = fin - fout
        threshold = max(LEAK_ABS, LEAK_REL * fin)
        if diff > threshold:
            zone.leak = round(diff, 2)
            ratio = min(0.7, diff / fin) if fin > 0 else 0.7
            zone.pressure = round(P_NOMINAL * (1 - ratio), 3)
        else:
            zone.leak = 0.0
            if not zone.suppressed:
                zone.pressure = P_NOMINAL
                zone.recovering = False

    def cascade_shut(root):
        """爆管关断后，对下游分区做压力级联重算（BFS 按拓扑深度）。"""
        frontier, depth, seen = [root], 0, {root.name}
        while frontier:
            depth += 1
            nxt = []
            for z in frontier:
                for d in downstream_of(z):
                    if d.name in seen:
                        continue
                    seen.add(d.name)
                    d.suppressed = True
                    d.recovering = False
                    d.pressure = round(max(0.0, P_NOMINAL - CASCADE_DROP * depth), 3)
                    nxt.append(d)
            frontier = nxt

    def cascade_recover(root, total_level):
        """泵站增压沿拓扑级联恢复下游压力。"""
        frontier, depth, seen = [root], 0, {root.name}
        while frontier:
            depth += 1
            nxt = []
            for z in frontier:
                for d in downstream_of(z):
                    if d.name in seen:
                        continue
                    seen.add(d.name)
                    gain = PUMP_STEP * total_level * (0.5 ** (depth - 1))
                    d.pressure = round(min(P_NOMINAL, d.pressure + gain), 3)
                    d.suppressed = False
                    d.recovering = d.pressure < P_NOMINAL - 1e-9
                    nxt.append(d)
            frontier = nxt

    def pumps_serving(zone_name):
        return [p for p in pumps.values() if p.zone == zone_name]

    outputs = []  # SNAPSHOT 等中途输出

    def state_of(z):
        if z.burst:
            return "爆管关断"
        if z.leak > 0:
            return "漏水"
        if z.suppressed:
            return "级联失压"
        if z.recovering:
            return "恢复中"
        return "正常"

    def render_status(title):
        lines = ["=" * 72, title, "=" * 72]
        lines.append(f"{'分区':<6}{'状态':<10}{'模型压力':>9}{'实测压力':>10}"
                     f"{'流入':>9}{'流出':>9}{'漏失':>9}  (压力MPa/流量m3/h)")
        lines.append("-" * 72)
        for z in zones.values():
            fin = meters.get(z.in_meter, float("nan"))
            fout = meters.get(z.out_meter, float("nan"))
            lines.append(
                f"{z.name:<6}{state_of(z):<10}{z.pressure:>9.3f}"
                f"{(f'{z.measured:.3f}' if z.measured is not None else '-'):>10}"
                f"{fin:>9.1f}{fout:>9.1f}{z.leak:>9.1f}")
        lines.append("-" * 72)
        lines.append(f"{'泵站':<8}{'服务分区':<10}{'当前档位':>9}   {'状态'}")
        for p in pumps.values():
            lines.append(f"{p.name:<8}{p.zone:<10}{p.level:>9}   "
                         f"{'运行' if p.active else '停机(爆管联动)'}")
        return "\n".join(lines)

    for lineno, raw in enumerate(stream, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        cmd = parts[0].upper()

        if cmd == "ZONE":
            if len(parts) != 4:
                err(lineno, "ZONE 格式应为: ZONE <分区名> <流入计量点> <流出计量点>")
                continue
            _, name, m_in, m_out = parts
            if name in zones:
                err(lineno, f"重复定义分区: {name}")
                continue
            zones[name] = Zone(name, m_in, m_out)
            known_meters.update((m_in, m_out))

        elif cmd == "PUMP":
            if len(parts) != 4:
                err(lineno, "PUMP 格式应为: PUMP <泵站名> <服务分区> <增压档位>")
                continue
            _, pname, zname, level_s = parts
            if pname in pumps:
                err(lineno, f"重复定义泵站: {pname}")
                continue
            if zname not in zones:
                err(lineno, f"泵站 {pname} 引用了不存在的分区: {zname}")
                continue
            try:
                level = int(level_s)
                if level < 0:
                    raise ValueError
            except ValueError:
                err(lineno, f"泵站 {pname} 增压档位非法: {level_s}（需非负整数）")
                continue
            pumps[pname] = Pump(pname, zname, level)

        elif cmd == "METER":
            if len(parts) != 3:
                err(lineno, "METER 格式应为: METER <计量点> <流量>")
                continue
            _, mp, flow_s = parts
            try:
                flow = float(flow_s)
            except ValueError:
                err(lineno, f"计量点 {mp} 流量非法: {flow_s}")
                continue
            if mp not in known_meters:
                err(lineno, f"计量点不存在: {mp}（未被任何分区声明为进/流出计量点）")
                continue
            if mp in meters:
                old = meters[mp]
                if abs(flow - old) > max(JUMP_ABS, JUMP_REL * abs(old)):
                    users = sorted({z.name for z in zones.values()
                                    if mp in (z.in_meter, z.out_meter)})
                    err(lineno, f"计量跳变: {mp} {old:g} -> {flow:g} m3/h "
                                f"(Δ={flow - old:+g})，关联分区: {','.join(users) or '无'}")
            meters[mp] = flow
            for z in zones.values():
                if mp in (z.in_meter, z.out_meter):
                    evaluate_zone(z, lineno)

        elif cmd == "MONITOR":
            if len(parts) != 3:
                err(lineno, "MONITOR 格式应为: MONITOR <分区> <压力>")
                continue
            _, zname, p_s = parts
            try:
                pressure = float(p_s)
            except ValueError:
                err(lineno, f"分区 {zname} 压力值非法: {p_s}")
                continue
            if zname not in zones:
                err(lineno, f"监测引用了不存在的分区: {zname}")
                continue
            z = zones[zname]
            z.measured = pressure
            if abs(pressure - z.pressure) > WARN_PRESSURE_DEV:
                err(lineno, f"压力偏差警告: 分区 {zname} 实测 {pressure:.3f} MPa "
                            f"与模型 {z.pressure:.3f} MPa 偏差超过 "
                            f"{WARN_PRESSURE_DEV:g} MPa")

        elif cmd == "EVENT":
            if len(parts) != 3 or parts[1].upper() not in ("BURST", "RECOVER"):
                err(lineno, "EVENT 格式应为: EVENT <BURST|RECOVER> <分区>")
                continue
            etype, zname = parts[1].upper(), parts[2]
            if zname not in zones:
                err(lineno, f"事件引用了不存在的分区: {zname}（{etype}）")
                continue
            z = zones[zname]

            if etype == "BURST":
                if z.burst:
                    err(lineno, f"重复爆管未恢复: 分区 {zname} 已处于爆管关断状态")
                    continue
                z.burst = True
                z.shut = True
                z.leak = 0.0
                z.suppressed = False
                z.pressure = 0.0
                for p in pumps_serving(zname):  # 服务泵站级联停机
                    p.active = False
                    p.level = 0
                cascade_shut(z)                # 下游压力级联重算

            else:  # RECOVER
                if not z.burst:
                    err(lineno, f"恢复事件无效: 分区 {zname} 当前未处于爆管状态")
                    continue
                z.burst = False
                z.shut = False
                for p in pumps_serving(zname):  # 泵站恢复原档位
                    p.active = True
                    p.level = p.orig_level
                total_level = sum(p.level for p in pumps_serving(zname))
                z.leak = 0.0  # 爆管修复后历史漏失清零，由后续计量重新判定
                z.pressure = round(min(P_NOMINAL, 0.20 + PUMP_STEP * total_level), 3)
                z.recovering = z.pressure < P_NOMINAL - 1e-9
                z.measured = None
                cascade_recover(z, total_level)

        elif cmd == "SNAPSHOT":
            if len(parts) != 1:
                err(lineno, "SNAPSHOT 不接受参数")
                continue
            outputs.append(render_status(f"状态快照 @ 输入行 {lineno}"))

        else:
            err(lineno, f"无法识别的记录类型: {cmd}")

    # ---------- 最终输出 ----------
    outputs.append(render_status("最终供水状态"))
    outputs += ["=" * 72,
                f"错误报告（共 {len(errors)} 条，按输入行号定位）",
                "=" * 72]
    if errors:
        outputs += [f"行 {ln:<4} {msg}" for ln, msg in errors]
    else:
        outputs.append("无")
    return "\n".join(outputs)


def main(argv):
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            print(run(f))
    else:
        print(run(sys.stdin))


if __name__ == "__main__":
    main(sys.argv)
