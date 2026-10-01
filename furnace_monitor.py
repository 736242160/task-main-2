#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高炉冶炼状态监控工具（纯 Python 标准库，单文件）。

输入（JSON，文件路径作为参数或从 stdin 读入）：
{
  "furnaces":  [{"name": "1号炉", "temp_min": 1400, "temp_max": 1550, "batch_limit": 1000}],
  "materials": [{"name": "铁矿石", "type": "矿石"}],          # type ∈ 矿石/焦炭/辅料
  "feeds":     [{"seq": 1, "furnace": "1号炉", "material": "铁矿石", "amount": 600}],
  "smelts":    [{"seq": 2, "furnace": "1号炉", "temp": 1480, "duration": 2.0, "result": "出铁"}]
}
feeds 与 smelts 按 seq 归并为统一事件流依次处理（同 seq 时加料先于冶炼），
状态跨流延续：加料累计影响冶炼时的配料比判定，冶炼结果影响后续加料合法性。

业务规则（自定，理由见各规则注释）：
R1 炉温：实测温度超出 [temp_min, temp_max] 即炉温失控，报告炉、温度、超限量。
R2 配料比：以当前批次累计配料总量为基数，矿石 50%~80%、焦炭 15%~40%、辅料 ≤15%。
   理由：矿石是还原主体须占多数；焦炭提供热量与还原剂，过低炉温不足、过高料柱
   透气性差；辅料（熔剂）仅用于造渣，过多会稀释炉料、增加渣量。冶炼时判定。
R3 配料上限：当前批次累计加料总量超过 batch_limit 即超限，报告超限量。
R4 级联停产：冶炼结果为“异常”的炉立即停产，且本工具不支持复产；
   停产炉后续加料、冶炼全部报错并跳过。
R5 重复冶炼：上一次冶炼未出铁（结果为“继续”），再次冶炼即报“重复冶炼未出铁”。
R6 引用校验：加料/冶炼引用不存在的炉或料，报错并跳过该事件。
R7 级联更新：每次合法加料后按类型与总量累计更新；冶炼“出铁”后批次累计清零，
   开始下一批次；“异常/继续”不清零。
"""

import argparse
import json
import sys
from dataclasses import dataclass, field

MATERIAL_TYPES = ("矿石", "焦炭", "辅料")
SMELT_RESULTS = ("出铁", "异常", "继续")

# R2 配料比规则：类型 -> (下限, 上限)，占当前批次累计总量的质量分数
RATIO_RULES = {
    "矿石": (0.50, 0.80),
    "焦炭": (0.15, 0.40),
    "辅料": (0.00, 0.15),
}


@dataclass
class Furnace:
    name: str
    temp_min: float
    temp_max: float
    batch_limit: float
    state: str = "运行"                       # 运行 / 停产
    batch: dict = field(default_factory=lambda: {t: 0.0 for t in MATERIAL_TYPES})
    feed_count: int = 0
    smelt_count: int = 0
    tap_count: int = 0
    anomaly_count: int = 0
    last_smelt_tapped: bool = True            # 上一炉次是否已出铁

    @property
    def batch_total(self):
        return sum(self.batch.values())


class Monitor:
    def __init__(self, furnaces, materials):
        self.furnaces = {f.name: f for f in furnaces}
        self.materials = dict(materials)      # name -> type
        self.errors = []

    def _err(self, seq, etype, message, **details):
        self.errors.append({"seq": seq, "type": etype, "message": message,
                            "details": details})

    # ---------- 加料事件 ----------
    def on_feed(self, seq, fname, mname, amount):
        if fname not in self.furnaces:                       # R6
            self._err(seq, "引用不存在的炉", f"加料引用了不存在的炉「{fname}」",
                      furnace=fname)
            return
        if mname not in self.materials:                      # R6
            self._err(seq, "引用不存在的料", f"加料引用了不存在的料「{mname}」",
                      material=mname)
            return
        if not isinstance(amount, (int, float)) or amount <= 0:
            self._err(seq, "非法加料量", f"加料量 {amount!r} 不是正数",
                      furnace=fname, material=mname)
            return
        f = self.furnaces[fname]
        if f.state == "停产":                                # R4
            self._err(seq, "停产炉加料",
                      f"炉「{fname}」已停产，拒绝加料 {mname} {amount}",
                      furnace=fname, material=mname, amount=amount)
            return
        mtype = self.materials[mname]
        f.batch[mtype] += amount                             # R7 级联累计
        f.feed_count += 1
        total = f.batch_total
        if total > f.batch_limit:                            # R3
            self._err(seq, "加料超配料上限",
                      f"炉「{fname}」批次累计 {total} 超上限 {f.batch_limit}，"
                      f"超限 {round(total - f.batch_limit, 6)}",
                      furnace=fname, batch_total=total,
                      batch_limit=f.batch_limit,
                      excess=round(total - f.batch_limit, 6))

    # ---------- 冶炼事件 ----------
    def on_smelt(self, seq, fname, temp, duration, result):
        if fname not in self.furnaces:                       # R6
            self._err(seq, "引用不存在的炉", f"冶炼引用了不存在的炉「{fname}」",
                      furnace=fname)
            return
        f = self.furnaces[fname]
        if f.state == "停产":                                # R4
            self._err(seq, "停产炉冶炼",
                      f"炉「{fname}」已停产，拒绝冶炼", furnace=fname)
            return
        if result not in SMELT_RESULTS:
            self._err(seq, "非法冶炼结果",
                      f"结果「{result}」应为 {SMELT_RESULTS} 之一", furnace=fname)
            return
        if not isinstance(temp, (int, float)):
            self._err(seq, "非法温度", f"实测温度 {temp!r} 不是数值", furnace=fname)
            return
        if not isinstance(duration, (int, float)) or duration <= 0:
            self._err(seq, "非法时长", f"冶炼时长 {duration!r} 不是正数",
                      furnace=fname)
            return

        f.smelt_count += 1

        if not f.last_smelt_tapped:                          # R5
            self._err(seq, "重复冶炼未出铁",
                      f"炉「{fname}」上一炉次未出铁，再次冶炼", furnace=fname)

        if temp > f.temp_max:                                # R1
            self._err(seq, "炉温超上限",
                      f"炉「{fname}」实测 {temp} 超上限 {f.temp_max}，"
                      f"超限量 {round(temp - f.temp_max, 6)}",
                      furnace=fname, temp=temp,
                      excess=round(temp - f.temp_max, 6))
        elif temp < f.temp_min:
            self._err(seq, "炉温低于下限",
                      f"炉「{fname}」实测 {temp} 低于下限 {f.temp_min}，"
                      f"超限量 {round(f.temp_min - temp, 6)}",
                      furnace=fname, temp=temp,
                      excess=round(f.temp_min - temp, 6))

        self._check_ratio(seq, f)                            # R2

        if result == "出铁":                                 # R7 批次清零
            f.tap_count += 1
            f.batch = {t: 0.0 for t in MATERIAL_TYPES}
            f.last_smelt_tapped = True
        elif result == "异常":                               # R4 级联停产
            f.anomaly_count += 1
            f.state = "停产"
            f.last_smelt_tapped = False
        else:  # 继续
            f.last_smelt_tapped = False

    def _check_ratio(self, seq, f):
        total = f.batch_total
        if total <= 0:
            self._err(seq, "配料比失衡",
                      f"炉「{f.name}」冶炼时批次内无任何配料", furnace=f.name)
            return
        bad = []
        for mtype, (lo, hi) in RATIO_RULES.items():
            share = f.batch[mtype] / total
            if share < lo or share > hi:
                bad.append({"type": mtype, "share": round(share, 4),
                            "required": [lo, hi],
                            "amount": f.batch[mtype]})
        if bad:
            self._err(seq, "配料比失衡",
                      f"炉「{f.name}」批次累计 {total}，配料比越限："
                      + "；".join(f"{b['type']} {b['share']:.1%}"
                                  f"（要求 {b['required'][0]:.0%}~{b['required'][1]:.0%}）"
                                  for b in bad),
                      furnace=f.name, batch_total=total, violations=bad)

    # ---------- 输出 ----------
    def status(self):
        return [{
            "furnace": f.name,
            "state": f.state,
            "batch_cumulative": {**f.batch, "合计": f.batch_total},
            "batch_limit": f.batch_limit,
            "feed_count": f.feed_count,
            "smelt_count": f.smelt_count,
            "tap_count": f.tap_count,
            "anomaly_count": f.anomaly_count,
        } for f in self.furnaces.values()]


def load_definitions(data, errors):
    furnaces, materials = [], {}
    for i, d in enumerate(data.get("furnaces", [])):
        try:
            furnaces.append(Furnace(str(d["name"]), float(d["temp_min"]),
                                    float(d["temp_max"]), float(d["batch_limit"])))
        except (KeyError, TypeError, ValueError) as e:
            errors.append({"seq": None, "type": "炉定义非法",
                           "message": f"第 {i} 条炉定义非法: {e}", "details": {}})
    names = set()
    for f in furnaces:
        if f.name in names:
            errors.append({"seq": None, "type": "炉重名",
                           "message": f"炉「{f.name}」重复定义", "details": {}})
        names.add(f.name)
        if f.temp_min >= f.temp_max:
            errors.append({"seq": None, "type": "炉定义非法",
                           "message": f"炉「{f.name}」温控区间为空", "details": {}})
    for i, d in enumerate(data.get("materials", [])):
        name, mtype = str(d.get("name", "")), d.get("type")
        if mtype not in MATERIAL_TYPES:
            errors.append({"seq": None, "type": "料定义非法",
                           "message": f"第 {i} 条料「{name}」类型「{mtype}」"
                                      f"应为 {MATERIAL_TYPES} 之一", "details": {}})
            continue
        if name in materials:
            errors.append({"seq": None, "type": "料重名",
                           "message": f"料「{name}」重复定义", "details": {}})
        materials[name] = mtype
    return furnaces, materials


def run(data):
    errors = []
    furnaces, materials = load_definitions(data, errors)
    mon = Monitor(furnaces, materials)
    mon.errors = errors

    events = []
    for d in data.get("feeds", []):
        events.append((d.get("seq", 0), 0, "feed", d))
    for d in data.get("smelts", []):
        events.append((d.get("seq", 0), 1, "smelt", d))
    events.sort(key=lambda e: (e[0], e[1]))          # 同 seq 加料先于冶炼

    for seq, _, kind, d in events:
        if kind == "feed":
            mon.on_feed(seq, str(d.get("furnace", "")), str(d.get("material", "")),
                        d.get("amount"))
        else:
            mon.on_smelt(seq, str(d.get("furnace", "")), d.get("temp"),
                         d.get("duration"), d.get("result"))
    return {"furnace_status": mon.status(), "errors": mon.errors}


DEMO_INPUT = {
    "furnaces": [
        {"name": "1号炉", "temp_min": 1400, "temp_max": 1550, "batch_limit": 1000},
        {"name": "2号炉", "temp_min": 1350, "temp_max": 1500, "batch_limit": 500},
    ],
    "materials": [
        {"name": "铁矿石", "type": "矿石"},
        {"name": "焦炭", "type": "焦炭"},
        {"name": "石灰石", "type": "辅料"},
    ],
    "feeds": [
        {"seq": 1,  "furnace": "1号炉", "material": "铁矿石", "amount": 600},
        {"seq": 2,  "furnace": "1号炉", "material": "焦炭",   "amount": 250},
        {"seq": 3,  "furnace": "1号炉", "material": "石灰石", "amount": 200},
        {"seq": 6,  "furnace": "1号炉", "material": "铁矿石", "amount": 500},
        {"seq": 7,  "furnace": "1号炉", "material": "焦炭",   "amount": 200},
        {"seq": 8,  "furnace": "1号炉", "material": "石灰石", "amount": 50},
        {"seq": 10, "furnace": "1号炉", "material": "焦炭",   "amount": 100},
        {"seq": 12, "furnace": "9号炉", "material": "焦炭",   "amount": 100},
        {"seq": 13, "furnace": "2号炉", "material": "萤石",   "amount": 10},
        {"seq": 14, "furnace": "2号炉", "material": "铁矿石", "amount": 300},
        {"seq": 15, "furnace": "2号炉", "material": "焦炭",   "amount": 150},
        {"seq": 16, "furnace": "2号炉", "material": "石灰石", "amount": 30},
    ],
    "smelts": [
        {"seq": 4,  "furnace": "1号炉", "temp": 1600, "duration": 2.0, "result": "继续"},
        {"seq": 5,  "furnace": "1号炉", "temp": 1480, "duration": 1.5, "result": "出铁"},
        {"seq": 9,  "furnace": "1号炉", "temp": 1450, "duration": 1.0, "result": "异常"},
        {"seq": 11, "furnace": "1号炉", "temp": 1450, "duration": 1.0, "result": "出铁"},
        {"seq": 17, "furnace": "2号炉", "temp": 1300, "duration": 2.0, "result": "出铁"},
    ],
}


def main(argv=None):
    ap = argparse.ArgumentParser(description="高炉冶炼状态监控工具（纯标准库）")
    ap.add_argument("input", nargs="?", help="输入 JSON 文件；缺省读 stdin")
    ap.add_argument("--demo", action="store_true", help="运行内置示例并输出报告")
    args = ap.parse_args(argv)

    if args.demo:
        data = DEMO_INPUT
    elif args.input:
        with open(args.input, encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = json.load(sys.stdin)

    report = run(data)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
