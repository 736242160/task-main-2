#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
film_scheduler.py — 剧组拍摄计划重排与冲突检测工具（纯 Python 标准库，单文件）

用法：
    python3 film_scheduler.py 计划文件.txt     # 从文件读取
    python3 film_scheduler.py < 计划文件.txt   # 从标准输入读取
    python3 film_scheduler.py --demo           # 运行内置示例（覆盖全部规则）

输入格式（四个流，以段标题分隔；空行与 # 注释被忽略）：

    [场景]
    场景名 内景|外景 器材1,器材2,...      # 器材列表可省略

    [器材]
    器材名 可用|维修                      # 初始状态
    日期 器材名 可用|维修                 # 自该日起状态变更（维修完成即恢复可用）

    [拍摄]
    场景名 日期(YYYY-MM-DD) 时段

    [天气]
    日期(YYYY-MM-DD) 晴|雨

自定规则（顺延策略）及理由：
  1. 外景拍摄日遇雨，顺延到次日同一时段；若次日仍雨则逐日继续顺延（级联重排）。
     理由：外景依赖天气，逐日顺延改动最小；保持同时段可复用原定的器材与人员班次。
  2. 单场次最多顺延 7 天（MAX_POSTPONE_DAYS），超过则报告“顺延超过期限”，
     并强制落在第 7 天。理由：无限等待会拖垮整个档期，7 天是常见的档期容忍上限。
  3. 内景不受天气影响，不顺延。

状态延续约定（跨流状态延续）：
  - 器材状态按时间线生效：初始状态一直持续，直到某条带日期的状态变更将其覆盖；
    “维修”之后出现一条“可用”即表示维修完成，其后的场次自动恢复可用（级联恢复）。
  - 天气、已排定的场次在整个处理过程中持续参与后续判断。

输出：摄制状态表（每场次的计划/实际日期、时段、状态）+ 错误清单。
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

MAX_POSTPONE_DAYS = 7          # 顺延期限（自定，见模块 docstring）
ONE_DAY = timedelta(days=1)
SLOT_ORDER = {"凌晨": 0, "上午": 1, "下午": 2, "晚上": 3}
VALID_KINDS = ("内景", "外景")
VALID_EQUIP_STATUS = ("可用", "维修")
VALID_WEATHER = ("晴", "雨")


@dataclass
class Scene:
    name: str
    kind: str                  # 内景 / 外景
    equipment: list


@dataclass
class Shoot:
    scene: str
    day: date
    slot: str
    line: int
    final_day: date = None
    postponed: int = 0
    notes: list = field(default_factory=list)


def slot_key(slot):
    return (SLOT_ORDER.get(slot, 99), slot)


def parse_date(text, lineno, errors):
    try:
        return date.fromisoformat(text)
    except ValueError:
        errors.append("[格式错误] 第%d行：无法解析日期 %r（应为 YYYY-MM-DD）" % (lineno, text))
        return None


def parse_text(text):
    scenes, equip, shoots, weather, errors = {}, {}, [], {}, []
    section = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        parts = line.split()
        if section == "场景":
            if len(parts) < 2:
                errors.append("[格式错误] 第%d行：场景定义至少需要 名称 与 内景/外景" % lineno)
                continue
            name, kind = parts[0], parts[1]
            needs = [e for e in parts[2].replace("，", ",").split(",") if e] if len(parts) > 2 else []
            if kind not in VALID_KINDS:
                errors.append("[格式错误] 第%d行：场景类型 %r 应为 内景/外景" % (lineno, kind))
                continue
            if name in scenes:
                errors.append("[重复定义] 第%d行：场景 %r 被重复定义" % (lineno, name))
                continue
            scenes[name] = Scene(name, kind, needs)
        elif section == "器材":
            if len(parts) == 2:
                day, name, status = None, parts[0], parts[1]
            elif len(parts) == 3:
                day = parse_date(parts[0], lineno, errors)
                name, status = parts[1], parts[2]
                if day is None:
                    continue
            else:
                errors.append("[格式错误] 第%d行：器材定义应为 名称 状态 或 日期 名称 状态" % lineno)
                continue
            if status not in VALID_EQUIP_STATUS:
                errors.append("[格式错误] 第%d行：器材状态 %r 应为 可用/维修" % (lineno, status))
                continue
            equip.setdefault(name, []).append((day, status))
        elif section == "拍摄":
            if len(parts) != 3:
                errors.append("[格式错误] 第%d行：拍摄定义应为 场景名 日期 时段" % lineno)
                continue
            day = parse_date(parts[1], lineno, errors)
            if day is None:
                continue
            shoots.append(Shoot(parts[0], day, parts[2], lineno))
        elif section == "天气":
            if len(parts) != 2:
                errors.append("[格式错误] 第%d行：天气定义应为 日期 状况" % lineno)
                continue
            day = parse_date(parts[0], lineno, errors)
            if day is None:
                continue
            if parts[1] not in VALID_WEATHER:
                errors.append("[格式错误] 第%d行：天气状况 %r 应为 晴/雨" % (lineno, parts[1]))
                continue
            if day in weather:
                errors.append("[重复天气] 第%d行：日期 %s 的天气被重复定义" % (lineno, day))
            weather[day] = parts[1]
        else:
            errors.append("[格式错误] 第%d行：内容不在任何 [段落] 内：%r" % (lineno, line))
    return scenes, equip, shoots, weather, errors


def equipment_status_on(events, day):
    """按时间线解析器材在某日的状态（状态延续 / 维修完成后级联恢复）。"""
    status = None
    for event_day, event_status in sorted(events, key=lambda e: e[0] or date.min):
        if event_day is None or event_day <= day:
            status = event_status
        else:
            break
    return status


def schedule(scenes, equip, shoots, weather):
    errors = []
    consulted_weather_dates = set()
    seen_scene_day = {}
    placed = []
    ordered = sorted(shoots, key=lambda s: (s.day, slot_key(s.slot), s.line))

    for sh in ordered:
        scene = scenes.get(sh.scene)
        if scene is None:
            errors.append("[未知场景] 第%d行：拍摄引用了未定义的场景 %r" % (sh.line, sh.scene))
            sh.final_day = sh.day
            sh.notes.append("未知场景")
            continue

        key = (sh.scene, sh.day)
        if key in seen_scene_day:
            errors.append("[重复拍摄] 场景 %r 在 %s 被重复安排（第%d行与第%d行）"
                          % (sh.scene, sh.day, seen_scene_day[key], sh.line))
            sh.notes.append("重复拍摄")
        else:
            seen_scene_day[key] = sh.line

        # 外景遇雨逐日顺延（级联），最多 MAX_POSTPONE_DAYS 天
        final = sh.day
        if scene.kind == "外景":
            while weather.get(final) == "雨":
                consulted_weather_dates.add(final)
                if sh.postponed >= MAX_POSTPONE_DAYS:
                    errors.append("[顺延超限] 场景 %r 自 %s 起连续降雨，顺延超过 %d 天期限，强制落在 %s"
                                  % (sh.scene, sh.day, MAX_POSTPONE_DAYS, final))
                    sh.notes.append("顺延超限")
                    break
                final += ONE_DAY
                sh.postponed += 1
            consulted_weather_dates.add(final)
        sh.final_day = final
        if 0 < sh.postponed <= MAX_POSTPONE_DAYS and "顺延超限" not in sh.notes:
            errors.append("[外景顺延] 场景 %r 因 %s 降雨，顺延 %d 天至 %s（%s）"
                          % (sh.scene, sh.day, sh.postponed, final, sh.slot))
            sh.notes.append("顺延%d天" % sh.postponed)

        # 顺延后与已有场次的时段冲突（至少一方为重排场次才报告）
        for other in placed:
            if other.final_day == final and other.slot == sh.slot and (sh.postponed or other.postponed):
                moved, stayed = (sh, other) if sh.postponed else (other, sh)
                errors.append("[时段冲突] 重排场次 %r（顺延至 %s %s）与已有场次 %r 同时段重叠"
                              % (moved.scene, final, sh.slot, stayed.scene))
                sh.notes.append("时段冲突")

        # 器材需求：缺失或维修中
        for eq in scene.equipment:
            if eq not in equip:
                errors.append("[器材缺失] 场景 %r 需要未定义的器材 %r" % (sh.scene, eq))
                sh.notes.append("器材缺失:%s" % eq)
            else:
                status = equipment_status_on(equip[eq], final)
                if status == "维修":
                    errors.append("[器材维修] 场景 %r 所需器材 %r 在 %s 处于维修中"
                                  % (sh.scene, eq, final))
                    sh.notes.append("器材维修:%s" % eq)
                elif status is None:
                    errors.append("[器材状态未知] 器材 %r 在 %s 之前没有生效的状态记录" % (eq, final))
                    sh.notes.append("器材状态未知:%s" % eq)
        placed.append(sh)

    # 同一器材同时段被多场次占用
    usage = {}
    for sh in placed:
        scene = scenes.get(sh.scene)
        if scene is None:
            continue
        for eq in scene.equipment:
            usage.setdefault((eq, sh.final_day, sh.slot), []).append(sh.scene)
    for (eq, day, slot), names in sorted(usage.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])):
        if len(names) > 1:
            errors.append("[器材冲突] 器材 %r 在 %s %s 被多个场次同时使用：%s"
                          % (eq, day, slot, "、".join(names)))

    # 天气流引用未知日期（既未被任何拍摄落在该日，也未在顺延判断中被查阅）
    final_days = {sh.final_day for sh in placed}
    for day in sorted(weather):
        if day not in consulted_weather_dates and day not in final_days:
            errors.append("[未知日期] 天气流日期 %s 没有任何拍摄引用" % day)
    return errors


def display_width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text, width):
    return text + " " * max(0, width - display_width(text))


def print_report(shoots, errors):
    print("=" * 30, "摄制状态", "=" * 30)
    header = ("场景", "计划日期", "实际日期", "时段", "状态")
    rows = []
    for sh in sorted(shoots, key=lambda s: (s.final_day or s.day, slot_key(s.slot), s.line)):
        rows.append((sh.scene, str(sh.day), str(sh.final_day or sh.day), sh.slot,
                     "正常" if not sh.notes else "；".join(sh.notes)))
    widths = [max(display_width(str(x)) for x in col) for col in zip(header, *(rows or [header]))]
    print("  ".join(pad(h, w) for h, w in zip(header, widths)))
    for row in rows:
        print("  ".join(pad(str(c), w) for c, w in zip(row, widths)))
    print()
    print("=" * 30, "错误清单（%d 条）" % len(errors), "=" * 30)
    if errors:
        for i, err in enumerate(errors, 1):
            print("%2d. %s" % (i, err))
    else:
        print("（无）")


DEMO_INPUT = """\
# 内置示例：覆盖全部检测规则
[场景]
宫殿对话 内景 摄像机,灯光
庭院漫步 外景 摄像机
山洞决斗 外景 摄像机,灯光
雨夜追车 外景 摄像机,轨道车,反光板
天台对决 外景 无人机
沙漠日出 外景 摄像机

[器材]
摄像机 可用
灯光 维修
2026-10-03 灯光 可用
轨道车 维修
无人机 可用

[拍摄]
宫殿对话 2026-10-01 上午
庭院漫步 2026-10-01 上午
宫殿对话 2026-10-01 下午
山洞决斗 2026-10-02 上午
雨夜追车 2026-10-02 下午
天台对决 2026-10-04 上午
沙漠日出 2026-10-10 上午

[天气]
2026-10-01 晴
2026-10-02 雨
2026-10-03 雨
2026-10-04 晴
2026-10-10 雨
2026-10-11 雨
2026-10-12 雨
2026-10-13 雨
2026-10-14 雨
2026-10-15 雨
2026-10-16 雨
2026-10-17 雨
2026-10-18 雨
2026-11-11 晴
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description="剧组拍摄计划重排与冲突检测工具（纯标准库）")
    parser.add_argument("input", nargs="?", help="输入文件（缺省读标准输入）")
    parser.add_argument("--demo", action="store_true", help="运行内置示例")
    args = parser.parse_args(argv)

    if args.demo:
        text = DEMO_INPUT
    elif args.input:
        with open(args.input, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()

    scenes, equip, shoots, weather, errors = parse_text(text)
    errors += schedule(scenes, equip, shoots, weather)
    print_report(shoots, errors)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
