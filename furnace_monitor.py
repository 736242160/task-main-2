#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
furnace_monitor.py — 高炉冶炼状态监控与错误报告工具（纯 Python 标准库，单文件）

用法：
    python3 furnace_monitor.py [输入文件]      # 缺省从标准输入读取

输入格式（行指令，空白分隔，# 开头为注释，空行忽略；按行顺序处理，状态跨行延续）：
    炉   <炉名> <温度下限> <温度上限> <配料上限>
    料   <料名> <类型>                 # 类型 ∈ {矿石, 焦炭, 辅料}
    加料 <炉名> <料名> <数量>
    冶炼 <炉名> <实测温度> <时长> <结果>  # 结果 ∈ {出铁, 异常}
    复风 <炉名>                        # 解除停产

自定规则（必须阅读，判定均以此为准）：
 1. 配料比失衡：以"当前炉次"（上一次出铁至今）的累计配料计算，每次冶炼时校验。
    - 焦比 = 焦炭量 / 矿石量，合理区间 [0.30, 0.60]。
      理由：焦炭是高炉的热源与还原剂，过低则炉温与还原氛围不足（炉温失控的前兆），
      过高则浪费燃料并恶化料柱透气性。
    - 辅料比 = 辅料量 / 总料量，上限 0.20。
      理由：辅料（熔剂）仅用于调渣，过量会稀释炉料、增加渣量与能耗。
2. 级联停产：同一炉连续 2 次冶炼结果为"异常"即自动停产；停产期间该炉的
   加料与冶炼一律报错并拒绝执行；"出铁"会将连续异常计数清零；"复风"解除停产。
   复风视为检修清炉：上一未出铁炉次的配料累计作废，从空炉料重新开始。
 3. 重复冶炼未出铁：上一次冶炼结果不是"出铁"（即上一炉次未正常出铁）又发起
    新的冶炼，即报告。
 4. 配料上限：当前炉次累计加料总量超过该炉配料上限即报告（超量仍计入累计，
    以保证累计级联更新与现场一致）；每次"出铁"后累计清零，进入新炉次。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

MATERIAL_TYPES = ("矿石", "焦炭", "辅料")
SMELT_RESULTS = ("出铁", "异常")

COKE_RATIO_MIN = 0.30   # 焦比下限（自定规则 1）
COKE_RATIO_MAX = 0.60   # 焦比上限（自定规则 1）
AUX_RATIO_MAX = 0.20    # 辅料比上限（自定规则 1）
ABNORMAL_SHUTDOWN_THRESHOLD = 2  # 连续异常停产阈值（自定规则 2）


def fmt(num: float) -> str:
    return f"{num:g}"


@dataclass
class Material:
    name: str
    kind: str


@dataclass
class Furnace:
    name: str
    temp_low: float
    temp_high: float
    charge_limit: float
    status: str = "生产中"                 # 生产中 / 停产
    abnormal_streak: int = 0               # 连续异常次数
    charges: dict[str, float] = field(default_factory=dict)  # 当前炉次：料名 -> 累计量
    iron_heats: int = 0                    # 累计出铁炉次数
    smelt_count: int = 0                   # 累计冶炼次数
    total_duration: float = 0.0            # 累计冶炼时长
    last_result: str | None = None         # 上一次冶炼结果

    @property
    def total_charged(self) -> float:
        return sum(self.charges.values())


class Monitor:
    def __init__(self) -> None:
        self.furnaces: dict[str, Furnace] = {}
        self.materials: dict[str, Material] = {}
        self.errors: list[tuple[int, str, str]] = []

    def error(self, line_no: int, category: str, message: str) -> None:
        self.errors.append((line_no, category, message))

    # ---------- 指令处理 ----------

    def run(self, text: str) -> None:
        handlers = {
            "炉": self.do_furnace_def,
            "料": self.do_material_def,
            "加料": self.do_charge,
            "冶炼": self.do_smelt,
            "复风": self.do_resume,
        }
        for line_no, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            handler = handlers.get(parts[0])
            if handler is None:
                self.error(line_no, "记录格式错误", f"未知指令: {parts[0]}")
                continue
            handler(line_no, parts)

    def do_furnace_def(self, line_no: int, parts: list[str]) -> None:
        if len(parts) != 5:
            self.error(line_no, "记录格式错误", "炉 指令应为: 炉 <炉名> <温度下限> <温度上限> <配料上限>")
            return
        _, name, low_s, high_s, limit_s = parts
        if name in self.furnaces:
            self.error(line_no, "重复定义", f"炉={name} 已定义")
            return
        try:
            low, high, limit = float(low_s), float(high_s), float(limit_s)
        except ValueError:
            self.error(line_no, "记录格式错误", f"炉={name} 温度区间/配料上限必须为数字")
            return
        if not low < high:
            self.error(line_no, "记录格式错误", f"炉={name} 温度下限 {fmt(low)} 必须小于上限 {fmt(high)}")
            return
        if limit <= 0:
            self.error(line_no, "记录格式错误", f"炉={name} 配料上限必须为正数")
            return
        self.furnaces[name] = Furnace(name, low, high, limit)

    def do_material_def(self, line_no: int, parts: list[str]) -> None:
        if len(parts) != 3:
            self.error(line_no, "记录格式错误", "料 指令应为: 料 <料名> <类型:矿石|焦炭|辅料>")
            return
        _, name, kind = parts
        if name in self.materials:
            self.error(line_no, "重复定义", f"料={name} 已定义")
            return
        if kind not in MATERIAL_TYPES:
            self.error(line_no, "记录格式错误", f"料={name} 类型={kind} 非法，应为 {'/'.join(MATERIAL_TYPES)}")
            return
        self.materials[name] = Material(name, kind)

    def do_charge(self, line_no: int, parts: list[str]) -> None:
        if len(parts) != 4:
            self.error(line_no, "记录格式错误", "加料 指令应为: 加料 <炉名> <料名> <数量>")
            return
        _, fname, mname, amount_s = parts
        fur = self.furnaces.get(fname)
        if fur is None:
            self.error(line_no, "引用不存在对象", f"加料引用了未定义的炉: {fname}")
            return
        if mname not in self.materials:
            self.error(line_no, "引用不存在对象", f"加料引用了未定义的料: {mname}")
            return
        try:
            amount = float(amount_s)
        except ValueError:
            self.error(line_no, "记录格式错误", f"加料量 {amount_s} 不是数字")
            return
        if amount <= 0:
            self.error(line_no, "记录格式错误", f"加料量必须为正数: {fmt(amount)}")
            return
        if fur.status == "停产":
            self.error(line_no, "停产炉加料",
                       f"炉={fname} 处于停产状态，拒绝加料 料={mname} 量={fmt(amount)}")
            return
        # 配料累计级联更新：同一炉次内逐笔累加
        fur.charges[mname] = fur.charges.get(mname, 0.0) + amount
        total = fur.total_charged
        if total > fur.charge_limit:
            self.error(line_no, "加料超配料上限",
                       f"炉={fname} 料={mname} 本次={fmt(amount)} 累计={fmt(total)} "
                       f"上限={fmt(fur.charge_limit)} 超出={fmt(total - fur.charge_limit)}")

    def do_smelt(self, line_no: int, parts: list[str]) -> None:
        if len(parts) != 5:
            self.error(line_no, "记录格式错误", "冶炼 指令应为: 冶炼 <炉名> <实测温度> <时长> <结果:出铁|异常>")
            return
        _, fname, temp_s, dur_s, result = parts
        fur = self.furnaces.get(fname)
        if fur is None:
            self.error(line_no, "引用不存在对象", f"冶炼引用了未定义的炉: {fname}")
            return
        try:
            temp, duration = float(temp_s), float(dur_s)
        except ValueError:
            self.error(line_no, "记录格式错误", f"炉={fname} 温度/时长必须为数字")
            return
        if duration <= 0:
            self.error(line_no, "记录格式错误", f"炉={fname} 冶炼时长必须为正数: {fmt(duration)}")
            return
        if result not in SMELT_RESULTS:
            self.error(line_no, "记录格式错误",
                       f"炉={fname} 冶炼结果={result} 非法，应为 {'/'.join(SMELT_RESULTS)}")
            return
        if fur.status == "停产":
            self.error(line_no, "停产炉冶炼",
                       f"炉={fname} 处于停产状态，拒绝冶炼 温度={fmt(temp)} 结果={result}")
            return
        if fur.last_result is not None and fur.last_result != "出铁":
            self.error(line_no, "重复冶炼未出铁",
                       f"炉={fname} 上一次冶炼结果={fur.last_result}，未出铁即再次冶炼")
        # 炉温超温控区间：报告炉、温度、超限量
        if temp < fur.temp_low:
            self.error(line_no, "炉温超限",
                       f"炉={fname} 温度={fmt(temp)} 低于下限 {fmt(fur.temp_low)}，"
                       f"超限量={fmt(fur.temp_low - temp)}")
        elif temp > fur.temp_high:
            self.error(line_no, "炉温超限",
                       f"炉={fname} 温度={fmt(temp)} 高于上限 {fmt(fur.temp_high)}，"
                       f"超限量={fmt(temp - fur.temp_high)}")
        self.check_ratio(line_no, fur)
        # 状态更新（跨流延续）
        fur.smelt_count += 1
        fur.total_duration += duration
        fur.last_result = result
        if result == "出铁":
            fur.iron_heats += 1
            fur.abnormal_streak = 0
            fur.charges.clear()  # 炉次结束，配料累计清零，进入新炉次
        else:
            fur.abnormal_streak += 1
            if fur.abnormal_streak >= ABNORMAL_SHUTDOWN_THRESHOLD:
                fur.status = "停产"
                self.error(line_no, "级联停产",
                           f"炉={fname} 连续 {fur.abnormal_streak} 次冶炼异常，"
                           f"级联停产；后续加料/冶炼将被拒绝，需 复风 恢复")

    def do_resume(self, line_no: int, parts: list[str]) -> None:
        if len(parts) != 2:
            self.error(line_no, "记录格式错误", "复风 指令应为: 复风 <炉名>")
            return
        _, fname = parts
        fur = self.furnaces.get(fname)
        if fur is None:
            self.error(line_no, "引用不存在对象", f"复风引用了未定义的炉: {fname}")
            return
        if fur.status != "停产":
            self.error(line_no, "状态错误", f"炉={fname} 未停产，复风无效")
            return
        fur.status = "生产中"
        fur.abnormal_streak = 0
        fur.charges.clear()  # 检修清炉：未出铁炉次的累计配料作废
        fur.last_result = None

    # ---------- 配料比校验 ----------

    def check_ratio(self, line_no: int, fur: Furnace) -> None:
        kinds = {kind: 0.0 for kind in MATERIAL_TYPES}
        for mname, amount in fur.charges.items():
            kinds[self.materials[mname].kind] += amount
        ore, coke, aux = kinds["矿石"], kinds["焦炭"], kinds["辅料"]
        total = ore + coke + aux
        problems = []
        if ore <= 0:
            problems.append("矿石量为 0，无法构成炉料")
        else:
            coke_ratio = coke / ore
            if not COKE_RATIO_MIN <= coke_ratio <= COKE_RATIO_MAX:
                problems.append(
                    f"焦比={coke_ratio:.2f} 超出区间 [{COKE_RATIO_MIN:.2f}, {COKE_RATIO_MAX:.2f}]"
                    f"（焦炭={fmt(coke)} 矿石={fmt(ore)}）")
        if total > 0:
            aux_ratio = aux / total
            if aux_ratio > AUX_RATIO_MAX:
                problems.append(
                    f"辅料比={aux_ratio:.2f} 超过上限 {AUX_RATIO_MAX:.2f}"
                    f"（辅料={fmt(aux)} 总量={fmt(total)}）")
        if problems:
            self.error(line_no, "配料比失衡", f"炉={fur.name} " + "；".join(problems))

    # ---------- 输出 ----------

    def render(self) -> str:
        out = ["===== 冶炼状态 ====="]
        if not self.furnaces:
            out.append("（无炉定义）")
        for fur in self.furnaces.values():
            kinds = {kind: 0.0 for kind in MATERIAL_TYPES}
            for mname, amount in fur.charges.items():
                kinds[self.materials[mname].kind] += amount
            ore, coke, aux = kinds["矿石"], kinds["焦炭"], kinds["辅料"]
            total = fur.total_charged
            coke_ratio = f"{coke / ore:.2f}" if ore > 0 else "—"
            aux_ratio = f"{aux / total:.2f}" if total > 0 else "—"
            out.append(
                f"炉 {fur.name} [{fur.status}] 温控={fmt(fur.temp_low)}~{fmt(fur.temp_high)} "
                f"出铁={fur.iron_heats}炉次 冶炼={fur.smelt_count}次 "
                f"累计时长={fmt(fur.total_duration)} 连续异常={fur.abnormal_streak}")
            charge_desc = " ".join(
                f"{mname}={fmt(amount)}({self.materials[mname].kind})"
                for mname, amount in fur.charges.items()) or "（空）"
            out.append(f"  当前炉次配料: {charge_desc}")
            out.append(f"  累计={fmt(total)}/{fmt(fur.charge_limit)} "
                       f"焦比={coke_ratio} 辅料比={aux_ratio}")
        out.append("")
        out.append(f"===== 错误报告（{len(self.errors)} 条）=====")
        if not self.errors:
            out.append("（无错误）")
        for idx, (line_no, category, message) in enumerate(self.errors, 1):
            out.append(f"{idx}. [行{line_no}] {category}: {message}")
        return "\n".join(out)


RULES_BRIEF = """===== 判定规则（自定）=====
焦比=焦炭/矿石 ∈ [0.30, 0.60]；辅料比=辅料/总量 ≤ 0.20（每次冶炼时按当前炉次累计校验）
连续 2 次冶炼异常 -> 级联停产，停产后加料/冶炼均被拒绝，需 复风 恢复
上一次冶炼未出铁即再次冶炼 -> 重复冶炼未出铁；出铁清零累计，复风清炉后从空料开始
"""


def main(argv: list[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and argv[1] in ("-h", "--help")):
        print(__doc__)
        return 0 if len(argv) == 2 else 2
    if len(argv) == 2:
        with open(argv[1], encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    monitor = Monitor()
    monitor.run(text)
    print(RULES_BRIEF + monitor.render())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
