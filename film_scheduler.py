#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""影视拍摄计划重排工具（纯标准库，单文件）。

用法:
    python3 film_scheduler.py            # 运行内置示例（可直接验证全部规则）
    python3 film_scheduler.py plan.json  # 从 JSON 文件读取输入

输入 JSON 格式（四个流）:
{
  "scenes":    [{"name": "茶馆夜话", "type": "内|外", "equipment": ["摄像机A"]}],
  "equipment": [{"name": "摄像机A", "status": "可用|维修"}],   // 流式：后出现的状态覆盖先前状态
  "shoots":    [{"scene": "茶馆夜话", "date": "2026-10-01", "slot": "上午|下午|晚上"}],
  "weather":   [{"date": "2026-10-02", "condition": "晴|雨"}]
}

顺延规则（自定，理由附后）:
  1. 外景拍摄日遇雨 -> 顺延至下一个非雨天的同一时段。
     理由：保持时段不变可最大限度保留灯光/演员/通告安排，改动最小。
  2. 顺延目标 (日期, 时段) 已被其他场次占用 -> 报告"时段冲突"，并继续顺延至再下一个
     非雨天同时段，级联直到找到空位。理由：后到的顺延场次不应挤掉已确定的场次。
  3. 顺延超过 7 天（MAX_POSTPONE_DAYS）-> 报告"超期未排"。
     理由：剧组档期/合同通常以周为单位，无限顺延没有可执行性。
  4. 无天气数据的日期默认按晴天处理。
"""
import json
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

SLOT_ORDER = ["上午", "下午", "晚上"]
MAX_POSTPONE_DAYS = 7
SCENE_TYPES = {"内", "外"}
EQUIP_STATUS = {"可用", "维修"}
WEATHER_KINDS = {"晴", "雨"}


@dataclass
class Scene:
    name: str
    stype: str
    equipment: list


@dataclass
class Shoot:
    scene_name: str
    day: date
    slot: str
    seq: int
    status: str = "已排定"
    notes: list = field(default_factory=list)


class Scheduler:
    def __init__(self):
        self.scenes = {}
        self.equipment = {}      # 器材名 -> 最终状态（流式覆盖，跨流延续）
        self.weather = {}        # 日期 -> 状况（流式覆盖，跨流延续）
        self.shoots = []
        self.messages = []       # (级别, 内容)

    def err(self, msg):
        self.messages.append(("错误", msg))

    def warn(self, msg):
        self.messages.append(("警告", msg))

    def info(self, msg):
        self.messages.append(("信息", msg))

    # ---------- 输入流加载 ----------

    def load_scenes(self, items):
        for it in items:
            name = it.get("name", "")
            stype = it.get("type", "")
            if not name:
                self.err("场景定义缺少名称，已忽略")
                continue
            if stype not in SCENE_TYPES:
                self.err(f"场景《{name}》类型非法（应为 内/外），已忽略")
                continue
            if name in self.scenes:
                self.warn(f"场景《{name}》重复定义，以后者为准")
            eq = it.get("equipment", [])
            self.scenes[name] = Scene(name, stype, list(eq))

    def load_equipment(self, items):
        for it in items:
            name = it.get("name", "")
            status = it.get("status", "")
            if not name:
                self.err("器材定义缺少名称，已忽略")
                continue
            if status not in EQUIP_STATUS:
                self.err(f"器材「{name}」状态非法（应为 可用/维修），已忽略")
                continue
            old = self.equipment.get(name)
            self.equipment[name] = status
            if old == "维修" and status == "可用":
                self.info(f"器材「{name}」维修完成，状态已级联恢复为可用")
            elif old and old != status:
                self.info(f"器材「{name}」状态由{old}变更为{status}")

    def load_weather(self, items):
        for it in items:
            raw = it.get("date", "")
            cond = it.get("condition", "")
            day = self._parse_date(raw, "天气流")
            if day is None:
                continue
            if cond not in WEATHER_KINDS:
                self.err(f"天气流 {raw} 状况非法（应为 晴/雨），已忽略")
                continue
            self.weather[day] = cond

    def load_shoots(self, items):
        seen = set()
        for seq, it in enumerate(items):
            name = it.get("scene", "")
            raw = it.get("date", "")
            slot = it.get("slot", "")
            day = self._parse_date(raw, "拍摄流")
            if day is None:
                continue
            if name not in self.scenes:
                self.err(f"拍摄流引用了未知场景《{name}》（{raw} {slot}），已忽略")
                continue
            if slot not in SLOT_ORDER:
                self.err(f"拍摄《{name}》时段非法（应为 {'/'.join(SLOT_ORDER)}），已忽略")
                continue
            if (name, day) in seen:
                self.err(f"重复拍摄：场次《{name}》在 {day.isoformat()} 已安排过，忽略本次（{slot}）")
                continue
            seen.add((name, day))
            self.shoots.append(Shoot(name, day, slot, seq))

    def _parse_date(self, raw, where):
        try:
            return date.fromisoformat(raw)
        except (ValueError, TypeError):
            self.err(f"{where}日期格式非法：{raw!r}（应为 YYYY-MM-DD），已忽略")
            return None

    # ---------- 校验与重排 ----------

    def check_weather_dates(self):
        if not self.shoots:
            return
        days = {sh.day for sh in self.shoots}
        lo, hi = min(days), max(days)
        hi += timedelta(days=MAX_POSTPONE_DAYS)  # 顺延窗口内的天气同样有效
        for d in sorted(self.weather):
            if d not in days and not (lo <= d <= hi):
                self.warn(f"天气流引用了拍摄计划之外的未知日期 {d.isoformat()}")

    def is_rain(self, day):
        return self.weather.get(day) == "雨"

    def reschedule(self):
        """两遍法：先安置不受雨影响的场次，再级联重排遇雨外景。"""
        occupied = {}
        rained = []
        for sh in self.shoots:
            sc = self.scenes[sh.scene_name]
            if sc.stype == "外" and self.is_rain(sh.day):
                rained.append(sh)
            else:
                occupied.setdefault((sh.day, sh.slot), []).append(sh)

        for sh in rained:
            orig = sh.day
            cur = sh.day
            while True:
                cur += timedelta(days=1)
                gap = (cur - orig).days
                if gap > MAX_POSTPONE_DAYS:
                    sh.status = "超期未排"
                    self.err(
                        f"外景《{sh.scene_name}》原定 {orig.isoformat()} {sh.slot}，"
                        f"连续阴雨/时段占用，顺延超过 {MAX_POSTPONE_DAYS} 天期限，标记为超期未排"
                    )
                    break
                if self.is_rain(cur):
                    continue
                occupants = occupied.get((cur, sh.slot), [])
                if occupants:
                    names = "、".join(f"《{o.scene_name}》" for o in occupants)
                    self.warn(
                        f"时段冲突：顺延场次《{sh.scene_name}》与 {cur.isoformat()} "
                        f"{sh.slot} 已有场次 {names} 重叠，继续顺延"
                    )
                    continue
                occupied.setdefault((cur, sh.slot), []).append(sh)
                sh.day = cur
                sh.notes.append(f"因雨自 {orig.isoformat()} 顺延 {gap} 天")
                self.warn(
                    f"外景《{sh.scene_name}》原定 {orig.isoformat()} {sh.slot} 遇雨，"
                    f"顺延至 {cur.isoformat()} {sh.slot}"
                )
                break
        self.occupied = occupied

    def check_equipment(self):
        for (day, slot), shoots in sorted(self.occupied.items()):
            for sh in shoots:
                sc = self.scenes[sh.scene_name]
                for eq in sc.equipment:
                    if eq not in self.equipment:
                        self.err(f"器材缺失：《{sh.scene_name}》需要「{eq}」，器材库中不存在")
                    elif self.equipment[eq] == "维修":
                        self.err(f"器材维修中：《{sh.scene_name}》需要「{eq}」，当前不可用")
            for i in range(len(shoots)):
                for j in range(i + 1, len(shoots)):
                    a = self.scenes[shoots[i].scene_name]
                    b = self.scenes[shoots[j].scene_name]
                    shared = sorted(set(a.equipment) & set(b.equipment))
                    if shared:
                        self.err(
                            f"器材冲突：{day.isoformat()} {slot}，《{a.name}》与《{b.name}》"
                            f"同时使用「{'、'.join(shared)}」"
                        )

    # ---------- 输出 ----------

    def report(self):
        print("=" * 64)
        print("摄制状态")
        print("=" * 64)
        print(f"{'日期':<12}{'时段':<5}{'场次':<10}{'类型':<4}{'器材':<18}状态/备注")
        print("-" * 64)
        placed = [sh for sh in self.shoots if sh.status != "超期未排"]
        placed.sort(key=lambda s: (s.day, SLOT_ORDER.index(s.slot), s.seq))
        for sh in placed:
            sc = self.scenes[sh.scene_name]
            eq = "、".join(sc.equipment) if sc.equipment else "-"
            note = "；".join([sh.status] + sh.notes)
            print(f"{sh.day.isoformat():<12}{sh.slot:<5}{sc.name:<10}{sc.stype + '景':<4}{eq:<18}{note}")
        unplaced = [sh for sh in self.shoots if sh.status == "超期未排"]
        for sh in unplaced:
            print(f"{'--':<12}{sh.slot:<5}{sh.scene_name:<10}{'':<4}{'':<18}{sh.status}")

        print("-" * 64)
        print("器材最终状态（跨流延续结果）")
        for name in sorted(self.equipment):
            print(f"  {name}: {self.equipment[name]}")

        print("=" * 64)
        print(f"错误与警告清单（共 {len(self.messages)} 条）")
        print("=" * 64)
        order = {"错误": 0, "警告": 1, "信息": 2}
        for level, msg in sorted(self.messages, key=lambda m: order[m[0]]):
            print(f"[{level}] {msg}")


def run(data):
    sch = Scheduler()
    sch.load_scenes(data.get("scenes", []))
    sch.load_equipment(data.get("equipment", []))
    sch.load_weather(data.get("weather", []))
    sch.load_shoots(data.get("shoots", []))
    sch.check_weather_dates()
    sch.reschedule()
    sch.check_equipment()
    sch.report()


DEMO = {
    "scenes": [
        {"name": "茶馆夜话", "type": "内", "equipment": ["摄像机A", "灯光B"]},
        {"name": "厨房对峙", "type": "内", "equipment": ["摄像机A"]},
        {"name": "屋顶追逐", "type": "外", "equipment": ["摄像机A"]},
        {"name": "街口追车", "type": "外", "equipment": ["轨道车C"]},
        {"name": "雨巷相逢", "type": "内", "equipment": ["麦克风D"]},
        {"name": "书房独白", "type": "内", "equipment": ["灯光B"]},
        {"name": "天台决战", "type": "外", "equipment": ["摄像机A"]},
        {"name": "码头黎明", "type": "内", "equipment": ["摄像机A"]},
    ],
    "equipment": [
        {"name": "摄像机A", "status": "维修"},
        {"name": "灯光B", "status": "维修"},
        {"name": "麦克风D", "status": "可用"},
        {"name": "摄像机A", "status": "可用"},
    ],
    "shoots": [
        {"scene": "茶馆夜话", "date": "2026-10-01", "slot": "上午"},
        {"scene": "厨房对峙", "date": "2026-10-01", "slot": "上午"},
        {"scene": "茶馆夜话", "date": "2026-10-01", "slot": "下午"},
        {"scene": "屋顶追逐", "date": "2026-10-02", "slot": "上午"},
        {"scene": "街口追车", "date": "2026-10-02", "slot": "下午"},
        {"scene": "雨巷相逢", "date": "2026-10-03", "slot": "上午"},
        {"scene": "书房独白", "date": "2026-10-04", "slot": "上午"},
        {"scene": "天台决战", "date": "2026-10-06", "slot": "上午"},
        {"scene": "码头黎明", "date": "2026-10-14", "slot": "上午"},
    ],
    "weather": [
        {"date": "2026-10-02", "condition": "雨"},
        {"date": "2026-10-03", "condition": "雨"},
        {"date": "2026-10-06", "condition": "雨"},
        {"date": "2026-10-07", "condition": "雨"},
        {"date": "2026-10-08", "condition": "雨"},
        {"date": "2026-10-09", "condition": "雨"},
        {"date": "2026-10-10", "condition": "雨"},
        {"date": "2026-10-11", "condition": "雨"},
        {"date": "2026-10-12", "condition": "雨"},
        {"date": "2026-10-13", "condition": "雨"},
        {"date": "2026-11-01", "condition": "晴"},
    ],
}


def main(argv):
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            run(json.load(f))
    else:
        print("（未提供输入文件，运行内置示例；用法: python3 film_scheduler.py plan.json）\n")
        run(DEMO)


if __name__ == "__main__":
    main(sys.argv)
