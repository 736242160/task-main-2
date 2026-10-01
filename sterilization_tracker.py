#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""医疗器械灭菌追溯工具（纯 Python 标准库，单文件）

用法:
    python3 sterilization_tracker.py 输入文件      # 从文件读取
    python3 sterilization_tracker.py              # 从标准输入读取
    python3 sterilization_tracker.py --demo       # 运行内置示例

输入格式（按行，顺序处理，# 开头为注释，空行忽略）:
    灭菌器 <名称> <类型:高温|低温>
    器械包 <编号> <类型:高温|低温> <器械1,器械2,...>
    灭菌   <记录号> <包编号> <灭菌器名称> <参数> <时长分钟> <结果:合格|不合格>
    放行   <记录号> <结果:放行|拒收>

参数含义: 高温灭菌器为温度(°C)，低温灭菌器为浓度(mg/L)。

判定标准（自定规则及理由）:
    高温(压力蒸汽): 参考 WS 310 / EN 285 常用灭菌周期——
        温度 >= 134°C 时 时长 >= 4 分钟；温度 >= 121°C 时 时长 >= 20 分钟；
        低于 121°C 一律不合格。温度越低所需时间越长，低于下限时
        芽孢杀灭率无法保证，故直接判不合格。
    低温(环氧乙烷): 常规要求浓度 450~1200 mg/L 且作用 >= 60 分钟——
        浓度 >= 450 mg/L 且 时长 >= 60 分钟判合格，否则不合格。
    申报结果为"合格"但参数不达标时，以参数判定为准改判不合格并报告。

批次定义: 同一灭菌器、相同参数、相同时长的灭菌记录视为同一灭菌批次
（同一次灭菌循环内参数曲线一致，可同批装载多个器械包）。
"""

import sys
from dataclasses import dataclass, field

HIGH_TEMP = "高温"
LOW_TEMP = "低温"
VALID_RESULTS = ("合格", "不合格")
VALID_RELEASE = ("放行", "拒收")


def check_params(stype, param, duration):
    """按灭菌器类型判定参数是否达标，返回 (是否达标, 依据说明)。"""
    if stype == HIGH_TEMP:
        if param >= 134 and duration >= 4:
            return True, "高温: >=134°C 且 >=4分钟"
        if param >= 121 and duration >= 20:
            return True, "高温: >=121°C 且 >=20分钟"
        return False, "高温: 需 >=134°C/4分钟 或 >=121°C/20分钟"
    if param >= 450 and duration >= 60:
        return True, "低温: 浓度>=450mg/L 且 >=60分钟"
    return False, "低温: 需 浓度>=450mg/L 且 >=60分钟"


@dataclass
class Sterilizer:
    name: str
    stype: str


@dataclass
class Pack:
    pid: str
    ptype: str
    instruments: tuple
    status: str = "待灭菌"


@dataclass
class SterRecord:
    rid: str
    pid: str
    sterilizer: str
    param: float
    duration: float
    declared: str
    effective: str = ""
    valid: bool = True
    note: str = ""

    def batch_key(self):
        return (self.sterilizer, self.param, self.duration)


class Tracker:
    def __init__(self):
        self.sterilizers = {}
        self.packs = {}
        self.records = {}
        self.released = set()
        self.errors = []   # (级别, 类别, 描述)

    def report(self, level, category, msg):
        self.errors.append((level, category, msg))

    # ---------- 定义 ----------
    def add_sterilizer(self, name, stype, lineno):
        if stype not in (HIGH_TEMP, LOW_TEMP):
            self.report("错误", "定义", f"第{lineno}行: 灭菌器 {name} 类型非法: {stype}")
            return
        self.sterilizers[name] = Sterilizer(name, stype)

    def add_pack(self, pid, ptype, instruments, lineno):
        if ptype not in (HIGH_TEMP, LOW_TEMP):
            self.report("错误", "定义", f"第{lineno}行: 器械包 {pid} 类型非法: {ptype}")
            return
        self.packs[pid] = Pack(pid, ptype, tuple(instruments))

    # ---------- 灭菌流 ----------
    def add_sterilization(self, rid, pid, sname, param, duration, declared, lineno):
        if rid in self.records:
            self.report("错误", "重复记录", f"第{lineno}行: 灭菌记录号 {rid} 重复")
            return
        rec = SterRecord(rid, pid, sname, param, duration, declared)
        self.records[rid] = rec

        pack = self.packs.get(pid)
        ster = self.sterilizers.get(sname)
        if pack is None:
            rec.valid = False
            self.report("错误", "未知器械包", f"第{lineno}行: 灭菌记录 {rid} 引用不存在的器械包 {pid}")
            return
        if ster is None:
            rec.valid = False
            self.report("错误", "未知灭菌器", f"第{lineno}行: 灭菌记录 {rid} 引用不存在的灭菌器 {sname}")
            return
        if pack.ptype != ster.stype:
            rec.valid = False
            rec.effective = "不合格"
            rec.note = "类型不匹配，记录无效"
            self.report("错误", "类型不匹配",
                        f"第{lineno}行: 器械包 {pid}({pack.ptype}) 与灭菌器 {sname}({ster.stype}) 类型不匹配，记录 {rid} 无效")
            return

        if any(r.pid == pid and r.valid for r in self.records.values() if r.rid != rid):
            self.report("警告", "重复灭菌",
                        f"第{lineno}行: 器械包 {pid} 已存在灭菌记录，记录 {rid} 为同包重复灭菌")

        ok, basis = check_params(ster.stype, param, duration)
        if declared == "合格" and not ok:
            rec.effective = "不合格"
            rec.note = f"申报合格但参数不达标({basis})，改判不合格"
            self.report("警告", "参数不达标",
                        f"第{lineno}行: 记录 {rid} 申报合格，但参数 {param}/{duration}分钟 未达标准({basis})，判定不合格")
        elif declared == "不合格" and ok:
            rec.effective = "不合格"
            rec.note = "申报不合格(参数达标，按申报判定)"
        else:
            rec.effective = declared
            rec.note = basis

        pack.status = "已灭菌" if rec.effective == "合格" else "灭菌失败"

        if rec.effective == "不合格":
            self.trace_batch(rec, pack)

    def trace_batch(self, rec, pack):
        """不合格批次追溯: 同批(同灭菌器+同参数+同时长)且器械清单相同的其他包级联复核。"""
        target = set(pack.instruments)
        for other in self.records.values():
            if other.rid == rec.rid or not other.valid or other.pid == rec.pid:
                continue
            if other.batch_key() != rec.batch_key():
                continue
            opack = self.packs[other.pid]
            if set(opack.instruments) != target:
                continue
            if opack.status == "待用":
                opack.status = "待复核(召回)"
                self.report("错误", "追溯召回",
                            f"记录 {rec.rid} 不合格，同批同器械清单的器械包 {opack.pid} "
                            f"(记录 {other.rid}) 已放行，级联召回复核")
            elif opack.status in ("已灭菌", "待灭菌"):
                opack.status = "待复核"
                self.report("警告", "追溯复核",
                            f"记录 {rec.rid} 不合格，同批同器械清单的器械包 {opack.pid} "
                            f"(记录 {other.rid}) 级联标记为待复核")

    # ---------- 放行流 ----------
    def add_release(self, rid, decision, lineno):
        rec = self.records.get(rid)
        if rec is None:
            self.report("错误", "记录不存在", f"第{lineno}行: 放行引用不存在的灭菌记录 {rid}")
            return
        if not rec.valid:
            self.report("错误", "记录无效", f"第{lineno}行: 灭菌记录 {rid} 无效(类型不匹配或引用错误)，不得放行")
            return
        if rid in self.released:
            self.report("错误", "重复放行", f"第{lineno}行: 灭菌记录 {rid} 已有放行记录，重复放行")
            return
        self.released.add(rid)

        pack = self.packs[rec.pid]
        if decision == "放行":
            if rec.effective != "合格":
                self.report("错误", "不合格放行",
                            f"第{lineno}行: 灭菌记录 {rid} 判定不合格，不合格批次不得放行 (器械包 {pack.pid})")
                return
            if pack.status.startswith("待复核"):
                self.report("错误", "复核中放行",
                            f"第{lineno}行: 器械包 {pack.pid} 处于{pack.status}状态，不得放行")
                return
            if pack.status != "已灭菌":
                self.report("错误", "状态异常",
                            f"第{lineno}行: 器械包 {pack.pid} 当前状态[{pack.status}]非已灭菌，不得放行")
                return
            pack.status = "待用"
        else:  # 拒收
            pack.status = "已拒收"

    # ---------- 解析 ----------
    def process_line(self, line, lineno):
        parts = line.split()
        cmd = parts[0]
        try:
            if cmd == "灭菌器" and len(parts) == 3:
                self.add_sterilizer(parts[1], parts[2], lineno)
            elif cmd == "器械包" and len(parts) == 4:
                self.add_pack(parts[1], parts[2], parts[3].split(","), lineno)
            elif cmd == "灭菌" and len(parts) == 7:
                if parts[6] not in VALID_RESULTS:
                    self.report("错误", "格式", f"第{lineno}行: 灭菌结果须为 合格/不合格: {parts[6]}")
                    return
                self.add_sterilization(parts[1], parts[2], parts[3],
                                       float(parts[4]), float(parts[5]), parts[6], lineno)
            elif cmd == "放行" and len(parts) == 3:
                if parts[2] not in VALID_RELEASE:
                    self.report("错误", "格式", f"第{lineno}行: 放行结果须为 放行/拒收: {parts[2]}")
                    return
                self.add_release(parts[1], parts[2], lineno)
            else:
                self.report("错误", "格式", f"第{lineno}行: 无法解析: {line}")
        except ValueError:
            self.report("错误", "格式", f"第{lineno}行: 参数/时长须为数字: {line}")

    # ---------- 输出 ----------
    def render(self):
        out = ["=== 灭菌状态 ===", "[器械包]"]
        for pid in self.packs:
            p = self.packs[pid]
            out.append(f"  {pid} ({p.ptype}, 器械: {','.join(p.instruments)}): {p.status}")
        out.append("[灭菌记录]")
        for rid in self.records:
            r = self.records[rid]
            flag = "" if r.valid else " [无效]"
            out.append(f"  {rid}: 包{r.pid} @ {r.sterilizer} 参数{r.param:g} 时长{r.duration:g}分钟 "
                       f"申报{r.declared} -> 判定{r.effective or '-'} ({r.note}){flag}")
        out.append("=== 错误报告 ===")
        if not self.errors:
            out.append("  无错误")
        else:
            for i, (level, cat, msg) in enumerate(self.errors, 1):
                out.append(f"  {i}. [{level}][{cat}] {msg}")
        return "\n".join(out)


SAMPLE = """\
# 灭菌器定义
灭菌器 高温炉A 高温
灭菌器 低温炉B 低温
# 器械包定义
器械包 P001 高温 剪刀,镊子,止血钳
器械包 P002 高温 剪刀,镊子,止血钳
器械包 P003 低温 内镜,导管
器械包 P004 高温 骨钻,锯片
# 灭菌流与放行流（按顺序处理，状态跨流延续）
灭菌 S1 P001 高温炉A 134 30 合格
放行 S1 放行
灭菌 S2 P002 高温炉A 134 30 不合格
灭菌 S3 P003 低温炉B 600 90 合格
灭菌 S4 P004 低温炉B 600 90 合格
灭菌 S5 P003 低温炉B 300 30 合格
放行 S2 放行
放行 S3 放行
放行 S9 放行
"""


def run(lines):
    tracker = Tracker()
    for lineno, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if line:
            tracker.process_line(line, lineno)
    return tracker.render()


def main(argv):
    if "--demo" in argv:
        print("--- 输入 ---")
        print(SAMPLE)
        print("--- 输出 ---")
        print(run(SAMPLE.splitlines()))
        return 0
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    print(run(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
