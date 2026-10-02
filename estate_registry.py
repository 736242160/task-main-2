#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
estate_registry.py — 不动产产权份额与抵押链登记流处理工具（纯 Python 标准库，单文件）

用法:
    python3 estate_registry.py 输入文件      # 处理登记文件
    python3 estate_registry.py --demo        # 运行内置示例
    cat 输入文件 | python3 estate_registry.py

输入格式（每行一条，# 之后为注释，空行忽略；行号用于错误定位）:
    房产 <房产号> <人=份额%> ...             定义房产与共有人份额
    抵押 <抵押号> <房产号> <金额> <顺位>      登记抵押（按行顺序生效）
    过户 <房产号> <出让人->受让人> <份额%>    过户登记
    析产 <房产号> <新房产号:比例%> ...        析产拆分（比例合计须为 100）
    合并 <新房产号> <房产号> <房产号> ...     合并登记
    解押 <抵押号>                            解除抵押

自定规则说明:
  1. 重复抵押: 同一抵押号在仍有有效抵押时重复登记，或同一房产已存在金额完全相同的
     有效抵押，视为重复录入并拒绝。理由: 正常的一房多押（顺位抵押）应以不同抵押号、
     不同金额区分；抵押号重复或(房产,金额)完全一致几乎必然是重复提交，放行会污染抵押链。
  2. 登记顺序: 任何登记引用的房产/抵押必须已定义且处于有效状态；新建编号不得与现存
     编号冲突；出让人必须是共有人。违反即视为登记顺序错误，该条登记不生效。
  3. 过户: 房产存在未解押抵押时禁止过户（先解押再过户）；过户份额不得超过出让人
     持有份额，否则整条登记不生效并报告(房产, 共有人, 超量)。
  4. 析产: 共有人份额与未解押抵押按拆分比例级联到各新房产，抵押顺位保持不变，
     解押任一抵押号即解除其全部分摊；原房产注销（状态=已析产）。
  5. 合并: 共有人份额按人累加（合计不得超过 100%）；各来源房产的抵押链按
     (原顺位, 合并指令中房产顺序, 抵押号) 重排为新顺位 1..k，同一抵押在多个来源
     房产上的分摊金额累加；来源房产注销（状态=已合并）。
  6. 状态跨登记延续: 全部登记按文件行顺序在同一登记簿上依次生效。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from fractions import Fraction

HUNDRED = Fraction(100)


# ---------- 数据模型 ----------

@dataclass
class Property:
    pid: str
    shares: dict = field(default_factory=dict)  # 共有人 -> 份额(%)
    status: str = "有效"                         # 有效 / 已析产 / 已合并


@dataclass
class Mortgage:
    mid: str
    total: Fraction                 # 抵押总金额
    priority: int                   # 顺位（全局，合并时可能重排）
    allocations: dict = field(default_factory=dict)  # 房产号 -> 分摊金额
    active: bool = True


@dataclass
class Error:
    line_no: int
    category: str
    detail: str


# ---------- 工具函数 ----------

def fmt_num(x: Fraction) -> str:
    if x.denominator == 1:
        return str(x.numerator)
    return f"{float(x):.6f}".rstrip("0").rstrip(".")


def parse_num(token: str) -> Fraction:
    return Fraction(token)  # 支持 整数 / 小数 / 分数(如 1/3)


# ---------- 登记簿 ----------

class Registry:
    def __init__(self):
        self.properties: dict[str, Property] = {}
        self.mortgages: dict[str, Mortgage] = {}
        self.errors: list[Error] = []
        self.logs: list[tuple[int, str]] = []

    def error(self, line_no, category, detail):
        self.errors.append(Error(line_no, category, detail))

    def log(self, line_no, msg):
        self.logs.append((line_no, msg))

    def active_chain(self, pid) -> list[Mortgage]:
        """某房产当前有效抵押链，按 (顺位, 抵押号) 排序。"""
        ms = [m for m in self.mortgages.values() if m.active and pid in m.allocations]
        return sorted(ms, key=lambda m: (m.priority, m.mid))

    def get_active_property(self, line_no, pid) -> Property | None:
        prop = self.properties.get(pid)
        if prop is None:
            self.error(line_no, "登记顺序错误", f"房产 {pid} 尚未定义")
            return None
        if prop.status != "有效":
            self.error(line_no, "登记顺序错误",
                       f"房产 {pid} {prop.status}，不可再作为登记对象")
            return None
        return prop

    # ----- 房产定义 -----
    def op_property(self, line_no, pid, share_tokens):
        if pid in self.properties:
            self.error(line_no, "登记顺序错误", f"房产 {pid} 重复定义")
            return
        if not share_tokens:
            raise ValueError("房产定义缺少共有人份额")
        shares = {}
        for token in share_tokens:
            if "=" not in token:
                raise ValueError(f"共有人份额 “{token}” 应为 人=份额 格式")
            name, s = token.split("=", 1)
            shares[name] = parse_num(s)
        total = sum(shares.values(), Fraction(0))
        if total > HUNDRED:
            self.error(line_no, "份额合计超百分百",
                       f"房产 {pid} 共有人份额合计 {fmt_num(total)}% > 100%，定义被拒绝")
            return
        self.properties[pid] = Property(pid, shares)
        body = "、".join(f"{n}={fmt_num(s)}%" for n, s in shares.items())
        self.log(line_no, f"房产 {pid} 定义成功：{body}")

    # ----- 抵押登记 -----
    def op_mortgage(self, line_no, mid, pid, amount, priority):
        if amount <= 0:
            raise ValueError("抵押金额必须为正数")
        old = self.mortgages.get(mid)
        if old is not None and old.active:
            self.error(line_no, "重复抵押",
                       f"抵押号 {mid} 已存在有效抵押，重复登记被拒绝")
            return
        prop = self.get_active_property(line_no, pid)
        if prop is None:
            return
        for m in self.active_chain(pid):
            if m.total == amount:
                self.error(line_no, "重复抵押",
                           f"房产 {pid} 已存在金额相同的有效抵押 {m.mid}"
                           f"（金额 {fmt_num(amount)}），疑似重复录入，登记被拒绝")
                return
        self.mortgages[mid] = Mortgage(mid, amount, priority, {pid: amount})
        self.log(line_no, f"抵押 {mid} 登记成功：房产 {pid} 金额 {fmt_num(amount)} 顺位 {priority}")

    # ----- 解押 -----
    def op_release(self, line_no, mid):
        m = self.mortgages.get(mid)
        if m is None or not m.active:
            self.error(line_no, "解押抵押不存在", f"抵押 {mid} 不存在或已解押")
            return
        m.active = False
        m.allocations.clear()
        self.log(line_no, f"解押成功：{mid}（全部分摊一并解除）")

    # ----- 过户 -----
    def op_transfer(self, line_no, pid, frm, to, share):
        if share <= 0:
            raise ValueError("过户份额必须为正数")
        prop = self.get_active_property(line_no, pid)
        if prop is None:
            return
        chain = self.active_chain(pid)
        if chain:
            ids = "、".join(m.mid for m in chain)
            self.error(line_no, "存在未解押抵押",
                       f"房产 {pid} 存在未解押抵押（{ids}），过户被拒绝")
            return
        held = prop.shares.get(frm)
        if held is None:
            self.error(line_no, "登记顺序错误",
                       f"{frm} 不是房产 {pid} 的共有人，过户被拒绝")
            return
        if share > held:
            self.error(line_no, "过户份额超持有",
                       f"房产 {pid} 共有人 {frm} 持有 {fmt_num(held)}%，"
                       f"欲过户 {fmt_num(share)}%，超量 {fmt_num(share - held)}%")
            return
        prop.shares[frm] -= share
        if prop.shares[frm] == 0:
            del prop.shares[frm]
        prop.shares[to] = prop.shares.get(to, Fraction(0)) + share
        self.log(line_no, f"过户成功：{pid} {frm} -> {to} {fmt_num(share)}%")

    # ----- 析产（级联拆分） -----
    def op_split(self, line_no, pid, parts):
        if len(parts) < 2:
            raise ValueError("析产至少拆分为 2 个新房产")
        prop = self.get_active_property(line_no, pid)
        if prop is None:
            return
        total = sum((pct for _, pct in parts), Fraction(0))
        if total != HUNDRED:
            self.error(line_no, "析产比例错误",
                       f"房产 {pid} 拆分比例合计 {fmt_num(total)}% ≠ 100%，析产被拒绝")
            return
        for new_pid, _ in parts:
            if new_pid in self.properties:
                self.error(line_no, "登记顺序错误",
                           f"新房产编号 {new_pid} 已存在，析产被拒绝")
                return
        for new_pid, pct in parts:
            shares = {n: s * pct / HUNDRED for n, s in prop.shares.items()}
            self.properties[new_pid] = Property(new_pid, shares)
        cascaded = []
        for m in self.active_chain(pid):
            alloc = m.allocations.pop(pid)
            for new_pid, pct in parts:
                m.allocations[new_pid] = alloc * pct / HUNDRED
            cascaded.append(m.mid)
        prop.status = "已析产"
        tgt = "、".join(f"{np}({fmt_num(pct)}%)" for np, pct in parts)
        msg = f"析产成功：{pid} 拆分为 {tgt}；原房产注销"
        if cascaded:
            msg += f"；抵押 {'、'.join(cascaded)} 已按比例级联且顺位不变"
        self.log(line_no, msg)

    # ----- 合并（级联合并） -----
    def op_merge(self, line_no, new_pid, src_pids):
        if len(src_pids) < 2:
            raise ValueError("合并至少需要 2 个来源房产")
        if new_pid in self.properties:
            self.error(line_no, "登记顺序错误", f"新房产编号 {new_pid} 已存在，合并被拒绝")
            return
        props = []
        for sp in src_pids:
            p = self.get_active_property(line_no, sp)
            if p is None:
                return
            props.append(p)
        merged: dict[str, Fraction] = {}
        for p in props:
            for name, s in p.shares.items():
                merged[name] = merged.get(name, Fraction(0)) + s
        total = sum(merged.values(), Fraction(0))
        if total > HUNDRED:
            self.error(line_no, "份额合计超百分百",
                       f"合并后房产 {new_pid} 共有人份额合计 {fmt_num(total)}% > 100%，合并被拒绝")
            return
        # 抵押链级联：按 (原顺位, 合并指令中房产顺序, 抵押号) 重排为新顺位 1..k
        ordered, seen = [], set()
        for sp in src_pids:
            for m in self.active_chain(sp):
                if m.mid not in seen:
                    seen.add(m.mid)
                    ordered.append(m)
        for idx, m in enumerate(ordered, 1):
            m.priority = idx
            m.allocations[new_pid] = sum(
                (m.allocations.pop(sp) for sp in src_pids if sp in m.allocations),
                Fraction(0))
        self.properties[new_pid] = Property(new_pid, merged)
        for p in props:
            p.status = "已合并"
        msg = f"合并成功：{'、'.join(src_pids)} 合并为 {new_pid}；来源房产注销"
        if ordered:
            chain = "、".join(f"{m.mid}(顺位{m.priority})" for m in ordered)
            msg += f"；抵押链延续并重排：{chain}"
        self.log(line_no, msg)

    # ----- 行分发 -----
    def process_line(self, line_no, line):
        tokens = line.split()
        op = tokens[0]
        try:
            if op == "房产":
                self.op_property(line_no, tokens[1], tokens[2:])
            elif op == "抵押":
                self.op_mortgage(line_no, tokens[1], tokens[2],
                                 parse_num(tokens[3]), int(tokens[4]))
            elif op == "过户":
                frm, to = tokens[2].split("->")
                self.op_transfer(line_no, tokens[1], frm, to, parse_num(tokens[3]))
            elif op == "析产":
                parts = []
                for t in tokens[2:]:
                    npid, pct = t.split(":")
                    parts.append((npid, parse_num(pct)))
                self.op_split(line_no, tokens[1], parts)
            elif op == "合并":
                self.op_merge(line_no, tokens[1], tokens[2:])
            elif op == "解押":
                self.op_release(line_no, tokens[1])
            else:
                self.error(line_no, "格式错误", f"未知登记类型 “{op}”")
        except (IndexError, ValueError) as exc:
            self.error(line_no, "格式错误", f"无法解析「{line}」（{exc}）")

    # ----- 输出 -----
    def render(self) -> str:
        out = ["===== 登记处理日志 ====="]
        for line_no, msg in self.logs:
            out.append(f"行{line_no:>3} [成功] {msg}")
        out += ["", "===== 最终产权状态 ====="]
        for pid, prop in self.properties.items():
            out.append(f"房产 {pid}（{prop.status}）")
            if prop.status != "有效":
                continue
            shares = "、".join(f"{n}={fmt_num(s)}%" for n, s in prop.shares.items())
            out.append(f"  共有人: {shares}")
            chain = self.active_chain(pid)
            if not chain:
                out.append("  抵押链: （无）")
            else:
                out.append("  抵押链:")
                for m in chain:
                    out.append(f"    顺位{m.priority}: {m.mid} "
                               f"分摊金额 {fmt_num(m.allocations[pid])}"
                               f"（抵押总额 {fmt_num(m.total)}）")
        out += ["", f"===== 错误报告（共 {len(self.errors)} 条）====="]
        for e in self.errors:
            out.append(f"行{e.line_no:>3} [{e.category}] {e.detail}")
        return "\n".join(out)


# ---------- 内置示例 ----------

DEMO = """\
# ---- 房产定义 ----
房产 P1 张三=60 李四=40
房产 P2 王五=70 赵六=40
# ---- 抵押权定义 ----
抵押 M1 P1 1000000 1
抵押 M2 P1 500000 2
抵押 M2 P1 700000 2
抵押 M3 P1 500000 3
抵押 M8 P9 100 1
# ---- 登记流 ----
过户 P1 张三->王五 10
析产 P1 P1A:50 P1B:50
抵押 M4 P1B 300000 3
解押 M1
解押 M1
过户 P1A 张三->王五 10
解押 M2
过户 P1A 张三->王五 10
解押 M4
过户 P1B 李四->赵六 45
抵押 M5 P1B 300000 1
合并 P1C P1A P1B
房产 P3 钱七=100
析产 P3 P3A:60 P3B:30
过户 P3 钱七->孙八 20
合并 P4 P1C P3
解押 M7
"""


def main(argv):
    if "--demo" in argv:
        text = DEMO
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    reg = Registry()
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        reg.process_line(i, line)
    print(reg.render())


if __name__ == "__main__":
    main(sys.argv)
