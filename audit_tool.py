#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_tool.py — 审计抽样与检查状态核对工具（纯 Python 标准库，单文件）

输入（JSON 文件或 stdin）：
{
  "threshold": 0.2,                       # 抽样比例偏差阈值（可选，默认 0.2）
  "vouchers":      [{"id": "V001", "amount": 1200.0, "type": "差旅费"}, ...],
  "rules":         [{"name": "R1", "condition": ">=1000", "ratio": 0.5}, ...],
  "sampling":      [{"rule": "R1", "vouchers": ["V001", "V002"]}, ...],
  "inspections":   [{"voucher": "V001", "result": "问题", "problem": "缺少发票"}, ...],
  "rectifications":[{"voucher": "V001", "result": "完成"}, ...]
}

金额条件支持：>= <= > < == != 及区间 "100-1000"（含端点）。
结果取值：检查 = 正常/问题；整改 = 完成/未完成。

用法：
  python3 audit_tool.py 数据文件.json
  python3 audit_tool.py < 数据文件.json
  python3 audit_tool.py --selftest      # 运行内置自测样例
"""

import json
import sys
import re

DEFAULT_THRESHOLD = 0.2

RESULT_NORMAL = "正常"
RESULT_PROBLEM = "问题"
RECT_DONE = "完成"
RECT_UNDONE = "未完成"


# ---------------------------------------------------------------- 条件解析

def parse_condition(cond):
    """把金额条件字符串解析为判定函数 amount -> bool。"""
    cond = str(cond).strip()
    m = re.fullmatch(r"(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)", cond)
    if m:
        op, val = m.group(1), float(m.group(2))
        ops = {
            ">=": lambda a: a >= val,
            "<=": lambda a: a <= val,
            ">":  lambda a: a > val,
            "<":  lambda a: a < val,
            "==": lambda a: a == val,
            "!=": lambda a: a != val,
        }
        return ops[op]
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)", cond)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return lambda a: lo <= a <= hi
    raise ValueError("无法解析的金额条件: %r（支持 >= <= > < == != 或 区间 a-b）" % cond)


# ---------------------------------------------------------------- 数据模型

class Voucher:
    def __init__(self, vid, amount, vtype):
        self.id = vid
        self.amount = float(amount)
        self.type = vtype


class Rule:
    def __init__(self, name, condition, ratio):
        self.name = name
        self.condition = condition
        self.matcher = parse_condition(condition)
        self.ratio = float(ratio)


class Error:
    """一条错误/异常报告。"""
    def __init__(self, category, message, blocking=False):
        self.category = category      # 错误类别
        self.message = message        # 描述
        self.blocking = blocking      # 是否阻止结案

    def __str__(self):
        tag = "阻断" if self.blocking else "提示"
        return "[%s][%s] %s" % (self.category, tag, self.message)


# ---------------------------------------------------------------- 审计引擎

class AuditEngine:
    def __init__(self, data):
        self.errors = []
        self.threshold = float(data.get("threshold", DEFAULT_THRESHOLD))

        # 凭证台账
        self.vouchers = {}
        for v in data.get("vouchers", []):
            vid = str(v["id"])
            if vid in self.vouchers:
                self._err("凭证定义", "凭证 %s 重复定义" % vid)
            self.vouchers[vid] = Voucher(vid, v["amount"], v["type"])

        # 抽样规则
        self.rules = {}
        for r in data.get("rules", []):
            name = str(r["name"])
            if name in self.rules:
                self._err("抽样规则", "规则 %s 重复定义" % name)
            try:
                self.rules[name] = Rule(name, r["condition"], r["ratio"])
            except (ValueError, KeyError) as e:
                self._err("抽样规则", "规则 %s 无效: %s" % (name, e))

        # 跨流状态：凭证 -> 状态信息（抽样/检查/整改在各流之间延续）
        self.sampled = {}        # vid -> rule name
        self.inspected = {}      # vid -> {"result":..., "problem":...}
        self.rectified = {}      # vid -> 整改结果
        self.open_problems = {}  # vid -> 问题描述（未结案问题清单，随整改级联更新）

        self._load_sampling(data.get("sampling", []))
        self._load_inspections(data.get("inspections", []))
        self._load_rectifications(data.get("rectifications", []))

        self._check_ratio_deviation()
        self._check_cascade_review()

    def _err(self, category, message, blocking=False):
        self.errors.append(Error(category, message, blocking))

    # ---- 抽样流 ----
    def _load_sampling(self, sampling):
        for entry in sampling:
            rname = str(entry.get("rule", ""))
            rule = self.rules.get(rname)
            if rule is None:
                self._err("抽样", "抽样引用不存在的规则 %s" % rname)
                continue
            for vid in entry.get("vouchers", []):
                vid = str(vid)
                v = self.vouchers.get(vid)
                if v is None:
                    self._err("抽样", "抽样引用不存在的凭证 %s（规则 %s）" % (vid, rname))
                    continue
                if vid in self.sampled:
                    self._err("抽样", "凭证 %s 被重复抽入（规则 %s 与 %s）"
                              % (vid, self.sampled[vid], rname))
                else:
                    self.sampled[vid] = rname
                if not rule.matcher(v.amount):
                    self._err("抽样", "凭证 %s（金额 %s，类型 %s）未命中规则 %s 的条件 %r 却被抽入"
                              % (vid, v.amount, v.type, rname, rule.condition))

    # ---- 检查流 ----
    def _load_inspections(self, inspections):
        for ins in inspections:
            vid = str(ins.get("voucher", ""))
            result = ins.get("result", "")
            problem = ins.get("problem", "")
            if vid not in self.vouchers:
                self._err("检查", "检查引用不存在的凭证 %s" % vid)
                continue
            if vid in self.inspected:
                self._err("检查", "凭证 %s 被重复检查（首次结果：%s，本次结果：%s）"
                          % (vid, self.inspected[vid]["result"], result))
                continue
            if result not in (RESULT_NORMAL, RESULT_PROBLEM):
                self._err("检查", "凭证 %s 检查结果非法：%r（应为 正常/问题）" % (vid, result))
                continue
            self.inspected[vid] = {"result": result, "problem": problem}
            if result == RESULT_PROBLEM:
                if not problem:
                    self._err("检查", "凭证 %s 判定为问题但缺少问题描述" % vid)
                self.open_problems[vid] = problem

    # ---- 整改流 ----
    def _load_rectifications(self, rectifications):
        for rect in rectifications:
            vid = str(rect.get("voucher", ""))
            result = rect.get("result", "")
            if vid not in self.vouchers:
                self._err("整改", "整改引用不存在的凭证 %s" % vid)
                continue
            if vid in self.rectified:
                self._err("整改", "凭证 %s 重复整改登记" % vid)
                continue
            if result not in (RECT_DONE, RECT_UNDONE):
                self._err("整改", "凭证 %s 整改结果非法：%r（应为 完成/未完成）" % (vid, result))
                continue
            if vid not in self.open_problems:
                self._err("整改", "凭证 %s 无未结问题却登记整改（%s）" % (vid, result))
                continue
            self.rectified[vid] = result
            if result == RECT_DONE:
                # 整改完成 -> 问题清单级联更新（移出未结清单）
                del self.open_problems[vid]
            else:
                self._err("整改", "问题凭证 %s 整改未完成，不得结案：%s"
                          % (vid, self.open_problems[vid]), blocking=True)

        # 从未登记整改的问题凭证 -> 阻断结案
        for vid, desc in sorted(self.open_problems.items()):
            if vid not in self.rectified:
                self._err("整改", "问题凭证 %s 未整改完成，不得结案：%s" % (vid, desc),
                          blocking=True)

    # ---- 抽样比例偏差 ----
    def _check_ratio_deviation(self):
        for rname, rule in sorted(self.rules.items()):
            eligible = [vid for vid, v in self.vouchers.items() if rule.matcher(v.amount)]
            actual = [vid for vid, r in self.sampled.items()
                      if r == rname and vid in self.vouchers
                      and rule.matcher(self.vouchers[vid].amount)]
            expected = rule.ratio * len(eligible)
            if expected == 0:
                continue
            deviation = abs(len(actual) - expected) / expected
            if deviation > self.threshold:
                self._err("抽样比例",
                          "规则 %s 应抽约 %.1f 张（符合条件 %d 张 × 比例 %.2f），"
                          "实际有效抽中 %d 张，偏差 %.1f%% 超过阈值 %.1f%%"
                          % (rname, expected, len(eligible), rule.ratio,
                             len(actual), deviation * 100, self.threshold * 100))

    # ---- 关联凭证级联复核 ----
    def _check_cascade_review(self):
        problem_vids = [vid for vid, r in self.inspected.items()
                        if r["result"] == RESULT_PROBLEM]
        for vid in problem_vids:
            v = self.vouchers[vid]
            related = [oid for oid, ov in self.vouchers.items()
                       if oid != vid and ov.amount == v.amount and ov.type == v.type]
            for oid in sorted(related):
                if oid not in self.inspected:
                    self._err("级联复核",
                              "问题凭证 %s 的关联凭证 %s（同金额 %s、同类型 %s）未复核"
                              % (vid, oid, v.amount, v.type), blocking=True)

    # ---- 输出 ----
    def status(self):
        blocking = [e for e in self.errors if e.blocking]
        return {
            "audit_status": "未结案" if blocking else "已结案",
            "vouchers_total": len(self.vouchers),
            "sampled": len(self.sampled),
            "inspected": len(self.inspected),
            "problems_open": sorted(self.open_problems.keys()),
            "rectified_done": sorted(v for v, r in self.rectified.items() if r == RECT_DONE),
            "error_count": len(self.errors),
            "blocking_count": len(blocking),
        }

    def report(self):
        s = self.status()
        lines = []
        lines.append("=" * 60)
        lines.append("审计状态：%s" % s["audit_status"])
        lines.append("凭证总数：%(vouchers_total)d  已抽样：%(sampled)d  已检查：%(inspected)d"
                     % s)
        lines.append("未结问题凭证：%s" % ("、".join(s["problems_open"]) or "无"))
        lines.append("已整改完成：%s" % ("、".join(s["rectified_done"]) or "无"))
        lines.append("-" * 60)
        lines.append("错误/异常清单（共 %d 条，其中阻断结案 %d 条）："
                     % (s["error_count"], s["blocking_count"]))
        if not self.errors:
            lines.append("  （无）")
        for i, e in enumerate(self.errors, 1):
            lines.append("  %2d. %s" % (i, e))
        lines.append("=" * 60)
        return "\n".join(lines)


# ---------------------------------------------------------------- 入口

def run(data):
    engine = AuditEngine(data)
    print(engine.report())
    return engine


SELFTEST_DATA = {
    "threshold": 0.2,
    "vouchers": [
        {"id": "V001", "amount": 1200.0, "type": "差旅费"},
        {"id": "V002", "amount": 800.0,  "type": "差旅费"},
        {"id": "V003", "amount": 1500.0, "type": "办公费"},
        {"id": "V004", "amount": 2000.0, "type": "差旅费"},
        {"id": "V005", "amount": 1200.0, "type": "差旅费"},   # V001 的关联凭证
        {"id": "V006", "amount": 300.0,  "type": "办公费"},
        {"id": "V007", "amount": 2500.0, "type": "会议费"},
        {"id": "V008", "amount": 1200.0, "type": "差旅费"},   # V001 的关联凭证（未复核 -> 级联报告）
    ],
    "rules": [
        {"name": "R1", "condition": ">=1000", "ratio": 0.5},
        {"name": "R2", "condition": "0-999",  "ratio": 0.5},
    ],
    "sampling": [
        {"rule": "R1", "vouchers": ["V001", "V003", "V004", "V007"]},
        {"rule": "R2", "vouchers": ["V002", "V006", "V999"]},   # V999 不存在
        {"rule": "R1", "vouchers": ["V002"]},                    # V002 未命中 R1 条件
    ],
    "inspections": [
        {"voucher": "V001", "result": "问题", "problem": "缺少发票"},
        {"voucher": "V003", "result": "正常"},
        {"voucher": "V004", "result": "问题", "problem": "审批链缺失"},
        {"voucher": "V004", "result": "正常"},                   # 重复检查
        {"voucher": "V005", "result": "正常"},
        {"voucher": "V888", "result": "正常"},                   # 不存在凭证
    ],
    "rectifications": [
        {"voucher": "V001", "result": "完成"},                   # 整改完成 -> 问题清单级联移除
        {"voucher": "V004", "result": "未完成"},                 # 未整改完成 -> 阻断结案
    ],
}

# 自测期望命中的关键报告片段
SELFTEST_EXPECT = [
    "审计状态：未结案",
    "抽样引用不存在的凭证 V999",
    "未命中规则 R1 的条件",
    "凭证 V004 被重复检查",
    "检查引用不存在的凭证 V888",
    "问题凭证 V004 整改未完成，不得结案",
    "关联凭证 V008（同金额 1200.0、同类型 差旅费）未复核",
    "偏差",
    "未结问题凭证：V004",
]


def selftest():
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run(SELFTEST_DATA)
    out = buf.getvalue()
    print(out)
    failed = [e for e in SELFTEST_EXPECT if e not in out]
    if failed:
        print("自测失败，缺少以下预期报告：")
        for f in failed:
            print("  - %s" % f)
        return 1
    print("自测通过：%d 项预期报告全部命中。" % len(SELFTEST_EXPECT))
    return 0


def main(argv):
    if "--selftest" in argv:
        return selftest()
    try:
        if len(argv) > 1:
            with open(argv[1], encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError) as e:
        print("输入读取失败: %s" % e, file=sys.stderr)
        return 2
    engine = AuditEngine(data)
    print(engine.report())
    return 1 if any(e.blocking for e in engine.errors) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
