#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forest_quota.py — 林地采伐限额监控工具（纯 Python 标准库，单文件）

用法：
    python3 forest_quota.py 输入文件        # 从文件读取
    python3 forest_quota.py < 输入文件      # 从标准输入读取

输入格式（每行一条记录，顺序任意；# 后为注释，空行忽略）：

    林地 <名称> <采伐限额> <禁伐期|无>
    采伐 <林地名称> <批次号> <数量> <日期YYYY-MM-DD>
    恢复 <林地名称> <批次号> <补种数量> <日期YYYY-MM-DD>
    检查 <林地名称> <合规|超采>

禁伐期格式：
    MM-DD~MM-DD               每年重复（支持跨年，如 11-01~02-28）
    YYYY-MM-DD~YYYY-MM-DD     一次性绝对区间
    无                         无禁伐期

名称含空格时用引号包裹，如：林地 "南山 3 号林" 1000 03-01~06-30

关键设计（自定规则及理由）：
 1. 剩余限额级联模型：剩余限额 = 限额 - 累计采伐 + 累计补种。
    每条采伐/恢复事件即时更新，后续所有超限判断基于最新值（级联更新）。
 2. 超采量 = max(0, 累计采伐 - 累计补种 - 限额)，即剩余限额为负时的绝对值。
 3. 停采规则：检查结果为"超采"即对该地停采。检查是权威确认，账面可能滞后，
    确认超采后立即冻结采伐，防止损失扩大。
 4. 复采标准：停采后累计补种量 >= 停采时刻的超采量，且当前剩余限额 >= 0。
    补种须完全弥补超采欠账才允许复采；双条件防止停采期间偷采造成新欠账时
    提前复采。
 5. 重复采伐批次、引用不存在的林地：报错并忽略该笔，保证台账不被脏数据污染。
 6. 禁伐期/停采期间的采伐：报错，但数量仍计入累计（采伐已实际发生）。
 7. 全部事件流汇入同一监控器，状态天然跨流延续。
"""
from __future__ import annotations

import argparse
import shlex
import sys
from dataclasses import dataclass, field
from datetime import date


def fmt(value: float) -> str:
    """数字紧凑显示：1000.0 -> 1000，2.5 -> 2.5。"""
    return f"{value:g}"


# ---------------------------------------------------------------- 禁伐期

class BanPeriod:
    """禁伐期。支持每年重复的 MM-DD~MM-DD（可跨年）与一次性绝对区间。"""

    def __init__(self, raw: str):
        raw = raw.strip()
        if "~" not in raw:
            raise ValueError(f"禁伐期格式错误（缺少 ~）：{raw}")
        start_s, end_s = (p.strip() for p in raw.split("~", 1))
        self.annual = len(start_s) == 5  # MM-DD 视为每年重复
        if self.annual:
            self.start = self._parse_md(start_s)
            self.end = self._parse_md(end_s)
        else:
            try:
                self.start = date.fromisoformat(start_s)
                self.end = date.fromisoformat(end_s)
            except ValueError:
                raise ValueError(f"禁伐期日期格式错误：{raw}")
            if self.start > self.end:
                raise ValueError(f"禁伐期起点晚于终点：{raw}")

    @staticmethod
    def _parse_md(text: str):
        try:
            month, day = (int(p) for p in text.split("-"))
            date(2000, month, day)  # 校验合法性（允许 02-29）
        except ValueError:
            raise ValueError(f"禁伐期月日格式错误：{text}")
        return (month, day)

    def contains(self, day: date) -> bool:
        if not self.annual:
            return self.start <= day <= self.end
        md = (day.month, day.day)
        if self.start <= self.end:
            return self.start <= md <= self.end
        return md >= self.start or md <= self.end  # 跨年，如 11-01~02-28

    def __str__(self) -> str:
        if self.annual:
            return (f"{self.start[0]:02d}-{self.start[1]:02d}~"
                    f"{self.end[0]:02d}-{self.end[1]:02d}(每年)")
        return f"{self.start.isoformat()}~{self.end.isoformat()}"


# ---------------------------------------------------------------- 事件解析

@dataclass
class Event:
    kind: str                 # land / harvest / recover / inspect
    line_no: int
    name: str = ""
    quota: float = 0.0
    ban: BanPeriod | None = None
    batch: str = ""
    qty: float = 0.0
    day: date | None = None
    result: str = ""


KEYWORDS = {
    "林地": "land", "land": "land",
    "采伐": "harvest", "harvest": "harvest",
    "恢复": "recover", "recover": "recover",
    "检查": "inspect", "inspect": "inspect",
}


def _positive_number(text: str, what: str, allow_zero: bool = False) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{what}「{text}」不是数字")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{what}必须为正数：{text}")
    return value


def parse_line(line: str, line_no: int) -> Event | None:
    try:
        tokens = shlex.split(line, comments=True)
    except ValueError as exc:
        raise ValueError(f"行解析失败：{exc}")
    if not tokens:
        return None
    kind = KEYWORDS.get(tokens[0])
    if kind is None:
        raise ValueError(f"未知记录类型「{tokens[0]}」（应为 林地/采伐/恢复/检查）")

    if kind == "land":
        if len(tokens) != 4:
            raise ValueError("林地记录应为：林地 <名称> <采伐限额> <禁伐期|无>")
        quota = _positive_number(tokens[2], "采伐限额", allow_zero=True)
        ban = None if tokens[3] in ("无", "-", "none") else BanPeriod(tokens[3])
        return Event("land", line_no, name=tokens[1], quota=quota, ban=ban)

    if kind in ("harvest", "recover"):
        label = "采伐" if kind == "harvest" else "恢复"
        if len(tokens) != 5:
            raise ValueError(f"{label}记录应为：{label} <林地> <批次> <数量> <日期YYYY-MM-DD>")
        qty = _positive_number(tokens[3], "数量")
        try:
            day = date.fromisoformat(tokens[4])
        except ValueError:
            raise ValueError(f"日期格式错误「{tokens[4]}」，应为 YYYY-MM-DD")
        return Event(kind, line_no, name=tokens[1], batch=tokens[2], qty=qty, day=day)

    # inspect
    if len(tokens) != 3:
        raise ValueError("检查记录应为：检查 <林地> <合规|超采>")
    if tokens[2] not in ("合规", "超采"):
        raise ValueError(f"检查结果「{tokens[2]}」应为 合规 或 超采")
    return Event("inspect", line_no, name=tokens[1], result=tokens[2])


# ---------------------------------------------------------------- 林地状态

@dataclass
class Land:
    name: str
    quota: float
    ban: BanPeriod | None
    harvested: float = 0.0                 # 累计采伐
    replanted: float = 0.0                 # 累计补种
    suspended: bool = False                # 是否停采中
    harvest_batches: set = field(default_factory=set)
    recover_batches: set = field(default_factory=set)
    replanted_since_suspend: float = 0.0   # 停采后累计补种
    resume_target: float = 0.0             # 复采标准（= 停采时刻超采量）

    @property
    def remaining(self) -> float:
        """剩余限额：限额 - 累计采伐 + 累计补种（级联更新核心）。"""
        return self.quota - self.harvested + self.replanted

    @property
    def over(self) -> float:
        """当前超采量。"""
        return max(0.0, -self.remaining)


# ---------------------------------------------------------------- 监控器

class Monitor:
    """事件流处理器：所有流汇入同一实例，状态跨流延续。"""

    def __init__(self):
        self.lands: dict[str, Land] = {}
        self.errors: list[tuple[int, str, str]] = []   # (行号, 类别, 详情)
        self.changes: list[str] = []                   # 状态变更日志（停采/复采）

    def error(self, line_no: int, category: str, message: str):
        self.errors.append((line_no, category, message))

    def process(self, ev: Event):
        handler = {
            "land": self._do_land,
            "harvest": self._do_harvest,
            "recover": self._do_recover,
            "inspect": self._do_inspect,
        }[ev.kind]
        handler(ev)

    # -- 林地定义 --
    def _do_land(self, ev: Event):
        if ev.name in self.lands:
            self.error(ev.line_no, "重复定义",
                       f"林地「{ev.name}」重复定义，保留首次定义，忽略本条")
            return
        self.lands[ev.name] = Land(ev.name, ev.quota, ev.ban)

    # -- 采伐 --
    def _do_harvest(self, ev: Event):
        land = self.lands.get(ev.name)
        if land is None:
            self.error(ev.line_no, "未知林地",
                       f"采伐引用不存在的林地「{ev.name}」（批次 {ev.batch}），已忽略该笔")
            return
        if ev.batch in land.harvest_batches:
            self.error(ev.line_no, "重复批次",
                       f"林地「{ev.name}」采伐批次 {ev.batch} 重复，已忽略该笔")
            return
        land.harvest_batches.add(ev.batch)
        land.harvested += ev.qty  # 级联更新：剩余限额随之减少

        if land.ban is not None and land.ban.contains(ev.day):
            self.error(ev.line_no, "禁伐期采伐",
                       f"林地「{ev.name}」批次 {ev.batch} 于 {ev.day.isoformat()} "
                       f"采伐 {fmt(ev.qty)}，处于禁伐期 {land.ban}")
        if land.suspended:
            self.error(ev.line_no, "停采期间采伐",
                       f"林地「{ev.name}」处于停采状态，批次 {ev.batch} "
                       f"采伐 {fmt(ev.qty)} 已计入台账")
        if land.remaining < 0:
            self.error(ev.line_no, "超限额采伐",
                       f"林地「{ev.name}」累计采伐 {fmt(land.harvested)}"
                       f"（补种抵扣 {fmt(land.replanted)}），限额 {fmt(land.quota)}，"
                       f"超采 {fmt(land.over)}")

    # -- 恢复（补种） --
    def _do_recover(self, ev: Event):
        land = self.lands.get(ev.name)
        if land is None:
            self.error(ev.line_no, "未知林地",
                       f"恢复引用不存在的林地「{ev.name}」（批次 {ev.batch}），已忽略该笔")
            return
        if ev.batch in land.recover_batches:
            self.error(ev.line_no, "重复批次",
                       f"林地「{ev.name}」恢复批次 {ev.batch} 重复，已忽略该笔")
            return
        land.recover_batches.add(ev.batch)
        land.replanted += ev.qty  # 级联更新：剩余限额随之回升

        if land.suspended:
            land.replanted_since_suspend += ev.qty
            # 复采标准：停采后补种补足停采时超采量，且剩余限额恢复非负
            if (land.replanted_since_suspend >= land.resume_target
                    and land.remaining >= 0):
                land.suspended = False
                self.changes.append(
                    f"[行{ev.line_no}] 林地「{ev.name}」补种达标"
                    f"（停采后累计补种 {fmt(land.replanted_since_suspend)} ≥ "
                    f"复采标准 {fmt(land.resume_target)}，剩余限额 "
                    f"{fmt(land.remaining)} ≥ 0），恢复采伐")

    # -- 检查 --
    def _do_inspect(self, ev: Event):
        land = self.lands.get(ev.name)
        if land is None:
            self.error(ev.line_no, "未知林地",
                       f"检查引用不存在的林地「{ev.name}」，已忽略该笔")
            return
        if ev.result == "超采":
            self.error(ev.line_no, "检查超采",
                       f"林地「{ev.name}」检查结果为超采"
                       f"（账面累计采伐 {fmt(land.harvested)}，限额 {fmt(land.quota)}，"
                       f"当前超采 {fmt(land.over)}）")
            if not land.suspended:
                land.suspended = True
                land.replanted_since_suspend = 0.0
                land.resume_target = land.over
                self.changes.append(
                    f"[行{ev.line_no}] 林地「{ev.name}」检查超采，停采；"
                    f"复采标准：补种 ≥ {fmt(land.resume_target)} 且剩余限额 ≥ 0")
        # 结果为"合规"：仅记录在案，不改变停采状态（复采只能经由补种达标）


# ---------------------------------------------------------------- 报告输出

def print_report(mon: Monitor):
    print("=" * 66)
    print("采伐状态")
    print("=" * 66)
    if not mon.lands:
        print("（无林地定义）")
    for land in mon.lands.values():
        if land.suspended:
            need = max(0.0, land.resume_target - land.replanted_since_suspend)
            status = f"停采中（复采还需补种 {fmt(need)}）"
        else:
            status = "正常"
        ban = str(land.ban) if land.ban else "无"
        print(f"林地 {land.name}")
        print(f"    限额 {fmt(land.quota)} | 累计采伐 {fmt(land.harvested)} | "
              f"累计补种 {fmt(land.replanted)} | 剩余限额 {fmt(land.remaining)}")
        print(f"    禁伐期 {ban} | 状态 {status}")
        if land.over > 0:
            print(f"    !! 当前超采 {fmt(land.over)}")

    print()
    print("=" * 66)
    print("状态变更（停采 / 复采）")
    print("=" * 66)
    if mon.changes:
        for item in mon.changes:
            print(item)
    else:
        print("（无）")

    print()
    print("=" * 66)
    print(f"错误清单（共 {len(mon.errors)} 条）")
    print("=" * 66)
    if mon.errors:
        for idx, (line_no, category, message) in enumerate(mon.errors, 1):
            print(f"{idx}. [行{line_no}] [{category}] {message}")
    else:
        print("（无错误）")


# ---------------------------------------------------------------- 入口

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="林地采伐限额监控工具：输出采伐状态与错误报告")
    parser.add_argument("input", nargs="?",
                        help="输入文件（缺省读取标准输入）")
    args = parser.parse_args(argv)

    if args.input:
        with open(args.input, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()

    mon = Monitor()
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            event = parse_line(line, line_no)
        except ValueError as exc:
            mon.error(line_no, "格式错误", f"{exc}；原始内容：{line}")
            continue
        if event is not None:
            mon.process(event)

    print_report(mon)
    return 0


if __name__ == "__main__":
    sys.exit(main())
