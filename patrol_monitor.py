#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""保护区巡护状态监控工具（纯 Python 标准库，单文件）。

输入一个文本文件，包含四类记录（# 开头为注释，空白分隔）：

    point  <名称> <区域> <巡护周期天数>        巡护点定义
    ranger <名称> <区域>                      巡护员定义
    patrol <巡护员> <巡护点> <YYYY-MM-DD> <正常|异常>   巡护流
    event  <巡护点> <盗猎|火灾|非法占用> <处置|上报> <YYYY-MM-DD>  事件流

规则说明（自定部分均在此注明理由）：

1. 超期未巡护：以巡护周期为“两次巡护允许的最大间隔”。若某点
   最近巡护日期 + 周期 < 基准日期（--as-of，默认为流中最大日期），
   或从未被巡护，则报告。理由：周期即管理部门承诺的最长空白期，
   超过即存在监管真空。
2. 跨区域巡护：巡护员区域 != 巡护点区域即报告（记录仍计入轨迹，
   因为人确实到过，但属于违规巡护）。
3. 事件“处置”后，该点未决上报清空、连续异常计数清零、状态级联
   回落为“正常”。
4. 事件“上报”后未处置前，该点持续异常（状态显示“异常(待处置)”），
   期间即使巡护结果为“正常”也不解除；汇总时报告未处置事件。
5. 连续异常累计升级：阈值定为 3 级——连续异常 1 次“关注”、
   2 次“预警”、>=3 次“严重”。理由：1~2 次可能是偶发，
   3 次连续异常说明问题持续存在，需升级处置。每次升级产生警告。
6. 同一巡护点同一日期出现多条巡护记录，第二条起报告重复巡护。
7. 引用不存在的巡护点/巡护员（含事件流引用）均报告，该条记录
   不参与状态计算。
8. 跨流状态延续：巡护流与事件流按 (日期, 文件行号) 归并排序后
   统一回放，状态在两条流之间连续传递。

输出：巡护状态表 + 错误清单；退出码 0 表示无“错误”级问题，1 表示有。
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date

VALID_RESULTS = {"正常", "异常"}
VALID_EVENT_TYPES = {"盗猎", "火灾", "非法占用"}
VALID_HANDLING = {"处置", "上报"}

# 连续异常次数 -> 状态级别（索引即次数，>=3 截断到“严重”）
LEVELS = ["正常", "关注", "预警", "严重"]
ESCALATION_THRESHOLD = 3  # 达到该连续异常次数即升为最高级“严重”


@dataclass
class Point:
    name: str
    region: str
    period_days: int
    last_patrol: date = None
    patrol_count: int = 0
    consecutive_abnormal: int = 0
    level: str = "正常"
    pending_reports: list = field(default_factory=list)  # [(date, 类型), ...]


@dataclass
class Ranger:
    name: str
    region: str


@dataclass
class Record:
    kind: str          # "patrol" | "event"
    fields: tuple
    day: date
    lineno: int


class Reporter:
    def __init__(self):
        self.items = []  # (severity, lineno_or_None, message)

    def error(self, lineno, msg):
        self.items.append(("错误", lineno, msg))

    def warning(self, lineno, msg):
        self.items.append(("警告", lineno, msg))

    @property
    def error_count(self):
        return sum(1 for s, _, _ in self.items if s == "错误")


def parse_date(text, lineno, reporter):
    try:
        return date.fromisoformat(text)
    except ValueError:
        reporter.error(lineno, "日期格式非法：%r（应为 YYYY-MM-DD）" % text)
        return None


def parse(text):
    points, rangers, records = {}, {}, []
    reporter = Reporter()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        kw, args = parts[0], parts[1:]
        if kw == "point":
            if len(args) != 3:
                reporter.error(lineno, "point 需要 3 个参数（名称 区域 周期天数）：%r" % line)
                continue
            name, region, period_s = args
            if name in points:
                reporter.error(lineno, "巡护点重复定义：%s（保留首次定义）" % name)
                continue
            try:
                period = int(period_s)
                if period <= 0:
                    raise ValueError
            except ValueError:
                reporter.error(lineno, "巡护周期必须为正整数：%r" % period_s)
                continue
            points[name] = Point(name, region, period)
        elif kw == "ranger":
            if len(args) != 2:
                reporter.error(lineno, "ranger 需要 2 个参数（名称 区域）：%r" % line)
                continue
            name, region = args
            if name in rangers:
                reporter.error(lineno, "巡护员重复定义：%s（保留首次定义）" % name)
                continue
            rangers[name] = Ranger(name, region)
        elif kw == "patrol":
            if len(args) != 4:
                reporter.error(lineno, "patrol 需要 4 个参数（巡护员 巡护点 日期 结果）：%r" % line)
                continue
            ranger, point, day_s, result = args
            if result not in VALID_RESULTS:
                reporter.error(lineno, "巡护结果非法：%r（应为 正常/异常）" % result)
                continue
            day = parse_date(day_s, lineno, reporter)
            if day is not None:
                records.append(Record("patrol", (ranger, point, result), day, lineno))
        elif kw == "event":
            if len(args) != 4:
                reporter.error(lineno, "event 需要 4 个参数（巡护点 类型 处置 日期）：%r" % line)
                continue
            point, etype, handling, day_s = args
            if etype not in VALID_EVENT_TYPES:
                reporter.error(lineno, "事件类型非法：%r（应为 盗猎/火灾/非法占用）" % etype)
                continue
            if handling not in VALID_HANDLING:
                reporter.error(lineno, "事件处置方式非法：%r（应为 处置/上报）" % handling)
                continue
            day = parse_date(day_s, lineno, reporter)
            if day is not None:
                records.append(Record("event", (point, etype, handling), day, lineno))
        else:
            reporter.error(lineno, "无法识别的记录类型：%r" % kw)
    return points, rangers, records, reporter


def level_for(consecutive):
    return LEVELS[min(consecutive, ESCALATION_THRESHOLD)]


def raise_level(point, new_level, lineno, reporter, reason):
    if LEVELS.index(new_level) > LEVELS.index(point.level):
        point.level = new_level
        reporter.warning(lineno, "巡护点「%s」状态级联升级为「%s」（%s）"
                         % (point.name, new_level, reason))


def process(points, rangers, records, reporter):
    seen_patrols = set()  # (巡护点, 日期) 用于重复巡护检测
    # 跨流状态延续：两流按日期归并，同日按文件先后顺序
    for rec in sorted(records, key=lambda r: (r.day, r.lineno)):
        if rec.kind == "patrol":
            ranger_name, point_name, result = rec.fields
            ranger = rangers.get(ranger_name)
            point = points.get(point_name)
            if ranger is None:
                reporter.error(rec.lineno, "巡护引用不存在的巡护员：%s" % ranger_name)
            if point is None:
                reporter.error(rec.lineno, "巡护引用不存在的巡护点：%s" % point_name)
            if ranger is None or point is None:
                continue
            if ranger.region != point.region:
                reporter.error(rec.lineno,
                               "巡护员「%s」（%s）跨区域巡护巡护点「%s」（%s）"
                               % (ranger.name, ranger.region, point.name, point.region))
            key = (point_name, rec.day)
            if key in seen_patrols:
                reporter.error(rec.lineno, "重复巡护：巡护点「%s」在 %s 已巡护过"
                               % (point_name, rec.day.isoformat()))
            else:
                seen_patrols.add(key)
            point.patrol_count += 1
            if point.last_patrol is None or rec.day > point.last_patrol:
                point.last_patrol = rec.day
            if result == "异常":
                point.consecutive_abnormal += 1
                raise_level(point, level_for(point.consecutive_abnormal), rec.lineno,
                            reporter, "连续异常 %d 次" % point.consecutive_abnormal)
            else:
                point.consecutive_abnormal = 0
                if point.pending_reports:
                    # 上报事件未处置：持续异常，不因一次正常巡护解除
                    raise_level(point, "关注", rec.lineno, reporter,
                                "存在未处置的上报事件，点持续异常")
                else:
                    point.level = "正常"
        else:  # event
            point_name, etype, handling = rec.fields
            point = points.get(point_name)
            if point is None:
                reporter.error(rec.lineno, "事件引用不存在的巡护点：%s" % point_name)
                continue
            if handling == "处置":
                # 处置完成：级联回落——未决上报清空、连续异常清零、状态回正常
                point.pending_reports.clear()
                point.consecutive_abnormal = 0
                point.level = "正常"
            else:  # 上报
                point.pending_reports.append((rec.day, etype))
                raise_level(point, "关注", rec.lineno, reporter,
                            "上报事件（%s）未处置，点转入异常" % etype)


def check_overdue(points, as_of, reporter):
    for point in points.values():
        if point.last_patrol is None:
            reporter.error(None, "巡护点「%s」（%s）从未巡护（周期 %d 天）"
                           % (point.name, point.region, point.period_days))
        else:
            gap = (as_of - point.last_patrol).days
            if gap > point.period_days:
                reporter.error(None,
                               "巡护点「%s」（%s）超期未巡护：最近巡护 %s，"
                               "距基准日 %s 已 %d 天，超过周期 %d 天"
                               % (point.name, point.region,
                                  point.last_patrol.isoformat(),
                                  as_of.isoformat(), gap, point.period_days))
        for day, etype in point.pending_reports:
            reporter.error(None, "巡护点「%s」上报事件（%s，%s）未处置，点持续异常"
                           % (point.name, etype, day.isoformat()))


def status_of(point):
    if point.pending_reports:
        return "异常(待处置)"
    if point.last_patrol is None:
        return "未巡护"
    return point.level


def render_text(points, reporter, as_of):
    out = []
    out.append("== 巡护状态（基准日 %s）==" % as_of.isoformat())
    header = ("巡护点", "区域", "周期(天)", "最近巡护", "巡护次数",
              "连续异常", "待处置事件", "状态")
    rows = []
    for p in points.values():
        rows.append((p.name, p.region, str(p.period_days),
                     p.last_patrol.isoformat() if p.last_patrol else "-",
                     str(p.patrol_count), str(p.consecutive_abnormal),
                     str(len(p.pending_reports)), status_of(p)))
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)] if rows else [len(h) for h in header]
    fmt = "  ".join("%%-%ds" % w for w in widths)
    out.append(fmt % header)
    out.append(fmt % tuple("-" * w for w in widths))
    for row in rows:
        out.append(fmt % row)
    out.append("")
    out.append("== 错误清单（错误 %d 条，警告 %d 条）=="
               % (reporter.error_count, len(reporter.items) - reporter.error_count))
    if not reporter.items:
        out.append("（无）")
    for i, (sev, lineno, msg) in enumerate(reporter.items, 1):
        loc = "行%d: " % lineno if lineno is not None else ""
        out.append("%d. [%s] %s%s" % (i, sev, loc, msg))
    return "\n".join(out)


def build_json(points, reporter, as_of):
    return {
        "as_of": as_of.isoformat(),
        "points": [{
            "name": p.name,
            "region": p.region,
            "period_days": p.period_days,
            "last_patrol": p.last_patrol.isoformat() if p.last_patrol else None,
            "patrol_count": p.patrol_count,
            "consecutive_abnormal": p.consecutive_abnormal,
            "pending_reports": [{"date": d.isoformat(), "type": t}
                                for d, t in p.pending_reports],
            "status": status_of(p),
        } for p in points.values()],
        "issues": [{
            "severity": sev,
            "line": lineno,
            "message": msg,
        } for sev, lineno, msg in reporter.items],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="保护区巡护状态监控工具（纯标准库单文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("input", help="输入文件路径（'-' 表示标准输入）")
    ap.add_argument("--as-of", metavar="YYYY-MM-DD",
                    help="超期判定的基准日期，默认为巡护/事件流中的最大日期")
    ap.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    args = ap.parse_args(argv)

    text = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8").read()
    points, rangers, records, reporter = parse(text)
    process(points, rangers, records, reporter)

    if args.as_of:
        as_of = parse_date(args.as_of, None, reporter)
        if as_of is None:
            return 2
    elif records:
        as_of = max(r.day for r in records)
    else:
        as_of = date.today()
    check_overdue(points, as_of, reporter)

    if args.json:
        print(json.dumps(build_json(points, reporter, as_of),
                         ensure_ascii=False, indent=2))
    else:
        print(render_text(points, reporter, as_of))
    return 1 if reporter.error_count else 0


if __name__ == "__main__":
    sys.exit(main())
