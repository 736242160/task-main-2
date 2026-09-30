#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dam_tool.py — 大坝蓄水/泄洪调度监测工具（纯标准库，单文件）

输入格式（行式文本，# 开头为注释，空白分隔）：
    DAM      <名称> <正常水位m> <警戒水位m>
    GATE     <闸名> <泄洪能力m3/s>
    MONITOR  <水位m> <坝体位移mm>
    DISPATCH <闸名> <full|half|closed|全开|半开|关闭>

用法：
    python3 dam_tool.py 输入文件        # 处理文件
    python3 dam_tool.py                 # 从 stdin 读
    python3 dam_tool.py --demo          # 运行内置自测样例

自定规则（理由）：
  R1 泄洪需求 = (水位 - 正常水位) * DEMAND_PER_METER (m3/s)。
     理由：超出正常蓄水的水量与超深成正比，系数按每米 100 m3/s 估算。
  R2 水位 > 警戒水位 => 报告并自动触发泄洪：按闸门能力从大到小全开，
     剩余不足一闸的尾量用半开补齐。理由：大能力闸优先可最少闸门满足需求。
  R3 已开闸门总能力 < 需求 => 报告缺口（需求-能力）。
  R4 坝体位移 > DISP_THRESHOLD => 报告，且警戒水位下调 ESCALATION_STEP
     （级联提升警戒，可多次叠加，最低不低于正常水位+0.1）。
     理由：坝体异常变形意味着安全裕度下降，应更早泄洪。
  R5 调度引用不存在的闸门 => 报告错误，忽略该调度。
  R6 同一闸门重复调度（本工具运行期内第二次及以后）=> 报告，但仍执行。
  R7 恢复条件：警报中水位回落到 <= 正常水位 => 解除警报，自动开启的
     闸门全部关闭，警戒水位恢复初始值，调度记录清空（进入新一轮）。
  R8 监测跳变：相邻两次水位差 > JUMP_LEVEL 或位移差 > JUMP_DISP => 报告。
     理由：跳变通常是传感器故障或极端工况，需人工复核。
  R9 多闸门联合泄洪后，任一闸门状态变化都会级联重算总泄洪能力，
     并在警报期间即时校核缺口。
  R10 跨流状态延续：MONITOR 与 DISPATCH 可任意交错，水位/位移/闸门
      状态在事件间持续保持。
"""

import sys
from dataclasses import dataclass, field

# ---- 规则常量 ----
DEMAND_PER_METER = 100.0   # R1: 每超 1m 需要的泄洪能力 m3/s
DISP_THRESHOLD = 10.0      # R4: 坝体位移阈值 mm
ESCALATION_STEP = 0.5      # R4: 每次超限警戒水位下调 m
JUMP_LEVEL = 2.0           # R8: 水位跳变阈值 m
JUMP_DISP = 3.0            # R8: 位移跳变阈值 mm

OPEN_FACTOR = {"full": 1.0, "half": 0.5, "closed": 0.0}
OPEN_ALIAS = {"全开": "full", "半开": "half", "关闭": "closed"}
OPEN_CN = {"full": "全开", "half": "半开", "closed": "关闭"}


@dataclass
class Gate:
    name: str
    capacity: float          # 全开泄洪能力 m3/s
    opening: str = "closed"  # full / half / closed
    auto: bool = False       # 是否由自动泄洪触发开启

    def discharge(self) -> float:
        return self.capacity * OPEN_FACTOR[self.opening]


@dataclass
class Dam:
    name: str
    normal_level: float
    warning_level: float          # 当前警戒水位（可因位移级联下调）
    base_warning_level: float     # 初始警戒水位（恢复时用）
    gates: dict = field(default_factory=dict)

    # 运行状态（跨流延续）
    cur_level: float = None
    cur_disp: float = None
    prev_level: float = None
    prev_disp: float = None
    alert: bool = False           # 是否处于超警戒泄洪状态
    dispatched: set = field(default_factory=set)  # 已调度过的闸门（R6）
    errors: list = field(default_factory=list)
    events: list = field(default_factory=list)
    seq: int = 0

    # ---- 输出辅助 ----
    def _log(self, msg):
        self.events.append("[事件 %03d] %s" % (self.seq, msg))

    def _err(self, msg):
        self.errors.append("[错误 %03d] %s" % (self.seq, msg))

    def demand(self) -> float:
        """R1: 当前泄洪需求 m3/s"""
        if self.cur_level is None:
            return 0.0
        return max(0.0, (self.cur_level - self.normal_level) * DEMAND_PER_METER)

    def total_discharge(self) -> float:
        return sum(g.discharge() for g in self.gates.values())

    # ---- 泄洪校核（R3/R9）----
    def check_shortfall(self, context):
        if not self.alert:
            return
        need = self.demand()
        have = self.total_discharge()
        if have < need:
            self._err("%s：泄洪能力不足，缺口 %.1f m3/s（需求 %.1f，已开 %.1f）"
                      % (context, need - have, need, have))

    # ---- 自动泄洪（R2）----
    def auto_discharge(self):
        need = self.demand()
        if need <= 0:
            return
        remaining = need
        # 大能力闸优先全开
        for g in sorted(self.gates.values(), key=lambda x: -x.capacity):
            if remaining <= 0:
                break
            if g.opening == "closed":
                g.opening = "full"
                g.auto = True
                self._log("自动泄洪：闸门 %s 全开（能力 %.1f m3/s）"
                          % (g.name, g.capacity))
                remaining -= g.capacity
        # 尾量用半开补齐
        if remaining > 0:
            for g in sorted(self.gates.values(), key=lambda x: x.capacity):
                if g.opening == "closed":
                    g.opening = "half"
                    g.auto = True
                    self._log("自动泄洪：闸门 %s 半开（补尾量，提供 %.1f m3/s）"
                              % (g.name, g.discharge()))
                    remaining -= g.discharge()
                    break
        self.check_shortfall("自动泄洪后")

    # ---- 监测事件 ----
    def on_monitor(self, level, disp):
        self.seq += 1
        # R8 跳变检测
        if self.prev_level is not None and abs(level - self.prev_level) > JUMP_LEVEL:
            self._err("水位跳变：%.2f -> %.2f m（变化 %.2f > 阈值 %.1f）"
                      % (self.prev_level, level, abs(level - self.prev_level), JUMP_LEVEL))
        if self.prev_disp is not None and abs(disp - self.prev_disp) > JUMP_DISP:
            self._err("位移跳变：%.2f -> %.2f mm（变化 %.2f > 阈值 %.1f）"
                      % (self.prev_disp, disp, abs(disp - self.prev_disp), JUMP_DISP))
        self.prev_level, self.prev_disp = level, disp
        self.cur_level, self.cur_disp = level, disp
        self._log("监测：水位 %.2f m，坝体位移 %.2f mm" % (level, disp))

        # R4 位移超限 -> 级联提升警戒
        if disp > DISP_THRESHOLD:
            old = self.warning_level
            self.warning_level = max(self.normal_level + 0.1,
                                     self.warning_level - ESCALATION_STEP)
            self._err("坝体位移超限：%.2f mm > 阈值 %.1f mm" % (disp, DISP_THRESHOLD))
            self._log("级联提升警戒：警戒水位 %.2f -> %.2f m" % (old, self.warning_level))

        # 超警戒 -> 触发泄洪（R2）
        if level > self.warning_level:
            if not self.alert:
                self.alert = True
                self._err("水位超警戒：%.2f m > 警戒水位 %.2f m，触发泄洪"
                          % (level, self.warning_level))
            else:
                self._log("水位仍超警戒：%.2f m > %.2f m" % (level, self.warning_level))
            self.auto_discharge()
        # R7 恢复
        elif self.alert and level <= self.normal_level:
            self.alert = False
            for g in self.gates.values():
                if g.auto and g.opening != "closed":
                    g.opening = "closed"
                    g.auto = False
                    self._log("恢复常规：自动闸门 %s 关闭" % g.name)
            if self.warning_level != self.base_warning_level:
                self._log("警戒水位恢复：%.2f -> %.2f m"
                          % (self.warning_level, self.base_warning_level))
                self.warning_level = self.base_warning_level
            self.dispatched.clear()
            self._log("水位回落至 %.2f m <= 正常水位 %.2f m，解除警报，恢复常规调度"
                      % (level, self.normal_level))

    # ---- 调度事件 ----
    def on_dispatch(self, gate_name, action):
        self.seq += 1
        action = OPEN_ALIAS.get(action, action)
        if action not in OPEN_FACTOR:
            self._err("非法开度指令：%s（应为 full/half/closed 或 全开/半开/关闭）" % action)
            return
        # R5 闸门不存在
        g = self.gates.get(gate_name)
        if g is None:
            self._err("调度失败：闸门 %s 不存在" % gate_name)
            return
        # R6 重复调度
        if gate_name in self.dispatched:
            self._err("重复调度：闸门 %s 已被调度过" % gate_name)
        self.dispatched.add(gate_name)
        g.opening = action
        g.auto = False  # 人工接管
        self._log("调度：闸门 %s -> %s（当前泄洪 %.1f m3/s）"
                  % (gate_name, OPEN_CN[action], g.discharge()))
        # R9 级联重算总能力并校核缺口
        self.check_shortfall("调度后")

    # ---- 状态输出 ----
    def status(self):
        lines = []
        lines.append("大坝：%s" % self.name)
        lines.append("  当前水位：%s m（正常 %.2f，警戒 %.2f%s）" % (
            "%.2f" % self.cur_level if self.cur_level is not None else "未知",
            self.normal_level, self.warning_level,
            "，已级联下调" if self.warning_level != self.base_warning_level else ""))
        lines.append("  坝体位移：%s mm（阈值 %.1f）" % (
            "%.2f" % self.cur_disp if self.cur_disp is not None else "未知",
            DISP_THRESHOLD))
        lines.append("  警报状态：%s" % ("泄洪警报中" if self.alert else "常规"))
        lines.append("  泄洪需求：%.1f m3/s，已开能力：%.1f m3/s"
                     % (self.demand(), self.total_discharge()))
        lines.append("  闸门状态：")
        for name in sorted(self.gates):
            g = self.gates[name]
            lines.append("    - %s：%s，泄洪 %.1f/%.1f m3/s%s" % (
                g.name, OPEN_CN[g.opening], g.discharge(), g.capacity,
                "（自动）" if g.auto else ""))
        return "\n".join(lines)


def run(lines):
    dam = None
    for lineno, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].upper()
        try:
            if cmd == "DAM":
                name, normal, warning = parts[1], float(parts[2]), float(parts[3])
                if warning <= normal:
                    print("[输入错误 行%d] 警戒水位必须大于正常水位" % lineno)
                    return None
                dam = Dam(name, normal, warning, warning)
            elif cmd == "GATE":
                if dam is None:
                    raise ValueError("GATE 之前必须先定义 DAM")
                dam.gates[parts[1]] = Gate(parts[1], float(parts[2]))
            elif cmd == "MONITOR":
                if dam is None:
                    raise ValueError("MONITOR 之前必须先定义 DAM")
                dam.on_monitor(float(parts[1]), float(parts[2]))
            elif cmd == "DISPATCH":
                if dam is None:
                    raise ValueError("DISPATCH 之前必须先定义 DAM")
                dam.on_dispatch(parts[1], parts[2])
            else:
                print("[输入错误 行%d] 未知指令：%s" % (lineno, parts[0]))
        except (IndexError, ValueError) as e:
            print("[输入错误 行%d] %s（行内容：%s）" % (lineno, e, line))
    return dam


def report(dam):
    print("=" * 60)
    print("事件日志")
    print("=" * 60)
    for e in dam.events:
        print(e)
    print()
    print("=" * 60)
    print("大坝状态")
    print("=" * 60)
    print(dam.status())
    print()
    print("=" * 60)
    print("错误报告（共 %d 条）" % len(dam.errors))
    print("=" * 60)
    if dam.errors:
        for e in dam.errors:
            print(e)
    else:
        print("无错误")


DEMO_INPUT = """\
# ===== 自测样例：覆盖全部规则 =====
DAM 青云坝 100.0 105.0
GATE 甲闸 300
GATE 乙闸 200
GATE 丙闸 100

# 常规监测，无异常
MONITOR 101.0 2.0

# 水位超警戒(105) -> 报告并自动泄洪；需求=(106-100)*100=600，
# 甲300+乙200+丙100=600 刚好满足（多闸联合泄洪，状态级联更新）
MONITOR 106.0 3.0

# 人工调度：不存在的闸门 -> 报告
DISPATCH 丁闸 full
# 人工半开丙闸（警报中重算能力：300+200+50=550 < 600 -> 报告缺口50）
DISPATCH 丙闸 half
# 同闸重复调度 -> 报告（仍执行，丙闸全开，能力恢复600刚好满足需求）
DISPATCH 丙闸 full

# 水位跳变 106->109 (>2) -> 报告；需求900，能力600 -> 缺口300
MONITOR 109.0 4.0

# 位移超限 12>10 -> 报告并级联提升警戒（105->104.5）
MONITOR 108.0 12.0

# 水位回落到正常水位以下 -> 恢复常规：自动闸门关闭、警戒水位复原
MONITOR 99.5 3.0

# 新一轮：位移跳变 3->8 (>3) -> 报告；水位 104 未超警戒，无泄洪
MONITOR 104.0 8.0
"""


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        print("内置自测样例输入：")
        print(DEMO_INPUT)
        dam = run(DEMO_INPUT.splitlines())
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            dam = run(f)
    else:
        dam = run(sys.stdin)
    if dam is not None:
        report(dam)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
