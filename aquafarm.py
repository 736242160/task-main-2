#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
养殖水质多参数监测与级联控制工具（纯 Python 标准库，单文件）

用法:
    python3 aquafarm.py [输入文件]      # 省略文件则从 stdin 读取

输入为逐行指令流（# 后为注释），跨指令/跨流状态自动延续:

    pond    <名称> <容量m3> <鱼种> <密度kg/m3>     定义养殖塘
    device  <名称> <类型> <塘1,塘2,...>            定义设备, 类型: aerator(增氧) exchanger(换水) feeder(投喂)
    feed    <塘> <饲料kg>                          投喂流
    monitor <塘> <溶氧mg/L> <温度℃> <氨氮mg/L>      监测流
    fault   <设备>                                 故障流
    repair  <设备>                                 故障修复
    status                                         立即打印中间状态

自定模型参数（理由见源码注释）:
    * 密度 >= 10 kg/m3 视为高密度塘: 溶氧下限 6.0、氨氮上限 0.2 (存塘量大耗氧快、
      残饵粪便多, 水质恶化余量小, 阈值从严); 低密度塘: 溶氧下限 4.0、氨氮上限 0.5。
    * 投喂净耗氧 0.005 kg O2/kg 饲料/监测周期 (总耗氧约 0.25, 绝大部分被同期
      大气复氧与光合产氧抵消); 净氨氮累积 0.0015 kg N/kg 饲料/周期 (总排泄约
      0.03, 大部分被硝化作用去除)。
    * 单次投喂限量 = 3% 存塘生物量 (常规日投饵率上限)。
    * 增氧设备开启后每个监测周期恢复溶氧 1.5 mg/L; 换水设备开启后每个监测
      周期去除 50% 氨氮; 每个监测周期基础耗氧 0.02*密度 mg/L。
    * 溶氧/氨氮矛盾判定: 溶氧超过该温度饱和值 5% (物理不可能, 传感器异常);
      或溶氧 < 2.0 而氨氮 < 0.1 (严重缺氧却无有机污染, 数据自相矛盾)。

示例输入见文件末尾 __demo__ 注释或示例运行。
"""

import sys
from dataclasses import dataclass, field

# ---------------- 自定模型参数 ----------------
HIGH_DENSITY_CUTOFF = 10.0   # kg/m3, 达到即高密度塘
DO_MIN_HIGH, NH3_MAX_HIGH = 6.0, 0.2   # 高密度阈值(从严)
DO_MIN_LOW,  NH3_MAX_LOW  = 4.0, 0.5   # 低密度阈值
DO_HYSTERESIS = 0.5          # 溶氧回滞, 防止设备频繁启停
NH3_CLEAR_RATIO = 0.8        # 氨氮降至上限 80% 以下才解除告警

FEED_DO_COST = 0.005         # kg O2 / kg 饲料 / 周期 (净耗氧)
FEED_NH3_GAIN = 0.0015       # kg N  / kg 饲料 / 周期 (净累积)
FEED_LIMIT_RATIO = 0.03      # 单次投喂 <= 3% 存塘生物量

RESPIRATION_PER_DENSITY = 0.02   # 每监测周期基础耗氧 = 0.02*密度 mg/L
AERATION_RECOVERY = 1.5          # 增氧开启时每周期溶氧恢复 mg/L
EXCHANGE_REMOVAL = 0.5           # 换水开启时每周期氨氮去除比例

DEVICE_TYPES = {"aerator": "增氧", "exchanger": "换水", "feeder": "投喂"}


def do_saturation(temp):
    """淡水溶氧饱和值 mg/L (Weiss 经验式)。"""
    return 14.652 - 0.41022 * temp + 0.0079910 * temp ** 2 - 0.000077774 * temp ** 3


@dataclass
class Pond:
    name: str
    capacity: float
    species: str
    density: float
    do: float = 8.0
    temp: float = 25.0
    nh3: float = 0.05
    alarms: dict = field(default_factory=dict)   # key -> 描述

    @property
    def biomass(self):
        return self.density * self.capacity

    def thresholds(self):
        if self.density >= HIGH_DENSITY_CUTOFF:
            return DO_MIN_HIGH, NH3_MAX_HIGH, "高密度"
        return DO_MIN_LOW, NH3_MAX_LOW, "低密度"


@dataclass
class Device:
    name: str
    dtype: str
    pond_names: list
    on: bool = False
    auto: bool = False      # 是否级联自动开启
    faulty: bool = False


class Farm:
    def __init__(self):
        self.ponds = {}
        self.devices = {}
        self.events = []     # (seq, kind, msg)
        self.seq = 0

    # ---------- 事件 ----------
    def log(self, kind, msg):
        self.seq += 1
        self.events.append((self.seq, kind, msg))
        print(f"[#{self.seq:03d}] {kind:<8} {msg}")

    def set_alarm(self, pond, key, msg):
        if key not in pond.alarms:
            pond.alarms[key] = msg
            self.log("ALARM", f"塘 {pond.name}: {msg}")

    def clear_alarm(self, pond, key):
        if key in pond.alarms:
            self.log("RESOLVED", f"塘 {pond.name}: 告警解除 - {pond.alarms.pop(key)}")

    # ---------- 指令 ----------
    def execute(self, line, lineno):
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd == "pond":
                self.cmd_pond(args)
            elif cmd == "device":
                self.cmd_device(args)
            elif cmd == "feed":
                self.cmd_feed(args)
            elif cmd == "monitor":
                self.cmd_monitor(args)
            elif cmd == "fault":
                self.cmd_fault(args)
            elif cmd == "repair":
                self.cmd_repair(args)
            elif cmd == "status":
                self.print_status()
            else:
                self.log("ERROR", f"第{lineno}行: 未知指令 '{cmd}'")
        except (IndexError, ValueError) as exc:
            self.log("ERROR", f"第{lineno}行: 参数错误 ({line!r}): {exc}")

    def cmd_pond(self, a):
        name, cap, species, dens = a[0], float(a[1]), a[2], float(a[3])
        if cap <= 0 or dens <= 0:
            raise ValueError("容量与密度必须为正数")
        self.ponds[name] = Pond(name, cap, species, dens)
        self.log("INFO", f"定义塘 {name}: {species} 容量{cap}m3 密度{dens}kg/m3")

    def cmd_device(self, a):
        name, dtype, plist = a[0], a[1].lower(), a[2].split(",")
        if dtype not in DEVICE_TYPES:
            raise ValueError(f"设备类型须为 {sorted(DEVICE_TYPES)}")
        known = []
        for pn in plist:
            if pn in self.ponds:
                known.append(pn)
            else:
                self.log("ERROR", f"设备 {name}: 服务塘 {pn} 不存在, 已忽略")
        self.devices[name] = Device(name, dtype, known)
        self.log("INFO", f"定义设备 {name}({DEVICE_TYPES[dtype]}) 服务塘 {known}")

    def cmd_feed(self, a):
        pname, amount = a[0], float(a[1])
        pond = self.ponds.get(pname)
        if pond is None:
            self.log("ERROR", f"投喂失败: 塘 {pname} 不存在")
            return
        feeders = [d for d in self.devices.values()
                   if d.dtype == "feeder" and pname in d.pond_names]
        if feeders and all(d.faulty for d in feeders):
            self.log("ERROR", f"投喂失败: 塘 {pname} 的投喂设备全部故障")
            return
        limit = FEED_LIMIT_RATIO * pond.biomass
        if amount > limit:
            self.log("ERROR", f"塘 {pname}: 投喂 {amount}kg 超限量 "
                              f"{limit:.1f}kg (3% 生物量), 仍已执行")
        # 投喂引起溶氧消耗与氨氮累积 (净系数, 见模块文档)
        pond.do = max(0.0, pond.do - amount * FEED_DO_COST * 1000 / pond.capacity)
        pond.nh3 += amount * FEED_NH3_GAIN * 1000 / pond.capacity
        self.log("INFO", f"塘 {pname}: 投喂 {amount}kg -> 溶氧 {pond.do:.2f}, "
                         f"氨氮 {pond.nh3:.3f}")
        self.evaluate(pond)

    def cmd_monitor(self, a):
        pname = a[0]
        do, temp, nh3 = float(a[1]), float(a[2]), float(a[3])
        pond = self.ponds.get(pname)
        if pond is None:
            self.log("ERROR", f"监测数据引用不存在的塘 {pname}, 已丢弃")
            return
        # 溶氧/氨氮矛盾检测
        sat = do_saturation(temp)
        if do > sat * 1.05:
            self.log("ERROR", f"塘 {pname}: 溶氧 {do} 超过 {temp}℃ 饱和值 "
                              f"{sat:.2f}, 数据矛盾(疑似传感器异常)")
        if do < 2.0 and nh3 < 0.1:
            self.log("ERROR", f"塘 {pname}: 溶氧 {do} 极低但氨氮 {nh3} 极低, "
                              f"数据矛盾(缺氧通常伴随有机污染)")
        pond.temp = temp
        # 跨周期自然过程与设备效果 (状态延续)
        pond.do = max(0.0, do - RESPIRATION_PER_DENSITY * pond.density)
        pond.nh3 = nh3
        if any(d.dtype == "aerator" and d.on and not d.faulty
               for d in self.devices_of(pname)):
            pond.do = min(pond.do + AERATION_RECOVERY, sat)
        if any(d.dtype == "exchanger" and d.on and not d.faulty
               for d in self.devices_of(pname)):
            pond.nh3 *= 1 - EXCHANGE_REMOVAL
        self.log("INFO", f"塘 {pname}: 监测 溶氧 {pond.do:.2f} 温度 {temp} "
                         f"氨氮 {pond.nh3:.3f}")
        self.evaluate(pond)

    def cmd_fault(self, a):
        dev = self.devices.get(a[0])
        if dev is None:
            self.log("ERROR", f"故障上报引用不存在的设备 {a[0]}")
            return
        dev.faulty, dev.on, dev.auto = True, False, False
        self.log("ERROR", f"设备 {dev.name}({DEVICE_TYPES[dev.dtype]}) 故障")
        for pname in dev.pond_names:           # 服务塘多参数级联告警
            pond = self.ponds[pname]
            self.set_alarm(pond, f"DEV_FAULT:{dev.name}",
                           f"设备 {dev.name}({DEVICE_TYPES[dev.dtype]}) 故障")
            self.evaluate(pond)

    def cmd_repair(self, a):
        dev = self.devices.get(a[0])
        if dev is None:
            self.log("ERROR", f"修复引用不存在的设备 {a[0]}")
            return
        if not dev.faulty:
            self.log("INFO", f"设备 {dev.name} 未处于故障状态")
            return
        dev.faulty = False
        self.log("INFO", f"设备 {dev.name} 修复")
        for pname in dev.pond_names:           # 级联解除
            pond = self.ponds[pname]
            self.clear_alarm(pond, f"DEV_FAULT:{dev.name}")
            self.evaluate(pond)

    # ---------- 评估与级联 ----------
    def devices_of(self, pname, dtype=None):
        return [d for d in self.devices.values()
                if pname in d.pond_names and (dtype is None or d.dtype == dtype)]

    def evaluate(self, pond):
        do_min, nh3_max, _ = pond.thresholds()
        # 溶氧
        if pond.do < do_min:
            self.set_alarm(pond, "LOW_DO",
                           f"溶氧 {pond.do:.2f} 低于阈值 {do_min}")
            self.cascade(pond, "aerator", True)
        elif pond.do >= do_min + DO_HYSTERESIS:
            self.clear_alarm(pond, "LOW_DO")
            self.cascade(pond, "aerator", False)
        # 氨氮
        if pond.nh3 > nh3_max:
            self.set_alarm(pond, "HIGH_NH3",
                           f"氨氮 {pond.nh3:.3f} 超过阈值 {nh3_max}")
            self.cascade(pond, "exchanger", True)
        elif pond.nh3 <= nh3_max * NH3_CLEAR_RATIO:
            self.clear_alarm(pond, "HIGH_NH3")
            self.cascade(pond, "exchanger", False)

    def cascade(self, pond, dtype, need):
        key = "NO_AERATION" if dtype == "aerator" else "NO_EXCHANGE"
        cname = DEVICE_TYPES[dtype]
        devs = self.devices_of(pond.name, dtype)
        avail = [d for d in devs if not d.faulty]
        if need:
            if not avail:
                self.set_alarm(pond, key,
                               f"需要{cname}但设备不可用(故障或未配置)")
                return
            self.clear_alarm(pond, key)
            for d in avail:
                if not d.on:
                    d.on, d.auto = True, True
                    self.log("CASCADE", f"级联开启{cname}设备 {d.name} (塘 {pond.name})")
        else:
            self.clear_alarm(pond, key)
            for d in devs:
                if d.on and d.auto and not any(
                        ("LOW_DO" if dtype == "aerator" else "HIGH_NH3")
                        in self.ponds[p].alarms for p in d.pond_names):
                    d.on = d.auto = False
                    self.log("CASCADE", f"级联关闭{cname}设备 {d.name} (水质恢复)")

    # ---------- 输出 ----------
    def print_status(self):
        print("\n========== 养殖状态 ==========")
        for p in self.ponds.values():
            do_min, nh3_max, level = p.thresholds()
            print(f"塘 {p.name} | {p.species} | {level}塘 "
                  f"(密度 {p.density} kg/m3, 生物量 {p.biomass:.0f} kg)")
            print(f"  溶氧 {p.do:.2f} mg/L (阈值>={do_min}) | "
                  f"温度 {p.temp:.1f} ℃ | 氨氮 {p.nh3:.3f} mg/L (阈值<={nh3_max})")
            devs = self.devices_of(p.name)
            desc = ", ".join(
                f"{d.name}({DEVICE_TYPES[d.dtype]}:"
                f"{'故障' if d.faulty else ('开' if d.on else '关')})"
                for d in devs) or "无"
            print(f"  设备: {desc}")
            print(f"  活动告警: {'; '.join(p.alarms.values()) or '无'}")
        print("==============================\n")

    def print_errors(self):
        print("========== 错误清单 ==========")
        errs = [(n, m) for n, k, m in self.events if k == "ERROR"]
        if errs:
            for n, m in errs:
                print(f"[#{n:03d}] {m}")
        else:
            print("无错误")
        active = sum(len(p.alarms) for p in self.ponds.values())
        print(f"未解除告警: {active} 项")
        print("==============================")


def main(argv):
    farm = Farm()
    src = open(argv[1], encoding="utf-8") if len(argv) > 1 else sys.stdin
    with src:
        for lineno, raw in enumerate(src, 1):
            line = raw.split("#", 1)[0].strip()
            if line:
                farm.execute(line, lineno)
    farm.print_status()
    farm.print_errors()


if __name__ == "__main__":
    main(sys.argv)
