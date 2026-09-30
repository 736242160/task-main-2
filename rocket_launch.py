#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rocket_launch.py — 火箭加注/检测/发射事件流仿真与错误报告工具（纯标准库，单文件）

输入 DSL（UTF-8 文本，每行一条，# 开头为注释，空行忽略）：

    火箭 <名称> <推进剂上限> <检测项目1,检测项目2,...>
    加注 <时刻> <火箭名> <推进剂类型> <量>
    检测 <时刻> <火箭名> <项目> <合格|异常>
    发射 <时刻> <火箭名> <窗口时刻> <成功|中止>

时刻格式：ISO-8601（如 2026-10-01T10:00:00）或当日 HH:MM[:SS]。
所有事件按时刻排序后依次处理（同时刻保持输入先后），状态跨流延续。

自定规则（均可用命令行参数调整）：
  * 窗口过期：实际发射时刻晚于 窗口时刻 + 宽限（默认 10 分钟）即视为窗口过期，
    强制中止并级联泄放（清空推进剂、作废全部检测结果，需重新加注/检测）。
  * 比例失衡：同一火箭已加注 >=2 种推进剂时，若单一类型占总量比例超过 2/3，
    判定推进剂比例失衡。理由：双组元（或多组元）推进剂的设计混合比通常使各组元
    装填量处于同一量级，任一组元占比超过 2/3 意味着混合比严重偏离设计点，
    继续加注/发射将造成推进剂浪费甚至燃烧不稳定。
  * 中止（含窗口过期强制中止与指令中止）后级联泄放：推进剂清零、检测结果作废，
    之后重新加注同类型不再视为重复加注（新一轮发射准备）。
  * 检测异常未排除（同一项目最后一次检测结果为异常）时禁止发射，本次发射记为中止，
    但不泄放推进剂（可排除故障后在窗口期内重试）。

用法：
    python3 rocket_launch.py 输入文件 [-w 宽限分钟] [-r 失衡占比阈值]
    python3 rocket_launch.py -            # 从标准输入读取
    python3 rocket_launch.py --demo       # 运行内置示例

退出码：0 = 无错误；1 = 存在错误报告；2 = 输入解析失败。
"""

import argparse
import shlex
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

KIND_DEF, KIND_FUEL, KIND_CHECK, KIND_LAUNCH = "火箭", "加注", "检测", "发射"


@dataclass
class Rocket:
    name: str
    capacity: float
    items: list
    propellants: dict = field(default_factory=dict)  # 类型 -> 已加注量
    checks: dict = field(default_factory=dict)       # 项目 -> 最近一次是否合格


@dataclass
class Event:
    order: int
    time: datetime
    kind: str
    args: list


class Simulation:
    def __init__(self, window_grace, imbalance_share):
        self.window_grace = window_grace
        self.imbalance_share = imbalance_share
        self.rockets = {}
        self.errors = []    # (时刻或None, 类别, 消息)
        self.launches = []  # (时刻, 火箭, 状态, 说明)

    # ---- 报告辅助 ------------------------------------------------------
    def error(self, when, category, message):
        self.errors.append((when, category, message))

    # ---- 事件处理 ------------------------------------------------------
    def define_rocket(self, name, capacity, items):
        if name in self.rockets:
            self.error(None, "重复定义", f"火箭“{name}”被重复定义，后者覆盖前者")
        self.rockets[name] = Rocket(name, capacity, items)

    def fuel(self, t, name, ptype, amount):
        r = self.rockets.get(name)
        if r is None:
            self.error(t, "加注引用不存在对象", f"加注引用了不存在的火箭“{name}”")
            return
        if ptype in r.propellants:
            self.error(t, "重复加注",
                       f"火箭“{name}”重复加注同类型推进剂“{ptype}”，本次 {amount} 已拒绝")
            return
        r.propellants[ptype] = amount
        total = sum(r.propellants.values())
        if total > r.capacity:
            self.error(t, "加注超推进剂上限",
                       f"火箭“{name}”加注后总量 {total:g} 超过推进剂上限 {r.capacity:g}")
        self.check_imbalance(t, r)

    def check(self, t, name, item, result):
        r = self.rockets.get(name)
        if r is None:
            self.error(t, "检测引用不存在对象", f"检测引用了不存在的火箭“{name}”")
            return
        if item not in r.items:
            self.error(t, "检测引用不存在对象",
                       f"火箭“{name}”不存在检测项目“{item}”")
            return
        r.checks[item] = (result == "合格")

    def launch(self, t, name, window_t, declared):
        r = self.rockets.get(name)
        if r is None:
            self.error(t, "发射引用不存在的火箭", f"发射引用了不存在的火箭“{name}”")
            return
        if t > window_t + self.window_grace:
            self.error(t, "窗口过期",
                       f"火箭“{name}”发射时刻 {fmt(t)} 晚于窗口 {fmt(window_t)}"
                       f"+宽限 {fmt_delta(self.window_grace)}，强制中止并级联泄放")
            self.defuel(r)
            self.launches.append((t, name, "中止", "窗口过期，已级联泄放"))
            return
        if declared == "中止":
            self.defuel(r)
            self.launches.append((t, name, "中止", "指令中止，已级联泄放"))
            return
        anomalies = [i for i, ok in r.checks.items() if not ok]
        if anomalies:
            self.error(t, "检测异常未排除",
                       f"火箭“{name}”检测项目 {','.join(anomalies)} 异常未排除，禁止发射")
            self.launches.append((t, name, "中止", "检测异常未排除，禁止发射（推进剂保留）"))
            return
        self.check_imbalance(t, r)
        total = sum(r.propellants.values())
        self.launches.append((t, name, "成功",
                              f"按时发射，消耗推进剂 {total:g}"))
        # 发射后推进剂耗尽、检测结果作废，进入新一轮准备
        self.defuel(r)

    # ---- 级联与规则 ----------------------------------------------------
    def defuel(self, r):
        """级联泄放：清空推进剂并作废全部检测结果。"""
        r.propellants.clear()
        r.checks.clear()

    def check_imbalance(self, t, r):
        if len(r.propellants) < 2:
            return
        total = sum(r.propellants.values())
        if total <= 0:
            return
        top_type = max(r.propellants, key=r.propellants.get)
        share = r.propellants[top_type] / total
        if share > self.imbalance_share:
            self.error(t, "推进剂比例失衡",
                       f"火箭“{name_of(r)}”推进剂“{top_type}”占比 {share:.1%} "
                       f"超过阈值 {self.imbalance_share:.0%}，混合比偏离设计点")

    # ---- 输出 ----------------------------------------------------------
    def report(self, out):
        out.append("=== 发射状态 ===")
        if not self.launches:
            out.append("（无发射事件）")
        for i, (t, name, status, detail) in enumerate(self.launches, 1):
            out.append(f"{i}. [{fmt(t)}] {name}: {status} — {detail}")
        out.append("")
        out.append("=== 错误报告 ===")
        if not self.errors:
            out.append("（无错误）")
        for when, category, message in self.errors:
            stamp = fmt(when) if when else "定义阶段"
            out.append(f"[{stamp}] {category}: {message}")
        return out


def name_of(r):
    return r.name


def fmt(t):
    return t.strftime("%Y-%m-%d %H:%M:%S")


def fmt_delta(d):
    minutes = d.total_seconds() / 60
    return f"{minutes:g}分钟"


def parse_time(text):
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(text, pattern)
            return datetime.now().replace(hour=parsed.hour, minute=parsed.minute,
                                          second=parsed.second, microsecond=0)
        except ValueError:
            continue
    raise ValueError(f"无法解析时刻：{text!r}")


def parse(text):
    """返回 (定义列表, 事件列表)；解析失败抛 ValueError。"""
    defs, events = [], []
    for lineno, raw in enumerate(text.splitlines(), 1):
        # 去注释：行首 # 或空白后的 #（推进剂名称/火箭名中不会出现 #）
        hash_pos = raw.find("#")
        line = (raw if hash_pos < 0 else raw[:hash_pos]).strip()
        if not line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            raise ValueError(f"第 {lineno} 行解析失败：{exc}")
        kind = tokens[0]
        if kind == KIND_DEF:
            if len(tokens) < 4:
                raise ValueError(f"第 {lineno} 行：火箭定义需要 名称/上限/检测项目")
            name, capacity, items = tokens[1], tokens[2], tokens[3].split(",")
            defs.append((name, float(capacity), [i for i in items if i], lineno))
        elif kind == KIND_FUEL:
            if len(tokens) != 5:
                raise ValueError(f"第 {lineno} 行：加注格式为 加注 时刻 火箭 类型 量")
            events.append(Event(lineno, parse_time(tokens[1]), KIND_FUEL,
                                (tokens[2], tokens[3], float(tokens[4]))))
        elif kind == KIND_CHECK:
            if len(tokens) != 5:
                raise ValueError(f"第 {lineno} 行：检测格式为 检测 时刻 火箭 项目 结果")
            if tokens[4] not in ("合格", "异常"):
                raise ValueError(f"第 {lineno} 行：检测结果须为 合格/异常")
            events.append(Event(lineno, parse_time(tokens[1]), KIND_CHECK,
                                (tokens[2], tokens[3], tokens[4])))
        elif kind == KIND_LAUNCH:
            if len(tokens) != 5:
                raise ValueError(f"第 {lineno} 行：发射格式为 发射 时刻 火箭 窗口时刻 结果")
            if tokens[4] not in ("成功", "中止"):
                raise ValueError(f"第 {lineno} 行：发射结果须为 成功/中止")
            events.append(Event(lineno, parse_time(tokens[1]), KIND_LAUNCH,
                                (tokens[2], parse_time(tokens[3]), tokens[4])))
        else:
            raise ValueError(f"第 {lineno} 行：未知事件类型 {kind!r}")
    events.sort(key=lambda e: (e.time, e.order))
    return defs, events


def run(text, window_grace, imbalance_share):
    defs, events = parse(text)
    sim = Simulation(window_grace, imbalance_share)
    for name, capacity, items, lineno in defs:
        try:
            sim.define_rocket(name, capacity, items)
        except ValueError:
            raise ValueError(f"第 {lineno} 行：推进剂上限不是数字")
    for ev in events:
        if ev.kind == KIND_FUEL:
            sim.fuel(ev.time, *ev.args)
        elif ev.kind == KIND_CHECK:
            sim.check(ev.time, *ev.args)
        else:
            sim.launch(ev.time, *ev.args)
    return sim


DEMO = """\
# 内置示例：覆盖全部错误类型
火箭 长征五号 1000 发动机,电气,结构
火箭 快舟一号 500 发动机,电气

加注 2026-10-01T08:00:00 长征五号 液氧 500
加注 2026-10-01T08:05:00 长征五号 液氢 300
加注 2026-10-01T08:06:00 长征五号 液氢 100        # 重复加注 -> 报告并拒绝
加注 2026-10-01T08:10:00 长征五号 煤油 400        # 总量 1200 超上限 -> 报告；占比未失衡
检测 2026-10-01T08:20:00 长征五号 发动机 合格
检测 2026-10-01T08:21:00 长征五号 电气 异常
检测 2026-10-01T08:22:00 长征五号 结构 合格
检测 2026-10-01T08:23:00 长征五号 导航 合格        # 项目不存在 -> 报告
发射 2026-10-01T08:30:00 长征五号 2026-10-01T09:00:00 成功   # 电气异常未排除 -> 禁止发射

检测 2026-10-01T08:40:00 长征五号 电气 合格        # 排除故障，窗口内重试
发射 2026-10-01T08:50:00 长征五号 2026-10-01T09:00:00 成功   # 成功，推进剂耗尽

加注 2026-10-01T09:10:00 快舟一号 液氧 480
加注 2026-10-01T09:15:00 快舟一号 液氢 10          # 液氧占比 98% -> 比例失衡
检测 2026-10-01T09:20:00 快舟一号 发动机 合格
检测 2026-10-01T09:21:00 快舟一号 电气 合格
发射 2026-10-01T10:00:00 快舟一号 2026-10-01T09:30:00 成功   # 超窗口+宽限 -> 强制中止并泄放

加注 2026-10-01T10:30:00 快舟一号 液氧 200        # 泄放后重新加注，不算重复
加注 2026-10-01T10:35:00 快舟一号 液氢 180
检测 2026-10-01T10:40:00 快舟一号 发动机 合格
检测 2026-10-01T10:41:00 快舟一号 电气 合格
发射 2026-10-01T10:50:00 快舟一号 2026-10-01T11:00:00 中止   # 指令中止 -> 级联泄放

加注 2026-10-01T11:00:00 幻影号 液氧 100          # 火箭不存在 -> 报告
检测 2026-10-01T11:01:00 幻影号 发动机 合格        # 火箭不存在 -> 报告
发射 2026-10-01T11:05:00 幻影号 2026-10-01T11:10:00 成功     # 火箭不存在 -> 报告
"""


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="火箭加注/检测/发射事件流仿真与错误报告工具")
    parser.add_argument("input", nargs="?", help="输入文件路径，'-' 表示标准输入")
    parser.add_argument("-w", "--window-grace", type=float, default=10.0,
                        help="发射窗口宽限（分钟），默认 10")
    parser.add_argument("-r", "--imbalance-share", type=float, default=2 / 3,
                        help="单一推进剂占比失衡阈值，默认 0.667")
    parser.add_argument("--demo", action="store_true", help="运行内置示例")
    args = parser.parse_args(argv)

    if args.demo:
        text = DEMO
    elif args.input and args.input != "-":
        with open(args.input, encoding="utf-8") as fh:
            text = fh.read()
    elif args.input == "-" or not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.error("请提供输入文件、'-' 或 --demo")

    try:
        sim = run(text, timedelta(minutes=args.window_grace), args.imbalance_share)
    except ValueError as exc:
        print(f"输入解析失败：{exc}", file=sys.stderr)
        return 2

    for line in sim.report([]):
        print(line)
    return 1 if sim.errors else 0


if __name__ == "__main__":
    sys.exit(main())
