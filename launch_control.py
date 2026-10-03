#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launch_control.py — 火箭发射窗口与中止条件判定工具（纯标准库，单文件）

用法:
    python3 launch_control.py 任务文件.txt      # 从文件读取事件流
    python3 launch_control.py                  # 从标准输入读取
    python3 launch_control.py --demo           # 运行内置演示

输入格式（按行处理，事件顺序即时间顺序，跨流状态自动延续；# 开头为注释）:

    火箭 <名称> 推进剂 <类型>:<上限>[,<类型>:<上限>...] 检测 <项目>[,<项目>...]
    加注 <火箭> <推进剂类型> <量>
    检测 <火箭> <项目> <合格|异常>
    发射 <火箭> <窗口时刻> <成功|中止>

判定规则（自定义部分的理由）:

  1. 加注超上限: 累计加注量超过该推进剂上限 -> 报错并拒绝本次加注。
  2. 重复加注: 同一推进剂在未泄放前再次加注 -> 报错并拒绝（防止把
     "补加" 误当 "首次加注" 而掩盖超量；泄放后重新加注属正常流程）。
  3. 比例失衡: 贮箱容积是按发动机设计混合比确定的，因此各推进剂的
     "加注饱满度"（已加注量/上限）应当一致。若最大与最小饱满度之差
     超过 10% 则判定失衡，禁止成功发射。理由：等饱满度即等效于
     按设计混合比加注，且对任意推进剂种数都适用。
  4. 检测异常未排除: 任一检测项目最近一次结果为 "异常"，或存在从未
     检测的项目，不得成功发射；复检 "合格" 即视为排除。
  5. 窗口过期: 窗口有效时长 WINDOW = 30 个时间单位。同一火箭相邻两次
     发射之间若未重新加注，且新窗口时刻超出上一窗口 +30，则判定窗口
     过期（低温推进剂蒸发/任务时序失效），必须中止并级联泄放。
  6. 中止级联: 任何中止（主动中止、窗口过期、前置条件不满足被强制
     中止）都会泄放全部推进剂、清空加注记录；之后重新加注不再视为
     重复加注。检测结论保留（异常仍需复检合格才能排除）。
  7. 引用校验: 加注/检测/发射引用未定义的火箭、未声明的推进剂类型、
     未声明的检测项目，以及引用已发射火箭，均报错并忽略该事件。
"""

import sys
from dataclasses import dataclass, field

WINDOW_DURATION = 30.0   # 窗口有效时长（时间单位与输入的窗口时刻一致）
RATIO_TOLERANCE = 0.10   # 饱满度允许偏差


@dataclass
class Rocket:
    name: str
    capacity: dict                       # 推进剂类型 -> 上限
    check_items: list                    # 检测项目列表
    loaded: dict = field(default_factory=dict)   # 推进剂类型 -> 已加注量
    checks: dict = field(default_factory=dict)   # 检测项目 -> 合格/异常
    launched: bool = False
    last_window: float = None            # 上一次发射的窗口时刻
    fueled_since_launch: bool = False    # 上次发射后是否重新加注过

    def fill_fractions(self):
        return {t: self.loaded.get(t, 0.0) / c for t, c in self.capacity.items()}


class MissionControl:
    def __init__(self):
        self.rockets = {}
        self.errors = []    # (行号, 类别, 描述)
        self.launches = []  # (行号, 火箭, 时刻, 请求结果, 最终状态, 说明)

    # ---------- 工具 ----------
    def err(self, ln, category, msg):
        self.errors.append((ln, category, msg))

    def get_rocket(self, ln, name, action):
        r = self.rockets.get(name)
        if r is None:
            self.err(ln, "引用错误", f"{action}引用了不存在的火箭 '{name}'，事件已忽略")
            return None
        if r.launched:
            self.err(ln, "引用错误", f"{action}引用了已发射的火箭 '{name}'，事件已忽略")
            return None
        return r

    # ---------- 事件处理 ----------
    def define_rocket(self, ln, tokens):
        # 火箭 <名称> 推进剂 a:1,b:2 检测 x,y
        if len(tokens) < 6 or tokens[2] != "推进剂" or "检测" not in tokens:
            self.err(ln, "格式错误", "火箭定义应为: 火箭 <名称> 推进剂 <类型:上限,...> 检测 <项目,...>")
            return
        name = tokens[1]
        if name in self.rockets:
            self.err(ln, "定义错误", f"火箭 '{name}' 重复定义，已忽略")
            return
        idx = tokens.index("检测")
        cap_tokens = tokens[3:idx]
        check_tokens = tokens[idx + 1:]
        capacity = {}
        for part in ",".join(cap_tokens).split(","):
            part = part.strip()
            if not part:
                continue
            if ":" not in part:
                self.err(ln, "格式错误", f"推进剂项 '{part}' 缺少 ':上限'")
                return
            ptype, _, cap_s = part.partition(":")
            try:
                cap = float(cap_s)
                assert cap > 0
            except (ValueError, AssertionError):
                self.err(ln, "格式错误", f"推进剂 '{ptype}' 上限 '{cap_s}' 不是正数")
                return
            capacity[ptype.strip()] = cap
        check_items = [c.strip() for c in ",".join(check_tokens).split(",") if c.strip()]
        if not capacity or not check_items:
            self.err(ln, "定义错误", f"火箭 '{name}' 必须至少有一种推进剂和一个检测项目")
            return
        self.rockets[name] = Rocket(name=name, capacity=capacity, check_items=check_items)

    def fuel(self, ln, tokens):
        # 加注 <火箭> <类型> <量>
        if len(tokens) != 4:
            self.err(ln, "格式错误", "加注应为: 加注 <火箭> <推进剂类型> <量>")
            return
        _, name, ptype, amount_s = tokens
        r = self.get_rocket(ln, name, "加注")
        if r is None:
            return
        if ptype not in r.capacity:
            self.err(ln, "引用错误", f"火箭 '{name}' 未声明推进剂类型 '{ptype}'，加注已忽略")
            return
        try:
            amount = float(amount_s)
            assert amount > 0
        except (ValueError, AssertionError):
            self.err(ln, "格式错误", f"加注量 '{amount_s}' 不是正数，已忽略")
            return
        if r.loaded.get(ptype, 0.0) > 0:
            self.err(ln, "重复加注", f"火箭 '{name}' 的 '{ptype}' 在未泄放前重复加注 {amount_s}，已拒绝")
            return
        new_total = r.loaded.get(ptype, 0.0) + amount
        if new_total > r.capacity[ptype] + 1e-9:
            self.err(ln, "超上限",
                     f"火箭 '{name}' 的 '{ptype}' 加注 {amount_s} 后累计 {new_total:g} "
                     f"超过上限 {r.capacity[ptype]:g}，本次加注已拒绝")
            return
        r.loaded[ptype] = new_total
        r.fueled_since_launch = True

    def check(self, ln, tokens):
        # 检测 <火箭> <项目> <合格|异常>
        if len(tokens) != 4 or tokens[3] not in ("合格", "异常"):
            self.err(ln, "格式错误", "检测应为: 检测 <火箭> <项目> <合格|异常>")
            return
        _, name, item, result = tokens
        r = self.get_rocket(ln, name, "检测")
        if r is None:
            return
        if item not in r.check_items:
            self.err(ln, "引用错误", f"火箭 '{name}' 未声明检测项目 '{item}'，结果已忽略")
            return
        r.checks[item] = result  # 复检合格即排除此前的异常

    def launch(self, ln, tokens):
        # 发射 <火箭> <窗口时刻> <成功|中止>
        if len(tokens) != 4 or tokens[3] not in ("成功", "中止"):
            self.err(ln, "格式错误", "发射应为: 发射 <火箭> <窗口时刻> <成功|中止>")
            return
        _, name, t_s, requested = tokens
        r = self.get_rocket(ln, name, "发射")
        if r is None:
            return
        try:
            t = float(t_s)
        except ValueError:
            self.err(ln, "格式错误", f"窗口时刻 '{t_s}' 不是数字，发射事件已忽略")
            return

        problems = []
        expired = False
        if r.last_window is not None:
            if t < r.last_window:
                self.err(ln, "时序错误",
                         f"火箭 '{name}' 窗口时刻 {t:g} 早于上一窗口 {r.last_window:g}")
            if t > r.last_window + WINDOW_DURATION and not r.fueled_since_launch:
                expired = True
                problems.append(
                    f"窗口过期（距上一窗口 {t - r.last_window:g} > {WINDOW_DURATION:g} 且未重新加注）")

        bad = [i for i in r.check_items if r.checks.get(i) == "异常"]
        if bad:
            problems.append(f"检测异常未排除: {', '.join(bad)}")
        unchecked = [i for i in r.check_items if r.checks.get(i) is None]
        if unchecked:
            problems.append(f"检测项目未完成: {', '.join(unchecked)}")

        missing = [pt for pt in r.capacity if r.loaded.get(pt, 0.0) <= 0]
        if missing:
            problems.append(f"推进剂未加注: {', '.join(missing)}")
        elif len(r.capacity) >= 2:
            fracs = r.fill_fractions()
            lo, hi = min(fracs.values()), max(fracs.values())
            if hi - lo > RATIO_TOLERANCE:
                detail = ", ".join(f"{pt}={f:.0%}" for pt, f in fracs.items())
                problems.append(f"推进剂比例失衡（饱满度差 {hi - lo:.0%} > {RATIO_TOLERANCE:.0%}: {detail}）")

        if requested == "中止":
            reason = "窗口过期，发射中止" if expired else "任务中止"
            final = "中止"
            if expired:
                self.err(ln, "窗口过期", f"火箭 '{name}' {reason}")
            else:
                self.err(ln, "发射中止", f"火箭 '{name}' 在窗口 {t:g} 主动中止")
        else:  # 请求成功
            if problems:
                final = "中止(强制)"
                reason = "前置条件不满足: " + "；".join(problems)
                for p in problems:
                    cat = "窗口过期" if p.startswith("窗口过期") else "禁止发射"
                    self.err(ln, cat, f"火箭 '{name}': {p}")
            else:
                final = "成功"
                reason = "全部前置条件满足"

        cascade = ""
        if final != "成功":
            dumped = {pt: amt for pt, amt in r.loaded.items() if amt > 0}
            r.loaded = {pt: 0.0 for pt in r.capacity}
            if dumped:
                cascade = "；级联泄放 " + ", ".join(f"{pt}={amt:g}" for pt, amt in dumped.items())
        else:
            r.launched = True

        r.last_window = t
        r.fueled_since_launch = False
        self.launches.append((ln, name, t, requested, final, reason + cascade))

    # ---------- 主循环 ----------
    def process(self, lines):
        handlers = {"火箭": self.define_rocket, "加注": self.fuel,
                    "检测": self.check, "发射": self.launch}
        for ln, raw in enumerate(lines, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            tokens = line.split()
            handler = handlers.get(tokens[0])
            if handler is None:
                self.err(ln, "格式错误", f"未知指令 '{tokens[0]}'，应为 火箭/加注/检测/发射")
                continue
            handler(ln, tokens)

    # ---------- 报告 ----------
    def report(self, out):
        w = out.write
        w("=" * 60 + "\n发射状态\n" + "=" * 60 + "\n")
        if not self.launches:
            w("（无发射事件）\n")
        for ln, name, t, requested, final, reason in self.launches:
            w(f"[行{ln:>3}] {name} 窗口={t:g} 请求={requested} -> {final}（{reason}）\n")

        w("\n" + "=" * 60 + "\n错误清单\n" + "=" * 60 + "\n")
        if not self.errors:
            w("（无错误）\n")
        for ln, cat, msg in self.errors:
            w(f"[行{ln:>3}] [{cat}] {msg}\n")

        w("\n" + "=" * 60 + "\n火箭最终状态\n" + "=" * 60 + "\n")
        if not self.rockets:
            w("（无火箭定义）\n")
        for r in self.rockets.values():
            loaded = ", ".join(f"{pt}={r.loaded.get(pt, 0.0):g}/{cap:g}"
                               for pt, cap in r.capacity.items())
            checks = ", ".join(f"{it}:{r.checks.get(it, '未检')}" for it in r.check_items)
            status = "已发射" if r.launched else ("已加注待发射" if any(r.loaded.values()) else "未加注")
            w(f"{r.name}: {status} | 推进剂 {loaded} | 检测 {checks}\n")
        w(f"\n合计: 发射 {len(self.launches)} 次，错误 {len(self.errors)} 条\n")


DEMO = """\
# 演示：覆盖正常流程与各类错误
火箭 长征甲 推进剂 液氧:500,煤油:300 检测 发动机,阀门,导航
火箭 长征乙 推进剂 液氢:200,液氧:400 检测 发动机,阀门

加注 长征甲 液氧 450
加注 长征甲 煤油 270
加注 长征甲 煤油 100        # 重复加注 -> 报错
加注 长征甲 液氧 999        # 重复且超上限 -> 报错
检测 长征甲 发动机 合格
检测 长征甲 阀门 异常
检测 长征甲 导航 合格
发射 长征甲 100 成功        # 阀门异常未排除 -> 强制中止并泄放
检测 长征甲 阀门 合格       # 复检排除异常
加注 长征甲 液氧 450        # 泄放后重新加注，不算重复
加注 长征甲 煤油 200        # 饱满度 90% vs 66.7% -> 比例失衡
发射 长征甲 110 成功        # 比例失衡 -> 强制中止
加注 长征甲 液氧 450
加注 长征甲 煤油 270
发射 长征甲 120 成功        # 全部满足 -> 成功
发射 长征甲 130 成功        # 引用已发射火箭 -> 报错

加注 长征乙 液氢 180
加注 长征乙 液氧 360
检测 长征乙 发动机 合格
检测 长征乙 阀门 合格
发射 长征乙 200 中止        # 主动中止 -> 级联泄放
发射 长征乙 250 成功        # 距上窗口 50>30 且未重新加注 -> 窗口过期强制中止

加注 不存在 液氧 10         # 引用不存在的火箭
检测 长征甲 发动机 合格      # 引用已发射火箭
发射 不存在 1 成功           # 引用不存在的火箭
"""


def main(argv):
    if "--demo" in argv:
        lines = DEMO.splitlines()
        print("（运行内置演示输入）\n")
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    mc = MissionControl()
    mc.process(lines)
    mc.report(sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
