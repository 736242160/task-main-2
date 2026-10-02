#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""医疗器械灭菌追溯工具（纯 Python 标准库，单文件）。

用法:
    python3 sterilization_tracker.py [输入文件|-]   # 无参数时运行内置示例

输入格式（行式 DSL，# 开头为注释，空行忽略）:
    灭菌器 <名称> <高温|低温>
    器械包 <编号> <高温|低温> <器械1,器械2,...>
    灭菌   <记录号> <包编号> <灭菌器名称> <温度=值|浓度=值> <时长=分钟> <合格|不合格>
    放行   <记录号> <放行|拒收>

判定规则（自定，理由：参考 WS 310 压力蒸汽/环氧乙烷灭菌关键参数下限）:
    高温灭菌: 温度 >= 132℃ 且 时长 >= 3 分钟
    低温灭菌: 浓度 >= 450 mg/L 且 时长 >= 60 分钟
    申报"合格"但参数不达标的记录，判定纠正为"不合格"并报告。

批次定义: 同一灭菌器 + 相同器械清单 视为同批，用于不合格追溯复核。

状态机（跨流延续，按事件顺序处理）:
    器械包: 待灭菌 -> 已灭菌(灭菌合格) -> 待用(放行)
    不合格后可重新灭菌（返工）；已灭菌/待用再灭菌视为重复灭菌，报告。
"""

import sys
from dataclasses import dataclass, field

HIGH_TEMP_MIN_TEMP = 132.0      # ℃
HIGH_TEMP_MIN_DURATION = 3.0    # 分钟
LOW_TEMP_MIN_CONC = 450.0       # mg/L
LOW_TEMP_MIN_DURATION = 60.0    # 分钟


@dataclass
class Sterilizer:
    name: str
    kind: str  # 高温 / 低温


@dataclass
class Pack:
    pid: str
    kind: str
    instruments: tuple
    status: str = "待灭菌"      # 待灭菌/已灭菌/待用
    need_recheck: bool = False  # 待复核（追溯标记）


@dataclass
class Sterilization:
    rid: str
    pack_id: str
    ster_name: str
    param_kind: str             # 温度 / 浓度
    param_value: float
    duration: float
    declared: str               # 申报结果 合格/不合格
    final: str = ""             # 判定结果
    valid: bool = True          # 记录本身是否有效（引用/匹配错误则无效）
    released: str = ""          # 放行 / 拒收 / ""


class Tracker:
    def __init__(self):
        self.sterilizers = {}
        self.packs = {}
        self.records = {}
        self.errors = []

    def err(self, msg):
        self.errors.append(msg)

    # ---------- 定义 ----------
    def add_sterilizer(self, name, kind):
        if kind not in ("高温", "低温"):
            self.err(f"[定义错误] 灭菌器 {name}: 类型须为 高温/低温，得到 {kind}")
            return
        if name in self.sterilizers:
            self.err(f"[定义错误] 灭菌器 {name} 重复定义")
            return
        self.sterilizers[name] = Sterilizer(name, kind)

    def add_pack(self, pid, kind, instruments):
        if kind not in ("高温", "低温"):
            self.err(f"[定义错误] 器械包 {pid}: 类型须为 高温/低温，得到 {kind}")
            return
        if pid in self.packs:
            self.err(f"[定义错误] 器械包 {pid} 重复定义")
            return
        self.packs[pid] = Pack(pid, kind, tuple(instruments))

    # ---------- 灭菌流 ----------
    def param_ok(self, ster_kind, param_kind, value, duration):
        if ster_kind == "高温":
            return (param_kind == "温度" and value >= HIGH_TEMP_MIN_TEMP
                    and duration >= HIGH_TEMP_MIN_DURATION)
        return (param_kind == "浓度" and value >= LOW_TEMP_MIN_CONC
                and duration >= LOW_TEMP_MIN_DURATION)

    def add_sterilization(self, rid, pid, ster_name, param_kind,
                          value, duration, declared):
        if rid in self.records:
            self.err(f"[记录错误] 灭菌记录 {rid} 记录号重复")
            return
        rec = Sterilization(rid, pid, ster_name, param_kind, value,
                            duration, declared)
        self.records[rid] = rec

        pack = self.packs.get(pid)
        ster = self.sterilizers.get(ster_name)
        if pack is None:
            self.err(f"[引用错误] 灭菌记录 {rid}: 器械包 {pid} 不存在")
            rec.valid = False
            return
        if ster is None:
            self.err(f"[引用错误] 灭菌记录 {rid}: 灭菌器 {ster_name} 不存在")
            rec.valid = False
            return

        # 类型匹配
        if pack.kind != ster.kind:
            self.err(f"[类型不匹配] 灭菌记录 {rid}: {pack.kind}器械包 {pid} "
                     f"使用了{ster.kind}灭菌器 {ster_name}")
            rec.valid = False
            return

        # 参数维度匹配（高温须报温度，低温须报浓度）
        expect = "温度" if ster.kind == "高温" else "浓度"
        if param_kind != expect:
            self.err(f"[参数错误] 灭菌记录 {rid}: {ster.kind}灭菌应记录"
                     f"{expect}，实际记录{param_kind}")
            rec.valid = False
            return

        # 重复灭菌（已灭菌/待用状态再次灭菌；不合格后的返工允许）
        if pack.status in ("已灭菌", "待用"):
            self.err(f"[重复灭菌] 灭菌记录 {rid}: 器械包 {pid} 当前状态为"
                     f"{pack.status}，属于重复灭菌")

        # 参数判定
        ok = self.param_ok(ster.kind, param_kind, value, duration)
        if declared == "合格" and not ok:
            self.err(f"[判定纠正] 灭菌记录 {rid}: 申报合格但参数未达标准 "
                     f"({param_kind}={value}, 时长={duration}分钟)，判定为不合格")
        rec.final = "合格" if (declared == "合格" and ok) else "不合格"

        if rec.final == "合格":
            pack.status = "已灭菌"
            pack.need_recheck = False
        else:
            self._trace_recall(pack, ster, rid)

    def _trace_recall(self, failed_pack, ster, rid):
        """不合格追溯：同批（同灭菌器+同器械清单）其他包级联复核。"""
        for other in self.packs.values():
            if other is failed_pack:
                continue
            if other.instruments != failed_pack.instruments:
                continue
            used_same = any(r.valid and r.pack_id == other.pid
                            and r.ster_name == ster.name
                            for r in self.records.values())
            if not used_same:
                continue
            other.need_recheck = True
            extra = ""
            if other.status == "待用":
                extra = "（已放行，需追回复核！）"
                self.err(f"[追溯追回] 灭菌记录 {rid} 不合格：同批器械包 "
                         f"{other.pid} 已放行，须追回复核")
            else:
                self.err(f"[追溯复核] 灭菌记录 {rid} 不合格：同批器械包 "
                         f"{other.pid}（同灭菌器同器械清单）标记待复核{extra}")

    # ---------- 放行流 ----------
    def add_release(self, rid, decision):
        rec = self.records.get(rid)
        if rec is None:
            self.err(f"[引用错误] 放行记录: 灭菌记录 {rid} 不存在")
            return
        if not rec.valid:
            self.err(f"[放行错误] 灭菌记录 {rid} 为无效记录，不得放行")
            return
        if rec.released:
            self.err(f"[放行错误] 灭菌记录 {rid} 已有放行结论，重复放行")
            return
        rec.released = decision
        pack = self.packs[rec.pack_id]
        if decision == "放行":
            if rec.final != "合格":
                self.err(f"[放行错误] 灭菌记录 {rid} 判定不合格，"
                         f"器械包 {pack.pid} 不得放行")
                rec.released = "拒收"
                return
            if pack.need_recheck:
                self.err(f"[放行错误] 器械包 {pack.pid} 处于待复核状态，"
                         f"不得放行")
                rec.released = "拒收"
                return
            pack.status = "待用"   # 级联：已灭菌 -> 待用
        # 拒收：状态不变，等待返工

    # ---------- 解析 ----------
    def process(self, text):
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            head = parts[0]
            try:
                if head == "灭菌器" and len(parts) == 3:
                    self.add_sterilizer(parts[1], parts[2])
                elif head == "器械包" and len(parts) == 4:
                    self.add_pack(parts[1], parts[2],
                                  [x for x in parts[3].split(",") if x])
                elif head == "灭菌" and len(parts) == 7:
                    pk, pv = self._parse_param(parts[4])
                    dur = self._parse_duration(parts[5])
                    if parts[6] not in ("合格", "不合格"):
                        raise ValueError("结果须为 合格/不合格")
                    self.add_sterilization(parts[1], parts[2], parts[3],
                                           pk, pv, dur, parts[6])
                elif head == "放行" and len(parts) == 3:
                    if parts[2] not in ("放行", "拒收"):
                        raise ValueError("放行结论须为 放行/拒收")
                    self.add_release(parts[1], parts[2])
                else:
                    raise ValueError("无法识别的指令或字段数量不对")
            except (ValueError, IndexError) as exc:
                self.err(f"[解析错误] 第{lineno}行 `{line}`: {exc}")

    @staticmethod
    def _parse_param(token):
        k, _, v = token.partition("=")
        if k not in ("温度", "浓度") or not v:
            raise ValueError(f"参数须为 温度=值 或 浓度=值，得到 {token}")
        return k, float(v)

    @staticmethod
    def _parse_duration(token):
        k, _, v = token.partition("=")
        if k != "时长" or not v:
            raise ValueError(f"时长须为 时长=分钟，得到 {token}")
        return float(v)

    # ---------- 输出 ----------
    def report(self):
        out = ["===== 灭菌状态 ====="]
        for p in self.packs.values():
            flag = " [待复核]" if p.need_recheck else ""
            out.append(f"器械包 {p.pid} ({p.kind}) "
                       f"器械[{','.join(p.instruments)}] 状态: {p.status}{flag}")
        out.append("")
        out.append("----- 灭菌记录 -----")
        for r in self.records.values():
            rel = r.released or "未放行"
            valid = "" if r.valid else " [无效记录]"
            out.append(f"记录 {r.rid}: 包={r.pack_id} 灭菌器={r.ster_name} "
                       f"{r.param_kind}={r.param_value} 时长={r.duration}分钟 "
                       f"申报={r.declared} 判定={r.final or '-'} 放行={rel}{valid}")
        out.append("")
        out.append("===== 错误报告 =====")
        if self.errors:
            out.extend(f"{i}. {e}" for i, e in enumerate(self.errors, 1))
        else:
            out.append("无错误。")
        return "\n".join(out)


EXAMPLE_INPUT = """\
# 灭菌器定义
灭菌器 高温锅A 高温
灭菌器 低温柜B 低温
# 器械包定义
器械包 P001 高温 手术刀,止血钳
器械包 P002 高温 手术刀,止血钳
器械包 P003 低温 内镜,活检钳
# 灭菌流
灭菌 S1 P001 高温锅A 温度=134 时长=4 合格
灭菌 S2 P002 高温锅A 温度=121 时长=2 合格
灭菌 S3 P003 高温锅A 温度=134 时长=4 合格
灭菌 S4 P003 低温柜B 浓度=600 时长=90 合格
灭菌 S5 P001 高温锅A 温度=135 时长=4 合格
# 放行流
放行 S1 放行
放行 S2 放行
放行 S9 放行
"""


def main(argv):
    if len(argv) > 1:
        src = sys.stdin.read() if argv[1] == "-" else open(
            argv[1], encoding="utf-8").read()
    else:
        print("（未提供输入文件，运行内置示例）")
        print("----- 示例输入 -----")
        print(EXAMPLE_INPUT)
        src = EXAMPLE_INPUT
    t = Tracker()
    t.process(src)
    print(t.report())


if __name__ == "__main__":
    main(sys.argv)
