#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""森林防火状态模拟与错误报告工具（纯 Python 标准库，单文件）。

用法:
    python3 forest_fire.py 输入文件          # 从文件读取
    python3 forest_fire.py < 输入文件        # 从标准输入读取
    python3 forest_fire.py --demo            # 运行内置示例

输入格式（行式文本，# 开头为注释，空行忽略；事件按出现顺序处理，状态跨流延续）:
    ZONE     <区名> <网格1,网格2,...> <low|medium|high>   林区定义
    TOWER    <塔名> <网格1,网格2,...>                     瞭望塔定义
    FIRE     <火情ID> <网格> <发现时刻HH:MM>              火情流
    SPREAD   <火情ID> <网格>                              蔓延流
    SUPPRESS <火情ID> <队伍> <网格>                       扑救流

自定规则及理由:
  * 网格命名: 列字母+行数字（如 A1、B12、AA3）。
  * 蔓延邻接: 四邻域（上下左右，冯·诺依曼邻域），不含对角。理由: 无风向、
    坡度数据时，四邻域是网格林火蔓延的标准保守近似，避免对角蔓延高估速度。
  * 蔓延级联: 新蔓延网格并入该火情的燃烧集合，后续蔓延可以其为新源点，
    因此蔓延必须落在该火情当前任一在烧网格的邻格上，否则报错。
  * 队伍需求: 每个在烧网格需 1 支队伍，高火险(high)林区在烧网格需 2 支；
    按火情统计已投入队伍数，不足部分记为缺口。
  * 瞭望发现: 瞭望塔覆盖网格内由蔓延产生的火情若没有对应 FIRE 发现记录，
    视为"覆盖范围内火情未发现"予以报告；起火点不在任何塔覆盖内记盲区提示。
  * 火情升级: 蔓延跨入另一林区即升级为跨区火情，相关林区自动结为联防区
    （双向联动），并在状态与报告中体现。

退出码: 存在 ERROR 级报告时为 1，否则为 0。
"""

import re
import sys
from collections import defaultdict

GRID_RE = re.compile(r"^([A-Za-z]+)(\d+)$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
RISKS = ("low", "medium", "high")
TEAMS_PER_GRID = {"low": 1, "medium": 1, "high": 2}

DEMO_INPUT = """\
# 林区定义: 名称 网格列表 火险等级
ZONE 东山 A1,A2,A3,B1,B2,B3 high
ZONE 西坡 C1,C2,C3,D1,D2,D3 medium
# 瞭望塔: 名称 覆盖网格
TOWER 瞭望一 A1,A2,B1,B2
TOWER 瞭望二 C1,C2,D1
# 火情流: 火情ID 网格 发现时刻
FIRE F1 A1 08:00
# 蔓延流: 火情ID 网格（级联: A1->A2->B2->C2 跨区升级）
SPREAD F1 A2
SPREAD F1 B2
SPREAD F1 C2
# 扑救流: 火情ID 队伍 网格
SUPPRESS F1 一队 A1
SUPPRESS F1 二队 A2
SUPPRESS F1 一队 A1
SUPPRESS F9 三队 B1
SPREAD F1 D9
"""


def parse_grid(name):
    """网格名 -> (列, 行)；非法返回 None。列按 26 进制字母展开。"""
    m = GRID_RE.match(name)
    if not m:
        return None
    col = 0
    for ch in m.group(1).upper():
        col = col * 26 + (ord(ch) - ord("A") + 1)
    return (col, int(m.group(2)))


def grid_key(name):
    p = parse_grid(name)
    return p if p else (10 ** 9, 10 ** 9)


def is_adjacent(a, b):
    pa, pb = parse_grid(a), parse_grid(b)
    if pa is None or pb is None:
        return False
    return abs(pa[0] - pb[0]) + abs(pa[1] - pb[1]) == 1


class State:
    def __init__(self):
        self.zones = {}                      # 区名 -> {"grids": set, "risk": str}
        self.grid_zone = {}                  # 网格 -> 区名
        self.towers = {}                     # 塔名 -> set(网格)
        self.tower_of = defaultdict(list)    # 网格 -> [塔名]
        self.fires = {}                      # 火情ID -> dict（保持插入序）
        self.fire_grids = set()              # 有 FIRE 发现记录的网格
        self.burning = set()                 # 全局在烧网格
        self.extinguished = set()            # 全局已扑灭网格
        self.links = defaultdict(set)        # 区名 -> 联防区集合（双向）
        self.errors = []                     # (level, code, message)

    def report(self, level, code, msg, lineno=None):
        where = "行%d: " % lineno if lineno else ""
        self.errors.append((level, code, where + msg))


def on_zone(st, lineno, name, grids_csv, risk):
    if name in st.zones:
        st.report("ERROR", "DUP_ZONE", "林区 %s 重复定义" % name, lineno)
        return
    if risk not in RISKS:
        st.report("ERROR", "BAD_RISK", "林区 %s 火险等级 %r 非法（应为 low/medium/high）" % (name, risk), lineno)
        return
    grids = set()
    for g in grids_csv.split(","):
        g = g.strip()
        if parse_grid(g) is None:
            st.report("ERROR", "BAD_GRID", "林区 %s 的网格名 %r 非法" % (name, g), lineno)
            continue
        if g in st.grid_zone:
            st.report("ERROR", "DUP_GRID", "网格 %s 同时划入林区 %s 与 %s" % (g, st.grid_zone[g], name), lineno)
            continue
        grids.add(g)
        st.grid_zone[g] = name
    st.zones[name] = {"grids": grids, "risk": risk}


def on_tower(st, lineno, name, grids_csv):
    if name in st.towers:
        st.report("ERROR", "DUP_TOWER", "瞭望塔 %s 重复定义" % name, lineno)
        return
    grids = set()
    for g in grids_csv.split(","):
        g = g.strip()
        if parse_grid(g) is None:
            st.report("ERROR", "BAD_GRID", "瞭望塔 %s 的网格名 %r 非法" % (name, g), lineno)
            continue
        if g not in st.grid_zone:
            st.report("WARN", "TOWER_GRID_OUTSIDE", "瞭望塔 %s 覆盖的网格 %s 不属于任何林区" % (name, g), lineno)
        grids.add(g)
        st.tower_of[g].append(name)
    st.towers[name] = grids


def on_fire(st, lineno, fid, grid, moment):
    if fid in st.fires:
        st.report("ERROR", "DUP_FIRE", "火情 %s 重复定义" % fid, lineno)
        return
    if not TIME_RE.match(moment):
        st.report("WARN", "BAD_TIME", "火情 %s 发现时刻 %r 不是 HH:MM 格式" % (fid, moment), lineno)
    zone = st.grid_zone.get(grid)
    if zone is None:
        st.report("ERROR", "UNKNOWN_GRID", "火情 %s 的起火网格 %s 不属于任何林区" % (fid, grid), lineno)
    st.fires[fid] = {
        "origin": grid, "time": moment,
        "burning": {grid}, "extinguished": set(),
        "teams": set(), "zones": {zone} if zone else set(),
        "spread_grids": set(),
    }
    st.fire_grids.add(grid)
    st.burning.add(grid)
    if zone is not None and not st.tower_of.get(grid):
        st.report("WARN", "BLIND_SPOT", "火情 %s 起火网格 %s 不在任何瞭望塔覆盖内（瞭望盲区）" % (fid, grid), lineno)


def on_spread(st, lineno, fid, grid):
    fire = st.fires.get(fid)
    if fire is None:
        st.report("ERROR", "UNKNOWN_FIRE", "蔓延引用了不存在的火情 %s" % fid, lineno)
        return
    if grid not in st.grid_zone:
        st.report("ERROR", "UNKNOWN_GRID", "火情 %s 蔓延目标网格 %s 不属于任何林区" % (fid, grid), lineno)
        return
    if grid in fire["extinguished"]:
        st.report("ERROR", "SPREAD_TO_EXTINGUISHED", "火情 %s 向已扑灭网格 %s 蔓延" % (fid, grid), lineno)
        return
    if grid in fire["burning"]:
        st.report("ERROR", "DUP_SPREAD", "火情 %s 重复蔓延到在烧网格 %s" % (fid, grid), lineno)
        return
    if not any(is_adjacent(grid, b) for b in fire["burning"]):
        st.report("ERROR", "NON_ADJACENT", "火情 %s 蔓延到 %s 与其任一在烧网格均不相邻（四邻域）" % (fid, grid), lineno)
        return
    fire["burning"].add(grid)
    fire["spread_grids"].add(grid)
    st.burning.add(grid)
    zone = st.grid_zone[grid]
    if zone not in fire["zones"]:
        old = sorted(z for z in fire["zones"] if z)
        fire["zones"].add(zone)
        for z in old:
            st.links[z].add(zone)
            st.links[zone].add(z)
        st.report("WARN", "ESCALATION",
                  "火情 %s 蔓延至林区 %s，升级为跨区火情；%s 与 %s 启动联防联动"
                  % (fid, zone, "、".join(old) if old else "（未知区）", zone), lineno)


def on_suppress(st, lineno, fid, team, grid):
    fire = st.fires.get(fid)
    if fire is None:
        st.report("ERROR", "UNKNOWN_FIRE", "扑救引用了不存在的火情 %s（队伍 %s）" % (fid, team), lineno)
        return
    if grid not in st.grid_zone:
        st.report("ERROR", "UNKNOWN_GRID", "扑救目标网格 %s 不属于任何林区" % grid, lineno)
        return
    if grid in fire["extinguished"]:
        st.report("ERROR", "REPEAT_SUPPRESS", "网格 %s 已扑灭，队伍 %s 重复扑救" % (grid, team), lineno)
        return
    if grid not in fire["burning"]:
        st.report("ERROR", "NOT_BURNING", "队伍 %s 扑救的网格 %s 并未在火情 %s 中燃烧" % (team, grid, fid), lineno)
        return
    fire["burning"].discard(grid)
    fire["extinguished"].add(grid)
    fire["teams"].add(team)
    st.burning.discard(grid)
    st.extinguished.add(grid)


def finalize(st):
    """全部事件处理完后的收尾核查（状态跨流延续后的最终一致性检查）。"""
    for fid, fire in st.fires.items():
        # 蔓延网格未派扑救
        for g in sorted(fire["spread_grids"] & fire["burning"], key=grid_key):
            st.report("ERROR", "UNSUPPRESSED_SPREAD",
                      "火情 %s 蔓延网格 %s 直至结束未派扑救" % (fid, g))
        # 扑救队伍缺口: 在烧网格 low/medium 各需 1 队，high 需 2 队
        required = 0
        for g in fire["burning"]:
            zone = st.grid_zone.get(g)
            risk = st.zones[zone]["risk"] if zone in st.zones else "medium"
            required += TEAMS_PER_GRID[risk]
        have = len(fire["teams"])
        if required > have:
            st.report("ERROR", "TEAM_SHORTAGE",
                      "火情 %s 在烧网格需队伍 %d 支，已投入 %d 支，缺口 %d 支"
                      % (fid, required, have, required - have))
        # 瞭望覆盖网格火情未发现
        for g in sorted(fire["spread_grids"], key=grid_key):
            if st.tower_of.get(g) and g not in st.fire_grids:
                st.report("WARN", "TOWER_MISS",
                          "网格 %s 在瞭望塔 %s 覆盖内，火情蔓延至此却无 FIRE 发现记录"
                          % (g, "、".join(st.tower_of[g])))


def process_line(st, lineno, line):
    line = line.strip()
    if not line or line.startswith("#"):
        return
    parts = line.split()
    kw, args = parts[0].upper(), parts[1:]
    if kw == "ZONE" and len(args) == 3:
        on_zone(st, lineno, args[0], args[1], args[2].lower())
    elif kw == "TOWER" and len(args) == 2:
        on_tower(st, lineno, args[0], args[1])
    elif kw == "FIRE" and len(args) == 3:
        on_fire(st, lineno, args[0], args[1], args[2])
    elif kw == "SPREAD" and len(args) == 2:
        on_spread(st, lineno, args[0], args[1])
    elif kw == "SUPPRESS" and len(args) == 3:
        on_suppress(st, lineno, args[0], args[1], args[2])
    else:
        st.report("ERROR", "BAD_LINE", "无法解析的行: %s" % line, lineno)


def render(st):
    out = ["=" * 56, "防火状态", "=" * 56, "[林区]"]
    for name in sorted(st.zones):
        z = st.zones[name]
        burning = sorted((g for g in z["grids"] if g in st.burning), key=grid_key)
        ext = sorted((g for g in z["grids"] if g in st.extinguished), key=grid_key)
        links = sorted(st.links.get(name, ()))
        out.append("  %s 火险=%s 网格数=%d 在烧=%d 已扑灭=%d 联防区=%s"
                   % (name, z["risk"], len(z["grids"]), len(burning), len(ext),
                      "、".join(links) if links else "无"))
        if burning:
            out.append("    在烧网格: " + ", ".join(burning))
        if ext:
            out.append("    已扑灭网格: " + ", ".join(ext))
    out.append("[瞭望塔]")
    for name in sorted(st.towers):
        out.append("  %s 覆盖网格 %d 个: %s"
                   % (name, len(st.towers[name]),
                      ", ".join(sorted(st.towers[name], key=grid_key))))
    out.append("[火情]")
    for fid, f in st.fires.items():
        status = "已扑灭" if not f["burning"] else "燃烧中"
        zones = sorted(z for z in f["zones"] if z)
        out.append("  %s 状态=%s 起火=%s@%s 涉及林区=%s 投入队伍=%s"
                   % (fid, status, f["origin"], f["time"],
                      "、".join(zones) if zones else "未知",
                      "、".join(sorted(f["teams"])) if f["teams"] else "无"))
        if f["burning"]:
            out.append("    在烧: " + ", ".join(sorted(f["burning"], key=grid_key)))
        if f["extinguished"]:
            out.append("    已扑灭: " + ", ".join(sorted(f["extinguished"], key=grid_key)))
    out += ["=" * 56, "错误报告（%d 条）" % len(st.errors), "=" * 56]
    if not st.errors:
        out.append("  无")
    for i, (level, code, msg) in enumerate(st.errors, 1):
        out.append("  %2d. [%s] %s %s" % (i, level, code, msg))
    return "\n".join(out)


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("-")]
    if "--demo" in argv:
        print("---- 示例输入 ----")
        print(DEMO_INPUT)
        lines = DEMO_INPUT.splitlines()
    elif args:
        with open(args[0], encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    st = State()
    for lineno, line in enumerate(lines, 1):
        process_line(st, lineno, line)
    finalize(st)
    print(render(st))
    return 1 if any(level == "ERROR" for level, _, _ in st.errors) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
