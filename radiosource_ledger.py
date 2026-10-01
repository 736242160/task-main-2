#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""radiosource_ledger.py — 放射源台账核对工具（纯 Python 标准库，单文件）

用法:
    python3 radiosource_ledger.py 台账文件.txt
    python3 radiosource_ledger.py            # 从标准输入读取
    cat 台账.txt | python3 radiosource_ledger.py

输入格式（逐行一条指令，# 之后为注释，空行忽略；关键字中英文均可）:

    warehouse <库名> [容量]                      # 定义库房，容量省略时默认 10
    source    <编号> <I|II|III> <存放库>          # 定义放射源（初始状态：在库）
    checkout  <源编号> <部门> <使用人> <剂量限值> [记录号]   # 领用；记录号省略时自动生成 J1、J2…
    return    <记录号>                            # 归还，必须引用仍处领用状态的记录号
    inspect   <源编号> <剂量>                     # 检测（单位 μSv）
    stocktake <库名> <源编号1,源编号2,...>         # 实物盘点（编号可用逗号或空格分隔）

自定规则说明（判定依据）:

  1. 剂量超限规则：每类源有默认安全限值（μSv）—— I 类 50、II 类 200、III 类 1000
     （类别越危险限值越严）。若源正处于领用中，领用时登记的剂量限值同时生效，
     有效限值 = min(类别限值, 领用限值)。检测剂量 > 有效限值 即判超限。
     理由：领用限值是使用部门承诺的本次作业上限，类别限值是源的固有安全红线，
     两者取严可保证任何一条红线被突破都会被发现。
  2. 超限级联：超限源立即置为「停用」；若其仍有未关闭的领用记录，该记录被强制
     关闭（停止使用级联），并产生警报；此后对该源的领用请求一律报错。
  3. 源丢失判定：stocktake 盘点某库房时，台账状态为「在库」且存放于该库、但实物
     清单中不存在的源判定为「丢失」，级联置状态为「丢失」并发出警报；实物清单中
     出现但台账未登记在该库在库的编号，作为「账实不符」警报。
  4. 库房容量：warehouse 定义时给定容量（默认 10）。源登记入库或归还入库后，
     若该库「在库」源数量超过容量即报错。
  5. 状态机：在库 --领用--> 使用中 --归还--> 在库；任何在库/使用中源可因超限
     转「停用」、因盘点缺失转「丢失」。使用中源重复领用、归还引用不存在或已
     关闭的领用记录、引用不存在的源/库，均逐条报错且不改状态。
  6. 跨流状态延续：指令按文件顺序逐条执行，状态在领用流、归还流、检测流、
     盘点流之间持续传递。

输出：放射源状态表 + 错误与警报清单。存在「错误」级条目时退出码为 1，否则为 0。
"""

import argparse
import sys
from dataclasses import dataclass, field

CATEGORY_LIMITS = {"I": 50.0, "II": 200.0, "III": 1000.0}  # μSv，类别越危险限值越严
DEFAULT_CAPACITY = 10

STORED, IN_USE, SUSPENDED, LOST = "在库", "使用中", "停用", "丢失"


@dataclass
class Source:
    sid: str
    category: str
    warehouse: str
    status: str = STORED
    note: str = ""


@dataclass
class Warehouse:
    name: str
    capacity: int = DEFAULT_CAPACITY


@dataclass
class Checkout:
    rid: str
    sid: str
    dept: str
    user: str
    dose_limit: float
    open: bool = True


class Ledger:
    def __init__(self):
        self.sources = {}
        self.warehouses = {}
        self.checkouts = {}
        self.issues = []          # (行号, 级别, 消息)，级别为 错误 / 警报
        self._auto_rid = 0

    # ---- 报告辅助 ------------------------------------------------------
    def error(self, line, msg):
        self.issues.append((line, "错误", msg))

    def alert(self, line, msg):
        self.issues.append((line, "警报", msg))

    # ---- 容量检查 ------------------------------------------------------
    def _check_capacity(self, line, wname):
        wh = self.warehouses.get(wname)
        if wh is None:
            return
        n = sum(1 for s in self.sources.values()
                if s.warehouse == wname and s.status == STORED)
        if n > wh.capacity:
            self.error(line, f"库房「{wname}」容量超限：在库 {n} 个，容量 {wh.capacity} 个")

    # ---- 各指令 --------------------------------------------------------
    def add_warehouse(self, line, name, capacity=None):
        if name in self.warehouses:
            self.error(line, f"库房「{name}」重复定义")
            return
        cap = DEFAULT_CAPACITY
        if capacity is not None:
            try:
                cap = int(capacity)
                if cap <= 0:
                    raise ValueError
            except ValueError:
                self.error(line, f"库房「{name}」容量「{capacity}」不是正整数，已按默认 {DEFAULT_CAPACITY} 处理")
                cap = DEFAULT_CAPACITY
        self.warehouses[name] = Warehouse(name, cap)

    def add_source(self, line, sid, category, wname):
        if sid in self.sources:
            self.error(line, f"放射源「{sid}」重复定义")
            return
        if category not in CATEGORY_LIMITS:
            self.error(line, f"放射源「{sid}」类别「{category}」非法（应为 I/II/III）")
            return
        if wname not in self.warehouses:
            self.error(line, f"放射源「{sid}」引用不存在的库房「{wname}」")
            return
        self.sources[sid] = Source(sid, category, wname)
        self._check_capacity(line, wname)

    def checkout(self, line, sid, dept, user, dose_limit, rid=None):
        src = self.sources.get(sid)
        if src is None:
            self.error(line, f"领用引用不存在的放射源「{sid}」")
            return
        try:
            limit = float(dose_limit)
            if limit <= 0:
                raise ValueError
        except ValueError:
            self.error(line, f"领用「{sid}」的剂量限值「{dose_limit}」不是正数")
            return
        if src.status == IN_USE:
            self.error(line, f"放射源「{sid}」正在使用中，重复领用被拒绝")
            return
        if src.status == SUSPENDED:
            self.error(line, f"放射源「{sid}」已停用（曾剂量超限），禁止领用")
            return
        if src.status == LOST:
            self.error(line, f"放射源「{sid}」已丢失，禁止领用")
            return
        if rid is None:
            self._auto_rid += 1
            rid = f"J{self._auto_rid}"
        if rid in self.checkouts:
            self.error(line, f"领用记录号「{rid}」重复")
            return
        self.checkouts[rid] = Checkout(rid, sid, dept, user, limit)
        src.status = IN_USE
        src.note = f"领用记录 {rid}（{dept}/{user}）"

    def give_back(self, line, rid):
        co = self.checkouts.get(rid)
        if co is None:
            self.error(line, f"归还引用不存在的领用记录「{rid}」")
            return
        if not co.open:
            self.error(line, f"归还引用的领用记录「{rid}」已关闭（源 {co.sid}）")
            return
        co.open = False
        src = self.sources[co.sid]
        if src.status == IN_USE:
            src.status = STORED
            src.note = ""
            self._check_capacity(line, src.warehouse)
        else:
            self.alert(line, f"领用记录「{rid}」关闭时源「{co.sid}」状态为「{src.status}」，未回写在库状态")

    def inspect(self, line, sid, dose):
        src = self.sources.get(sid)
        if src is None:
            self.error(line, f"检测引用不存在的放射源「{sid}」")
            return
        try:
            dose = float(dose)
            if dose < 0:
                raise ValueError
        except ValueError:
            self.error(line, f"检测「{sid}」的剂量「{dose}」不是非负数")
            return
        if src.status == LOST:
            self.error(line, f"放射源「{sid}」已丢失，无法检测")
            return
        limit = CATEGORY_LIMITS[src.category]
        active = next((c for c in self.checkouts.values() if c.sid == sid and c.open), None)
        if active is not None:
            limit = min(limit, active.dose_limit)
        if dose > limit:
            self.error(line, f"放射源「{sid}」检测剂量 {dose:g} μSv 超过有效限值 {limit:g} μSv")
            src.status = SUSPENDED
            src.note = f"剂量超限（{dose:g} μSv > {limit:g} μSv）"
            if active is not None:
                active.open = False
                self.alert(line, f"级联：源「{sid}」超限停用，领用记录「{active.rid}」"
                                 f"（{active.dept}/{active.user}）被强制关闭，立即停止使用")

    def stocktake(self, line, wname, physical_ids):
        if wname not in self.warehouses:
            self.error(line, f"盘点引用不存在的库房「{wname}」")
            return
        physical = set(physical_ids)
        for src in self.sources.values():
            if src.warehouse == wname and src.status == STORED and src.sid not in physical:
                src.status = LOST
                src.note = "盘点缺失"
                self.error(line, f"放射源「{src.sid}」台账在库（{wname}）但实物缺失，判定丢失")
                self.alert(line, f"级联警报：源「{src.sid}」（{src.category} 类）丢失，"
                                 f"请立即启动寻源与安保应急流程")
        for pid in sorted(physical):
            src = self.sources.get(pid)
            if src is None:
                self.alert(line, f"盘点实物「{pid}」在台账中不存在，账实不符")
            elif not (src.warehouse == wname and src.status == STORED):
                self.alert(line, f"盘点实物「{pid}」台账状态为「{src.status}」"
                                 f"（库 {src.warehouse}），与实盘库「{wname}」不符")


KEYWORDS = {
    "warehouse": "warehouse", "库": "warehouse", "库房": "warehouse",
    "source": "source", "源": "source", "放射源": "source",
    "checkout": "checkout", "领用": "checkout",
    "return": "return", "归还": "return",
    "inspect": "inspect", "检测": "inspect",
    "stocktake": "stocktake", "盘点": "stocktake",
}


def run(text, ledger):
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd = KEYWORDS.get(parts[0].lower())
        if cmd is None:
            ledger.error(lineno, f"无法识别的指令「{parts[0]}」")
            continue
        args = parts[1:]
        try:
            if cmd == "warehouse":
                if len(args) not in (1, 2):
                    raise ValueError("格式：warehouse <库名> [容量]")
                ledger.add_warehouse(lineno, args[0], args[1] if len(args) == 2 else None)
            elif cmd == "source":
                if len(args) != 3:
                    raise ValueError("格式：source <编号> <I|II|III> <存放库>")
                ledger.add_source(lineno, *args)
            elif cmd == "checkout":
                if len(args) not in (4, 5):
                    raise ValueError("格式：checkout <源编号> <部门> <使用人> <剂量限值> [记录号]")
                ledger.checkout(lineno, *args)
            elif cmd == "return":
                if len(args) != 1:
                    raise ValueError("格式：return <记录号>")
                ledger.give_back(lineno, args[0])
            elif cmd == "inspect":
                if len(args) != 2:
                    raise ValueError("格式：inspect <源编号> <剂量>")
                ledger.inspect(lineno, *args)
            elif cmd == "stocktake":
                if len(args) < 2:
                    raise ValueError("格式：stocktake <库名> <源编号...>")
                ids = [x for tok in args[1:] for x in tok.split(",") if x]
                ledger.stocktake(lineno, args[0], ids)
        except ValueError as exc:
            ledger.error(lineno, str(exc))


def report(ledger, out):
    print("=" * 60, file=out)
    print("放射源状态", file=out)
    print("=" * 60, file=out)
    if not ledger.sources:
        print("（无放射源定义）", file=out)
    for s in ledger.sources.values():
        note = f"　备注：{s.note}" if s.note else ""
        print(f"  {s.sid}\t{s.category} 类\t{s.status}\t库:{s.warehouse}{note}", file=out)
    print(file=out)
    print("=" * 60, file=out)
    print("错误与警报清单", file=out)
    print("=" * 60, file=out)
    if not ledger.issues:
        print("  无错误，台账逐笔相符。", file=out)
    for lineno, level, msg in ledger.issues:
        print(f"  [行 {lineno:>3}] [{level}] {msg}", file=out)
    n_err = sum(1 for _, lv, _ in ledger.issues if lv == "错误")
    n_alr = sum(1 for _, lv, _ in ledger.issues if lv == "警报")
    print(file=out)
    print(f"合计：错误 {n_err} 条，警报 {n_alr} 条。", file=out)
    return 1 if n_err else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="放射源台账核对工具（纯标准库单文件）")
    ap.add_argument("file", nargs="?", help="台账指令文件；省略时从标准输入读取")
    args = ap.parse_args(argv)
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    ledger = Ledger()
    run(text, ledger)
    return report(ledger, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
