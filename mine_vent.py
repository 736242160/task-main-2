#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mine_vent.py — 煤矿通风与瓦斯监测模拟工具（纯 Python 标准库，单文件，零依赖）

功能
====
读取采掘面定义、风机定义，以及按时间顺序交错出现的监测流与调节流，
维护跨流延续的通风状态，输出事件日志、最终通风状态与错误清单。

阈值规则（自定，附理由）
========================
- 报警阈值 ALARM_THRESHOLD    = 1.0% CH4
- 断电停工阈值 SHUTDOWN_THRESHOLD = 1.5% CH4
  理由：参照《煤矿安全规程》对采掘工作面瓦斯浓度的规定——报警值 1.0%、
  断电值 1.5%。浓度 ≥1.0% 报警；≥1.5% 该面停工并级联关联面停工。
- 恢复阈值 RESUME_THRESHOLD   = 1.0% CH4
  理由：回落到报警值以下才允许复工，避免在临界值附近反复停/复工。
- 浓度跳变阈值 JUMP_THRESHOLD = 0.3% CH4
  理由：正常通风下瓦斯浓度变化平缓，相邻两次监测差超过 0.3% 通常意味着
  异常涌出或传感器漂移/故障，应报告提示人工核查。

通风模型
========
- 每台风机将其供风量平均分配给其服务的所有工作面；
  工作面风量 = 所有“未故障且服务该面”的风机分摊风量之和。
- 风机故障（FAIL）或调节（ADJUST）后，所有工作面风量立即级联重算。
- 工作面风量 < 需风量 即判“风量不足”，该面停工并报告；恢复后报告复工。
- 瓦斯超限级联：超限面及其“关联面”（与该面共用至少一台风机的所有面）停工。
- 停工原因按集合管理（风量不足 / 瓦斯超限），全部解除后方可复工。

输入格式（文件或标准输入，每行一条指令，# 开头为注释）
======================================================
  FACE    <面名> <需风量m3/s>                 定义采掘面（须先于引用它的 FAN）
  FAN     <机名> <供风量m3/s> <面1,面2,...>   定义风机及其服务面列表
  MONITOR <面名> <瓦斯浓度%>                  监测流事件
  ADJUST  <机名> <新供风量|FAIL|RECOVER>      调节流事件（FAIL=故障, RECOVER=恢复）
  STATUS                                      立即输出一次当前通风状态快照
  END                                         结束输入（其后内容忽略）

用法示例
========
  python3 mine_vent.py input.txt      # 从文件读取
  python3 mine_vent.py < input.txt    # 从标准输入读取
  python3 mine_vent.py --help         # 显示本说明

示例输入：
  FACE F1 10
  FACE F2 8
  FAN K1 20 F1,F2
  MONITOR F1 0.6
  ADJUST K1 12
  MONITOR F1 1.7
  ADJUST K1 FAIL
  ADJUST K1 RECOVER
  END

退出码：处理过程中出现 ERROR 级事件则为 1，否则为 0。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

ALARM_THRESHOLD = 1.0      # % CH4，报警阈值
SHUTDOWN_THRESHOLD = 1.5   # % CH4，断电停工阈值
RESUME_THRESHOLD = 1.0     # % CH4，复工阈值
JUMP_THRESHOLD = 0.3       # % CH4，同面相邻监测跳变阈值
EPS = 1e-9

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"


@dataclass
class Face:
    name: str
    required: float
    airflow: float = 0.0
    last_conc: float | None = None
    alarm_active: bool = False
    insufficient: bool = False
    shutdown_reasons: set[str] = field(default_factory=set)


@dataclass
class Fan:
    name: str
    supply: float
    faces: list[str]
    failed: bool = False


class VentilationSystem:
    def __init__(self) -> None:
        self.faces: dict[str, Face] = {}
        self.fans: dict[str, Fan] = {}
        self.events: list[tuple[int, str, str]] = []
        self.output: list[str] = []
        self._seq = 0
        self._gas_cascades: dict[str, set[str]] = {}  # 超限面 -> 被级联的面集合

    # ---------- 事件记录 ----------
    def _emit(self, level: str, message: str) -> None:
        self._seq += 1
        line = f"#{self._seq:03d} [{level}] {message}"
        self.events.append((self._seq, level, message))
        self.output.append(line)

    # ---------- 定义 ----------
    def define_face(self, name: str, required: float) -> None:
        if name in self.faces:
            self._emit(WARN, f"工作面重复定义：{name}，以新定义覆盖")
        self.faces[name] = Face(name=name, required=required)

    def define_fan(self, name: str, supply: float, face_names: list[str]) -> None:
        if name in self.fans:
            self._emit(WARN, f"风机重复定义：{name}，以新定义覆盖")
        known, unknown = [], []
        for fname in face_names:
            (known if fname in self.faces else unknown).append(fname)
        for fname in unknown:
            self._emit(ERROR, f"风机 {name} 的服务面 {fname} 不存在，已从服务列表剔除")
        if not known:
            self._emit(WARN, f"风机 {name} 没有有效的服务面")
        self.fans[name] = Fan(name=name, supply=supply, faces=known)

    # ---------- 风量重算（调节/故障/恢复后级联） ----------
    def recompute(self, cause: str = "") -> None:
        for face in self.faces.values():
            face.airflow = sum(
                fan.supply / len(fan.faces)
                for fan in self.fans.values()
                if not fan.failed and fan.faces and face.name in fan.faces
            )
        suffix = f"（{cause}）" if cause else ""
        for face in self.faces.values():
            insufficient = face.airflow < face.required - EPS
            if insufficient and not face.insufficient:
                face.insufficient = True
                face.shutdown_reasons.add("风量不足")
                self._emit(ERROR,
                           f"风量不足：工作面 {face.name} 供风量 {face.airflow:.2f} m³/s "
                           f"低于需风量 {face.required:.2f} m³/s，工作面停工{suffix}")
            elif not insufficient and face.insufficient:
                face.insufficient = False
                face.shutdown_reasons.discard("风量不足")
                if face.shutdown_reasons:
                    rest = "、".join(sorted(face.shutdown_reasons))
                    self._emit(INFO,
                               f"风量恢复：工作面 {face.name} 供风量 {face.airflow:.2f} m³/s "
                               f"已满足需风量 {face.required:.2f} m³/s{suffix}；仍停工，原因：{rest}")
                else:
                    self._emit(INFO,
                               f"风量恢复：工作面 {face.name} 供风量 {face.airflow:.2f} m³/s "
                               f"满足需风量 {face.required:.2f} m³/s，工作面复工{suffix}")

    # ---------- 监测流 ----------
    def _associated_faces(self, name: str) -> set[str]:
        assoc: set[str] = set()
        for fan in self.fans.values():
            if name in fan.faces:
                assoc.update(fan.faces)
        assoc.discard(name)
        return assoc

    def monitor(self, name: str, conc: float) -> None:
        face = self.faces.get(name)
        if face is None:
            self._emit(ERROR, f"监测引用不存在的工作面：{name}（浓度 {conc:.2f}% 已忽略）")
            return
        if face.last_conc is not None and abs(conc - face.last_conc) > JUMP_THRESHOLD:
            self._emit(WARN,
                       f"浓度跳变：工作面 {name} 瓦斯浓度由 {face.last_conc:.2f}% 跳变为 "
                       f"{conc:.2f}%（跳变阈值 {JUMP_THRESHOLD:.2f}%），疑似异常涌出或传感器异常")
        face.last_conc = conc

        if conc < ALARM_THRESHOLD:
            face.alarm_active = False

        if conc >= SHUTDOWN_THRESHOLD:
            face.alarm_active = True
            if name not in self._gas_cascades:
                affected = {name} | self._associated_faces(name)
                self._gas_cascades[name] = affected
                face.shutdown_reasons.add(f"瓦斯超限({name})")
                self._emit(ERROR,
                           f"瓦斯超限：工作面 {name} 浓度 {conc:.2f}% 达到停工阈值 "
                           f"{SHUTDOWN_THRESHOLD:.2f}%，工作面停工")
                for other in sorted(affected - {name}):
                    oface = self.faces.get(other)
                    if oface is None:
                        continue
                    oface.shutdown_reasons.add(f"瓦斯超限({name})")
                    self._emit(ERROR,
                               f"级联停工：工作面 {other} 与超限面 {name} 共用通风系统，关联停工")
        elif conc >= ALARM_THRESHOLD:
            if not face.alarm_active:
                face.alarm_active = True
                self._emit(WARN,
                           f"瓦斯超限报警：工作面 {name} 浓度 {conc:.2f}% 超过报警阈值 "
                           f"{ALARM_THRESHOLD:.2f}%")

        if conc <= RESUME_THRESHOLD and name in self._gas_cascades:
            affected = self._gas_cascades.pop(name)
            for fname in sorted(affected):
                f = self.faces.get(fname)
                if f is None:
                    continue
                f.shutdown_reasons.discard(f"瓦斯超限({name})")
                tag = "超限面" if fname == name else "关联面"
                if f.shutdown_reasons:
                    rest = "、".join(sorted(f.shutdown_reasons))
                    self._emit(INFO,
                               f"瓦斯恢复：{tag} {fname} 解除瓦斯停工（超限面 {name} 浓度回落至 "
                               f"{conc:.2f}%，≤ 恢复阈值 {RESUME_THRESHOLD:.2f}%）；仍停工，原因：{rest}")
                else:
                    self._emit(INFO,
                               f"瓦斯恢复：{tag} {fname} 解除瓦斯停工（超限面 {name} 浓度回落至 "
                               f"{conc:.2f}%，≤ 恢复阈值 {RESUME_THRESHOLD:.2f}%），工作面复工")

    # ---------- 调节流 ----------
    def adjust(self, name: str, token: str) -> None:
        fan = self.fans.get(name)
        if fan is None:
            self._emit(ERROR, f"调节引用不存在的风机：{name}（指令 {token} 已忽略）")
            return
        upper = token.upper()
        if upper == "FAIL":
            if fan.failed:
                self._emit(WARN, f"风机 {name} 已处于故障状态，重复故障指令已忽略")
                return
            fan.failed = True
            served = ",".join(fan.faces) or "无"
            self._emit(ERROR, f"风机故障：{name} 停止供风，服务面（{served}）通风级联重分配")
            self.recompute(cause=f"风机 {name} 故障")
        elif upper == "RECOVER":
            if not fan.failed:
                self._emit(WARN, f"风机 {name} 未处于故障状态，恢复指令已忽略")
                return
            fan.failed = False
            self._emit(INFO, f"风机恢复：{name} 恢复供风 {fan.supply:.2f} m³/s，通风级联重分配")
            self.recompute(cause=f"风机 {name} 恢复")
        else:
            try:
                value = float(token)
            except ValueError:
                self._emit(ERROR,
                           f"调节指令无效：风机 {name} 的新供风量 {token!r} 不是数值、FAIL 或 RECOVER")
                return
            if value < 0:
                self._emit(ERROR, f"调节指令无效：风机 {name} 供风量不能为负（{value}）")
                return
            old = fan.supply
            fan.supply = value
            if fan.failed:
                fan.failed = False
                self._emit(INFO,
                           f"风机调节：{name} 故障期间收到数值调节，视为修复，"
                           f"供风量 {old:.2f} → {value:.2f} m³/s，通风级联重分配")
            else:
                self._emit(INFO, f"风机调节：{name} 供风量 {old:.2f} → {value:.2f} m³/s，通风级联重分配")
            self.recompute(cause=f"风机 {name} 调节")

    # ---------- 状态输出 ----------
    def status_lines(self) -> list[str]:
        lines = ["工作面："]
        for f in self.faces.values():
            conc = "无监测数据" if f.last_conc is None else f"{f.last_conc:.2f}%"
            air = "充足" if not f.insufficient else "不足"
            state = "正常" if not f.shutdown_reasons else \
                "停工（" + "、".join(sorted(f.shutdown_reasons)) + "）"
            lines.append(f"  {f.name}: 需风量 {f.required:.2f} m³/s | 当前供风量 "
                         f"{f.airflow:.2f} m³/s（{air}）| 最近瓦斯 {conc} | 状态 {state}")
        lines.append("风机：")
        for fan in self.fans.values():
            st = "故障" if fan.failed else "运行"
            served = ",".join(fan.faces) or "无"
            lines.append(f"  {fan.name}: 供风量 {fan.supply:.2f} m³/s | 状态 {st} | 服务面 {served}")
        return lines

    def snapshot(self, title: str) -> None:
        self.output.append(f"---- {title} ----")
        self.output.extend(self.status_lines())

    def final_report(self) -> list[str]:
        out = list(self.output)
        out.append("==== 通风状态（最终） ====")
        out.extend(self.status_lines())
        out.append("==== 错误与警告清单 ====")
        bad = [f"#{seq:03d} [{level}] {msg}"
               for seq, level, msg in self.events if level in (ERROR, WARN)]
        out.extend(bad if bad else ["（无）"])
        return out


def run(lines: list[str]) -> tuple[VentilationSystem, list[str]]:
    vs = VentilationSystem()
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        cmd, args = parts[0].upper(), parts[1:]
        try:
            if cmd == "FACE":
                name, required = args[0], float(args[1])
                if required < 0:
                    raise ValueError("需风量不能为负")
                vs.define_face(name, required)
            elif cmd == "FAN":
                name, supply = args[0], float(args[1])
                if supply < 0:
                    raise ValueError("供风量不能为负")
                faces = [f for f in ",".join(args[2:]).split(",") if f]
                vs.define_fan(name, supply, faces)
            elif cmd == "MONITOR":
                name, conc = args[0], float(args[1])
                if conc < 0:
                    raise ValueError("瓦斯浓度不能为负")
                vs.recompute()  # 保证监测前风量状态是最新的（含首次核算）
                vs.monitor(name, conc)
            elif cmd == "ADJUST":
                vs.adjust(args[0], args[1])
            elif cmd == "STATUS":
                vs.recompute()
                vs.snapshot(f"通风状态快照（第 {lineno} 行指令后）")
            elif cmd == "END":
                break
            else:
                vs._emit(ERROR, f"第 {lineno} 行：未知指令 {parts[0]!r}，已忽略")
        except (IndexError, ValueError) as exc:
            vs._emit(ERROR, f"第 {lineno} 行：指令解析失败（{exc}）：{line!r}")
    vs.recompute()
    return vs, vs.final_report()


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] in ("-h", "--help"):
        sys.stdout.write(__doc__ or "")
        return 0
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    vs, report = run(lines)
    sys.stdout.write("\n".join(report) + "\n")
    return 1 if any(level == ERROR for _, level, _ in vs.events) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
