#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""煤矿通风与瓦斯监测模拟工具（纯 Python 标准库，单文件）

用法:
    python3 mine_vent.py 输入文件        # 从文件读取事件流
    python3 mine_vent.py                 # 从标准输入读取事件流
    python3 mine_vent.py --demo          # 运行内置示例

输入格式（每行一条指令，# 开头为注释，空白行忽略）:
    face    <面名> <需风量>                  定义采掘面
    fan     <风机名> <供风量> <面1> [面2...] 定义风机及其服务面
    monitor <面名> <瓦斯浓度%>               监测事件
    adjust  <风机名> <新供风量>              调节事件
    fail    <风机名>                         风机故障事件
    recover <风机名>                         风机故障恢复事件

规则说明（阈值均为本工具自定，理由如下）:
    瓦斯报警阈值   1.0%  —— 参考《煤矿安全规程》采掘工作面报警/断电浓度
    瓦斯停工阈值   1.5%  —— 同上，达到断电撤人级别，触发停工及级联
    瓦斯解除阈值   0.8%  —— 低于报警值留迟滞，防止浓度在阈值附近抖动导致反复停复工
    浓度跳变阈值   0.3%  —— 同一面相邻两次监测差值，突变提示异常涌出或传感器异常
    停工风量比     60%   —— 实供风量低于需风量 60% 时稀释瓦斯能力严重不足，强制停工
    级联停工       超限面 + 与其共享风机（同一通风网络）的关联面连通闭包，
                   因为瓦斯可经共同风流扩散至关联面
    风量分配       每台风机按其服务面需风量比例分配（水填法，供给超过需求时按需求封顶）；
                   停工面仍参与配风（停工不停风，需继续稀释瓦斯）
    状态延续       所有事件按输入顺序处理，风量/停工/故障/最近浓度等状态跨事件延续
"""
import sys

GAS_WARN = 1.0       # 瓦斯报警阈值 %
GAS_STOP = 1.5       # 瓦斯停工阈值 %
GAS_CLEAR = 0.8      # 瓦斯解除阈值 %
GAS_JUMP = 0.3       # 浓度跳变阈值 %
AIR_MIN_RATIO = 0.6  # 强制停工风量比例
EPS = 1e-9


class Face:
    def __init__(self, name, need):
        self.name = name
        self.need = need
        self.airflow = 0.0
        self.stop_reasons = set()  # 'gas' / 'airflow' / ('cascade', 起源面)
        self.last_gas = None

    @property
    def stopped(self):
        return bool(self.stop_reasons)


class Fan:
    def __init__(self, name, supply, face_names):
        self.name = name
        self.supply = supply
        self.face_names = list(face_names)
        self.failed = False


class Simulator:
    def __init__(self):
        self.faces = {}
        self.fans = {}
        self.reports = []       # (事件号, 级别, 内容)
        self.insufficient = set()
        self.event_no = 0
        self.dirty = False      # 定义变更后尚未重算

    def report(self, level, msg):
        self.reports.append((self.event_no, level, msg))

    # ---------- 定义 ----------
    def define_face(self, name, need):
        if name in self.faces:
            self.report('ERROR', "采掘面重复定义：%s" % name)
            return
        if need <= 0:
            self.report('ERROR', "采掘面 %s 需风量必须为正：%s" % (name, need))
            return
        self.faces[name] = Face(name, need)
        self.dirty = True

    def define_fan(self, name, supply, face_names):
        if name in self.fans:
            self.report('ERROR', "风机重复定义：%s" % name)
            return
        if supply < 0:
            self.report('ERROR', "风机 %s 供风量不能为负：%s" % (name, supply))
            return
        ok = []
        for fn in face_names:
            if fn not in self.faces:
                self.report('ERROR', "风机 %s 引用了不存在的采掘面：%s" % (name, fn))
            else:
                ok.append(fn)
        if not ok:
            self.report('ERROR', "风机 %s 没有任何有效服务面，定义被拒绝" % name)
            return
        self.fans[name] = Fan(name, supply, ok)
        self.dirty = True

    def _ensure_computed(self):
        if self.dirty:
            self.recompute()

    # ---------- 风量重算（水填法：按需风比例分配，封顶于需求） ----------
    def recompute(self):
        for f in self.faces.values():
            f.airflow = 0.0
        for fan in self.fans.values():
            supply = 0.0 if fan.failed else fan.supply
            served = [self.faces[n] for n in fan.face_names]
            total_need = sum(f.need for f in served)
            if total_need <= 0:
                continue
            if supply >= total_need:
                for f in served:
                    f.airflow += f.need
            else:
                for f in served:
                    f.airflow += supply * f.need / total_need
        # 风量过低强制停工 / 恢复
        for f in self.faces.values():
            if f.airflow < AIR_MIN_RATIO * f.need - EPS:
                if 'airflow' not in f.stop_reasons:
                    f.stop_reasons.add('airflow')
                    self.report('ALARM',
                                "采掘面 %s 实供风量 %.1f 低于需风量 %.1f 的 %d%%，强制停工"
                                % (f.name, f.airflow, f.need, int(AIR_MIN_RATIO * 100)))
            elif 'airflow' in f.stop_reasons:
                f.stop_reasons.discard('airflow')
                self._maybe_resume(f, True)
        # 风量不足告警（仅对新进入不足状态的面报告）
        now_insuf = {n for n, f in self.faces.items() if f.airflow < f.need - EPS}
        for n in sorted(now_insuf - self.insufficient):
            f = self.faces[n]
            self.report('WARN', "采掘面 %s 风量不足：实供 %.1f < 需风 %.1f"
                        % (n, f.airflow, f.need))
        self.insufficient = now_insuf
        summary = "，".join("%s=%.1f/%.1f" % (f.name, f.airflow, f.need)
                            for f in self.faces.values())
        self.report('INFO', "风量重算结果：%s" % (summary or "（无采掘面）"))
        self.dirty = False

    def _maybe_resume(self, face, was_stopped):
        if was_stopped and not face.stop_reasons:
            self.report('INFO', "采掘面 %s 所有停工原因解除，复工" % face.name)

    # ---------- 级联：共享风机的连通闭包 ----------
    def cascade_component(self, origin):
        adj = {n: set() for n in self.faces}
        for fan in self.fans.values():
            for a in fan.face_names:
                adj[a].update(b for b in fan.face_names if b != a)
        seen, stack = {origin}, [origin]
        while stack:
            for y in adj[stack.pop()]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return seen

    # ---------- 事件 ----------
    def on_monitor(self, name, gas):
        self._ensure_computed()
        f = self.faces.get(name)
        if f is None:
            self.report('ERROR', "监测引用了不存在的采掘面：%s" % name)
            return
        if gas < 0:
            self.report('ERROR', "采掘面 %s 瓦斯浓度不能为负：%s" % (name, gas))
            return
        if f.last_gas is not None:
            delta = gas - f.last_gas
            if abs(delta) >= GAS_JUMP:
                self.report('WARN',
                            "采掘面 %s 瓦斯浓度跳变 %+.2f%%（%.2f→%.2f），超过跳变阈值 %.2f%%"
                            % (name, delta, f.last_gas, gas, GAS_JUMP))
        if gas >= GAS_STOP:
            self.report('ALARM', "采掘面 %s 瓦斯浓度 %.2f%% 超过停工阈值 %.2f%%"
                        % (name, gas, GAS_STOP))
            if 'gas' not in f.stop_reasons:
                f.stop_reasons.add('gas')
                self.report('ALARM', "采掘面 %s 停工" % name)
            for other in sorted(self.cascade_component(name) - {name}):
                of = self.faces[other]
                key = ('cascade', name)
                if key not in of.stop_reasons:
                    of.stop_reasons.add(key)
                    self.report('ALARM', "采掘面 %s 因关联面 %s 瓦斯超限被级联停工"
                                % (other, name))
        elif gas >= GAS_WARN:
            self.report('WARN', "采掘面 %s 瓦斯浓度 %.2f%% 超过报警阈值 %.2f%%"
                        % (name, gas, GAS_WARN))
        if gas < GAS_CLEAR:
            was_stopped = {n: f2.stopped for n, f2 in self.faces.items()}
            if 'gas' in f.stop_reasons:
                f.stop_reasons.discard('gas')
                self.report('INFO', "采掘面 %s 瓦斯降至 %.2f%%（< %.2f%%），解除瓦斯停工"
                            % (name, gas, GAS_CLEAR))
            for of in self.faces.values():
                if ('cascade', name) in of.stop_reasons:
                    of.stop_reasons.discard(('cascade', name))
                    self.report('INFO', "采掘面 %s 关联瓦斯风险（源自 %s）解除"
                                % (of.name, name))
            for of in self.faces.values():
                self._maybe_resume(of, was_stopped[of.name])
        f.last_gas = gas

    def on_adjust(self, name, supply):
        fan = self.fans.get(name)
        if fan is None:
            self.report('ERROR', "调节引用了不存在的风机：%s" % name)
            return
        if supply < 0:
            self.report('ERROR', "风机 %s 新供风量不能为负：%s" % (name, supply))
            return
        fan.supply = supply
        self.report('INFO', "风机 %s 供风量调节为 %.1f" % (name, supply))
        self.recompute()

    def on_fail(self, name):
        fan = self.fans.get(name)
        if fan is None:
            self.report('ERROR', "故障指令引用了不存在的风机：%s" % name)
            return
        if fan.failed:
            self.report('WARN', "风机 %s 已处于故障状态" % name)
            return
        fan.failed = True
        self.report('ALARM', "风机 %s 故障停机，其服务面：%s"
                    % (name, '、'.join(fan.face_names)))
        self.recompute()

    def on_recover(self, name):
        fan = self.fans.get(name)
        if fan is None:
            self.report('ERROR', "恢复指令引用了不存在的风机：%s" % name)
            return
        if not fan.failed:
            self.report('WARN', "风机 %s 并未处于故障状态" % name)
            return
        fan.failed = False
        self.report('INFO', "风机 %s 故障恢复，重新投入运行" % name)
        self.recompute()

    # ---------- 解析 ----------
    def process_line(self, line):
        self.event_no += 1
        parts = line.split()
        cmd = parts[0].lower()
        try:
            if cmd == 'face':
                self.define_face(parts[1], float(parts[2]))
            elif cmd == 'fan':
                self.define_fan(parts[1], float(parts[2]), parts[3:])
            elif cmd == 'monitor':
                self.on_monitor(parts[1], float(parts[2]))
            elif cmd == 'adjust':
                self.on_adjust(parts[1], float(parts[2]))
            elif cmd == 'fail':
                self.on_fail(parts[1])
            elif cmd == 'recover':
                self.on_recover(parts[1])
            else:
                self.report('ERROR', "未知指令：%s" % parts[0])
        except IndexError:
            self.report('ERROR', "指令参数不足：%s" % line)
        except ValueError:
            self.report('ERROR', "数值格式错误：%s" % line)

    # ---------- 输出 ----------
    @staticmethod
    def _reason_text(reason):
        if reason == 'gas':
            return '瓦斯超限'
        if reason == 'airflow':
            return '风量过低'
        return '级联(%s)' % reason[1]

    def render(self):
        out = ["===== 事件处理报告 ====="]
        out += ["[事件%03d][%s] %s" % r for r in self.reports]
        out += ["", "===== 最终通风状态 =====", "-- 采掘面 --"]
        for f in self.faces.values():
            status = ('正常' if not f.stopped else '停工[' + ','.join(
                self._reason_text(r) for r in sorted(f.stop_reasons, key=str)) + ']')
            gas = '-' if f.last_gas is None else '%.2f%%' % f.last_gas
            out.append("  %s: 需风 %.1f  实供 %.1f  最近瓦斯 %s  状态 %s"
                       % (f.name, f.need, f.airflow, gas, status))
        out.append("-- 风机 --")
        for fan in self.fans.values():
            out.append("  %s: 供风 %.1f  状态 %s  服务面 %s"
                       % (fan.name, fan.supply, '故障' if fan.failed else '运行',
                          '、'.join(fan.face_names)))
        out += ["", "===== 错误与告警清单 ====="]
        bad = [r for r in self.reports if r[1] in ('WARN', 'ALARM', 'ERROR')]
        out += (["[事件%03d][%s] %s" % r for r in bad] or ["  （无）"])
        return '\n'.join(out)


DEMO = """\
# 采掘面与风机定义
face 综采一面 1000
face 综采二面 800
face 掘进三面 600
fan 主扇甲 1500 综采一面 综采二面
fan 主扇乙 900 综采二面 掘进三面
# 监测流
monitor 综采一面 0.60
monitor 综采一面 1.20
monitor 综采二面 1.60
monitor 综采二面 0.50
# 调节流
adjust 主扇甲 600
fail 主扇乙
recover 主扇乙
# 错误引用
monitor 幽灵面 0.5
adjust 幽灵风机 100
"""


def main(argv):
    if len(argv) > 1 and argv[1] == '--demo':
        text = DEMO
    elif len(argv) > 1:
        with open(argv[1], encoding='utf-8') as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    sim = Simulator()
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith('#'):
            sim.process_line(line)
    if sim.dirty:
        sim.recompute()
    print(sim.render())


if __name__ == '__main__':
    main(sys.argv)
