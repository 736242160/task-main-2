#!/usr/bin/env python3
"""trunk.py — 快递干线调度状态与错误报告工具(纯 Python 标准库,单文件)

用法:
    python3 trunk.py [输入文件]     # 缺省读标准输入
    python3 trunk.py --demo         # 运行内置样例

输入格式(UTF-8 文本,每行一条指令,'#' 之后为注释,空行忽略):

    LINE    <线路名> <计划发车HH:MM> <运行时长分钟> <装载上限件数>
    VEHICLE <车辆编号> <可用|检修>
    LOAD    <线路名> <车辆编号> <包裹量>
    DEPART  <线路名> <车辆编号> <实际发车HH:MM>

指令按文件顺序逐条处理,定义须先于引用出现;全部状态跨指令持续(跨流状态延续)。
时刻 HH:MM 中小时可 >= 24(如 25:30 表示次日 01:30),内部统一为"分钟数"。

自定规则(设计取舍):
    1. 晚点判定:实际发车时刻 > 线路"当前有效计划时刻"即晚点。
       有效计划 = 基础计划 + 累计顺延量。
    2. 级联顺延:某班次晚点 δ 分钟,则该线后续班次的有效计划全额顺延 δ;
       一旦某班次不晚点(实际 <= 有效计划),顺延量清零。理由:干线班次通常
       共用场站泊位/分拣资源,晚点会真实挤压后续班次;而准点班次说明资源已
       恢复,不应把历史晚点无限传递。
    3. 装载上限按"单线-单车-单班次"核算(即某车在某线当前班次上的累计装载),
       超出即报超装(线、车、超装量);超装的装载仍计入状态(货已装上,只能
       报错不能假装没发生)。
    4. 检修车发车:报错并拒绝该次发车,车辆与线路状态不变。
    5. 发车冲突:车辆上一班次在途期间([实际发车, 实际发车+运行时长))再次
       发车即冲突,拒绝;同一时刻多线路发车是其特例。理由:同一时刻冲突只是
       物理不可行的最小情形,在途未归同样不可行。
    6. 重复装载:同车同线在该班次发车前第二次装载,报错并忽略(保留首次)。
       发车后该(线,车)装载清零,下一班次可重新装载。
    7. 引用不存在的线路/车辆:报错并忽略该事件。
    8. 发车后级联更新:该(线,车)装载清零、车辆进入"在途"至 实际发车+运行
       时长、线路班次计数与累计包裹更新、线路顺延量按规则 2 更新。
    9. 重复定义同名线路/车辆:报错并保留首次定义。

退出码:有错误时为 1,否则为 0(便于接入调度流水线)。
"""

import sys
from dataclasses import dataclass, field


# ---------------------------------------------------------------- 基础工具

def parse_time(text):
    """'HH:MM' -> 分钟数;小时允许 >= 24 以表示跨日。"""
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("时刻格式应为 HH:MM")
    hours, minutes = int(parts[0]), int(parts[1])
    if hours < 0 or not 0 <= minutes < 60:
        raise ValueError("非法时刻")
    return hours * 60 + minutes


def fmt_time(minutes):
    return "%02d:%02d" % (minutes // 60, minutes % 60)


# ---------------------------------------------------------------- 数据模型

@dataclass
class Line:
    name: str
    base_depart: int          # 基础计划发车(分钟)
    duration: int             # 运行时长(分钟)
    cap: int                  # 装载上限(件/车/班次)
    shift: int = 0            # 累计顺延量(分钟)
    trips: int = 0            # 已发班次数
    total_pkgs: int = 0       # 累计承运包裹
    last_actual: int = None   # 最近一班实际发车

    @property
    def planned(self):
        return self.base_depart + self.shift


@dataclass
class Vehicle:
    vid: str
    status: str                       # 可用 / 检修
    available_until: int = None       # 在途至该时刻(None 表示空闲)
    trips: int = 0
    pending: dict = field(default_factory=dict)  # 线路名 -> 已装待运包裹


class Reporter:
    def __init__(self):
        self.errors = []              # (行号, 类别, 描述)

    def error(self, lineno, category, message):
        self.errors.append((lineno, category, message))


# ---------------------------------------------------------------- 指令处理

def handle_define_line(tokens, lineno, lines, reporter):
    if len(tokens) != 5:
        reporter.error(lineno, "PARSE", "LINE 需要 4 个参数: 名称 发车时刻 运行时长 装载上限")
        return
    _, name, depart_s, duration_s, cap_s = tokens
    try:
        depart = parse_time(depart_s)
        duration = int(duration_s)
        cap = int(cap_s)
        if duration <= 0 or cap < 0:
            raise ValueError
    except ValueError:
        reporter.error(lineno, "PARSE", "LINE 参数非法: 时刻须为 HH:MM, 时长须为正整数, 上限须为非负整数")
        return
    if name in lines:
        reporter.error(lineno, "REDEFINE", "线路 %s 重复定义, 保留首次定义" % name)
        return
    lines[name] = Line(name, depart, duration, cap)


def handle_define_vehicle(tokens, lineno, vehicles, reporter):
    if len(tokens) != 3:
        reporter.error(lineno, "PARSE", "VEHICLE 需要 2 个参数: 编号 状态(可用|检修)")
        return
    _, vid, status = tokens
    if status not in ("可用", "检修"):
        reporter.error(lineno, "PARSE", "车辆状态须为 可用 或 检修, 得到 %r" % status)
        return
    if vid in vehicles:
        reporter.error(lineno, "REDEFINE", "车辆 %s 重复定义, 保留首次定义" % vid)
        return
    vehicles[vid] = Vehicle(vid, status)


def handle_load(tokens, lineno, lines, vehicles, reporter):
    if len(tokens) != 4:
        reporter.error(lineno, "PARSE", "LOAD 需要 3 个参数: 线路 车辆 包裹量")
        return
    _, line_name, vid, pkgs_s = tokens
    line = lines.get(line_name)
    if line is None:
        reporter.error(lineno, "UNKNOWN_LINE", "装载引用不存在的线路 %s" % line_name)
        return
    veh = vehicles.get(vid)
    if veh is None:
        reporter.error(lineno, "UNKNOWN_VEHICLE", "装载引用不存在的车辆 %s" % vid)
        return
    try:
        pkgs = int(pkgs_s)
        if pkgs < 0:
            raise ValueError
    except ValueError:
        reporter.error(lineno, "PARSE", "包裹量须为非负整数, 得到 %r" % pkgs_s)
        return
    if line_name in veh.pending:
        reporter.error(lineno, "DUPLICATE_LOAD",
                       "车辆 %s 在线路 %s 当前班次重复装载(已装 %d 件), 忽略本次 %d 件"
                       % (vid, line_name, veh.pending[line_name], pkgs))
        return
    if pkgs > line.cap:
        reporter.error(lineno, "OVERLOAD",
                       "线路 %s 车辆 %s 超装 %d 件(装载 %d, 上限 %d)"
                       % (line_name, vid, pkgs - line.cap, pkgs, line.cap))
    veh.pending[line_name] = pkgs


def handle_depart(tokens, lineno, lines, vehicles, reporter, state):
    if len(tokens) != 4:
        reporter.error(lineno, "PARSE", "DEPART 需要 3 个参数: 线路 车辆 实际时刻")
        return
    _, line_name, vid, time_s = tokens
    line = lines.get(line_name)
    if line is None:
        reporter.error(lineno, "UNKNOWN_LINE", "发车引用不存在的线路 %s" % line_name)
        return
    veh = vehicles.get(vid)
    if veh is None:
        reporter.error(lineno, "UNKNOWN_VEHICLE", "发车引用不存在的车辆 %s" % vid)
        return
    try:
        actual = parse_time(time_s)
    except ValueError:
        reporter.error(lineno, "PARSE", "发车时刻非法: %r" % time_s)
        return
    state["max_time"] = max(state["max_time"], actual)

    if veh.status == "检修":
        reporter.error(lineno, "MAINTENANCE_DEPART",
                       "检修车辆 %s 不得发车(线路 %s, 时刻 %s), 已拒绝"
                       % (vid, line_name, fmt_time(actual)))
        return
    if veh.available_until is not None and actual < veh.available_until:
        reporter.error(lineno, "CONFLICT",
                       "车辆 %s 时刻 %s 发车冲突: 上一班次在途至 %s(线路 %s), 已拒绝"
                       % (vid, fmt_time(actual), fmt_time(veh.available_until), line_name))
        return

    planned = line.planned
    if actual > planned:
        late = actual - planned
        reporter.error(lineno, "LATE",
                       "线路 %s 车辆 %s 晚点 %d 分钟(计划 %s, 实际 %s), 后续班次顺延"
                       % (line_name, vid, late, fmt_time(planned), fmt_time(actual)))
        line.shift += late            # 晚点全额顺延
    else:
        line.shift = 0                # 准点/提前: 顺延清零

    pkgs = veh.pending.pop(line_name, 0)   # 装载随发车清零
    line.trips += 1
    line.total_pkgs += pkgs
    line.last_actual = actual
    veh.trips += 1
    veh.available_until = actual + line.duration


HANDLERS = {
    "LINE": handle_define_line,
    "VEHICLE": handle_define_vehicle,
    "LOAD": handle_load,
    "DEPART": handle_depart,
}


def process(text):
    lines = {}
    vehicles = {}
    reporter = Reporter()
    state = {"max_time": 0}
    for lineno, raw in enumerate(text.splitlines(), 1):
        body = raw.split("#", 1)[0].strip()
        if not body:
            continue
        tokens = body.split()
        handler = HANDLERS.get(tokens[0].upper())
        if handler is None:
            reporter.error(lineno, "PARSE", "未知指令 %r" % tokens[0])
            continue
        if handler is handle_define_line:
            handler(tokens, lineno, lines, reporter)
        elif handler is handle_define_vehicle:
            handler(tokens, lineno, vehicles, reporter)
        elif handler is handle_load:
            handler(tokens, lineno, lines, vehicles, reporter)
        else:
            handler(tokens, lineno, lines, vehicles, reporter, state)
    return lines, vehicles, reporter, state


# ---------------------------------------------------------------- 报告输出

def render(lines, vehicles, reporter, state):
    out = []
    out.append("===== 干线状态 =====")
    out.append("[线路]")
    if not lines:
        out.append("  (无线路)")
    for line in lines.values():
        last = fmt_time(line.last_actual) if line.last_actual is not None else "无"
        out.append(
            "  %s 计划%s 顺延+%d分 已发%d班 最近发车%s 累计包裹%d件"
            % (line.name, fmt_time(line.base_depart), line.shift,
               line.trips, last, line.total_pkgs))
    out.append("[车辆]")
    if not vehicles:
        out.append("  (无车辆)")
    for veh in vehicles.values():
        if veh.status == "检修":
            status = "检修"
        elif veh.available_until is not None and veh.available_until > state["max_time"]:
            status = "在途(至%s)" % fmt_time(veh.available_until)
        else:
            status = "可用"
        pending = sum(veh.pending.values())
        out.append("  %s %s 已发%d班 待运%d件" % (veh.vid, status, veh.trips, pending))

    out.append("===== 错误清单 =====")
    if not reporter.errors:
        out.append("  (无错误)")
    for idx, (lineno, category, message) in enumerate(reporter.errors, 1):
        out.append("  %d. [%s] 行%d: %s" % (idx, category, lineno, message))
    out.append("共 %d 个错误" % len(reporter.errors))
    return "\n".join(out)


# ---------------------------------------------------------------- 入口

SAMPLE = """\
# 线路: 名称 计划发车 运行时长(分) 装载上限
LINE L1 08:00 120 500
LINE L2 09:00 90 300
# 车辆: 编号 状态
VEHICLE V1 可用
VEHICLE V2 可用
VEHICLE V3 检修
LOAD L1 V1 600        # 超装 100 件
LOAD L1 V1 10         # 同车同线重复装载
LOAD L9 V1 10         # 线路不存在
LOAD L1 V9 10         # 车辆不存在
DEPART L1 V1 08:40    # 晚点 40 分, L1 后续班次顺延 40
DEPART L1 V2 08:40    # 有效计划已顺延至 08:40, 准点, 顺延清零
DEPART L2 V1 08:40    # V1 在途至 10:40, 同时刻冲突
DEPART L2 V3 09:30    # 检修车发车
DEPART L2 V2 11:00    # V2 已于 10:40 归队; L2 晚点 120 分
"""


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        text = SAMPLE
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    lines, vehicles, reporter, state = process(text)
    print(render(lines, vehicles, reporter, state))
    return 1 if reporter.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
