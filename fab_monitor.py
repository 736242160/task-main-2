#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fab_monitor.py — 晶圆制造状态监控与错误报告工具（纯 Python 标准库，单文件）

用法：
    python3 fab_monitor.py 输入文件        # 处理指定输入文件
    python3 fab_monitor.py                 # 从标准输入读取
    python3 fab_monitor.py --demo          # 运行内嵌自测样例

输入格式（行式文本，顺序处理、状态跨行延续；# 开头为注释；字段空白分隔；
列表用英文逗号，"-" 表示空）：

    工序 <名称> <依赖工序列表|-> <设备类型>
    设备 <名称> <设备类型> <可用|检修>
    批次 <编号> <工序序列(逗号分隔)> <目标良率: 0~1 或 百分比如 85%>
    加工 <批次> <工序> <设备> <良品|废品>
    故障 <设备>          # 设备停机转检修，队列批次级联改派
    修复 <设备>          # 设备恢复可用，待派批次自动补派

自定规则及理由：
  1. 良率 = 良品次数 / 总加工次数，逐条加工事件实时统计（晶圆厂按批次
     实时盯良率，越早发现异常越能止损）。
  2. 良率处置阈值：实时良率低于目标即触发。低于目标超过 15 个百分点
     （DOWNGRADE_MARGIN）判【报废】——差距过大说明批次已无可挽回，
     继续流片只会浪费后续工序产能；差距在 15 个百分点以内判【降级】
     ——批次尚有残值，允许继续流片但全程标记，出货时降档处理。
  3. 报废批次后续工序全部级联取消，并从设备队列中清除，拒绝再加工。
  4. 设备故障后，其队列中的批次按"同设备类型 + 状态可用 + 队列负载
     最轻（负载相同按名称序）"规则级联改派；无可用设备则挂起为待派，
     待同类型设备修复后自动补派。
"""

import sys

DOWNGRADE_MARGIN = 0.15  # 低于目标超过 15 个百分点判报废，否则判降级


class Process:
    def __init__(self, name, deps, equip_type):
        self.name = name
        self.deps = deps
        self.equip_type = equip_type


class Equipment:
    def __init__(self, name, eq_type, status):
        self.name = name
        self.type = eq_type
        self.status = status
        self.queue = []  # 已派工、等待加工的批次编号


class Lot:
    def __init__(self, lot_id, sequence, target):
        self.id = lot_id
        self.sequence = sequence
        self.target = target
        self.cursor = 0          # 下一道待做工序的下标
        self.good = 0
        self.total = 0
        self.completed = set()
        self.status = "在制"      # 在制/降级/报废/完成/完成(降级)
        self.assigned = None      # 当前派工设备
        self.pending = False      # 是否待派（无可用设备）

    @property
    def yield_rate(self):
        return self.good / self.total if self.total else None


class Fab:
    def __init__(self):
        self.processes = {}
        self.equipment = {}
        self.lots = {}
        self.errors = []
        self.events = []

    # ---------- 工具 ----------

    def err(self, line, msg):
        self.errors.append("行%d: %s" % (line, msg))

    def assign(self, lot, line, reason="派工"):
        """把批次当前工序派给 同类型+可用+负载最轻 的设备。"""
        if lot.status == "报废" or lot.cursor >= len(lot.sequence):
            return None
        proc = self.processes.get(lot.sequence[lot.cursor])
        if proc is None:
            return None
        cands = sorted(
            (e for e in self.equipment.values()
             if e.type == proc.equip_type and e.status == "可用"),
            key=lambda e: (len(e.queue), e.name))
        if not cands:
            lot.pending = True
            lot.assigned = None
            self.events.append(
                "行%d: 批次 %s 待派（无可用 %s 设备）" % (line, lot.id, proc.equip_type))
            return None
        eq = cands[0]
        eq.queue.append(lot.id)
        lot.assigned = eq.name
        lot.pending = False
        self.events.append(
            "行%d: %s 批次 %s -> 设备 %s（工序 %s）" % (line, reason, lot.id, eq.name, proc.name))
        return eq.name

    def dequeue(self, lot):
        if lot.assigned and lot.assigned in self.equipment:
            q = self.equipment[lot.assigned].queue
            if lot.id in q:
                q.remove(lot.id)
        lot.assigned = None

    # ---------- 指令处理 ----------

    def cmd_process(self, name, deps, eq_type, line):
        if name in self.processes:
            self.err(line, "工序 %s 重复定义" % name)
            return
        for d in deps:
            if d not in self.processes:
                self.err(line, "工序 %s 引用了不存在的依赖工序 %s" % (name, d))
        self.processes[name] = Process(name, deps, eq_type)

    def cmd_equip(self, name, eq_type, status, line):
        if name in self.equipment:
            self.err(line, "设备 %s 重复定义" % name)
            return
        if status not in ("可用", "检修"):
            self.err(line, "设备 %s 状态非法：%s（应为 可用/检修）" % (name, status))
            return
        self.equipment[name] = Equipment(name, eq_type, status)

    def cmd_lot(self, lot_id, seq, target, line):
        if lot_id in self.lots:
            self.err(line, "批次 %s 重复定义" % lot_id)
            return
        ok = True
        for p in seq:
            if p not in self.processes:
                self.err(line, "批次 %s 引用了不存在的工序 %s" % (lot_id, p))
                ok = False
        if not ok:
            return
        lot = Lot(lot_id, seq, target)
        self.lots[lot_id] = lot
        self.assign(lot, line)

    def cmd_run(self, lot_id, pname, eq_name, result, line):
        lot = self.lots.get(lot_id)
        proc = self.processes.get(pname)
        eq = self.equipment.get(eq_name)
        ok = True
        if lot is None:
            self.err(line, "加工引用了不存在的批次 %s" % lot_id)
            ok = False
        if proc is None:
            self.err(line, "加工引用了不存在的工序 %s" % pname)
            ok = False
        if eq is None:
            self.err(line, "加工引用了不存在的设备 %s" % eq_name)
            ok = False
        if result not in ("良品", "废品"):
            self.err(line, "加工结果非法：%s（应为 良品/废品）" % result)
            ok = False
        if not ok:
            return

        if lot.status == "报废":
            self.err(line, "批次 %s 已报废，后续工序 %s 级联取消，拒绝加工" % (lot_id, pname))
            return
        if lot.cursor >= len(lot.sequence):
            self.err(line, "批次 %s 全部工序已完成，重复加工工序 %s" % (lot_id, pname))
            return
        if pname in lot.completed:
            self.err(line, "重复加工：批次 %s 的工序 %s 已完成" % (lot_id, pname))
            return
        expected = lot.sequence[lot.cursor]
        if pname != expected:
            if pname in lot.sequence:
                self.err(line, "工序顺序错误：批次 %s 应执行 %s，实际报工 %s（前序工序未完成）"
                         % (lot_id, expected, pname))
            else:
                self.err(line, "工序 %s 不在批次 %s 的工序序列中" % (pname, lot_id))
            return
        missing = [d for d in proc.deps if d not in lot.completed]
        if missing:
            self.err(line, "依赖未完成：工序 %s 依赖 %s，批次 %s 尚未完成"
                     % (pname, ",".join(missing), lot_id))
            return
        if eq.type != proc.equip_type:
            self.err(line, "设备类型不匹配：工序 %s 需要 %s，设备 %s 为 %s"
                     % (pname, proc.equip_type, eq_name, eq.type))
            return
        if eq.status != "可用":
            self.err(line, "设备 %s 检修中，不得加工（批次 %s 工序 %s）" % (eq_name, lot_id, pname))
            return

        # ---- 执行加工 ----
        lot.total += 1
        if result == "良品":
            lot.good += 1
        lot.completed.add(pname)
        lot.cursor += 1
        self.dequeue(lot)

        y = lot.yield_rate
        # ---- 良率实时处置 ----
        if y < lot.target:
            if y < lot.target - DOWNGRADE_MARGIN:
                lot.status = "报废"
                lot.pending = False
                remaining = lot.sequence[lot.cursor:]
                self.events.append(
                    "行%d: 批次 %s 实时良率 %.1f%% 低于目标 %.1f%% 超过 %.0f%%，判报废；"
                    "后续工序级联取消：%s"
                    % (line, lot_id, y * 100, lot.target * 100, DOWNGRADE_MARGIN * 100,
                       ",".join(remaining) if remaining else "无"))
                return
            if lot.status != "降级":
                lot.status = "降级"
                self.events.append(
                    "行%d: 批次 %s 实时良率 %.1f%% 低于目标 %.1f%%（差距 %.0f%% 以内），判降级，"
                    "允许继续流片并标记" % (line, lot_id, y * 100, lot.target * 100,
                                            DOWNGRADE_MARGIN * 100))

        if lot.cursor >= len(lot.sequence):
            lot.status = "完成(降级)" if lot.status == "降级" else "完成"
            self.events.append(
                "行%d: 批次 %s 全部工序完成，最终良率 %.1f%%（%s）"
                % (line, lot_id, y * 100, lot.status))
        else:
            self.assign(lot, line)

    def cmd_fail(self, name, line):
        eq = self.equipment.get(name)
        if eq is None:
            self.err(line, "故障引用了不存在的设备 %s" % name)
            return
        eq.status = "检修"
        self.events.append("行%d: 设备 %s 故障停机，转入检修" % (line, name))
        queued = list(eq.queue)
        eq.queue.clear()
        for lot_id in queued:
            lot = self.lots[lot_id]
            if lot.status == "报废":
                continue
            lot.assigned = None
            self.events.append("行%d: 批次 %s 受设备 %s 故障影响，级联改派" % (line, lot_id, name))
            self.assign(lot, line, reason="改派")

    def cmd_repair(self, name, line):
        eq = self.equipment.get(name)
        if eq is None:
            self.err(line, "修复引用了不存在的设备 %s" % name)
            return
        eq.status = "可用"
        self.events.append("行%d: 设备 %s 修复完成，恢复可用" % (line, name))
        for lot in self.lots.values():
            if lot.pending and lot.status != "报废" and lot.cursor < len(lot.sequence):
                proc = self.processes[lot.sequence[lot.cursor]]
                if proc.equip_type == eq.type:
                    self.assign(lot, line, reason="补派")

    # ---------- 解析主循环 ----------

    def feed(self, text):
        for line_no, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            cmd, args = parts[0], parts[1:]
            try:
                if cmd == "工序" and len(args) == 3:
                    deps = [] if args[1] == "-" else args[1].split(",")
                    self.cmd_process(args[0], deps, args[2], line_no)
                elif cmd == "设备" and len(args) == 3:
                    self.cmd_equip(args[0], args[1], args[2], line_no)
                elif cmd == "批次" and len(args) == 3:
                    target = args[2]
                    target = float(target[:-1]) / 100 if target.endswith("%") else float(target)
                    if not 0 <= target <= 1:
                        raise ValueError
                    self.cmd_lot(args[0], args[1].split(","), target, line_no)
                elif cmd == "加工" and len(args) == 4:
                    self.cmd_run(args[0], args[1], args[2], args[3], line_no)
                elif cmd == "故障" and len(args) == 1:
                    self.cmd_fail(args[0], line_no)
                elif cmd == "修复" and len(args) == 1:
                    self.cmd_repair(args[0], line_no)
                else:
                    self.err(line_no, "无法解析的指令或字段数量错误：%s" % raw.strip())
            except ValueError:
                self.err(line_no, "数值格式错误：%s" % raw.strip())

    # ---------- 输出 ----------

    def report(self):
        out = ["===== 制造状态 =====", "[批次]"]
        for lot in self.lots.values():
            y = lot.yield_rate
            y_str = "%.1f%%（良品%d/共%d）" % (y * 100, lot.good, lot.total) if y is not None else "—（未加工）"
            cur = lot.sequence[lot.cursor] if lot.cursor < len(lot.sequence) else "-"
            out.append(
                "批次 %-6s 状态=%-6s 进度=%d/%d 当前工序=%-4s 实时良率=%-18s 目标=%.1f%% 派工设备=%s"
                % (lot.id, lot.status, lot.cursor, len(lot.sequence), cur,
                   y_str, lot.target * 100, lot.assigned or ("待派" if lot.pending else "-")))
        out.append("[设备]")
        for eq in self.equipment.values():
            out.append("设备 %-6s 类型=%-6s 状态=%-2s 队列=[%s]"
                       % (eq.name, eq.type, eq.status, ",".join(eq.queue)))
        out.append("===== 事件报告 =====")
        out.extend(self.events if self.events else ["（无）"])
        out.append("===== 错误清单 =====")
        out.extend(self.errors if self.errors else ["（无错误）"])
        return "\n".join(out)


DEMO = """\
# ---------- 工序定义 ----------
工序 氧化 - 炉管
工序 光刻 氧化 光刻机
工序 刻蚀 光刻 刻蚀机
工序 沉积 刻蚀 炉管
工序 注入 氧化 注入机
# ---------- 设备定义 ----------
设备 F1 炉管 可用
设备 F2 炉管 检修
设备 L1 光刻机 可用
设备 E1 刻蚀机 可用
设备 E2 刻蚀机 可用
设备 I1 注入机 可用
# ---------- 批次流 ----------
批次 LOT1 氧化,光刻,刻蚀,沉积 0.8
批次 LOT2 氧化,光刻,刻蚀 0.9
批次 LOT3 氧化,光刻 0.6
批次 LOT4 氧化,注入 0.5
# ---------- 加工流（状态跨行延续） ----------
加工 LOT1 氧化 F1 良品
加工 LOT1 光刻 L1 良品
加工 LOT1 光刻 L1 良品        # 重复加工同批次同工序 -> 报错
加工 LOT2 刻蚀 E1 良品        # 前序未完成，工序顺序错误 -> 报错
加工 LOT2 氧化 F2 良品        # F2 检修中 -> 报错
加工 LOT2 氧化 L1 良品        # 光刻机干炉管活，类型不匹配 -> 报错
加工 LOT2 氧化 F1 废品        # 良率 0% 远低于目标 90% -> 报废，级联取消后续
加工 LOT2 光刻 L1 良品        # 已报废批次拒绝加工 -> 报错
加工 LOT9 氧化 F1 良品        # 批次不存在 -> 报错
加工 LOT1 刻蚀 E9 良品        # 设备不存在 -> 报错
故障 E1                       # LOT1 在 E1 队列中 -> 级联改派 E2
加工 LOT1 刻蚀 E2 良品
加工 LOT3 氧化 F1 良品
加工 LOT3 光刻 L1 废品        # 良率 50% 低于目标 60% 但差距 <=15% -> 降级
加工 LOT4 氧化 F1 良品
故障 I1                       # 无备用注入机 -> LOT4 挂起待派
修复 I1                       # 修复后 LOT4 自动补派
加工 LOT4 注入 I1 良品
加工 LOT1 沉积 F1 良品        # LOT1 全部完成
加工 LOT1 沉积 F1 良品        # 已完成批次再加工 -> 报错
"""


def main(argv):
    fab = Fab()
    if len(argv) > 1 and argv[1] == "--demo":
        print("----- 自测样例输入 -----")
        print(DEMO)
        fab.feed(DEMO)
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            fab.feed(f.read())
    else:
        fab.feed(sys.stdin.read())
    print(fab.report())
    return 1 if fab.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
