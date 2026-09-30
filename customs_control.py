#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""海关布控查验通关状态工具（纯 Python 标准库，单文件）。

用法:
    python3 customs_control.py 数据文件        # 从文件读取事件流
    cat 数据文件 | python3 customs_control.py  # 从标准输入读取
    python3 customs_control.py --demo          # 运行内置示例并输出结果
    python3 customs_control.py --selftest      # 内置自检（断言关键状态与错误）

输入格式（每行一个事件，# 开头为注释，三流可交错，状态跨流延续）:
    RULE   <规则号> <商品类别|*> <命中条件> <机检|人工>
    DECL   <申报号> <商品类别> <申报价值> <申报量>
    INSP   <申报号> <规则号|抽查> <放行|异常> [扣留|退运|-]
    REDECL <申报号> <新申报价值> <新申报量>     # 仅扣留中的申报可二次申报

命中条件: 全部 | 价值>10000 | 数量>=100 | 价值>10000且数量>10 | 价值<100或数量>500
          支持字段 价值/数量，运算符 > >= < <= == !=，且 优先级高于 或。

抽查规则（自定，理由）:
    未命中任何布控规则的申报，按 crc32(申报号) % 10 == 0 抽查（约 10%）。
    理由: 1) 纯标准库、零依赖；2) 确定性可复现，同一申报号在任何批次/机器上
    结果一致，满足"跨流状态延续"与审计要求；3) 与申报号绑定，避免人为挑选，
    不依赖随机数发生器状态，重放数据不会漏查。

状态机:
    申报入流 -> 命中规则或被抽查: 待查验；否则: 放行
    待查验 --查验放行--> 放行
    待查验 --查验异常+扣留--> 扣留 --二次申报(REDECL)--> 重新命中判定 -> 待查验/放行
    待查验 --查验异常+退运--> 退运（终态）
    扣留/退运为终态，须 REDECL（仅扣留）进入新一轮后才可再查验。
"""

import re
import sys
import zlib

SAMPLE_MOD = 10  # crc32(申报号) % SAMPLE_MOD == 0 -> 抽查

COND_RE = re.compile(r"^(价值|数量)(>=|<=|==|!=|>|<)(\d+(?:\.\d+)?)$")
OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}
METHODS = ("机检", "人工")
RESULTS = ("放行", "异常")
HANDLINGS = ("扣留", "退运")


def parse_condition(text):
    """解析命中条件，返回 or-of-ands 结构；'全部' 返回 None。非法则抛 ValueError。"""
    if text == "全部":
        return None
    groups = []
    for or_part in text.split("或"):
        group = []
        for atom in or_part.split("且"):
            m = COND_RE.match(atom.strip())
            if not m:
                raise ValueError("无法解析的条件片段: %r" % atom)
            field, op, num = m.group(1), m.group(2), float(m.group(3))
            group.append((field, op, num))
        groups.append(group)
    return groups


class Rule:
    def __init__(self, rid, category, cond_text, method):
        self.rid = rid
        self.category = category
        self.method = method
        self.groups = parse_condition(cond_text)

    def matches(self, category, value, qty):
        if self.category != "*" and self.category != category:
            return False
        if self.groups is None:
            return True
        ctx = {"价值": value, "数量": qty}
        for group in self.groups:
            if all(OPS[op](ctx[field], num) for field, op, num in group):
                return True
        return False


class Declaration:
    def __init__(self, did, category, value, qty):
        self.did = did
        self.category = category
        self.value = value
        self.qty = qty
        self.round = 1            # 申报轮次，REDECL 后 +1
        self.hit_rules = []       # 本轮命中的规则号
        self.sampled = False      # 本轮是否被抽查
        self.status = ""          # 待查验/放行/扣留/退运
        self.inspected_round = 0  # 本轮是否已查验（用于重复查验判定）
        self.history = []         # 查验记录: (轮次, 规则号, 结果, 处理)


class Engine:
    def __init__(self):
        self.rules = {}
        self.decls = {}
        self.errors = []

    def err(self, lineno, msg):
        self.errors.append("[行%d] %s" % (lineno, msg))

    # ---------- 各事件 ----------
    def do_rule(self, ln, args):
        if len(args) != 4:
            self.err(ln, "RULE 需要 4 个字段: 规则号 商品类别 命中条件 查验方式")
            return
        rid, category, cond, method = args
        if rid in self.rules:
            self.err(ln, "规则 %s 重复定义" % rid)
            return
        if method not in METHODS:
            self.err(ln, "规则 %s 查验方式 %r 非法，须为 机检/人工" % (rid, method))
            return
        try:
            self.rules[rid] = Rule(rid, category, cond, method)
        except ValueError as e:
            self.err(ln, "规则 %s 命中条件非法: %s" % (rid, e))

    def _refresh_status(self, d):
        """按当前价值/数量重新命中判定并级联更新状态（申报与二次申报共用）。"""
        d.hit_rules = [r.rid for r in self.rules.values()
                       if r.matches(d.category, d.value, d.qty)]
        d.sampled = not d.hit_rules and zlib.crc32(d.did.encode("utf-8")) % SAMPLE_MOD == 0
        d.status = "待查验" if (d.hit_rules or d.sampled) else "放行"

    def do_decl(self, ln, args):
        if len(args) != 4:
            self.err(ln, "DECL 需要 4 个字段: 申报号 商品类别 申报价值 申报量")
            return
        did, category = args[0], args[1]
        try:
            value, qty = float(args[2]), float(args[3])
        except ValueError:
            self.err(ln, "申报 %s 价值/数量不是数字" % did)
            return
        if did in self.decls:
            self.err(ln, "申报 %s 编号重复" % did)
            return
        d = Declaration(did, category, value, qty)
        self._refresh_status(d)
        self.decls[did] = d

    def do_insp(self, ln, args):
        if len(args) not in (3, 4):
            self.err(ln, "INSP 需要 3~4 个字段: 申报号 规则号|抽查 结果 [处理]")
            return
        did, rid, result = args[0], args[1], args[2]
        handling = args[3] if len(args) == 4 else "-"
        d = self.decls.get(did)
        if d is None:
            self.err(ln, "查验引用了不存在的申报 %s" % did)
            return
        if rid != "抽查" and rid not in self.rules:
            self.err(ln, "查验引用了不存在的规则 %s（申报 %s）" % (rid, did))
            return
        if result not in RESULTS:
            self.err(ln, "申报 %s 查验结果 %r 非法，须为 放行/异常" % (did, result))
            return
        if d.status in ("扣留", "退运"):
            self.err(ln, "申报 %s 当前状态为%s，须先二次申报(REDECL)进入新一轮后才能查验"
                     % (did, d.status))
            return
        if d.inspected_round == d.round:
            self.err(ln, "申报 %s 第 %d 轮重复查验，忽略本次查验" % (did, d.round))
            return
        # 查验依据校验
        if rid == "抽查":
            if not d.sampled:
                self.err(ln, "申报 %s 未被抽查，抽查查验无依据" % did)
                return
        else:
            if rid not in d.hit_rules:
                self.err(ln, "申报 %s 未命中规则 %s，查验依据不符（本轮命中: %s）"
                         % (did, rid, ",".join(d.hit_rules) or "无"))
                return
        # 结果与处理一致性
        if result == "放行":
            if handling != "-":
                self.err(ln, "申报 %s 查验放行不应带处理措施 %r" % (did, handling))
                return
            d.status = "放行"
        else:  # 异常
            if handling not in HANDLINGS:
                self.err(ln, "申报 %s 查验异常但处理措施 %r 非法，须为 扣留/退运"
                         % (did, handling))
                return
            d.status = handling  # 级联：异常+处理 -> 扣留/退运
        d.inspected_round = d.round
        d.history.append((d.round, rid, result, handling))

    def do_redecl(self, ln, args):
        if len(args) != 3:
            self.err(ln, "REDECL 需要 3 个字段: 申报号 新申报价值 新申报量")
            return
        did = args[0]
        d = self.decls.get(did)
        if d is None:
            self.err(ln, "二次申报引用了不存在的申报 %s" % did)
            return
        if d.status != "扣留":
            self.err(ln, "申报 %s 当前状态为%s，仅扣留中的申报允许二次申报"
                     % (did, d.status))
            return
        try:
            value, qty = float(args[1]), float(args[2])
        except ValueError:
            self.err(ln, "申报 %s 二次申报的价值/数量不是数字" % did)
            return
        # 级联重检：新一轮，重置查验状态，按新价值/数量重新命中判定
        d.value, d.qty = value, qty
        d.round += 1
        self._refresh_status(d)

    # ---------- 驱动 ----------
    def feed(self, lines):
        for ln, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            cmd, args = parts[0].upper(), parts[1:]
            if cmd == "RULE":
                self.do_rule(ln, args)
            elif cmd == "DECL":
                self.do_decl(ln, args)
            elif cmd == "INSP":
                self.do_insp(ln, args)
            elif cmd == "REDECL":
                self.do_redecl(ln, args)
            else:
                self.err(ln, "未知事件类型 %r" % parts[0])
        self.finalize()

    def finalize(self):
        """流结束后兜底：命中规则/被抽查却始终未查验的申报视为未查验直接放行。"""
        for d in self.decls.values():
            if d.status != "待查验":
                continue
            if d.hit_rules:
                self.errors.append("[流末] 申报 %s 命中布控规则 %s 但未查验即放行"
                         % (d.did, ",".join(d.hit_rules)))
            elif d.sampled:
                self.errors.append("[流末] 申报 %s 被抽查但未查验即放行" % d.did)

    # ---------- 输出 ----------
    def report(self):
        out = ["===== 通关状态 ====="]
        out.append("申报号 | 类别 | 价值 | 数量 | 轮次 | 命中规则 | 抽查 | 状态 | 查验记录(轮次:规则/结果/处理)")
        for did in sorted(self.decls):
            d = self.decls[did]
            hist = "; ".join("%d:%s/%s/%s" % h for h in d.history) or "-"
            out.append("%s | %s | %g | %g | %d | %s | %s | %s | %s" % (
                d.did, d.category, d.value, d.qty, d.round,
                ",".join(d.hit_rules) or "-",
                "是" if d.sampled else "否", d.status, hist))
        out.append("")
        out.append("===== 错误报告（共 %d 条）=====" % len(self.errors))
        out.extend(self.errors if self.errors else ["无"])
        return "\n".join(out)


DEMO_INPUT = """\
# ---- 布控规则 ----
RULE R1 电子产品 价值>10000 机检
RULE R2 食品 数量>=100 人工
RULE R3 * 价值>1000000 人工
# ---- 申报流 ----
DECL D001 电子产品 15000 5
DECL D002 电子产品 20000 3
DECL D003 电子产品 18000 2
DECL D004 服装 500 10
DECL D005 服装 600 8
DECL D006 食品 50 200
DECL D007 电子产品 30000 1
DECL D008 家具 800 4
DECL D009 电子产品 12000 6
# ---- 查验流 ----
INSP D001 R1 放行
INSP D002 R1 异常 扣留
INSP D004 抽查 放行
INSP D006 R2 异常 退运
INSP D007 R1 异常
INSP D008 R1 放行
INSP D009 R1 放行 扣留
INSP D001 R1 放行
INSP D999 R1 放行
INSP D003 R9 放行
# ---- 扣留后二次申报（修改价值/数量后重报），级联重检 ----
REDECL D002 12000 3
INSP D002 R1 放行
REDECL D008 100 1
"""


def selftest():
    eng = Engine()
    eng.feed(DEMO_INPUT.splitlines())
    d = eng.decls
    checks = [
        ("D001 查验放行", d["D001"].status == "放行"),
        ("D002 扣留后二次申报级联重检并放行",
         d["D002"].status == "放行" and d["D002"].round == 2 and d["D002"].hit_rules == ["R1"]),
        ("D003 命中规则未查验", d["D003"].status == "待查验"),
        ("D004 抽查后放行", d["D004"].status == "放行" and d["D004"].sampled),
        ("D005 抽查未查验", d["D005"].status == "待查验" and d["D005"].sampled),
        ("D006 异常退运", d["D006"].status == "退运"),
        ("D007 异常缺处理保持待查验", d["D007"].status == "待查验"),
        ("D008 未命中规则自动放行", d["D008"].status == "放行"),
        ("D009 放行带处理被拒绝", d["D009"].status == "待查验"),
    ]
    err_text = "\n".join(eng.errors)
    for kw in ["重复查验", "不存在的申报 D999", "不存在的规则 R9",
               "未查验即放行", "处理措施", "未命中规则 R1", "仅扣留中的申报允许二次申报"]:
        checks.append(("错误报告含: " + kw, kw in err_text))
    failed = [name for name, ok in checks if not ok]
    print(eng.report())
    print()
    if failed:
        print("自检失败: " + "; ".join(failed))
        return 1
    print("自检通过（%d 项断言）" % len(checks))
    return 0


def main(argv):
    if len(argv) >= 2 and argv[1] == "--selftest":
        return selftest()
    if len(argv) >= 2 and argv[1] == "--demo":
        eng = Engine()
        eng.feed(DEMO_INPUT.splitlines())
        print(eng.report())
        return 0
    if len(argv) >= 2:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    eng = Engine()
    eng.feed(lines)
    print(eng.report())
    return 1 if eng.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
