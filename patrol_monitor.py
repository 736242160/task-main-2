#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""保护区巡护状态监控工具（纯 Python 标准库，单文件）

用法:
    python3 patrol_monitor.py 数据文件        # 从文件读取
    python3 patrol_monitor.py - < 数据文件    # 从标准输入读取
    python3 patrol_monitor.py --demo          # 运行内置示例（触发全部规则）

输入格式（按行解析，# 开头为注释，空行忽略，字段以空白分隔）:
    POINT   名称 区域 巡护周期(天)            # 巡护点定义
    RANGER  名称 区域                         # 巡护员定义
    PATROL  巡护员 巡护点 日期(YYYY-MM-DD) 结果(正常|异常)
    EVENT   巡护点 类型(盗猎|火灾|非法占用) 处置(处置|上报)

处理规则（设计理由见 README 注释）:
  1. 超期未巡护: 以数据流中出现的最大日期为基准日，巡护点最近一次巡护距基准日
     超过其巡护周期即判超期；从未巡护过的点位直接报告。理由：巡护周期定义了
     可接受的最大监控空窗，超过即存在漏管风险；用数据内最大日期而非系统时间，
     保证结果可复现、可回放历史数据。
  2. 跨区域巡护: 巡护员负责区域与巡护点区域不一致即报告（巡护记录仍然生效，
     因为巡护事实已发生，但属于违规行为需追责）。
  3. 事件级联: 事件"处置"后，该点状态级联恢复为正常，连续异常计数与未处置
     上报一并清零；"上报"事件使点位进入异常并挂起，直到后续"处置"事件到达。
  4. 上报未处置: 存在未处置上报事件期间，点位持续保持异常；此期间若巡护结果
     为"正常"，属于状态矛盾，报告错误且点位仍保持异常；流结束时仍未处置的
     上报逐点汇总报告。
  5. 连续异常升级: 同一巡护点连续异常巡护达到 3 次（阈值 ESCALATE_THRESHOLD），
     状态由"异常"级联升级为"严重"并报告。理由：单次异常可能是偶发误报，
     连续 3 次表明问题持续存在，必须升级处置级别。正常巡护或事件处置可清零。
  6. 重复巡护: 同一巡护点同一日期出现多条巡护记录，仅首条生效，其余报告。
  7. 引用校验: 巡护/事件引用不存在的巡护点或巡护员，报告并跳过该记录。
  8. 跨流状态延续: 巡护流与事件流按文件出现顺序统一处理，点位状态在两条流
     之间持续延续，后面的记录能看到前面记录造成的状态。
"""
import sys
from datetime import date

ESCALATE_THRESHOLD = 3  # 连续异常升级阈值

VALID_RESULTS = ("正常", "异常")
VALID_EVENT_TYPES = ("盗猎", "火灾", "非法占用")
VALID_HANDLINGS = ("处置", "上报")

STATUS_NORMAL = "正常"
STATUS_ABNORMAL = "异常"
STATUS_CRITICAL = "严重"

KEYWORDS = {
    "POINT": "POINT", "点": "POINT", "巡护点": "POINT",
    "RANGER": "RANGER", "员": "RANGER", "巡护员": "RANGER",
    "PATROL": "PATROL", "巡护": "PATROL",
    "EVENT": "EVENT", "事件": "EVENT",
}


class PointState:
    """单个巡护点的运行时状态（跨流延续）。"""

    def __init__(self, name, area, period):
        self.name = name
        self.area = area
        self.period = period
        self.status = STATUS_NORMAL
        self.last_patrol_date = None
        self.consecutive_abnormal = 0
        self.pending_reports = 0   # 未处置的上报事件数
        self.escalated = False     # 是否已升级到"严重"


class Monitor:
    def __init__(self):
        self.points = {}           # 名称 -> PointState
        self.rangers = {}          # 名称 -> 区域
        self.errors = []           # 错误/告警清单
        self.patrol_seen = set()   # (巡护点, 日期) 用于重复巡护检测
        self.max_date = None       # 数据流中出现的最大日期（超期基准日）

    def error(self, line_no, message):
        prefix = "行%d: " % line_no if line_no else ""
        self.errors.append(prefix + message)

    def _track_date(self, d):
        if self.max_date is None or d > self.max_date:
            self.max_date = d

    # ---------- 定义 ----------

    def add_point(self, line_no, fields):
        if len(fields) != 3:
            self.error(line_no, "POINT 需要 3 个字段(名称 区域 周期)，实际 %d 个" % len(fields))
            return
        name, area, period_text = fields
        if name in self.points:
            self.error(line_no, "巡护点 '%s' 重复定义" % name)
            return
        try:
            period = int(period_text)
            if period <= 0:
                raise ValueError
        except ValueError:
            self.error(line_no, "巡护点 '%s' 的巡护周期 '%s' 不是正整数" % (name, period_text))
            return
        self.points[name] = PointState(name, area, period)

    def add_ranger(self, line_no, fields):
        if len(fields) != 2:
            self.error(line_no, "RANGER 需要 2 个字段(名称 区域)，实际 %d 个" % len(fields))
            return
        name, area = fields
        if name in self.rangers:
            self.error(line_no, "巡护员 '%s' 重复定义" % name)
            return
        self.rangers[name] = area

    # ---------- 巡护流 ----------

    def add_patrol(self, line_no, fields):
        if len(fields) != 4:
            self.error(line_no, "PATROL 需要 4 个字段(巡护员 巡护点 日期 结果)，实际 %d 个" % len(fields))
            return
        ranger_name, point_name, date_text, result = fields

        if ranger_name not in self.rangers:
            self.error(line_no, "巡护记录引用了不存在的巡护员 '%s'，已跳过" % ranger_name)
            return
        if point_name not in self.points:
            self.error(line_no, "巡护记录引用了不存在的巡护点 '%s'，已跳过" % point_name)
            return
        try:
            patrol_date = date.fromisoformat(date_text)
        except ValueError:
            self.error(line_no, "日期 '%s' 格式非法（应为 YYYY-MM-DD），已跳过" % date_text)
            return
        if result not in VALID_RESULTS:
            self.error(line_no, "巡护结果 '%s' 非法（应为 正常|异常），已跳过" % result)
            return

        point = self.points[point_name]
        self._track_date(patrol_date)

        if (point_name, patrol_date) in self.patrol_seen:
            self.error(line_no, "重复巡护：巡护点 '%s' 在 %s 已有巡护记录，本条忽略"
                       % (point_name, patrol_date.isoformat()))
            return
        self.patrol_seen.add((point_name, patrol_date))

        ranger_area = self.rangers[ranger_name]
        if ranger_area != point.area:
            self.error(line_no, "跨区域巡护：巡护员 '%s' 负责区域 '%s'，巡护了点 '%s'(区域 '%s')"
                       % (ranger_name, ranger_area, point_name, point.area))

        if point.last_patrol_date and patrol_date < point.last_patrol_date:
            self.error(line_no, "日期乱序：巡护点 '%s' 的巡护日期 %s 早于已有记录 %s"
                       % (point_name, patrol_date.isoformat(), point.last_patrol_date.isoformat()))
        if point.last_patrol_date is None or patrol_date > point.last_patrol_date:
            point.last_patrol_date = patrol_date

        if result == "异常":
            point.consecutive_abnormal += 1
            if point.consecutive_abnormal >= ESCALATE_THRESHOLD and not point.escalated:
                point.escalated = True
                point.status = STATUS_CRITICAL
                self.error(line_no, "级联升级：巡护点 '%s' 连续异常达 %d 次，状态升级为'严重'"
                           % (point_name, point.consecutive_abnormal))
            elif point.status != STATUS_CRITICAL:
                point.status = STATUS_ABNORMAL
        else:  # 正常
            if point.pending_reports > 0:
                self.error(line_no, "状态矛盾：巡护点 '%s' 有 %d 起上报事件未处置，"
                           "巡护结果不能记为'正常'，点位保持异常"
                           % (point_name, point.pending_reports))
                # 点位持续异常，不清零、不恢复
                if point.status == STATUS_NORMAL:
                    point.status = STATUS_ABNORMAL
            else:
                point.status = STATUS_NORMAL
                point.consecutive_abnormal = 0
                point.escalated = False

    # ---------- 事件流 ----------

    def add_event(self, line_no, fields):
        if len(fields) != 3:
            self.error(line_no, "EVENT 需要 3 个字段(巡护点 类型 处置)，实际 %d 个" % len(fields))
            return
        point_name, event_type, handling = fields

        if point_name not in self.points:
            self.error(line_no, "事件引用了不存在的巡护点 '%s'，已跳过" % point_name)
            return
        if event_type not in VALID_EVENT_TYPES:
            self.error(line_no, "事件类型 '%s' 非法（应为 %s），已跳过"
                       % (event_type, "/".join(VALID_EVENT_TYPES)))
            return
        if handling not in VALID_HANDLINGS:
            self.error(line_no, "处置方式 '%s' 非法（应为 处置|上报），已跳过" % handling)
            return

        point = self.points[point_name]
        if handling == "上报":
            point.pending_reports += 1
            if point.status == STATUS_NORMAL:
                point.status = STATUS_ABNORMAL
        else:  # 处置 -> 级联恢复
            point.pending_reports = 0
            point.consecutive_abnormal = 0
            point.escalated = False
            point.status = STATUS_NORMAL

    # ---------- 入口 ----------

    def process_line(self, line_no, line):
        line = line.strip()
        if not line or line.startswith("#"):
            return
        parts = line.split()
        keyword = KEYWORDS.get(parts[0].upper()) or KEYWORDS.get(parts[0])
        if keyword is None:
            self.error(line_no, "无法识别的记录类型 '%s'" % parts[0])
            return
        fields = parts[1:]
        if keyword == "POINT":
            self.add_point(line_no, fields)
        elif keyword == "RANGER":
            self.add_ranger(line_no, fields)
        elif keyword == "PATROL":
            self.add_patrol(line_no, fields)
        elif keyword == "EVENT":
            self.add_event(line_no, fields)

    def finalize(self):
        """流结束后做全局检查：超期未巡护、未处置上报。"""
        reference = self.max_date or date.today()
        for name in sorted(self.points):
            point = self.points[name]
            if point.last_patrol_date is None:
                self.error(0, "巡护点 '%s'(区域 '%s') 从未被巡护" % (name, point.area))
            else:
                gap = (reference - point.last_patrol_date).days
                if gap > point.period:
                    self.error(0, "超期未巡护：巡护点 '%s' 最近巡护 %s，距基准日 %s 已 %d 天，"
                               "超过巡护周期 %d 天"
                               % (name, point.last_patrol_date.isoformat(),
                                  reference.isoformat(), gap, point.period))
            if point.pending_reports > 0:
                self.error(0, "上报未处置：巡护点 '%s' 有 %d 起上报事件至流结束仍未处置，"
                           "点位持续异常" % (name, point.pending_reports))

    # ---------- 输出 ----------

    def report(self, out=sys.stdout):
        out.write("===== 巡护点状态 =====\n")
        if not self.points:
            out.write("（无巡护点定义）\n")
        for name in sorted(self.points):
            p = self.points[name]
            last = p.last_patrol_date.isoformat() if p.last_patrol_date else "从未"
            extra = ""
            if p.pending_reports:
                extra = " [待处置上报 %d 起]" % p.pending_reports
            out.write("点位=%s 区域=%s 周期=%d天 状态=%s 最近巡护=%s 连续异常=%d次%s\n"
                      % (p.name, p.area, p.period, p.status, last,
                         p.consecutive_abnormal, extra))
        out.write("\n===== 错误/告警清单 =====\n")
        if not self.errors:
            out.write("（无错误）\n")
        for i, message in enumerate(self.errors, 1):
            out.write("%d. %s\n" % (i, message))
        out.write("\n汇总：巡护点 %d 个，巡护员 %d 名，错误/告警 %d 条\n"
                  % (len(self.points), len(self.rangers), len(self.errors)))


DEMO_INPUT = """\
# 巡护点定义：POINT 名称 区域 巡护周期(天)
POINT 东山哨卡 东区 5
POINT 湿地观测点 东区 5
POINT 北岭界桩 北区 10
POINT 孤岛样地 西区 3

# 巡护员定义：RANGER 名称 负责区域
RANGER 张三 东区
RANGER 李四 北区

# 巡护流：PATROL 巡护员 巡护点 日期 结果(正常|异常)
PATROL 张三 东山哨卡 2026-09-01 正常
PATROL 张三 东山哨卡 2026-09-01 正常
PATROL 李四 湿地观测点 2026-09-02 异常
PATROL 张三 湿地观测点 2026-09-04 异常
PATROL 张三 湿地观测点 2026-09-06 异常
PATROL 张三 北岭界桩 2026-09-03 正常
PATROL 王五 东山哨卡 2026-09-05 正常
PATROL 张三 幽灵点位 2026-09-05 正常

# 事件流：EVENT 巡护点 类型(盗猎|火灾|非法占用) 处置(处置|上报)
EVENT 湿地观测点 盗猎 处置
EVENT 北岭界桩 火灾 上报
PATROL 李四 北岭界桩 2026-09-08 正常
EVENT 北岭界桩 火灾 处置
EVENT 不存在的点 盗猎 上报
EVENT 东山哨卡 非法占用 上报
"""


def run(stream, out=sys.stdout):
    monitor = Monitor()
    for line_no, line in enumerate(stream, 1):
        monitor.process_line(line_no, line)
    monitor.finalize()
    monitor.report(out)
    return 0 if not monitor.errors else 1


def main(argv):
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__)
        return 2
    if argv[0] == "--demo":
        sys.stdout.write("----- 示例输入 -----\n%s\n----- 处理结果 -----\n" % DEMO_INPUT)
        return run(DEMO_INPUT.splitlines())
    if argv[0] == "-":
        return run(sys.stdin)
    with open(argv[0], encoding="utf-8") as handle:
        return run(handle)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
