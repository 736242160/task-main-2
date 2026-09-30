#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""猪场防疫状态跟踪工具（纯 Python 标准库，单文件）。

用法：
    python3 pig_farm_guard.py [输入文件]     # 省略文件时从标准输入读取

输入格式（每行一条指令，# 之后为注释，空白分隔）：
    猪群 <编号> <栏舍>                 定义猪群
    疫苗 <名称> <免疫间隔天数>          定义疫苗
    免疫 <猪群> <疫苗> <YYYY-MM-DD>     免疫流
    检疫 <猪群> <合格|疑似>             检疫流
    疫病 <猪群> <疫情|解除>             疫病流
    出栏 <猪群> <YYYY-MM-DD>           出栏流（规则自定，见下）

事件按文件出现顺序处理，各流共享同一份猪群状态（跨流状态延续）。

自定规则（设计说明）：
 1. 检疫“疑似”：该猪群隔离；同栏舍其余“正常”猪群级联隔离（同居一栏视为
    密切接触，必须一并观察）。检疫“合格”仅解除该群自身的检疫性隔离。
 2. 疫病“疫情”：发病群标记为“疫情”（假定隔离治疗，不直接扑杀）；同栏舍
    其余未出栏/未扑杀猪群一律级联扑杀（同栏直接接触，传染风险最高，
    扑杀是止损常规手段）。不同栏舍不级联（栏舍为物理隔离单元）。
 3. 疫病“解除”：发病群恢复“正常”；全场所有“隔离”猪群级联恢复“正常”
    （解除视为全场疫情警报解除，预防性隔离一并取消）。
 4. 出栏：无任何免疫记录的猪群出栏 → 报错“未免疫出栏”；隔离/疫情状态
    下出栏 → 报错；扑杀/已出栏猪群再出栏 → 报错。
 5. 对已扑杀/已出栏猪群执行免疫、检疫、疫病操作 → 报错（状态机非法迁移）。
 6. 同一猪群同一疫苗两次免疫间隔小于疫苗定义间隔 → 报错“免疫间隔不足”，
    但该次免疫仍记录在案（物理上已发生，后续间隔从最近一次算起）。
 7. 免疫/检疫/疫病/出栏引用未定义的猪群或疫苗 → 报错，该条指令忽略。
"""

import sys
from dataclasses import dataclass, field
from datetime import date

STATUS_NORMAL = "正常"
STATUS_ISOLATED = "隔离"
STATUS_OUTBREAK = "疫情"
STATUS_CULLED = "扑杀"
STATUS_SOLD = "出栏"

ACTIVE_STATUSES = (STATUS_NORMAL, STATUS_ISOLATED, STATUS_OUTBREAK)


@dataclass
class Group:
    gid: str
    pen: str
    status: str = STATUS_NORMAL
    isolation_reason: str = ""  # 隔离原因：检疫 / 疫情
    immunizations: list = field(default_factory=list)  # [(疫苗名, 日期), ...]


@dataclass
class Vaccine:
    name: str
    interval: int  # 免疫间隔（天）


class Farm:
    def __init__(self):
        self.groups = {}    # 编号 -> Group（保持定义顺序）
        self.vaccines = {}  # 名称 -> Vaccine
        self.errors = []    # [(行号, 消息), ...]

    # ---------- 基础工具 ----------
    def error(self, lineno, msg):
        self.errors.append((lineno, msg))

    def get_active_group(self, lineno, gid, action):
        """取出猪群并校验：存在且未扑杀/未出栏。失败则报错并返回 None。"""
        group = self.groups.get(gid)
        if group is None:
            self.error(lineno, f"{action}引用了不存在的猪群 '{gid}'")
            return None
        if group.status in (STATUS_CULLED, STATUS_SOLD):
            self.error(lineno, f"{action}作用于已{group.status}的猪群 '{gid}'")
            return None
        return group

    @staticmethod
    def parse_date(lineno, text, errors_sink):
        try:
            return date.fromisoformat(text)
        except ValueError:
            errors_sink(lineno, f"日期 '{text}' 非法，应为 YYYY-MM-DD")
            return None

    # ---------- 定义指令 ----------
    def define_group(self, lineno, gid, pen):
        if gid in self.groups:
            self.error(lineno, f"猪群 '{gid}' 重复定义")
            return
        self.groups[gid] = Group(gid, pen)

    def define_vaccine(self, lineno, name, interval_text):
        if name in self.vaccines:
            self.error(lineno, f"疫苗 '{name}' 重复定义")
            return
        try:
            interval = int(interval_text)
            if interval < 0:
                raise ValueError
        except ValueError:
            self.error(lineno, f"疫苗 '{name}' 的免疫间隔 '{interval_text}' 不是非负整数")
            return
        self.vaccines[name] = Vaccine(name, interval)

    # ---------- 免疫流 ----------
    def immunize(self, lineno, gid, vname, date_text):
        group = self.get_active_group(lineno, gid, "免疫")
        if group is None:
            return
        vaccine = self.vaccines.get(vname)
        if vaccine is None:
            self.error(lineno, f"免疫引用了不存在的疫苗 '{vname}'")
            return
        day = self.parse_date(lineno, date_text, self.error)
        if day is None:
            return
        previous = [d for (vn, d) in group.immunizations if vn == vname]
        if previous:
            last = max(previous)
            gap = (day - last).days
            if gap < vaccine.interval:
                self.error(
                    lineno,
                    f"免疫间隔不足：猪群 '{gid}' 疫苗 '{vname}' 距上次免疫 "
                    f"{last.isoformat()} 仅 {gap} 天（要求 >= {vaccine.interval} 天）",
                )
        group.immunizations.append((vname, day))

    # ---------- 检疫流 ----------
    def quarantine(self, lineno, gid, result):
        group = self.get_active_group(lineno, gid, "检疫")
        if group is None:
            return
        if result == "疑似":
            # 本群隔离（疫情状态更重，不降级）
            if group.status == STATUS_NORMAL:
                group.status = STATUS_ISOLATED
                group.isolation_reason = "检疫"
            # 同栏舍其余正常猪群级联隔离
            for other in self.groups.values():
                if other is not group and other.pen == group.pen \
                        and other.status == STATUS_NORMAL:
                    other.status = STATUS_ISOLATED
                    other.isolation_reason = "检疫"
        elif result == "合格":
            # 仅解除本群的检疫性隔离
            if group.status == STATUS_ISOLATED and group.isolation_reason == "检疫":
                group.status = STATUS_NORMAL
                group.isolation_reason = ""
        else:
            self.error(lineno, f"检疫结果 '{result}' 非法，应为 合格/疑似")

    # ---------- 疫病流 ----------
    def epidemic(self, lineno, gid, kind):
        group = self.get_active_group(lineno, gid, "疫病")
        if group is None:
            return
        if kind == "疫情":
            group.status = STATUS_OUTBREAK
            group.isolation_reason = ""
            # 同栏舍其余在场猪群级联扑杀
            for other in self.groups.values():
                if other is not group and other.pen == group.pen \
                        and other.status in (STATUS_NORMAL, STATUS_ISOLATED):
                    other.status = STATUS_CULLED
                    other.isolation_reason = ""
        elif kind == "解除":
            if group.status != STATUS_OUTBREAK:
                self.error(lineno, f"猪群 '{gid}' 未发生疫情，无法解除")
                return
            group.status = STATUS_NORMAL
            # 全场隔离猪群级联恢复
            for other in self.groups.values():
                if other.status == STATUS_ISOLATED:
                    other.status = STATUS_NORMAL
                    other.isolation_reason = ""
        else:
            self.error(lineno, f"疫病类型 '{kind}' 非法，应为 疫情/解除")

    # ---------- 出栏流 ----------
    def sell(self, lineno, gid, date_text):
        group = self.get_active_group(lineno, gid, "出栏")
        if group is None:
            return
        day = self.parse_date(lineno, date_text, self.error)
        if day is None:
            return
        if not group.immunizations:
            self.error(lineno, f"未免疫出栏：猪群 '{gid}' 无任何免疫记录")
        if group.status in (STATUS_ISOLATED, STATUS_OUTBREAK):
            self.error(lineno, f"猪群 '{gid}' 处于{group.status}状态，禁止出栏")
            return
        group.status = STATUS_SOLD

    # ---------- 解析与驱动 ----------
    def process(self, text):
        handlers = {
            "猪群": (2, lambda ln, a: self.define_group(ln, a[0], a[1])),
            "疫苗": (2, lambda ln, a: self.define_vaccine(ln, a[0], a[1])),
            "免疫": (3, lambda ln, a: self.immunize(ln, a[0], a[1], a[2])),
            "检疫": (2, lambda ln, a: self.quarantine(ln, a[0], a[1])),
            "疫病": (2, lambda ln, a: self.epidemic(ln, a[0], a[1])),
            "出栏": (2, lambda ln, a: self.sell(ln, a[0], a[1])),
        }
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            keyword, args = parts[0], parts[1:]
            entry = handlers.get(keyword)
            if entry is None:
                self.error(lineno, f"无法识别的指令 '{keyword}'")
                continue
            arity, handler = entry
            if len(args) != arity:
                self.error(lineno, f"指令 '{keyword}' 需要 {arity} 个参数，实际 {len(args)} 个")
                continue
            handler(lineno, args)

    # ---------- 输出 ----------
    def report(self):
        lines = ["===== 防疫状态 ====="]
        if not self.groups:
            lines.append("（无猪群定义）")
        for g in self.groups.values():
            detail = f"猪群 {g.gid} | 栏舍 {g.pen} | 状态：{g.status}"
            if g.status == STATUS_ISOLATED and g.isolation_reason:
                detail += f"（{g.isolation_reason}所致）"
            if g.immunizations:
                records = ", ".join(f"{vn}@{d.isoformat()}" for vn, d in g.immunizations)
                detail += f" | 免疫 {len(g.immunizations)} 次：{records}"
            else:
                detail += " | 无免疫记录"
            lines.append(detail)
        lines.append("")
        lines.append("===== 错误报告 =====")
        if self.errors:
            for i, (lineno, msg) in enumerate(self.errors, 1):
                lines.append(f"{i}. [行 {lineno}] {msg}")
        else:
            lines.append("无错误")
        return "\n".join(lines)


def main(argv):
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    farm = Farm()
    farm.process(text)
    print(farm.report())
    return 1 if farm.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
