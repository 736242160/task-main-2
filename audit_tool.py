#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_tool.py — 审计抽样检查工具（纯 Python 标准库，单文件，可直接运行）

输入格式（行式 DSL，# 开头为注释，空行忽略）：
    凭证 <编号> <金额> <类型>
    规则 <名称> <金额条件> <抽样比例>
    抽样 <规则名称> <凭证编号>[,<凭证编号>...] ...
    检查 <凭证编号> <正常|问题> [问题描述...]
    整改 <凭证编号> <完成|未完成>
    阈值 <比例偏差阈值>                # 可选，默认 0.05

金额条件写法：>1000 / >=1000 / <500 / <=500 / =100 / 100-500（区间）/ *（全部）
抽样比例写法：0.3 或 30%

用法：
    python3 audit_tool.py 数据文件     # 审计指定文件
    python3 audit_tool.py -            # 从标准输入读取
    python3 audit_tool.py              # 运行内置自测样例
"""

import sys


# ---------------------------------------------------------------- 条件解析

def make_predicate(cond_text):
    """把金额条件文本编译成判定函数 amount -> bool。"""
    text = cond_text.strip()
    if text in ("*", "全部"):
        return lambda amount: True
    if "-" in text and not text.startswith("-"):
        low_text, high_text = text.split("-", 1)
        low, high = float(low_text), float(high_text)
        return lambda amount, lo=low, hi=high: lo <= amount <= hi
    for op in (">=", "<=", ">", "<", "="):
        if text.startswith(op):
            limit = float(text[len(op):])
            if op == ">=":
                return lambda amount, lim=limit: amount >= lim
            if op == "<=":
                return lambda amount, lim=limit: amount <= lim
            if op == ">":
                return lambda amount, lim=limit: amount > lim
            if op == "<":
                return lambda amount, lim=limit: amount < lim
            return lambda amount, lim=limit: amount == lim
    raise ValueError("无法解析金额条件: %s" % cond_text)


def parse_ratio(text):
    """抽样比例：支持 0.3 与 30% 两种写法。"""
    text = text.strip()
    if text.endswith("%"):
        return float(text[:-1]) / 100.0
    return float(text)


def fmt_amount(value):
    return "%g" % value


# ---------------------------------------------------------------- 审计引擎

class AuditEngine:
    """跨流状态延续：凭证/规则定义后，抽样流、检查流、整改流按顺序处理，
    全部状态保留在引擎内，最终统一终检并输出审计状态与错误清单。"""

    def __init__(self, threshold=0.05):
        self.threshold = threshold
        self.vouchers = {}        # 编号 -> (金额, 类型)
        self.rules = {}           # 名称 -> (条件文本, 判定函数, 抽样比例)
        self.samples = {}         # 规则名称 -> [凭证编号]
        self.checks = {}          # 凭证编号 -> (结果, 问题描述)
        self.problems = {}        # 凭证编号 -> 问题描述（历史问题清单）
        self.rectifications = {}  # 凭证编号 -> 整改结果（最后一次为准）
        self.errors = []          # [(错误码, 描述)]

    def report(self, code, message):
        self.errors.append((code, message))

    # ---------------- 定义流 ----------------

    def add_voucher(self, vid, amount, vtype):
        if vid in self.vouchers:
            self.report("E00", "凭证重复定义: %s" % vid)
            return
        self.vouchers[vid] = (round(float(amount), 2), vtype)

    def add_rule(self, name, cond_text, ratio_text):
        try:
            predicate = make_predicate(cond_text)
            ratio = parse_ratio(ratio_text)
        except ValueError as exc:
            self.report("E00", "规则 %s 定义错误: %s" % (name, exc))
            return
        if not 0.0 <= ratio <= 1.0:
            self.report("E00", "规则 %s 抽样比例越界: %s" % (name, ratio_text))
            return
        self.rules[name] = (cond_text, predicate, ratio)

    # ---------------- 抽样流 ----------------

    def do_sample(self, rule_name, vids):
        if rule_name not in self.rules:
            self.report("E02", "抽样引用不存在的规则: %s" % rule_name)
            return
        cond_text, predicate, _ratio = self.rules[rule_name]
        bucket = self.samples.setdefault(rule_name, [])
        for vid in vids:
            if vid not in self.vouchers:
                self.report("E01", "抽样引用不存在的凭证: %s（规则 %s）" % (vid, rule_name))
                continue
            amount, vtype = self.vouchers[vid]
            if not predicate(amount):
                self.report(
                    "E03",
                    "未命中条件被抽入: 凭证 %s（金额 %s，类型 %s）不满足规则 %s 的条件 %s"
                    % (vid, fmt_amount(amount), vtype, rule_name, cond_text),
                )
            bucket.append(vid)

    # ---------------- 检查流 ----------------

    def do_check(self, vid, result, desc):
        if vid not in self.vouchers:
            self.report("E04", "检查引用不存在的凭证: %s" % vid)
            return
        if vid in self.checks:
            self.report("E05", "同凭证重复检查: %s（首次结果 %s，本次结果 %s，本次已忽略）"
                        % (vid, self.checks[vid][0], result))
            return
        self.checks[vid] = (result, desc)
        if result == "问题":
            self.problems[vid] = desc or "（未填写问题描述）"

    # ---------------- 整改流 ----------------

    def do_rectify(self, vid, result):
        if vid not in self.vouchers:
            self.report("E06", "整改引用不存在的凭证: %s" % vid)
            return
        if vid not in self.problems:
            self.report("E07", "整改凭证无问题记录，证据链断裂: %s" % vid)
            return
        self.rectifications[vid] = result  # 状态延续：以最后一次整改结果为准

    # ---------------- 终检 ----------------

    def open_problems(self):
        """问题清单级联更新：整改完成的凭证从未闭环清单中剔除。"""
        return {vid: desc for vid, desc in self.problems.items()
                if self.rectifications.get(vid) != "完成"}

    def finalize(self):
        # E08 问题凭证未整改完成，不得结案
        for vid, desc in sorted(self.open_problems().items()):
            self.report("E08", "问题凭证未整改完成，不得结案: %s（问题：%s，整改状态：%s）"
                        % (vid, desc, self.rectifications.get(vid, "未整改")))

        # E09 问题凭证关联凭证（同金额同类型）级联复核
        for vid in sorted(self.problems):
            amount, vtype = self.vouchers[vid]
            for other, (o_amount, o_type) in sorted(self.vouchers.items()):
                if other == vid:
                    continue
                if o_amount == amount and o_type == vtype and other not in self.checks:
                    self.report("E09", "关联凭证未复核: %s（与问题凭证 %s 同金额 %s 同类型 %s）"
                                % (other, vid, fmt_amount(amount), vtype))

        # E10 抽样比例偏差超阈值
        for name, (cond_text, predicate, ratio) in sorted(self.rules.items()):
            if name not in self.samples:
                continue
            eligible = [vid for vid, (amount, _t) in self.vouchers.items() if predicate(amount)]
            if not eligible:
                continue
            valid_sampled = [vid for vid in self.samples[name] if vid in self.vouchers]
            actual = len(valid_sampled) / float(len(eligible))
            deviation = abs(actual - ratio)
            if deviation > self.threshold:
                self.report("E10",
                            "抽样比例偏差超阈值: 规则 %s 应抽 %.1f%% 实抽 %.1f%%（命中 %d 张，"
                            "抽入 %d 张，偏差 %.1f%% > 阈值 %.1f%%）"
                            % (name, ratio * 100, actual * 100, len(eligible),
                               len(valid_sampled), deviation * 100, self.threshold * 100))

        status = "结案" if not self.errors else "不予结案"
        return status


# ---------------------------------------------------------------- 输入解析

def run_text(text):
    """解析输入文本，依次驱动各流，返回 (engine, status)。"""
    engine = AuditEngine()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        head, args = parts[0], parts[1:]
        try:
            if head == "阈值":
                engine.threshold = parse_ratio(args[0])
            elif head == "凭证":
                engine.add_voucher(args[0], args[1], args[2])
            elif head == "规则":
                engine.add_rule(args[0], args[1], args[2])
            elif head == "抽样":
                vids = []
                for token in args[1:]:
                    vids.extend(t for t in token.split(",") if t)
                engine.do_sample(args[0], vids)
            elif head == "检查":
                result = args[1]
                if result not in ("正常", "问题"):
                    raise ValueError("检查结果须为 正常/问题，得到: %s" % result)
                engine.do_check(args[0], result, " ".join(args[2:]))
            elif head == "整改":
                result = args[1]
                if result not in ("完成", "未完成"):
                    raise ValueError("整改结果须为 完成/未完成，得到: %s" % result)
                engine.do_rectify(args[0], result)
            else:
                raise ValueError("未知指令: %s" % head)
        except (IndexError, ValueError) as exc:
            engine.report("E00", "第 %d 行格式错误: %s（%s）" % (lineno, exc, line))
    status = engine.finalize()
    return engine, status


# ---------------------------------------------------------------- 报告输出

def render_report(title, engine, status):
    lines = []
    lines.append("=" * 60)
    lines.append("【%s】审计报告" % title)
    lines.append("=" * 60)
    lines.append("凭证总数: %d    规则数: %d    已检查: %d"
                 % (len(engine.vouchers), len(engine.rules), len(engine.checks)))
    lines.append("问题凭证: %d    未闭环问题: %d"
                 % (len(engine.problems), len(engine.open_problems())))
    lines.append("-" * 60)
    lines.append("审计状态: %s" % status)
    lines.append("-" * 60)
    if engine.errors:
        lines.append("错误清单（共 %d 项）:" % len(engine.errors))
        for index, (code, message) in enumerate(engine.errors, 1):
            lines.append("  %2d. [%s] %s" % (index, code, message))
    else:
        lines.append("错误清单: 无")
    if engine.open_problems():
        lines.append("-" * 60)
        lines.append("未闭环问题清单（级联更新后）:")
        for vid, desc in sorted(engine.open_problems().items()):
            lines.append("  - %s: %s（整改状态：%s）"
                         % (vid, desc, engine.rectifications.get(vid, "未整改")))
    return "\n".join(lines)


# ---------------------------------------------------------------- 自测样例

SAMPLE_CLEAN = """
# 干净样例：条件命中正确、问题整改完成、关联凭证已复核、比例无偏差 -> 结案
阈值 0.05
凭证 V1 1200 货款
凭证 V2 800  货款
凭证 V3 1200 货款
凭证 V4 5000 费用
凭证 V5 1500 费用
凭证 V6 1200 货款
规则 R1 >=1000 0.6
抽样 R1 V1,V4,V5
检查 V1 问题 发票缺失
检查 V4 正常
检查 V5 正常
检查 V3 正常
检查 V6 正常
整改 V1 完成
"""

SAMPLE_ERRORS = """
# 异常样例：覆盖全部 10 类错误 -> 不予结案
阈值 0.05
凭证 A1 2000 货款
凭证 A2 2000 货款
凭证 A3 500  费用
凭证 A4 9000 费用
规则 RA >1000 0.5
抽样 RA A1,A3,AX
抽样 RB A1
检查 A1 问题 金额不符
检查 A1 正常
检查 AY 正常
整改 A1 未完成
整改 AZ 完成
整改 A4 完成
"""


def self_test():
    engine_ok, status_ok = run_text(SAMPLE_CLEAN)
    print(render_report("自测样例一：正常流程", engine_ok, status_ok))
    print()
    engine_bad, status_bad = run_text(SAMPLE_ERRORS)
    print(render_report("自测样例二：异常流程", engine_bad, status_bad))
    print()

    codes = {code for code, _ in engine_bad.errors}
    expected = {"E01", "E02", "E03", "E04", "E05", "E06", "E07", "E08", "E09", "E10"}
    missing = expected - codes
    assert status_ok == "结案" and not engine_ok.errors, "干净样例应结案且无错误"
    assert status_bad == "不予结案", "异常样例应不予结案"
    assert not missing, "异常样例缺少错误类型: %s" % sorted(missing)
    print("自测断言全部通过：干净样例结案，异常样例覆盖 E01~E10 全部错误类型。")


def main(argv):
    if len(argv) == 1:
        self_test()
        return 0
    if argv[1] == "-":
        text = sys.stdin.read()
        title = "标准输入"
    else:
        with open(argv[1], "r", encoding="utf-8") as handle:
            text = handle.read()
        title = argv[1]
    engine, status = run_text(text)
    print(render_report(title, engine, status))
    return 0 if status == "结案" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
