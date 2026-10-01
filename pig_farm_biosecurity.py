#!/usr/bin/env python3
"""猪场防疫状态跟踪工具（纯 Python 标准库，单文件）。

用法：
    python3 pig_farm_biosecurity.py 输入文件      # 从文件读取
    python3 pig_farm_biosecurity.py               # 从标准输入读取
    python3 pig_farm_biosecurity.py --demo        # 运行内置示例

输入格式（行式文本，空白分隔，# 起为注释，按行序处理，跨流状态延续）：
    猪群   <编号> <栏舍>
    疫苗   <名称> <免疫间隔天数>
    免疫   <猪群编号> <疫苗名称> <YYYY-MM-DD>
    检疫   <猪群编号> <合格|疑似>
    疫病   <猪群编号> <疫情|解除>

防疫规则（自定，理由见各条）：
  R1 栏舍是防疫基本单元：同栏舍猪群共同暴露（共槽、通气、人员流动），
     检疫隔离与疫情扑杀均按栏舍级联。
  R2 检疫疑似：该群所在栏舍全部猪群隔离（来源记为"检疫"）。
  R3 检疫合格：视为该栏舍复检通过，移除栏舍内所有猪群的"检疫"来源；
     无其他隔离来源的猪群恢复正常。
  R4 疫情：该群所在栏舍全部猪群扑杀（参照非洲猪瘟"全群扑杀"处置，
     同栏即同一流行病学单元，部分扑杀无法阻断传播）；同时全场封锁，
     其余栏舍全部猪群隔离（来源记为"疫情"），疫情未解除前禁止解封。
  R5 解除：该栏舍须存在活动疫情，否则报错。全场无活动疫情时，移除
     所有猪群的"疫情"来源，隔离来源为空的猪群级联恢复正常；仍持有
     "检疫"来源的猪群保持隔离；已扑杀猪群不恢复。
  R6 免疫间隔：同群同疫苗两次免疫间隔小于疫苗定义间隔即报错
     （免疫仍记录，保证后续间隔基于最新日期计算）。
  R7 出栏规则：全部输入处理完毕后，未扑杀且无任何免疫记录的猪群
     报告"未免疫禁止出栏"；仍处于隔离状态的猪群报告"隔离中禁止出栏"。
  R8 免疫/检疫/疫病引用不存在的猪群或疫苗即报错并跳过该条。

退出码：存在错误时为 1，否则为 0（警告不影响退出码）。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date

STATUS_NORMAL = "正常"
STATUS_ISOLATED = "隔离"
STATUS_CULLED = "扑杀"

SRC_QUARANTINE = "检疫"
SRC_EPIDEMIC = "疫情"


@dataclass
class Group:
    gid: str
    pen: str
    status: str = STATUS_NORMAL
    isolation_sources: set = field(default_factory=set)
    vaccines: dict = field(default_factory=dict)  # 疫苗名 -> 已排序的免疫日期列表

    def refresh_status(self) -> None:
        if self.status == STATUS_CULLED:
            return
        self.status = STATUS_ISOLATED if self.isolation_sources else STATUS_NORMAL

    def immunization_summary(self) -> str:
        if not self.vaccines:
            return "无"
        parts = []
        for name in sorted(self.vaccines):
            dates = self.vaccines[name]
            parts.append(f"{name}x{len(dates)}(最近{max(dates).isoformat()})")
        return ", ".join(parts)


@dataclass
class Issue:
    level: str  # "错误" 或 "警告"
    line_no: int | None
    message: str


class Farm:
    def __init__(self) -> None:
        self.groups: dict[str, Group] = {}
        self.vaccine_intervals: dict[str, int] = {}
        self.active_epidemic_pens: set[str] = set()
        self.issues: list[Issue] = []

    # ---------- 报告 ----------
    def error(self, line_no: int | None, message: str) -> None:
        self.issues.append(Issue("错误", line_no, message))

    def warn(self, line_no: int | None, message: str) -> None:
        self.issues.append(Issue("警告", line_no, message))

    # ---------- 辅助 ----------
    def pen_groups(self, pen: str):
        return [g for g in self.groups.values() if g.pen == pen]

    def isolate_pen(self, pen: str, source: str) -> None:
        for g in self.pen_groups(pen):
            if g.status != STATUS_CULLED:
                g.isolation_sources.add(source)
                g.refresh_status()

    def release_pen_source(self, pen: str, source: str) -> None:
        for g in self.pen_groups(pen):
            g.isolation_sources.discard(source)
            g.refresh_status()

    # ---------- 定义 ----------
    def define_group(self, line_no: int, gid: str, pen: str) -> None:
        if gid in self.groups:
            self.error(line_no, f"猪群 {gid} 重复定义")
            return
        self.groups[gid] = Group(gid, pen)

    def define_vaccine(self, line_no: int, name: str, interval_text: str) -> None:
        if name in self.vaccine_intervals:
            self.error(line_no, f"疫苗 {name} 重复定义")
            return
        try:
            interval = int(interval_text)
        except ValueError:
            self.error(line_no, f"疫苗 {name} 的免疫间隔不是整数: {interval_text}")
            return
        if interval < 0:
            self.error(line_no, f"疫苗 {name} 的免疫间隔不能为负: {interval}")
            return
        self.vaccine_intervals[name] = interval

    # ---------- 免疫流 ----------
    def immunize(self, line_no: int, gid: str, vaccine: str, date_text: str) -> None:
        group = self.groups.get(gid)
        if group is None:
            self.error(line_no, f"免疫引用了不存在的猪群: {gid}")
            return
        if vaccine not in self.vaccine_intervals:
            self.error(line_no, f"免疫引用了不存在的疫苗: {vaccine}")
            return
        try:
            day = date.fromisoformat(date_text)
        except ValueError:
            self.error(line_no, f"免疫日期格式非法（应为 YYYY-MM-DD）: {date_text}")
            return
        if group.status == STATUS_CULLED:
            self.error(line_no, f"猪群 {gid} 已扑杀，不能再免疫")
            return
        interval = self.vaccine_intervals[vaccine]
        history = group.vaccines.setdefault(vaccine, [])
        if history:
            last = max(history)
            gap = (day - last).days
            if gap < interval:
                self.error(
                    line_no,
                    f"免疫间隔不足: 猪群 {gid} 疫苗 {vaccine} "
                    f"距上次免疫仅 {gap} 天 < 要求 {interval} 天",
                )
        history.append(day)
        history.sort()

    # ---------- 检疫流 ----------
    def quarantine(self, line_no: int, gid: str, result: str) -> None:
        group = self.groups.get(gid)
        if group is None:
            self.error(line_no, f"检疫引用了不存在的猪群: {gid}")
            return
        if result not in ("合格", "疑似"):
            self.error(line_no, f"检疫结果非法（应为 合格/疑似）: {result}")
            return
        if group.status == STATUS_CULLED:
            self.error(line_no, f"猪群 {gid} 已扑杀，不能再检疫")
            return
        if result == "疑似":
            self.isolate_pen(group.pen, SRC_QUARANTINE)  # R2：栏舍级联隔离
        else:
            self.release_pen_source(group.pen, SRC_QUARANTINE)  # R3：栏舍级联解除

    # ---------- 疫病流 ----------
    def epidemic(self, line_no: int, gid: str, kind: str) -> None:
        group = self.groups.get(gid)
        if group is None:
            self.error(line_no, f"疫病记录引用了不存在的猪群: {gid}")
            return
        if kind not in ("疫情", "解除"):
            self.error(line_no, f"疫病类型非法（应为 疫情/解除）: {kind}")
            return
        if kind == "疫情":
            self._outbreak(line_no, group)
        else:
            self._lift(line_no, group)

    def _outbreak(self, line_no: int, group: Group) -> None:
        if group.status == STATUS_CULLED:
            self.error(line_no, f"猪群 {gid_fmt(group)} 已扑杀，不能再次发生疫情")
            return
        if group.pen in self.active_epidemic_pens:
            self.error(line_no, f"栏舍 {group.pen} 疫情尚未解除，不能重复报告疫情")
            return
        self.active_epidemic_pens.add(group.pen)
        # R4a：同栏舍全部扑杀
        for g in self.pen_groups(group.pen):
            g.status = STATUS_CULLED
            g.isolation_sources.clear()
        # R4b：全场封锁，其余栏舍猪群隔离（来源"疫情"）
        for g in self.groups.values():
            if g.status != STATUS_CULLED:
                g.isolation_sources.add(SRC_EPIDEMIC)
                g.refresh_status()

    def _lift(self, line_no: int, group: Group) -> None:
        if group.pen not in self.active_epidemic_pens:
            self.error(line_no, f"栏舍 {group.pen} 没有活动疫情，无法解除")
            return
        self.active_epidemic_pens.discard(group.pen)
        # R5：全场无活动疫情时才解除封锁，级联恢复
        if not self.active_epidemic_pens:
            for g in self.groups.values():
                g.isolation_sources.discard(SRC_EPIDEMIC)
                g.refresh_status()

    # ---------- 出栏检查 ----------
    def finalize(self) -> None:
        for gid in sorted(self.groups):
            g = self.groups[gid]
            if g.status == STATUS_CULLED:
                continue
            if not any(g.vaccines.values()):
                self.warn(None, f"猪群 {gid}（栏舍 {g.pen}）从未免疫，禁止出栏")
            if g.status == STATUS_ISOLATED:
                src = "+".join(sorted(g.isolation_sources))
                self.warn(None, f"猪群 {gid}（栏舍 {g.pen}）仍在隔离（来源: {src}），禁止出栏")

    # ---------- 解析与驱动 ----------
    def run(self, text: str) -> None:
        for line_no, raw in enumerate(text.splitlines(), start=1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            cmd, args = parts[0], parts[1:]
            if cmd == "猪群" and len(args) == 2:
                self.define_group(line_no, args[0], args[1])
            elif cmd == "疫苗" and len(args) == 2:
                self.define_vaccine(line_no, args[0], args[1])
            elif cmd == "免疫" and len(args) == 3:
                self.immunize(line_no, args[0], args[1], args[2])
            elif cmd == "检疫" and len(args) == 2:
                self.quarantine(line_no, args[0], args[1])
            elif cmd == "疫病" and len(args) == 2:
                self.epidemic(line_no, args[0], args[1])
            else:
                self.error(line_no, f"无法解析的行: {raw.strip()}")
        self.finalize()

    # ---------- 输出 ----------
    def render_status(self) -> str:
        lines = ["========== 防疫状态 =========="]
        pens: dict[str, list[Group]] = {}
        for g in self.groups.values():
            pens.setdefault(g.pen, []).append(g)
        for pen in sorted(pens):
            lines.append(f"栏舍 {pen}" + ("  [疫情中]" if pen in self.active_epidemic_pens else ""))
            for g in sorted(pens[pen], key=lambda x: x.gid):
                status = g.status
                if g.status == STATUS_ISOLATED:
                    status += "(来源: " + "+".join(sorted(g.isolation_sources)) + ")"
                lines.append(f"  猪群 {g.gid}  状态: {status}  免疫: {g.immunization_summary()}")
        return "\n".join(lines)

    def render_report(self) -> str:
        lines = ["========== 错误与警告清单 =========="]
        if not self.issues:
            lines.append("（无）")
            return "\n".join(lines)
        for i, issue in enumerate(self.issues, start=1):
            where = f"第 {issue.line_no} 行: " if issue.line_no is not None else ""
            lines.append(f"{i}. [{issue.level}] {where}{issue.message}")
        errors = sum(1 for x in self.issues if x.level == "错误")
        warns = len(self.issues) - errors
        lines.append(f"合计: {errors} 个错误, {warns} 个警告")
        return "\n".join(lines)

    @property
    def error_count(self) -> int:
        return sum(1 for x in self.issues if x.level == "错误")


def gid_fmt(group: Group) -> str:
    return group.gid


DEMO_INPUT = """\
# 猪群与疫苗定义
猪群 G1 A栏
猪群 G2 A栏
猪群 G3 B栏
猪群 G4 C栏
疫苗 猪瘟 21
疫苗 口蹄疫 30

# 免疫流
免疫 G1 猪瘟 2026-01-01
免疫 G1 猪瘟 2026-01-10      # 间隔仅 9 天 < 21 天，应报错
免疫 G2 猪瘟 2026-01-05
免疫 G9 猪瘟 2026-01-05      # 猪群不存在，应报错
免疫 G3 未知苗 2026-01-05    # 疫苗不存在，应报错

# 检疫流
检疫 G3 疑似                 # B栏级联隔离
检疫 G3 合格                 # B栏解除检疫隔离

# 疫病流
疫病 G1 疫情                 # A栏 G1/G2 扑杀；G3/G4 封锁隔离
免疫 G2 口蹄疫 2026-02-01    # 已扑杀，应报错
检疫 G4 疑似                 # G4 叠加"检疫"隔离来源
疫病 G1 解除                 # 解除封锁：G3 恢复正常，G4 仍因检疫隔离
疫病 G3 解除                 # B栏无活动疫情，应报错
检疫 G4 合格                 # G4 恢复正常
免疫 G4 口蹄疫 2026-03-01
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="猪场防疫状态跟踪工具（纯标准库）")
    parser.add_argument("input", nargs="?", help="输入文件（缺省从标准输入读取）")
    parser.add_argument("--demo", action="store_true", help="运行内置示例")
    args = parser.parse_args(argv)

    if args.demo:
        text = DEMO_INPUT
    elif args.input:
        with open(args.input, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()

    farm = Farm()
    farm.run(text)
    print(farm.render_status())
    print()
    print(farm.render_report())
    return 1 if farm.error_count else 0


if __name__ == "__main__":
    sys.exit(main())
