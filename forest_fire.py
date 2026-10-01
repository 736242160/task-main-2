#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forest_fire.py — 森林防火状态监控与错误报告工具（纯 Python 标准库，单文件）

用法：
    python3 forest_fire.py 输入文件        # 从文件读取
    python3 forest_fire.py < 输入文件      # 从标准输入读取
    python3 forest_fire.py --demo          # 运行内置示例

输入格式（行式文本；空行与 # 开头的注释行被忽略；字段以空白分隔）：
    ZONE     <区名> <火险等级> <网格> [<网格>...]     林区定义
    TOWER    <塔名> <网格> [<网格>...]                瞭望塔定义
    FIRE     <火情ID> <网格> <发现时刻>               火情流
    SUPPRESS <火情ID> <队伍名> <网格>                 扑救流
    SPREAD   <火情ID> <网格>                          蔓延流
网格写法：x,y（整数坐标，逗号两侧无空格），例如 3,5。

自定规则及理由：
 1. 蔓延邻接规则：四邻域（上/下/左/右共享边的网格）。地表火依赖连续可燃物
    沿共享边界蔓延，对角方向仅角点相接、可燃物不连续，故不认可对角蔓延。
    蔓延可级联：新蔓延成功的网格立即成为后续蔓延的出发网格。
 2. 队伍容量规则：一支队伍在同一火情中最多同时负责 2 个网格（人员与安全
    半径限制）。需求队伍数 = ceil(燃烧网格数 / 2)，实际投入不足即报缺口。
 3. 瞭望发现规则：瞭望塔覆盖网格上出现火情（含蔓延）却无任何 FIRE 发现
    记录时，报告“覆盖网格火情未发现”（每网格只报一次）。
 4. 跨区联防规则：火情蔓延至另一林区的网格即触发升级，所有涉及林区进入
    联动联防，状态与联动防区清单在输出中体现。

输出：防火状态（状态事件时间线 + 每起火情明细）与错误清单（含行号）。
"""
import sys
from collections import defaultdict

MAX_GRIDS_PER_TEAM = 2  # 每支队伍在同一火情中最多同时负责的网格数

DEMO = """\
# 林区定义：ZONE 区名 火险等级 网格...
ZONE 东区 高 0,0 0,1 1,0 1,1
ZONE 西区 中 0,2 0,3 1,2 1,3
# 瞭望塔定义：TOWER 塔名 网格...
TOWER 白塔 0,0 0,1
TOWER 北塔 0,2
# 火情流 / 蔓延流（四邻域级联）/ 扑救流
FIRE F1 0,0 08:00
SPREAD F1 0,1
SPREAD F1 1,1
SPREAD F1 1,0
SPREAD F1 0,2
SPREAD F1 0,3
SPREAD F1 1,2
SPREAD F1 1,3
SUPPRESS F1 甲队 0,0
SUPPRESS F1 甲队 0,1
SUPPRESS F1 乙队 1,1
SUPPRESS F1 乙队 0,0
SPREAD F1 3,3
SPREAD F2 0,3
SUPPRESS F9 丙队 0,3
"""


def parse_grid(token):
    """把 'x,y' 解析为 (x, y)；失败返回 None。"""
    parts = token.split(",")
    if len(parts) != 2:
        return None
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def neighbors(grid):
    x, y = grid
    return ((x, y - 1), (x, y + 1), (x - 1, y), (x + 1, y))


class Monitor:
    def __init__(self):
        self.zones = {}                       # 区名 -> {"risk": str, "grids": set}
        self.grid_zone = {}                   # 网格 -> 区名
        self.towers = {}                      # 塔名 -> set(网格)
        self.grid_towers = defaultdict(list)  # 网格 -> [塔名]
        self.fires = {}                       # 火情ID -> 火情记录
        self.discovered = set()               # 有 FIRE 发现记录的网格
        self.errors = []                      # (行号, 消息)
        self.timeline = []                    # 状态事件（发现/升级/联防）

    # ---------- 工具 ----------
    def error(self, lineno, msg):
        self.errors.append((lineno, msg))

    @staticmethod
    def fmt(grid):
        return "%d,%d" % grid

    # ---------- 定义类指令 ----------
    def cmd_zone(self, lineno, tok):
        if len(tok) < 4:
            self.error(lineno, "ZONE 参数不足：需要 区名 火险等级 网格...")
            return
        name, risk, grids = tok[1], tok[2], tok[3:]
        if name in self.zones:
            self.error(lineno, "林区重复定义：%s" % name)
            return
        gset = set()
        for t in grids:
            g = parse_grid(t)
            if g is None:
                self.error(lineno, "网格格式错误：%s（应为 x,y）" % t)
                continue
            if g in self.grid_zone:
                self.error(lineno, "网格 %s 同时划入林区 %s 与 %s"
                           % (t, self.grid_zone[g], name))
                continue
            self.grid_zone[g] = name
            gset.add(g)
        self.zones[name] = {"risk": risk, "grids": gset}

    def cmd_tower(self, lineno, tok):
        if len(tok) < 3:
            self.error(lineno, "TOWER 参数不足：需要 塔名 网格...")
            return
        name = tok[1]
        if name in self.towers:
            self.error(lineno, "瞭望塔重复定义：%s" % name)
            return
        gset = set()
        for t in tok[2:]:
            g = parse_grid(t)
            if g is None:
                self.error(lineno, "网格格式错误：%s（应为 x,y）" % t)
                continue
            if g not in self.grid_zone:
                self.error(lineno, "瞭望塔 %s 覆盖网格 %s 不属于任何林区"
                           % (name, t))
                continue
            gset.add(g)
            self.grid_towers[g].append(name)
        self.towers[name] = gset

    # ---------- 事件流指令（状态跨流延续） ----------
    def cmd_fire(self, lineno, tok):
        if len(tok) != 4:
            self.error(lineno, "FIRE 参数应为：火情ID 网格 发现时刻")
            return
        fid, gtoken, moment = tok[1], tok[2], tok[3]
        g = parse_grid(gtoken)
        if g is None:
            self.error(lineno, "网格格式错误：%s（应为 x,y）" % gtoken)
            return
        if fid in self.fires:
            self.error(lineno, "火情重复定义：%s" % fid)
            return
        if g not in self.grid_zone:
            self.error(lineno, "火情 %s 引用了不存在的网格 %s" % (fid, gtoken))
            return
        zone = self.grid_zone[g]
        self.fires[fid] = {
            "origin": g, "time": moment,
            "burning": {g}, "extinguished": set(),
            "spread_grids": set(),
            "teams": defaultdict(set),   # 队伍 -> 负责的网格
            "zones": {zone},
        }
        self.discovered.add(g)
        self.timeline.append("%s 火情 %s 于 林区[%s] 网格 %s 发现（火险等级 %s）"
                             % (moment, fid, zone, gtoken,
                                self.zones[zone]["risk"]))

    def cmd_spread(self, lineno, tok):
        if len(tok) != 3:
            self.error(lineno, "SPREAD 参数应为：火情ID 网格")
            return
        fid, gtoken = tok[1], tok[2]
        fire = self.fires.get(fid)
        if fire is None:
            self.error(lineno, "蔓延引用了不存在的火情：%s" % fid)
            return
        g = parse_grid(gtoken)
        if g is None:
            self.error(lineno, "网格格式错误：%s（应为 x,y）" % gtoken)
            return
        if g not in self.grid_zone:
            self.error(lineno, "火情 %s 蔓延到不存在的网格 %s" % (fid, gtoken))
            return
        if g in fire["extinguished"]:
            self.error(lineno, "火情 %s 蔓延到已扑灭网格 %s，拒绝"
                       % (fid, gtoken))
            return
        if g in fire["burning"]:
            self.error(lineno, "火情 %s 重复蔓延网格 %s" % (fid, gtoken))
            return
        # 四邻域级联：必须与任一正在燃烧的网格边相邻（含此前蔓延来的网格）
        if not any(n in fire["burning"] for n in neighbors(g)):
            self.error(lineno, "火情 %s 蔓延网格 %s 与燃烧区不相邻（四邻域），拒绝"
                       % (fid, gtoken))
            return
        fire["burning"].add(g)
        fire["spread_grids"].add(g)
        # 瞭望覆盖网格火情未发现（每网格只报一次）
        if g in self.grid_towers and g not in self.discovered:
            self.error(lineno, "瞭望塔 %s 覆盖网格 %s 火情未发现"
                       % ("、".join(self.grid_towers[g]), gtoken))
            self.discovered.add(g)
        # 跨区蔓延 -> 火情升级，联动联防
        zone = self.grid_zone[g]
        if zone not in fire["zones"]:
            fire["zones"].add(zone)
            linked = "、".join(sorted(fire["zones"]))
            self.timeline.append(
                "火情升级：%s 跨区蔓延至 林区[%s]，启动联防联动（联动防区：%s）"
                % (fid, zone, linked))

    def cmd_suppress(self, lineno, tok):
        if len(tok) != 4:
            self.error(lineno, "SUPPRESS 参数应为：火情ID 队伍 网格")
            return
        fid, team, gtoken = tok[1], tok[2], tok[3]
        fire = self.fires.get(fid)
        if fire is None:
            self.error(lineno, "扑救引用了不存在的火情：%s" % fid)
            return
        g = parse_grid(gtoken)
        if g is None:
            self.error(lineno, "网格格式错误：%s（应为 x,y）" % gtoken)
            return
        if g not in self.grid_zone:
            self.error(lineno, "扑救引用了不存在的网格 %s" % gtoken)
            return
        if g in fire["extinguished"]:
            self.error(lineno, "网格 %s 已扑灭，队伍 %s 重复扑救"
                       % (gtoken, team))
            return
        if g not in fire["burning"]:
            self.error(lineno, "队伍 %s 扑救的网格 %s 并未在燃烧（火情 %s）"
                       % (team, gtoken, fid))
            return
        if g in fire["teams"][team]:
            self.error(lineno, "队伍 %s 重复扑救网格 %s" % (team, gtoken))
            return
        if len(fire["teams"][team]) >= MAX_GRIDS_PER_TEAM:
            self.error(lineno, "队伍 %s 超负荷：同一火情最多负责 %d 个网格"
                       % (team, MAX_GRIDS_PER_TEAM))
            return
        fire["burning"].discard(g)
        fire["extinguished"].add(g)
        fire["teams"][team].add(g)

    # ---------- 驱动 ----------
    def run(self, text):
        handlers = {"ZONE": self.cmd_zone, "TOWER": self.cmd_tower,
                    "FIRE": self.cmd_fire, "SPREAD": self.cmd_spread,
                    "SUPPRESS": self.cmd_suppress}
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            tok = line.split()
            handler = handlers.get(tok[0].upper())
            if handler is None:
                self.error(lineno, "无法识别的指令：%s" % tok[0])
                continue
            handler(lineno, tok)
        self.finalize()

    def finalize(self):
        """流结束后做全局核查：队伍缺口、蔓延网格未派扑救。"""
        for fid, fire in self.fires.items():
            burning = len(fire["burning"])
            if burning:
                need = (burning + MAX_GRIDS_PER_TEAM - 1) // MAX_GRIDS_PER_TEAM
                have = len(fire["teams"])
                if have < need:
                    self.error(0, "火情 %s 扑救队伍不足：燃烧网格 %d 个，"
                               "需 %d 支队伍，实到 %d 支，缺口 %d 支"
                               % (fid, burning, need, have, need - have))
            suppressed = set().union(*fire["teams"].values()) if fire["teams"] else set()
            unattended = sorted(fire["spread_grids"] - suppressed)
            if unattended:
                self.error(0, "火情 %s 蔓延网格未派扑救：%s"
                           % (fid, " ".join(self.fmt(g) for g in unattended)))

    # ---------- 输出 ----------
    def report(self):
        out = ["===== 防火状态 ====="]
        out.append("林区 %d 个，瞭望塔 %d 座，火情 %d 起"
                   % (len(self.zones), len(self.towers), len(self.fires)))
        if self.timeline:
            out.append("[状态事件]")
            out += ["  " + e for e in self.timeline]
        if self.fires:
            out.append("[火情明细]")
            for fid, f in self.fires.items():
                if not f["burning"]:
                    status = "已扑灭"
                elif len(f["zones"]) > 1:
                    status = "跨区联防·燃烧中"
                else:
                    status = "燃烧中"
                teams = "、".join(sorted(f["teams"])) or "无"
                zones = "、".join(sorted(f["zones"]))
                burning = " ".join(self.fmt(g) for g in sorted(f["burning"])) or "无"
                out.append("  %s 状态=%s 涉及防区=%s 队伍=%s"
                           % (fid, status, zones, teams))
                out.append("    燃烧网格(%d)=%s 已扑灭(%d)"
                           % (len(f["burning"]), burning, len(f["extinguished"])))
        out.append("===== 错误清单 =====")
        if not self.errors:
            out.append("  无")
        else:
            for i, (lineno, msg) in enumerate(self.errors, 1):
                where = "第%d行" % lineno if lineno else "全局核查"
                out.append("  %d. [%s] %s" % (i, where, msg))
        return "\n".join(out)


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        print("----- 内置示例输入 -----")
        print(DEMO)
        text = DEMO
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    mon = Monitor()
    mon.run(text)
    print(mon.report())
    return 1 if mon.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
