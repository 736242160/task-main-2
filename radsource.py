#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""radsource.py — 放射源台账管理工具（纯 Python 标准库，单文件）

用法:
    python3 radsource.py 台账文件.txt      # 处理台账事件文件
    python3 radsource.py - < 台账文件.txt  # 从标准输入读取
    python3 radsource.py --sample          # 打印示例输入文件

输入格式（每行一条事件，# 之后为注释，空行忽略）:
    WAREHOUSE <库房名称> [容量]                 定义库房（容量缺省 10）
    SOURCE    <源编号> <类别I|II|III> <库房>    定义放射源（入库存放）
    CHECKOUT  <源编号> <部门> <使用人> <剂量限值>  领用（生成领用记录，序号从 1 起）
    RETURN    <领用记录序号>                    归还（按领用记录序号引用）
    INSPECT   <源编号> <剂量>                   检测；剂量为负数表示实物盘点缺失

自定规则（判定依据与理由）:
  1. 检测剂量超限：类别上限 I=100 / II=200 / III=500（单位自定，μSv/h）。
     理由：I 类源危险程度最高，监管裕度应最大，故上限最严；类别越低上限越宽。
     若源处于"使用中"，同时受该次领用声明的剂量限值约束，实际限值取两者较小者。
  2. 超限级联：超限源立即"停用"；若当时有未关闭领用记录，该记录被"异常终止"，
     此后对该源的领用/检测均报错。
  3. 源丢失判定：INSPECT 剂量为负数视为一次实物盘点且实物缺失。若台账状态为
     "在库"，判定为源丢失并触发警报级联（状态置"丢失"，释放库房占用）；
     若台账为"使用中"，同样置"丢失"并异常终止其领用记录。
  4. 库房容量：每库默认容量 10，可在 WAREHOUSE 行覆盖。在库源数量超过容量即报错
     （定义入库与归还入库时均检查）。

输出: 放射源状态表、库房状态表、领用记录表、错误与警报清单。
退出码: 0=无问题, 1=存在错误或警报, 2=输入文件无法读取。
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from dataclasses import dataclass

STATUS_IN_STORAGE = "在库"
STATUS_IN_USE = "使用中"
STATUS_SUSPENDED = "停用"
STATUS_LOST = "丢失"

CATEGORIES = ("I", "II", "III")
CATEGORY_DOSE_CEILING = {"I": 100.0, "II": 200.0, "III": 500.0}
DEFAULT_WAREHOUSE_CAPACITY = 10

CHECKOUT_OPEN = "有效"
CHECKOUT_RETURNED = "已归还"
CHECKOUT_ABORTED = "异常终止"


@dataclass
class Source:
    sid: str
    category: str
    warehouse: str
    status: str = STATUS_IN_STORAGE
    checkout_id: int | None = None


@dataclass
class Warehouse:
    name: str
    capacity: int = DEFAULT_WAREHOUSE_CAPACITY


@dataclass
class Checkout:
    cid: int
    source: str
    department: str
    user: str
    dose_limit: float
    line: int
    state: str = CHECKOUT_OPEN


class Ledger:
    def __init__(self) -> None:
        self.sources: dict[str, Source] = {}
        self.warehouses: dict[str, Warehouse] = {}
        self.checkouts: list[Checkout] = []
        self.problems: list[tuple[str, int, str]] = []  # (级别, 行号, 消息)

    # ---- 报告收集 ------------------------------------------------------
    def error(self, line: int, msg: str) -> None:
        self.problems.append(("错误", line, msg))

    def alarm(self, line: int, msg: str) -> None:
        self.problems.append(("警报", line, msg))

    # ---- 库房容量 ------------------------------------------------------
    def occupancy(self, warehouse: str) -> int:
        return sum(
            1
            for s in self.sources.values()
            if s.warehouse == warehouse and s.status == STATUS_IN_STORAGE
        )

    def check_capacity(self, line: int, warehouse: str) -> None:
        wh = self.warehouses[warehouse]
        used = self.occupancy(warehouse)
        if used > wh.capacity:
            self.error(
                line,
                f"库房容量超限：库房 {warehouse} 容量 {wh.capacity}，当前在库 {used}",
            )

    # ---- 事件处理 ------------------------------------------------------
    def on_warehouse(self, line: int, args: list[str]) -> None:
        if len(args) not in (1, 2):
            self.error(line, "WAREHOUSE 格式应为：WAREHOUSE <名称> [容量]")
            return
        name = args[0]
        if name in self.warehouses:
            self.error(line, f"库房重复定义：{name}")
            return
        capacity = DEFAULT_WAREHOUSE_CAPACITY
        if len(args) == 2:
            try:
                capacity = int(args[1])
                if capacity <= 0:
                    raise ValueError
            except ValueError:
                self.error(line, f"库房容量非法：{args[1]}（应为正整数）")
                return
        self.warehouses[name] = Warehouse(name, capacity)

    def on_source(self, line: int, args: list[str]) -> None:
        if len(args) != 3:
            self.error(line, "SOURCE 格式应为：SOURCE <编号> <类别I|II|III> <库房>")
            return
        sid, category, warehouse = args
        if sid in self.sources:
            self.error(line, f"放射源重复定义：{sid}")
            return
        if category not in CATEGORIES:
            self.error(line, f"源 {sid} 类别非法：{category}（应为 I/II/III）")
            return
        if warehouse not in self.warehouses:
            self.error(line, f"源 {sid} 引用不存在的库房：{warehouse}")
            return
        self.sources[sid] = Source(sid, category, warehouse)
        self.check_capacity(line, warehouse)

    def on_checkout(self, line: int, args: list[str]) -> None:
        if len(args) != 4:
            self.error(line, "CHECKOUT 格式应为：CHECKOUT <源编号> <部门> <使用人> <剂量限值>")
            return
        sid, department, user, limit_text = args
        source = self.sources.get(sid)
        if source is None:
            self.error(line, f"领用引用不存在的源：{sid}")
            return
        try:
            dose_limit = float(limit_text)
            if dose_limit <= 0:
                raise ValueError
        except ValueError:
            self.error(line, f"领用剂量限值非法：{limit_text}（应为正数）")
            return
        if source.status == STATUS_IN_USE:
            self.error(
                line,
                f"重复领用：源 {sid} 正在使用中（领用记录 #{source.checkout_id}），"
                "须先归还才能再次领用",
            )
            return
        if source.status == STATUS_SUSPENDED:
            self.error(line, f"领用被拒绝：源 {sid} 已因检测超限停用")
            return
        if source.status == STATUS_LOST:
            self.error(line, f"领用被拒绝：源 {sid} 已丢失")
            return
        cid = len(self.checkouts) + 1
        self.checkouts.append(Checkout(cid, sid, department, user, dose_limit, line))
        source.status = STATUS_IN_USE
        source.checkout_id = cid

    def on_return(self, line: int, args: list[str]) -> None:
        if len(args) != 1:
            self.error(line, "RETURN 格式应为：RETURN <领用记录序号>")
            return
        try:
            cid = int(args[0])
        except ValueError:
            self.error(line, f"归还引用的领用记录序号非法：{args[0]}")
            return
        if cid < 1 or cid > len(self.checkouts):
            self.error(line, f"归还引用不存在的领用记录：#{cid}")
            return
        record = self.checkouts[cid - 1]
        if record.state != CHECKOUT_OPEN:
            self.error(
                line,
                f"归还无效：领用记录 #{cid}（源 {record.source}）已处于"
                f"「{record.state}」状态",
            )
            return
        record.state = CHECKOUT_RETURNED
        source = self.sources[record.source]
        source.status = STATUS_IN_STORAGE
        source.checkout_id = None
        self.check_capacity(line, source.warehouse)

    def on_inspect(self, line: int, args: list[str]) -> None:
        if len(args) != 2:
            self.error(line, "INSPECT 格式应为：INSPECT <源编号> <剂量>")
            return
        sid, dose_text = args
        source = self.sources.get(sid)
        if source is None:
            self.error(line, f"检测引用不存在的源：{sid}")
            return
        try:
            dose = float(dose_text)
        except ValueError:
            self.error(line, f"检测剂量非法：{dose_text}")
            return

        if dose < 0:  # 负数剂量 = 实物盘点缺失（自定判定）
            if source.status == STATUS_LOST:
                self.error(line, f"重复报失：源 {sid} 已处于丢失状态")
                return
            if source.status == STATUS_IN_USE:
                record = self.checkouts[source.checkout_id - 1]
                record.state = CHECKOUT_ABORTED
                self.alarm(
                    line,
                    f"源丢失：源 {sid} 台账为使用中（领用记录 #{record.cid}，"
                    f"部门 {record.department}，使用人 {record.user}），实物盘点缺失；"
                    "领用记录已异常终止，状态级联置为丢失",
                )
            else:
                self.alarm(
                    line,
                    f"源丢失：源 {sid} 台账为{source.status}（库房 {source.warehouse}），"
                    "实物盘点缺失；状态级联置为丢失",
                )
            source.status = STATUS_LOST
            source.checkout_id = None
            return

        if source.status == STATUS_LOST:
            self.error(line, f"检测无效：源 {sid} 已丢失，无法检测")
            return

        ceiling = CATEGORY_DOSE_CEILING[source.category]
        effective = ceiling
        limit_basis = f"{source.category} 类上限 {ceiling:g}"
        if source.status == STATUS_IN_USE and source.checkout_id is not None:
            record = self.checkouts[source.checkout_id - 1]
            if record.dose_limit < effective:
                effective = record.dose_limit
                limit_basis = f"领用记录 #{record.cid} 限值 {record.dose_limit:g}"

        if dose > effective:
            self.error(
                line,
                f"检测剂量超限：源 {sid} 检测剂量 {dose:g} 超过限值 {effective:g}"
                f"（{limit_basis}）",
            )
            cascade = f"源 {sid} 已级联停止使用（状态置为停用）"
            if source.status == STATUS_IN_USE and source.checkout_id is not None:
                record = self.checkouts[source.checkout_id - 1]
                record.state = CHECKOUT_ABORTED
                cascade += f"，领用记录 #{record.cid} 异常终止"
                source.checkout_id = None
            source.status = STATUS_SUSPENDED
            self.alarm(line, cascade)

    # ---- 驱动 ----------------------------------------------------------
    def dispatch(self, line: int, keyword: str, args: list[str]) -> None:
        handler = {
            "WAREHOUSE": self.on_warehouse,
            "SOURCE": self.on_source,
            "CHECKOUT": self.on_checkout,
            "RETURN": self.on_return,
            "INSPECT": self.on_inspect,
        }.get(keyword)
        if handler is None:
            self.error(line, f"无法识别的事件类型：{keyword}")
            return
        handler(line, args)

    def run(self, text: str) -> None:
        for lineno, raw in enumerate(text.splitlines(), 1):
            row = raw.split("#", 1)[0].strip()
            if not row:
                continue
            parts = row.split()
            self.dispatch(lineno, parts[0].upper(), parts[1:])


# ---- 输出 --------------------------------------------------------------

def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(1, width - display_width(text))


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], display_width(cell))
    lines = ["  ".join(pad(h, widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(pad(c, widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


def render_report(ledger: Ledger) -> str:
    out: list[str] = []

    out.append("===== 放射源状态 =====")
    rows = []
    for s in ledger.sources.values():
        current = f"#{s.checkout_id}" if s.checkout_id is not None else "-"
        rows.append([s.sid, s.category, s.warehouse, s.status, current])
    out.append(render_table(["编号", "类别", "存放库", "状态", "当前领用"], rows) if rows else "（无）")

    out.append("")
    out.append("===== 库房状态 =====")
    rows = []
    for w in ledger.warehouses.values():
        used = ledger.occupancy(w.name)
        flag = "超限" if used > w.capacity else "正常"
        rows.append([w.name, str(w.capacity), str(used), flag])
    out.append(render_table(["库房", "容量", "在库数量", "状态"], rows) if rows else "（无）")

    out.append("")
    out.append("===== 领用记录 =====")
    rows = []
    for c in ledger.checkouts:
        rows.append([f"#{c.cid}", c.source, c.department, c.user, f"{c.dose_limit:g}", c.state])
    out.append(
        render_table(["序号", "源", "部门", "使用人", "剂量限值", "状态"], rows) if rows else "（无）"
    )

    out.append("")
    out.append("===== 错误与警报清单 =====")
    if ledger.problems:
        for level, line, msg in ledger.problems:
            out.append(f"[{level}] 第 {line} 行: {msg}")
    else:
        out.append("（无）")

    errors = sum(1 for p in ledger.problems if p[0] == "错误")
    alarms = sum(1 for p in ledger.problems if p[0] == "警报")
    out.append("")
    out.append(f"统计: 错误 {errors} 条, 警报 {alarms} 条")
    return "\n".join(out)


SAMPLE = """\
# 示例台账
WAREHOUSE 主库 3
WAREHOUSE 分库

SOURCE S001 I   主库
SOURCE S002 II  主库
SOURCE S003 III 主库
SOURCE S004 III 主库
SOURCE S005 II  分库

CHECKOUT S001 放射科 张三 80
CHECKOUT S001 放疗科 李四 60      # 重复领用 -> 错误
RETURN 1
CHECKOUT S001 放疗科 李四 60
INSPECT S001 150                  # 超过 I 类上限 100 -> 超限并停用
RETURN 2                          # 记录 #2 已异常终止 -> 错误
INSPECT S002 120                  # II 类上限 200，未超限
INSPECT S003 -1                   # 实物盘点缺失 -> 源丢失警报
RETURN 9                          # 不存在的领用记录 -> 错误
CHECKOUT S003 放射科 王五 50      # 源已丢失 -> 错误
CHECKOUT S999 放射科 王五 50      # 不存在的源 -> 错误
SOURCE S006 II 幽灵库             # 不存在的库房 -> 错误
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="放射源台账管理工具：处理事件流并输出源状态与错误报告"
    )
    parser.add_argument("input", nargs="?", help="台账事件文件（缺省或 - 表示标准输入）")
    parser.add_argument("--sample", action="store_true", help="打印示例输入文件后退出")
    args = parser.parse_args(argv)

    if args.sample:
        sys.stdout.write(SAMPLE)
        return 0

    try:
        if args.input in (None, "-"):
            text = sys.stdin.read()
        else:
            with open(args.input, "r", encoding="utf-8") as fh:
                text = fh.read()
    except OSError as exc:
        print(f"无法读取输入：{exc}", file=sys.stderr)
        return 2

    ledger = Ledger()
    ledger.run(text)
    print(render_report(ledger))
    return 1 if ledger.problems else 0


if __name__ == "__main__":
    sys.exit(main())
