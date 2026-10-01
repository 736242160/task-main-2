#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline_guard.py — 油气管道压力/泄漏监测与级联关断工具（纯 Python 标准库，单文件）

输入格式（文本行，字段以空白分隔，# 之后为注释，空行忽略）：
    段   <名称> <上游段|-> <下游段|->     定义管段，- 表示无
    阀   <名称> <所在段> <开|关>          定义阀门及初始状态
    输送 <段名> <流量>                    设置该段当前输送流量
    监测 <段名> <压力MPa>                 上报该段压力监测值
    事件 <泄漏|恢复> <段名>               泄漏/恢复事件，状态跨事件延续

判定规则（阈值为本文件顶部常量，可自定；理由如下）：
    R1 压力上限 PRESSURE_HIGH = 10.0 MPa：长输油气管道常见设计压力（MAOP）
       量级，超压有爆管风险，任何工况下超限都报警。
    R2 压力下限 PRESSURE_LOW  = 2.0 MPa ：正常输送压力远高于此值；但停输
       （流量为 0）管段低压属正常工况，故低压报警仅在该段流量 > 0 时触发，
       避免关断停输后的误报。
    R3 流量/压力矛盾：流量 >= FLOW_HIGH(100) 且压力 <= PRESSURE_LOW，
       高流量却测得低压力，物理上矛盾，疑似泄漏或仪表故障，报警。
    R4 泄漏事件：级联关断该段及全部上游段上的阀门（截断来气方向）；
       被关阀的段及泄漏段的全部下游段流量级联归零（上游被切断，下游必然
       断流）；归零前的流量做快照，供恢复时还原。
    R5 恢复事件：级联开启该段及全部上游段阀门，并按快照恢复被归零的流量。
    R6 错误报告：重复泄漏（未恢复再次泄漏）、恢复未泄漏的段、监测/输送/
       事件/阀门引用不存在的段、管段上下游引用不存在的段、重复定义等。

用法：
    python3 pipeline_guard.py 输入文件      # 处理指定输入文件
    python3 pipeline_guard.py               # 从标准输入读取
    python3 pipeline_guard.py --demo        # 运行内置自测样例
"""

import sys

PRESSURE_HIGH = 10.0   # MPa，压力上限（R1）
PRESSURE_LOW = 2.0     # MPa，压力下限（R2）
FLOW_HIGH = 100.0      # 流量高阈值，配合 R3 判定流量/压力矛盾

SAMPLE_INPUT = """\
# ===== 内置自测样例 =====
# --- 管段定义：S1 -> S2 -> S3 -> S4 一条干线 ---
段 S1 - S2
段 S2 S1 S3
段 S3 S2 S4
段 S4 S3 -
# --- 阀门定义 ---
阀 V1 S1 开
阀 V2 S2 开
阀 V3 S3 开
阀 V4 S4 开
# --- 输送流 ---
输送 S1 120
输送 S2 120
输送 S3 120
输送 S4 120
# --- 监测流 ---
监测 S1 6.5
监测 S2 11.5
监测 S9 5.0
监测 S3 1.5
# --- 事件流 ---
事件 泄漏 S2
监测 S4 0.5
事件 泄漏 S2
事件 恢复 S2
事件 恢复 S2
监测 S2 6.8
"""


def fmt(num):
    return "%g" % num


class PipelineGuard:
    def __init__(self):
        self.segments = {}        # 段名 -> {"up": 上游段名|None, "down": 下游段名|None}
        self.valves = {}          # 阀名 -> {"seg": 段名, "open": bool}
        self.flow = {}            # 段名 -> 当前流量
        self.leaking = set()      # 泄漏中（未恢复）的段名
        self.leak_snapshot = {}   # 泄漏段名 -> {被归零段名: 原流量}
        self.logs = []            # (行号, 级别, 消息)，级别: 动作/正常/报警/错误

    # ---------- 记录 ----------
    def log(self, lineno, level, msg):
        self.logs.append((lineno, level, msg))

    def error(self, lineno, msg):
        self.log(lineno, "错误", msg)

    def alarm(self, lineno, msg):
        self.log(lineno, "报警", msg)

    # ---------- 拓扑遍历 ----------
    def upstream_chain(self, seg):
        """从 seg（含）向上游遍历，遇缺失段或环即停。"""
        chain, seen, cur = [], set(), seg
        while cur and cur not in seen:
            seen.add(cur)
            if cur not in self.segments:
                break
            chain.append(cur)
            cur = self.segments[cur]["up"]
        return chain

    def downstream_chain(self, seg):
        """从 seg（含）向下游遍历，遇缺失段或环即停。"""
        chain, seen, cur = [], set(), seg
        while cur and cur not in seen:
            seen.add(cur)
            if cur not in self.segments:
                break
            chain.append(cur)
            cur = self.segments[cur]["down"]
        return chain

    def valves_on(self, seg):
        return [name for name, v in self.valves.items() if v["seg"] == seg]

    # ---------- 各类输入行处理 ----------
    def def_segment(self, lineno, name, up, down):
        if name in self.segments:
            self.error(lineno, "段 %s 重复定义，忽略本次定义" % name)
            return
        self.segments[name] = {
            "up": None if up == "-" else up,
            "down": None if down == "-" else down,
        }
        self.flow.setdefault(name, 0.0)
        self.log(lineno, "动作", "定义段 %s（上游=%s，下游=%s）" % (name, up, down))

    def def_valve(self, lineno, name, seg, state):
        if name in self.valves:
            self.error(lineno, "阀 %s 重复定义，忽略本次定义" % name)
            return
        if seg not in self.segments:
            self.error(lineno, "阀 %s 所在段 %s 不存在，忽略该阀" % (name, seg))
            return
        if state not in ("开", "关"):
            self.error(lineno, "阀 %s 状态 %r 非法（应为 开/关），忽略该阀" % (name, state))
            return
        self.valves[name] = {"seg": seg, "open": state == "开"}
        self.log(lineno, "动作", "定义阀 %s（段 %s，初始 %s）" % (name, seg, state))

    def set_flow(self, lineno, seg, value):
        if seg not in self.segments:
            self.error(lineno, "输送引用不存在的段 %s，忽略" % seg)
            return
        self.flow[seg] = value
        self.log(lineno, "动作", "段 %s 输送流量设为 %s" % (seg, fmt(value)))

    def monitor(self, lineno, seg, pressure):
        if seg not in self.segments:
            self.error(lineno, "监测引用不存在的段 %s" % seg)
            return
        flow = self.flow.get(seg, 0.0)
        problems = []
        if pressure > PRESSURE_HIGH:
            problems.append("压力 %s MPa 超过上限 %s MPa（R1）"
                            % (fmt(pressure), fmt(PRESSURE_HIGH)))
        elif pressure < PRESSURE_LOW and flow > 0:
            problems.append("输送中（流量 %s）压力 %s MPa 低于下限 %s MPa（R2）"
                            % (fmt(flow), fmt(pressure), fmt(PRESSURE_LOW)))
        if flow >= FLOW_HIGH and pressure <= PRESSURE_LOW:
            problems.append("高流量（%s）低压力（%s MPa）矛盾，疑似泄漏或仪表故障（R3）"
                            % (fmt(flow), fmt(pressure)))
        if problems:
            for p in problems:
                self.alarm(lineno, "段 %s：%s" % (seg, p))
        else:
            note = "（停输段，低压不报警）" if pressure < PRESSURE_LOW else ""
            self.log(lineno, "正常", "段 %s 压力 %s MPa，流量 %s，正常%s"
                     % (seg, fmt(pressure), fmt(flow), note))

    def do_leak(self, lineno, seg):
        if seg not in self.segments:
            self.error(lineno, "泄漏事件引用不存在的段 %s" % seg)
            return
        if seg in self.leaking:
            self.error(lineno, "段 %s 重复泄漏：此前泄漏尚未恢复，忽略本次事件" % seg)
            return
        self.leaking.add(seg)
        self.alarm(lineno, "段 %s 发生泄漏，启动级联关断" % seg)
        # R4a：级联关断该段及全部上游段的阀门
        shut_segs = self.upstream_chain(seg)
        closed = []
        for s in shut_segs:
            for vname in self.valves_on(s):
                v = self.valves[vname]
                if v["open"]:
                    v["open"] = False
                    closed.append(vname)
        self.log(lineno, "动作", "级联关断上游阀门：%s（涉及段 %s）"
                 % ("、".join(closed) if closed else "无（均已关或不存在）",
                    "、".join(shut_segs)))
        # R4b：被关阀的段及泄漏段的全部下游段，流量级联归零（先快照）
        zero_roots = set(self.valves[v]["seg"] for v in closed)
        zero_roots.add(seg)
        snapshot = {}
        for root in zero_roots:
            for s in self.downstream_chain(root):
                if s not in snapshot:
                    snapshot[s] = self.flow.get(s, 0.0)
                self.flow[s] = 0.0
        self.leak_snapshot[seg] = snapshot
        self.log(lineno, "动作", "流量级联归零：%s" % "、".join(snapshot))

    def do_recover(self, lineno, seg):
        if seg not in self.segments:
            self.error(lineno, "恢复事件引用不存在的段 %s" % seg)
            return
        if seg not in self.leaking:
            self.error(lineno, "段 %s 未处于泄漏状态，恢复事件无效" % seg)
            return
        self.leaking.discard(seg)
        # R5a：级联开启该段及全部上游段的阀门
        opened = []
        for s in self.upstream_chain(seg):
            for vname in self.valves_on(s):
                v = self.valves[vname]
                if not v["open"]:
                    v["open"] = True
                    opened.append(vname)
        self.log(lineno, "动作", "级联开启上游阀门：%s"
                 % ("、".join(opened) if opened else "无（均已开或不存在）"))
        # R5b：按泄漏时快照恢复被归零的流量
        snapshot = self.leak_snapshot.pop(seg, {})
        restored = []
        for s, old_flow in snapshot.items():
            if self.flow.get(s, 0.0) == 0.0 and old_flow != 0.0:
                self.flow[s] = old_flow
                restored.append("%s=%s" % (s, fmt(old_flow)))
        self.log(lineno, "动作", "段 %s 泄漏已恢复，还原流量：%s"
                 % (seg, "、".join(restored) if restored else "无"))

    # ---------- 主解析循环 ----------
    def process(self, text):
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            head, args = parts[0], parts[1:]
            if head == "段" and len(args) == 3:
                self.def_segment(lineno, *args)
            elif head == "阀" and len(args) == 3:
                self.def_valve(lineno, *args)
            elif head == "输送" and len(args) == 2:
                value = self.parse_number(lineno, args[1], "流量")
                if value is not None:
                    self.set_flow(lineno, args[0], value)
            elif head == "监测" and len(args) == 2:
                value = self.parse_number(lineno, args[1], "压力")
                if value is not None:
                    self.monitor(lineno, args[0], value)
            elif head == "事件" and len(args) == 2:
                if args[0] == "泄漏":
                    self.do_leak(lineno, args[1])
                elif args[0] == "恢复":
                    self.do_recover(lineno, args[1])
                else:
                    self.error(lineno, "未知事件类型 %r（应为 泄漏/恢复）" % args[0])
            else:
                self.error(lineno, "无法解析的行：%s" % raw.strip())
        self.validate_topology()

    def parse_number(self, lineno, text, what):
        try:
            return float(text)
        except ValueError:
            self.error(lineno, "%s数值 %r 非法，忽略该行" % (what, text))
            return None

    def validate_topology(self):
        for name, seg in self.segments.items():
            for key, label in (("up", "上游"), ("down", "下游")):
                ref = seg[key]
                if ref is not None and ref not in self.segments:
                    self.error(0, "段 %s 的%s段 %s 不存在" % (name, label, ref))

    # ---------- 输出 ----------
    def render(self):
        out = []
        out.append("===== 判定规则 =====")
        out.append("R1 压力上限 %s MPa：任何工况超限即报警（防超压爆管）" % fmt(PRESSURE_HIGH))
        out.append("R2 压力下限 %s MPa：仅输送中（流量>0）报警，停输段低压属正常" % fmt(PRESSURE_LOW))
        out.append("R3 流量>=%s 且压力<=%s MPa：高流量低压力矛盾，疑似泄漏/仪表故障"
                   % (fmt(FLOW_HIGH), fmt(PRESSURE_LOW)))
        out.append("R4 泄漏：级联关断该段及上游段阀门，被关阀段及下游段流量归零")
        out.append("R5 恢复：级联开启该段及上游段阀门，按快照还原流量")
        out.append("")
        out.append("===== 处理日志 =====")
        for lineno, level, msg in self.logs:
            where = "行%-3d" % lineno if lineno else "终检 "
            out.append("[%s][%s] %s" % (where, level, msg))
        out.append("")
        out.append("===== 管道最终状态 =====")
        for name in self.segments:
            flow = self.flow.get(name, 0.0)
            if name in self.leaking:
                status = "泄漏中"
            elif flow > 0:
                status = "输送中"
            else:
                status = "停输"
            valves = self.valves_on(name)
            vdesc = "，阀门：" + "、".join(
                "%s=%s" % (v, "开" if self.valves[v]["open"] else "关") for v in valves
            ) if valves else ""
            out.append("段 %-4s 流量=%-6s 状态=%s%s" % (name, fmt(flow), status, vdesc))
        out.append("")
        out.append("===== 错误与报警清单 =====")
        problems = [(n, lv, m) for n, lv, m in self.logs if lv in ("错误", "报警")]
        if problems:
            for i, (lineno, level, msg) in enumerate(problems, 1):
                where = "行%d" % lineno if lineno else "终检"
                out.append("%2d. [%s][%s] %s" % (i, where, level, msg))
        else:
            out.append("（无）")
        n_err = sum(1 for _, lv, _ in problems if lv == "错误")
        n_alm = sum(1 for _, lv, _ in problems if lv == "报警")
        out.append("")
        out.append("汇总：错误 %d 条，报警 %d 条" % (n_err, n_alm))
        return "\n".join(out)


def main(argv):
    if "--demo" in argv:
        text = SAMPLE_INPUT
        print("（运行内置自测样例）\n")
    elif len(argv) > 1:
        with open(argv[1], "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    guard = PipelineGuard()
    guard.process(text)
    print(guard.render())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
