#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""林木采伐限额监控工具（纯 Python 标准库，单文件）。

输入格式（每行一条记录，# 开头为注释，字段以空白分隔）：

    林地  <名称> <采伐限额> <禁伐期>
        禁伐期格式为 MM-DD~MM-DD（支持跨年，如 11-01~02-28），"-" 表示无禁伐期。
    采伐  <林地名称> <批次> <数量> <日期YYYY-MM-DD>
    恢复  <林地名称> <批次> <补种数量> <日期YYYY-MM-DD>
    检查  <林地名称> <结果: 合规|超采>

所有记录按文件顺序统一处理，采伐/恢复/检查跨流共享并延续同一份林地状态。

用法：
    python3 forest_monitor.py [输入文件]
    省略输入文件且 stdin 为终端时，运行内置演示。

关键规则（自定部分已注明）：
  1. 净采伐量 = 累计采伐 - 累计补种；剩余限额 = 限额 - 净采伐量。
     采伐与恢复都会级联更新剩余限额。
  2. 采伐后净采伐量 > 限额：报告超采（地、累计、限额、超采量）。
  3. 禁伐期内采伐：属违规事实，计入台账并报告（不自台账中扣除）。
  4. 检查结果为"超采"：该林地立即停采（自定规则：行政检查结论具有强制力，
     无论账面是否超采均停采，防止带病继续采伐）。
  5. 停采期间的采伐一律拒绝（不计入台账）并报告。
  6. 复采标准（自定）：停采后累计补种量 >= 停采时账面超采量，且至少补种 1 株
     （即账面无超采时，也需完成一次恢复以体现整改），达标后自动复采。
  7. 采伐/恢复/检查引用不存在的林地：报告并忽略该条。
  8. 同一林地同一批次重复采伐：报告并忽略（不重复入账）。
"""

from __future__ import annotations

import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date


# ---------------------------------------------------------------- 数据模型

@dataclass(frozen=True)
class BanPeriod:
    """禁伐期，start/end 为 (月, 日)，支持跨年区间。"""
    start: tuple
    end: tuple

    def contains(self, day: date) -> bool:
        md = (day.month, day.day)
        if self.start <= self.end:
            return self.start <= md <= self.end
        return md >= self.start or md <= self.end

    def __str__(self) -> str:
        fmt = lambda md: "%02d-%02d" % md
        return "%s~%s" % (fmt(self.start), fmt(self.end))


@dataclass
class Forest:
    name: str
    limit: float
    ban: BanPeriod | None
    cum_harvest: float = 0.0          # 累计采伐
    cum_recovery: float = 0.0         # 累计补种
    batches: set = field(default_factory=set)  # 已入账的采伐批次
    suspended: bool = False           # 是否停采中
    suspend_target: float = 0.0       # 复采所需累计补种量（自停采起）
    recovered_since_suspend: float = 0.0
    resume_count: int = 0             # 复采次数

    @property
    def net(self) -> float:
        """净采伐量（级联：采伐增加、恢复冲减）。"""
        return self.cum_harvest - self.cum_recovery

    @property
    def remaining(self) -> float:
        """剩余限额（级联更新）。"""
        return self.limit - self.net

    @property
    def status(self) -> str:
        if self.suspended:
            return "停采中"
        if self.resume_count:
            return "正常(复采%d次)" % self.resume_count
        return "正常"


@dataclass
class Issue:
    line: int
    kind: str
    message: str

    def __str__(self) -> str:
        return "第%-4d行 [%s] %s" % (self.line, self.kind, self.message)


# ---------------------------------------------------------------- 监控引擎

class Monitor:
    def __init__(self) -> None:
        self.forests: dict[str, Forest] = {}
        self.errors: list[Issue] = []    # 错误/违规清单
        self.notices: list[Issue] = []   # 状态变更通知（停采/复采）

    # ---- 工具 ----
    def _err(self, line: int, kind: str, msg: str) -> None:
        self.errors.append(Issue(line, kind, msg))

    def _note(self, line: int, kind: str, msg: str) -> None:
        self.notices.append(Issue(line, kind, msg))

    # ---- 记录定义 ----
    def add_forest(self, line: int, name: str, limit: float, ban: BanPeriod | None) -> None:
        if name in self.forests:
            self._err(line, "重复定义", "林地 '%s' 已定义，忽略重复定义" % name)
            return
        if limit < 0:
            self._err(line, "参数错误", "林地 '%s' 采伐限额不能为负: %g" % (name, limit))
            return
        self.forests[name] = Forest(name=name, limit=limit, ban=ban)

    def harvest(self, line: int, name: str, batch: str, qty: float, day: date) -> None:
        forest = self.forests.get(name)
        if forest is None:
            self._err(line, "未知林地", "采伐引用不存在的林地 '%s'（批次 %s）" % (name, batch))
            return
        if batch in forest.batches:
            self._err(line, "重复批次",
                      "林地 '%s' 批次 '%s' 重复采伐，忽略本次入账" % (name, batch))
            return
        if forest.suspended:
            self._err(line, "停采中采伐",
                      "林地 '%s' 处于停采状态，批次 '%s' 采伐 %g 被拒绝" % (name, batch, qty))
            return
        if qty <= 0:
            self._err(line, "参数错误", "林地 '%s' 采伐数量必须为正: %g" % (name, qty))
            return

        # 入账并级联更新剩余限额
        forest.cum_harvest += qty
        forest.batches.add(batch)

        if forest.ban and forest.ban.contains(day):
            self._err(line, "禁伐期采伐",
                      "林地 '%s' 于 %s 采伐 %g，处于禁伐期 %s"
                      % (name, day.isoformat(), qty, forest.ban))

        if forest.net > forest.limit:
            self._err(line, "超限额采伐",
                      "林地 '%s' 累计净采伐 %g 超过限额 %g，超采量 %g"
                      % (name, forest.net, forest.limit, forest.net - forest.limit))

    def recover(self, line: int, name: str, batch: str, qty: float, day: date) -> None:
        forest = self.forests.get(name)
        if forest is None:
            self._err(line, "未知林地", "恢复引用不存在的林地 '%s'（批次 %s）" % (name, batch))
            return
        if qty <= 0:
            self._err(line, "参数错误", "林地 '%s' 补种数量必须为正: %g" % (name, qty))
            return

        # 入账并级联更新剩余限额
        forest.cum_recovery += qty

        # 停采期间：累计整改补种量，达标则级联复采
        if forest.suspended:
            forest.recovered_since_suspend += qty
            if (forest.recovered_since_suspend >= forest.suspend_target
                    and forest.recovered_since_suspend > 0):
                forest.suspended = False
                forest.resume_count += 1
                self._note(line, "复采",
                           "林地 '%s' 停采后累计补种 %g 达到复采标准 %g，恢复采伐资格"
                           % (name, forest.recovered_since_suspend, forest.suspend_target))

    def inspect(self, line: int, name: str, result: str) -> None:
        forest = self.forests.get(name)
        if forest is None:
            self._err(line, "未知林地", "检查引用不存在的林地 '%s'" % name)
            return
        if result == "超采":
            if forest.suspended:
                self._note(line, "检查", "林地 '%s' 检查结论超采，但其已处于停采状态" % name)
                return
            forest.suspended = True
            forest.suspend_target = max(forest.net - forest.limit, 0.0)
            forest.recovered_since_suspend = 0.0
            self._note(line, "停采",
                       "林地 '%s' 检查结论为超采，立即停采；复采需补种 >= %g"
                       % (name, max(forest.suspend_target, 1)))
        elif result == "合规":
            self._note(line, "检查", "林地 '%s' 检查结论合规" % name)
        else:
            self._err(line, "参数错误",
                      "林地 '%s' 检查结果应为 合规/超采，实际为 '%s'" % (name, result))

    # ---- 输出 ----
    def report(self) -> str:
        out = []
        out.append("=" * 72)
        out.append("采伐状态")
        out.append("=" * 72)
        header = ("林地", "限额", "累计采伐", "累计补种", "净采伐", "剩余限额", "禁伐期", "状态")
        rows = []
        for f in self.forests.values():
            rows.append((f.name, _num(f.limit), _num(f.cum_harvest), _num(f.cum_recovery),
                         _num(f.net), _num(f.remaining),
                         str(f.ban) if f.ban else "-", f.status))
        out.extend(_table(header, rows))
        if not rows:
            out.append("（无林地定义）")

        out.append("")
        out.append("=" * 72)
        out.append("状态变更通知（%d 条）" % len(self.notices))
        out.append("=" * 72)
        out.extend(str(n) for n in self.notices) if self.notices else out.append("（无）")

        out.append("")
        out.append("=" * 72)
        out.append("错误清单（%d 条）" % len(self.errors))
        out.append("=" * 72)
        out.extend(str(e) for e in self.errors) if self.errors else out.append("（无）")
        return "\n".join(out)


# ---------------------------------------------------------------- 解析

def _num(x: float) -> str:
    return "%g" % x


def _display_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _display_width(s))


def _table(header, rows):
    cols = list(zip(*([header] + rows))) if rows else [(h,) for h in header]
    widths = [max(_display_width(str(c)) for c in col) for col in cols]
    lines = ["  ".join(_pad(str(c), w) for c, w in zip(header, widths))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(_pad(str(c), w) for c, w in zip(row, widths)))
    return lines


def _parse_ban(text: str, line: int, mon: Monitor) -> BanPeriod | None:
    if text == "-":
        return None
    try:
        start_s, end_s = text.split("~")
        start = tuple(int(p) for p in start_s.split("-"))
        end = tuple(int(p) for p in end_s.split("-"))
        if len(start) != 2 or len(end) != 2:
            raise ValueError
        for m, d in (start, end):
            date(2000, m, d)  # 校验月日合法（2000 为闰年，允许 02-29）
        return BanPeriod(start, end)
    except ValueError:
        mon._err(line, "参数错误", "禁伐期格式应为 MM-DD~MM-DD 或 -，实际为 '%s'" % text)
        return None


def _parse_float(text: str) -> float:
    return float(text)


def _parse_date(text: str) -> date:
    return date.fromisoformat(text)


def process(lines, mon: Monitor) -> None:
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        kind, args = parts[0], parts[1:]
        try:
            if kind == "林地":
                if len(args) != 3:
                    raise ValueError("林地 需要 3 个参数：名称 限额 禁伐期")
                mon.add_forest(lineno, args[0], _parse_float(args[1]),
                               _parse_ban(args[2], lineno, mon))
            elif kind == "采伐":
                if len(args) != 4:
                    raise ValueError("采伐 需要 4 个参数：林地 批次 数量 日期")
                mon.harvest(lineno, args[0], args[1], _parse_float(args[2]),
                            _parse_date(args[3]))
            elif kind == "恢复":
                if len(args) != 4:
                    raise ValueError("恢复 需要 4 个参数：林地 批次 补种数量 日期")
                mon.recover(lineno, args[0], args[1], _parse_float(args[2]),
                            _parse_date(args[3]))
            elif kind == "检查":
                if len(args) != 2:
                    raise ValueError("检查 需要 2 个参数：林地 结果(合规|超采)")
                mon.inspect(lineno, args[0], args[1])
            else:
                mon._err(lineno, "未知记录", "无法识别的记录类型 '%s'" % kind)
        except ValueError as exc:
            mon._err(lineno, "解析错误", "%s：%s" % (kind, exc))


# ---------------------------------------------------------------- 演示与入口

DEMO = """\
# 林地定义：名称 限额 禁伐期
林地 东山 1000 03-01~05-31
林地 西坡 500 -
林地 北岭 300 11-01~02-28

# 正常采伐
采伐 东山 P01 400 2026-01-10
采伐 东山 P02 300 2026-02-15
# 禁伐期采伐（东山 3-1~5-31 禁伐）
采伐 东山 P03 200 2026-04-01
# 恢复补种，级联回补限额
恢复 东山 R01 250 2026-06-01
# 累计净采伐超限（400+300+200-250=650 未超；继续采伐触发超限）
采伐 东山 P04 500 2026-07-01
# 重复批次
采伐 东山 P04 100 2026-07-02
# 引用不存在的林地
采伐 南山 X01 50 2026-07-03
恢复 南山 X02 50 2026-07-04
# 检查超采 -> 停采
检查 东山 超采
# 停采期间采伐被拒绝
采伐 东山 P05 100 2026-08-01
# 停采后补种达标 -> 复采（停采时超采 150，需补种 >=150）
恢复 东山 R02 100 2026-08-10
恢复 东山 R03 80 2026-08-20
# 复采后可正常采伐
采伐 东山 P06 100 2026-09-01
# 跨年禁伐期（北岭 11-1~2-28）
采伐 北岭 B01 100 2026-12-15
检查 西坡 合规
"""


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(__doc__)
        return 2
    if len(argv) == 2 and argv[1] == "--demo":
        print("（内置演示）\n")
        lines = DEMO.splitlines()
    elif len(argv) == 2:
        with open(argv[1], encoding="utf-8") as fh:
            lines = fh.readlines()
    elif not sys.stdin.isatty():
        lines = sys.stdin.readlines()
    else:
        print("（未提供输入文件，运行内置演示）\n")
        lines = DEMO.splitlines()

    mon = Monitor()
    process(lines, mon)
    print(mon.report())
    return 1 if mon.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
