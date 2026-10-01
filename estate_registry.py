#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
estate_registry.py — 不动产产权份额与抵押链登记工具（纯 Python 标准库，单文件）

用法:
    python3 estate_registry.py 输入文件
    命令 | python3 estate_registry.py        # 从标准输入读取

输入格式（每行一条指令，'#' 后为注释，空行忽略；行序即登记顺序）:
    房产  <房产号> <人>=<份额%> ...            定义房产及共有人份额
    抵押  <抵押号> <房产号> <金额> [顺位]      设立抵押（顺位省略时自动取当前最大顺位+1）
    解押  <抵押号>                            注销抵押
    过户  <房产号> <转让人> <受让人> <份额%>   共有人之间/对外转让份额
    析产  <房产号> <新房产>=<比例%> ...        一房拆多房，产权与抵押级联拆分
    合并  <新房产> <房产>[=<权重>] ...         多房并一房，产权与抵押级联合并

自定规则（错误分类）:
    E01 格式错误      指令无法解析
    E02 登记顺序错误  引用的房产/抵押不存在或已注销；编号重复定义；转让人非共有人
                      （规则：任何登记引用的客体必须“已定义且当前有效”）
    E03 份额合计超限  房产定义时共有人份额合计 > 100%，该定义被拒绝
    E04 过户份额超限  拟过户份额 > 转让人持有份额，报告房产/共有人/超量，过户被拒绝
    E05 重复抵押      同一房产上有效抵押的顺位必须唯一、抵押号不得重复
                      （理由：顺位是抵押清偿顺序的唯一依据，同顺位会使清偿顺序不确定）
    E06 抵押未解押    房产存在有效抵押时禁止过户，过户被拒绝
    E07 解押无效      解押引用不存在或已注销的抵押
    E08 比例错误      析产比例合计须恰为 100%；合并权重须为正数

级联规则:
    析产：原房产注销；各新房产按拆分比例继承各共有人份额；原每笔有效抵押按同一
          比例拆到每个新房产上（金额同比拆分、顺位不变），新抵押号 = 原号@新房产。
    合并：各源房产注销；新房产共有人份额按权重加权合并；各源房产的有效抵押按
          “源房产在合并清单中的顺序、再按原顺位”串成新抵押链，顺位重排为 1..n，
          金额不变，新抵押号 = 原号@新房产。
"""

import sys
from dataclasses import dataclass
from fractions import Fraction


# ---------------------------------------------------------------- 数据模型

@dataclass
class Mortgage:
    mid: str
    pid: str
    amount: Fraction
    rank: int
    active: bool = True
    origin: str = ""        # 级联来源说明
    close_note: str = ""    # 注销原因


@dataclass
class Property:
    pid: str
    owners: dict            # 姓名 -> 份额(%)
    active: bool = True
    origin: str = ""        # 级联来源说明
    close_note: str = ""    # 注销原因


def fmt(x: Fraction) -> str:
    """分数的紧凑显示：整数直接显示，否则显示小数。"""
    if x.denominator == 1:
        return str(x.numerator)
    return f"{float(x):.6g}"


# ---------------------------------------------------------------- 登记簿

class Registry:
    def __init__(self):
        self.properties = {}   # pid -> Property（保持定义顺序）
        self.mortgages = {}    # mid -> Mortgage（保持设立顺序）
        self.errors = []       # (行号, 类别, 说明)

    def fail(self, lineno, code, msg):
        self.errors.append((lineno, code, msg))

    # ---- 内部工具 ----

    def get_active_property(self, lineno, pid):
        p = self.properties.get(pid)
        if p is None:
            self.fail(lineno, "E02 登记顺序错误",
                      f"房产 {pid} 不存在（尚未定义或登记顺序有误）")
            return None
        if not p.active:
            self.fail(lineno, "E02 登记顺序错误",
                      f"房产 {pid} 已注销（{p.close_note}），不能再作为登记对象")
            return None
        return p

    def active_mortgages(self, pid):
        return sorted((m for m in self.mortgages.values()
                       if m.pid == pid and m.active), key=lambda m: m.rank)

    # ---- 指令实现 ----

    def define_property(self, lineno, pid, pairs):
        if pid in self.properties:
            self.fail(lineno, "E02 登记顺序错误", f"房产 {pid} 重复定义")
            return
        owners = {}
        for name, share in pairs:
            if share <= 0:
                self.fail(lineno, "E01 格式错误",
                          f"房产 {pid} 共有人 {name} 的份额须为正数")
                return
            owners[name] = owners.get(name, Fraction(0)) + share
        if not owners:
            self.fail(lineno, "E01 格式错误", f"房产 {pid} 至少需一名共有人")
            return
        total = sum(owners.values())
        if total > 100:
            self.fail(lineno, "E03 份额合计超限",
                      f"房产 {pid} 共有人份额合计 {fmt(total)}% > 100%，定义被拒绝")
            return
        self.properties[pid] = Property(pid, owners)

    def register_mortgage(self, lineno, mid, pid, amount, rank):
        if mid in self.mortgages:
            self.fail(lineno, "E02 登记顺序错误", f"抵押 {mid} 编号重复")
            return
        if self.get_active_property(lineno, pid) is None:
            return
        if amount <= 0:
            self.fail(lineno, "E01 格式错误", f"抵押 {mid} 金额须为正数")
            return
        if rank is None:
            used = [m.rank for m in self.active_mortgages(pid)]
            rank = max(used) + 1 if used else 1
        else:
            for m in self.active_mortgages(pid):
                if m.rank == rank:
                    self.fail(lineno, "E05 重复抵押",
                              f"房产 {pid} 顺位 {rank} 已被有效抵押 {m.mid} 占用，"
                              f"抵押 {mid} 被拒绝（同一房产的有效抵押顺位须唯一）")
                    return
        self.mortgages[mid] = Mortgage(mid, pid, amount, rank)

    def release_mortgage(self, lineno, mid):
        m = self.mortgages.get(mid)
        if m is None:
            self.fail(lineno, "E07 解押无效", f"抵押 {mid} 不存在")
            return
        if not m.active:
            self.fail(lineno, "E07 解押无效",
                      f"抵押 {mid} 已注销（{m.close_note}），无法解押")
            return
        m.active = False
        m.close_note = f"第{lineno}行解押"

    def transfer(self, lineno, pid, src, dst, share):
        p = self.get_active_property(lineno, pid)
        if p is None:
            return
        if share <= 0:
            self.fail(lineno, "E01 格式错误", "过户份额须为正数")
            return
        if src not in p.owners:
            self.fail(lineno, "E02 登记顺序错误",
                      f"{src} 不是房产 {pid} 的共有人，不能作为转让人")
            return
        held = p.owners[src]
        if share > held:
            self.fail(lineno, "E04 过户份额超限",
                      f"房产 {pid} 共有人 {src} 持有 {fmt(held)}%，"
                      f"拟过户 {fmt(share)}%，超量 {fmt(share - held)}%，过户被拒绝")
            return
        chain = self.active_mortgages(pid)
        if chain:
            ids = "、".join(m.mid for m in chain)
            self.fail(lineno, "E06 抵押未解押",
                      f"房产 {pid} 存在未解押抵押（{ids}），过户被拒绝")
            return
        p.owners[src] = held - share
        if p.owners[src] == 0:
            del p.owners[src]
        p.owners[dst] = p.owners.get(dst, Fraction(0)) + share

    def split(self, lineno, pid, parts):
        p = self.get_active_property(lineno, pid)
        if p is None:
            return
        if len(parts) < 2:
            self.fail(lineno, "E01 格式错误", "析产至少拆分为两处新房产")
            return
        total = sum(pct for _, pct in parts)
        if total != 100:
            self.fail(lineno, "E08 比例错误",
                      f"房产 {pid} 析产比例合计 {fmt(total)}% ≠ 100%，析产被拒绝")
            return
        for npid, pct in parts:
            if pct <= 0:
                self.fail(lineno, "E08 比例错误", f"新房产 {npid} 比例须为正数")
                return
            if npid in self.properties:
                self.fail(lineno, "E02 登记顺序错误", f"新房产 {npid} 编号已存在")
                return
        # 先取出原抵押链，再注销原房产
        chain = self.active_mortgages(pid)
        p.active = False
        p.close_note = f"第{lineno}行析产注销"
        for npid, pct in parts:
            owners = {n: s * pct / 100 for n, s in p.owners.items()}
            self.properties[npid] = Property(
                npid, owners,
                origin=f"第{lineno}行由 {pid} 析产 {fmt(pct)}% 而来")
        for m in chain:
            m.active = False
            m.close_note = f"第{lineno}行随析产级联拆分"
            for npid, pct in parts:
                nmid = f"{m.mid}@{npid}"
                self.mortgages[nmid] = Mortgage(
                    nmid, npid, m.amount * pct / 100, m.rank,
                    origin=f"由 {m.mid} 级联拆分（金额×{fmt(pct)}%，顺位{m.rank}不变）")

    def merge(self, lineno, newpid, sources):
        if newpid in self.properties:
            self.fail(lineno, "E02 登记顺序错误", f"新房产 {newpid} 编号已存在")
            return
        if len(sources) < 2:
            self.fail(lineno, "E01 格式错误", "合并至少需要两处源房产")
            return
        props = []
        for pid, weight in sources:
            q = self.get_active_property(lineno, pid)
            if q is None:
                return
            if weight <= 0:
                self.fail(lineno, "E08 比例错误", f"房产 {pid} 合并权重须为正数")
                return
            props.append((q, weight))
        total_w = sum(w for _, w in props)
        owners = {}
        for q, w in props:
            for n, s in q.owners.items():
                owners[n] = owners.get(n, Fraction(0)) + s * w / total_w
        # 抵押链：按“源房产顺序 + 原顺位”串联，顺位重排 1..n，金额不变
        chain = []
        for q, _ in props:
            chain.extend(self.active_mortgages(q.pid))
        src_desc = "+".join(q.pid for q, _ in props)
        for q, _ in props:
            q.active = False
            q.close_note = f"第{lineno}行合并注销"
        self.properties[newpid] = Property(
            newpid, owners, origin=f"第{lineno}行由 {src_desc} 合并而来")
        for i, m in enumerate(chain, 1):
            m.active = False
            m.close_note = f"第{lineno}行随合并级联转移"
            nmid = f"{m.mid}@{newpid}"
            self.mortgages[nmid] = Mortgage(
                nmid, newpid, m.amount, i,
                origin=f"由 {m.mid} 级联合并（原顺位{m.rank}→新顺位{i}，金额不变）")


# ---------------------------------------------------------------- 解析

def _kv(token):
    if "=" not in token:
        raise ValueError(f"缺少 '='：{token!r}")
    k, v = token.rsplit("=", 1)
    return k, Fraction(v)


def _need(args, n, usage):
    if len(args) < n:
        raise ValueError(f"参数不足，用法：{usage}")


def process_line(reg, lineno, line):
    tok = line.split()
    cmd, args = tok[0], tok[1:]
    try:
        if cmd == "房产":
            _need(args, 2, "房产 <房产号> <人>=<份额%> ...")
            reg.define_property(lineno, args[0], [_kv(a) for a in args[1:]])
        elif cmd == "抵押":
            _need(args, 3, "抵押 <抵押号> <房产号> <金额> [顺位]")
            rank = int(args[3]) if len(args) > 3 else None
            reg.register_mortgage(lineno, args[0], args[1], Fraction(args[2]), rank)
        elif cmd == "解押":
            _need(args, 1, "解押 <抵押号>")
            reg.release_mortgage(lineno, args[0])
        elif cmd == "过户":
            _need(args, 4, "过户 <房产号> <转让人> <受让人> <份额%>")
            reg.transfer(lineno, args[0], args[1], args[2], Fraction(args[3]))
        elif cmd == "析产":
            _need(args, 2, "析产 <房产号> <新房产>=<比例%> ...")
            reg.split(lineno, args[0], [_kv(a) for a in args[1:]])
        elif cmd == "合并":
            _need(args, 3, "合并 <新房产> <房产>[=<权重>] ...")
            newpid = args[0]
            sources = [_kv(a) if "=" in a else (a, Fraction(1)) for a in args[1:]]
            reg.merge(lineno, newpid, sources)
        else:
            raise ValueError(f"未知指令 {cmd!r}")
    except ValueError as e:
        reg.fail(lineno, "E01 格式错误", f"{e}（行内容：{line}）")


# ---------------------------------------------------------------- 输出

def render(reg):
    out = []
    out.append("=" * 60)
    out.append("产权状态")
    out.append("=" * 60)
    for p in reg.properties.values():
        status = "有效" if p.active else f"已注销（{p.close_note}）"
        out.append(f"房产 {p.pid} [{status}]")
        if p.origin:
            out.append(f"    来源: {p.origin}")
        if p.owners:
            owners = sorted(p.owners.items(), key=lambda kv: (-kv[1], kv[0]))
            out.append("    共有人: " + "  ".join(f"{n} {fmt(s)}%" for n, s in owners))
        chain = reg.active_mortgages(p.pid)
        if chain:
            out.append("    抵押链: " + "  ".join(
                f"[顺位{m.rank}] {m.mid} 金额{fmt(m.amount)}" for m in chain))
    out.append("")
    out.append("-" * 60)
    out.append("抵押台账（含已注销，展示抵押链延续）")
    out.append("-" * 60)
    for m in reg.mortgages.values():
        status = "有效" if m.active else f"已注销（{m.close_note}）"
        line = f"  {m.mid:<14} 房产{m.pid:<6} 金额{fmt(m.amount):<8} 顺位{m.rank}  {status}"
        if m.origin:
            line += f"  ← {m.origin}"
        out.append(line)
    out.append("")
    out.append("=" * 60)
    out.append(f"错误清单（共 {len(reg.errors)} 条）")
    out.append("=" * 60)
    for lineno, code, msg in reg.errors:
        out.append(f"第{lineno:>3}行 [{code}] {msg}")
    if not reg.errors:
        out.append("（无错误）")
    return "\n".join(out)


def main(argv):
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    reg = Registry()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if line:
            process_line(reg, lineno, line)
    print(render(reg))


if __name__ == "__main__":
    main(sys.argv)
