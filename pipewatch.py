#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipewatch.py —— 油气管道压力异常监测与级联关断模拟工具
特点：纯 Python 标准库、单文件、可直接运行。

输入（UTF-8 文本，每行一条记录，# 开头为注释，字段用空白分隔）：
    段   <名称> <上游段|-> <下游段|->
    阀   <名称> <所在段> <开|关>
    输送 <段> <流量>
    监测 <段> <压力，单位 MPa>
    事件 <泄漏|恢复> <段>

自定规则及理由：
  1. 正常压力区间 [2.0, 8.0] MPa：
     - 低于下限：疑似泄漏泄压或供压不足；
     - 高于上限：管道承压超限，有爆管/超压事故风险。
  2. 流量-压力矛盾阈值：有效流量 >= 80 且 监测压力 <= 2.5 MPa。
     高流量输送需要足够压差驱动，二者同时出现物理上不成立，
     判定为仪表故障或存在未记录的泄漏。
  3. 泄漏级联关断：关闭"泄漏段及其全部上游段"上的所有阀门，
     以切断泄漏点来流。
  4. 关断后流量级联归零：某段被关断后，其全部下游段（沿下游链）
     有效流量逐级归零——上游没有来流，下游无法维持输送。
  5. 恢复级联开启：解除该泄漏事件造成的关断，被联带关闭的阀门
     重新开启、下游流量恢复；定义时本来就是"关"的阀门保持关闭。
  6. 同一泄漏未恢复又报泄漏 => "重复泄漏未恢复"；
     对未泄漏段发恢复 => "无对应泄漏的恢复"。
  7. 状态跨事件延续：active_leaks 是累积集合，每处理一个事件
     都基于当前全部在泄漏段重新计算阀门状态与有效流量。

用法：
    python3 pipewatch.py <输入文件>
    cat 输入文件 | python3 pipewatch.py
"""

import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

# ---- 规则阈值（可按实际管道工艺调整） ----
P_MIN = 2.0          # 正常压力下限 MPa
P_MAX = 8.0          # 正常压力上限 MPa
FLOW_HIGH = 80.0     # "高流量"阈值
P_CONTRA = 2.5       # 与高流量矛盾的"低压力"阈值


def norm(name: str) -> Optional[str]:
    return None if name in ("-", "无", "none", "NONE") else name


@dataclass
class Segment:
    name: str
    upstream: Optional[str]
    downstream: Optional[str]


@dataclass
class Valve:
    name: str
    segment: str
    base_open: bool   # 定义文件中声明的初始状态，恢复时不会强行打开本就关闭的阀


class Pipeline:
    def __init__(self) -> None:
        self.segments: Dict[str, Segment] = {}
        self.valves: Dict[str, Valve] = {}
        self.valves_by_seg: Dict[str, List[str]] = {}
        self.base_flow: Dict[str, float] = {}
        self.pressure: Dict[str, float] = {}
        self.active_leaks: Set[str] = set()
        self.errors: List[str] = []
        self._reported: Set[str] = set()

    # ---------- 错误收集（去重，避免跨事件快照重复报同一条） ----------
    def error(self, msg: str) -> None:
        if msg not in self._reported:
            self._reported.add(msg)
            self.errors.append(msg)

    # ---------- 解析 ----------
    def feed(self, lineno: int, parts: List[str]) -> None:
        kw = parts[0]
        try:
            if kw == "段":
                if len(parts) != 4:
                    self.error(f"第{lineno}行: 段定义格式错误（应为: 段 名称 上游 下游）: {' '.join(parts)}")
                    return
                name, up, down = parts[1], norm(parts[2]), norm(parts[3])
                if name in self.segments:
                    self.error(f"第{lineno}行: 管段重复定义: {name}")
                    return
                self.segments[name] = Segment(name, up, down)

            elif kw == "阀":
                if len(parts) != 4 or parts[3] not in ("开", "关"):
                    self.error(f"第{lineno}行: 阀门定义格式错误（应为: 阀 名称 段 开|关）: {' '.join(parts)}")
                    return
                vname, seg, state = parts[1], parts[2], parts[3]
                if vname in self.valves:
                    self.error(f"第{lineno}行: 阀门重复定义: {vname}")
                    return
                self.valves[vname] = Valve(vname, seg, state == "开")
                self.valves_by_seg.setdefault(seg, []).append(vname)

            elif kw in ("输送", "监测"):
                if len(parts) != 3:
                    self.error(f"第{lineno}行: {kw}记录格式错误: {' '.join(parts)}")
                    return
                seg, raw_val = parts[1], parts[2]
                try:
                    val = float(raw_val)
                except ValueError:
                    self.error(f"第{lineno}行: {kw}数值非法: {raw_val}")
                    return
                if val < 0:
                    self.error(f"第{lineno}行: {kw}数值不能为负: {val}")
                    return
                if kw == "输送":
                    if seg in self.base_flow:
                        self.error(f"第{lineno}行: 段 {seg} 输送流量重复定义")
                        return
                    self.base_flow[seg] = val
                else:
                    if seg in self.pressure:
                        self.error(f"第{lineno}行: 段 {seg} 监测压力重复定义")
                        return
                    self.pressure[seg] = val

            else:
                self.error(f"第{lineno}行: 无法识别的记录类型: {' '.join(parts)}")
        except IndexError:
            self.error(f"第{lineno}行: 字段缺失: {' '.join(parts)}")

    # ---------- 引用完整性校验 ----------
    def validate(self) -> None:
        for seg in self.segments.values():
            if seg.upstream and seg.upstream not in self.segments:
                self.error(f"管段 {seg.name} 的上游段不存在: {seg.upstream}")
            if seg.downstream and seg.downstream not in self.segments:
                self.error(f"管段 {seg.name} 的下游段不存在: {seg.downstream}")
        for valve in self.valves.values():
            if valve.segment not in self.segments:
                self.error(f"阀门 {valve.name} 引用不存在的段: {valve.segment}")
        for seg in self.base_flow:
            if seg not in self.segments:
                self.error(f"输送流引用不存在的段: {seg}")
        for seg in self.pressure:
            if seg not in self.segments:
                self.error(f"监测引用不存在的段: {seg}")

    # ---------- 事件 ----------
    def do_event(self, kind: str, seg_name: str, lineno: int) -> None:
        if seg_name not in self.segments:
            self.error(f"第{lineno}行: 事件引用不存在的段: {kind} {seg_name}")
            return
        if kind == "泄漏":
            if seg_name in self.active_leaks:
                self.error(f"重复泄漏未恢复: 段 {seg_name} 在上次泄漏未恢复时再次报泄漏")
                return
            self.active_leaks.add(seg_name)
        elif kind == "恢复":
            if seg_name not in self.active_leaks:
                self.error(f"无对应泄漏的恢复: 段 {seg_name} 当前没有未恢复的泄漏")
                return
            self.active_leaks.discard(seg_name)
        else:
            self.error(f"第{lineno}行: 未知事件类型: {kind}（只支持 泄漏/恢复）")

    # ---------- 状态计算 ----------
    def leak_closure(self) -> Set[str]:
        """所有在泄漏段 => 需关断段集合（泄漏段 + 全部上游链）。"""
        closed: Set[str] = set()
        for leak in self.active_leaks:
            cur: Optional[str] = leak
            seen: Set[str] = set()
            while cur and cur in self.segments and cur not in seen:
                seen.add(cur)
                closed.add(cur)
                cur = self.segments[cur].upstream
        return closed

    def valve_open(self, valve_name: str, closed_segs: Set[str]) -> bool:
        valve = self.valves[valve_name]
        return valve.base_open and valve.segment not in closed_segs

    def seg_is_closed(self, name: str, closed_segs: Set[str]) -> bool:
        if name in closed_segs:
            return True
        return any(not self.valves[v].base_open for v in self.valves_by_seg.get(name, ()))

    def effective_flows(self, closed_segs: Set[str]) -> Dict[str, float]:
        """沿上游链递推：本段关断或上游有效流量为 0，则本段流量级联归零。"""
        memo: Dict[str, float] = {}

        def calc(name: str, stack: Set[str]) -> float:
            if name in memo:
                return memo[name]
            seg = self.segments.get(name)
            if seg is None or name in stack:   # 引用缺失或成环：按断流处理，防死循环
                return 0.0
            stack.add(name)
            if self.seg_is_closed(name, closed_segs):
                result = 0.0
            elif seg.upstream and seg.upstream in self.segments and calc(seg.upstream, stack) <= 0.0:
                result = 0.0   # 上游不来流 => 下游级联归零
            else:
                result = self.base_flow.get(name, 0.0)
            stack.discard(name)
            memo[name] = result
            return result

        for name in self.segments:
            calc(name, set())
        return memo

    # ---------- 规则检查 ----------
    def run_checks(self, flows: Dict[str, float]) -> None:
        for name in sorted(self.pressure):
            if name not in self.segments:
                continue
            pressure = self.pressure[name]
            if pressure < P_MIN:
                self.error(f"压力超限(欠压): 段 {name} 监测压力 {pressure:g} MPa 低于下限 {P_MIN} MPa，疑似泄漏/供压不足")
            elif pressure > P_MAX:
                self.error(f"压力超限(超压): 段 {name} 监测压力 {pressure:g} MPa 高于上限 {P_MAX} MPa，存在爆管风险")
        for name in sorted(self.segments):
            flow = flows.get(name, 0.0)
            pressure = self.pressure.get(name)
            if pressure is not None and flow >= FLOW_HIGH and pressure <= P_CONTRA:
                self.error(
                    f"流量压力矛盾: 段 {name} 有效流量 {flow:g} >= {FLOW_HIGH:g}（高流量），"
                    f"但压力 {pressure:g} MPa <= {P_CONTRA:g} MPa（低压力），疑似仪表故障或未记录泄漏"
                )

    # ---------- 输出 ----------
    def state_text(self, flows: Dict[str, float], closed_segs: Set[str]) -> str:
        lines = []
        for name in sorted(self.segments):
            seg = self.segments[name]
            valves = self.valves_by_seg.get(name, [])
            vdesc = ",".join(f"{v}={'开' if self.valve_open(v, closed_segs) else '关'}" for v in valves) or "无"
            pressure = self.pressure.get(name)
            pdesc = f"{pressure:g}MPa" if pressure is not None else "未监测"
            flags = []
            if name in self.active_leaks:
                flags.append("【泄漏中】")
            if self.seg_is_closed(name, closed_segs):
                flags.append("【已关断】")
            if flows.get(name, 0.0) <= 0.0 and self.base_flow.get(name, 0.0) > 0.0:
                flags.append("【流量归零】")
            lines.append(
                f"  段 {name} (上游={seg.upstream or '-'} 下游={seg.downstream or '-'}): "
                f"有效流量={flows.get(name, 0.0):g} 压力={pdesc} 阀门[{vdesc}] {' '.join(flags)}".rstrip()
            )
        return "\n".join(lines)

    def print_errors(self) -> None:
        print("\n================ 错误/告警报告 ================")
        if not self.errors:
            print("无错误。")
            return
        for i, msg in enumerate(self.errors, 1):
            print(f"  {i:>2}. {msg}")
        print(f"共 {len(self.errors)} 条。")


def main(argv: List[str]) -> int:
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
        source = argv[1]
    else:
        text = sys.stdin.read()
        source = "<stdin>"

    pipeline = Pipeline()
    events: List[tuple] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] == "事件":
            if len(parts) != 3:
                pipeline.error(f"第{lineno}行: 事件格式错误（应为: 事件 泄漏|恢复 段）: {line}")
            else:
                events.append((lineno, parts[1], parts[2]))
        else:
            pipeline.feed(lineno, parts)

    print(f"输入来源: {source}")
    pipeline.validate()

    closed = pipeline.leak_closure()
    flows = pipeline.effective_flows(closed)
    pipeline.run_checks(flows)
    print("\n======= 初始状态（事件发生前） =======")
    print(pipeline.state_text(flows, closed))

    for lineno, kind, seg_name in events:
        print(f"\n======= 事件: {kind} {seg_name} （第{lineno}行） =======")
        pipeline.do_event(kind, seg_name, lineno)
        closed = pipeline.leak_closure()
        flows = pipeline.effective_flows(closed)
        pipeline.run_checks(flows)
        print(pipeline.state_text(flows, closed))

    pipeline.print_errors()
    return 0 if not pipeline.errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
