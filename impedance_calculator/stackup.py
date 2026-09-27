"""把嘉立创叠层（叠构）翻译成 SI9000 需要的 H / Er / T / C 参数。

嘉立创后台返回的叠构是一条从顶层到底层的材料列表 ``basicDataList``::

    materialType = 1  铜箔        （一层导体）
    materialType = 2  芯板        （上铜 + 介质 + 下铜）
    materialType = 0  PP / 半固化片（纯介质）
    materialType = 3  光板
    materialType = 4  AD 胶

把它展平成 ``导体 + 介质`` 交替的序列之后：

* 外层走线（L1 / Ln）只有一个参考面 —— ``H1`` = 相邻介质厚度，``Er1`` = 该介质 Dk；
* 内层走线有两个参考面 —— ``H1`` = 上侧介质，``H2`` = 下侧介质。

这里的取值与官网完全一致（例如 JLC04161H-3313 的 L1：H1 = 0.0994 mm = 3.9134 mil，Er1 = 4.1）。
"""

from dataclasses import dataclass, field
from collections.abc import Sequence
from typing import Any, Optional

MM_PER_MIL = 0.0254

#: 官方文档给的默认铜厚换算（mil），后台配置表取不到时用它
DEFAULT_T1 = {"outer": {1.0: 1.6}, "inner": {0.5: 0.6, 1.0: 1.2}}
#: 官方文档给的默认阻焊厚度（mil）
DEFAULT_COVERLAY = {"outer": (1.2, 0.6, 1.2), "inner": (None, None, None)}
DEFAULT_CER = 3.8


def mil(mm: float) -> float:
    return mm / MM_PER_MIL


def _cu_oz(thickness_mm: float) -> float:
    """把铜厚（mm）就近折算成 oz。"""
    if thickness_mm <= 0:
        return 0.0
    oz = thickness_mm / 0.0343
    for cand in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        if abs(oz - cand) < 0.3:
            return cand
    return round(oz * 2) / 2


@dataclass
class Conductor:
    name: str
    thickness_mm: float

    @property
    def oz(self) -> float:
        return _cu_oz(self.thickness_mm)


@dataclass
class Dielectric:
    thickness_mm: float
    dk: float
    material: str = ""
    #: 若这层介质属于某个「芯板」，记录它上下两面导体的下标 (上, 下)
    owner: tuple | None = None


@dataclass
class Stackup:
    """一个可计算的叠层。"""

    code: str
    name: str
    conductors: list[Conductor]
    dielectrics: list[Dielectric]          # len == len(conductors) - 1
    outer_oz: float = 1.0
    inner_oz: float = 0.5

    # ------------------------------------------------------------------ #
    @property
    def copper_layers(self) -> int:
        return len(self.conductors)

    @property
    def total_dielectric_mm(self) -> float:
        return sum(d.thickness_mm for d in self.dielectrics)

    def layer_names(self) -> list[str]:
        n = self.copper_layers
        if n == 1:
            return ["L1"]
        if n == 2:
            return ["L1", "L2"]
        return ["L1"] + ["L%d" % i for i in range(2, n)] + ["L%d" % n]

    def index_of(self, layer: str) -> int:
        """把 ``L1`` / ``1`` / ``F.Cu`` / ``top`` 之类解析成 0 基下标。"""
        s = str(layer).strip().upper().replace(".CU", "")
        n = self.copper_layers
        alias = {"F": 0, "TOP": 0, "L1": 0,
                 "B": n - 1, "BOT": n - 1, "BOTTOM": n - 1, "L%d" % n: n - 1}
        if s in alias:
            return alias[s]
        if s.startswith("IN") and s[2:].isdigit():
            # KiCad 命名：In1.Cu 是第 1 个内层 → 0 基下标 1
            return int(s[2:])
        if s.startswith("L") and s[1:].isdigit():
            return int(s[1:]) - 1
        if s.isdigit():
            return int(s) - 1
        raise ValueError("无法识别的层: %r" % layer)

    def is_outer(self, index: int) -> bool:
        return index in (0, self.copper_layers - 1)

    # ------------------------------------------------------------------ #
    def si9000_geometry(self, layer: str) -> dict[str, float]:
        """返回该层走线的 ``H1/Er1[/H2/Er2]/T1``（与官网的计算逻辑一致）。

        外层：``H1`` = 到相邻参考层的介质。
        内层：官网（``computedBaseMaterialThickness``）会把
        **该层所属芯板的那层介质**放在 ``H1``，另一侧介质 **加上该层铜厚** 放在 ``H2``。
        """
        i = self.index_of(layer)
        if not 0 <= i < self.copper_layers:
            raise ValueError("层号越界: %s" % layer)
        out: dict[str, float] = {}
        if self.is_outer(i):
            d = self.dielectrics[i if i == 0 else i - 1]
            out["H1"] = mil(d.thickness_mm)
            out["Er1"] = d.dk
            out["T1"] = self.t1_for(index=i)
            return out

        up, down = self.dielectrics[i - 1], self.dielectrics[i]
        cu = mil(self.conductors[i].thickness_mm)          # 该层铜厚（mil）
        if down.owner and down.owner[0] == i:              # 该层是这块芯板的上铜
            h1, h2 = down, up
        elif up.owner and up.owner[1] == i:                # 该层是这块芯板的下铜
            h1, h2 = up, down
        else:                                              # 两侧都是 PP：按上下半区判断
            upper = (i + 1) <= self.copper_layers / 2.0
            h1, h2 = (down, up) if upper else (up, down)
        out["H1"] = mil(h1.thickness_mm)
        out["Er1"] = h1.dk
        out["H2"] = mil(h2.thickness_mm) + cu
        out["Er2"] = h2.dk
        out["T1"] = self.t1_for(index=i)
        return out

    def t1_for(self, index: int, copper_config: Sequence[dict[str, Any]] | None = None) -> float:
        """铜厚 T1（mil）。优先用嘉立创的配置表，取不到就用官方文档默认值。"""
        outer = self.is_outer(index)
        oz = self.outer_oz if outer else self.inner_oz
        if copper_config:
            rows = [r for r in copper_config
                    if abs(r["systemCopperThickness"] - oz) < 0.01
                    and r["layerType"] == (1 if outer else 2)]
            if rows:
                row = next((r for r in rows if abs(r["baseCopperThickness"] - 0.5) < 0.01), rows[0])
                return float(row["traceCopperThickness"])
        table = DEFAULT_T1["outer" if outer else "inner"]
        if oz in table:
            return table[oz]
        return 1.6 if outer else (1.2 if oz >= 1.18 else 0.6)

    def w2_delta(self, index: int, copper_config: Sequence[dict[str, Any]] | None = None) -> float:
        """W1 - W2（蚀刻线宽增量，mil）。"""
        outer = self.is_outer(index)
        oz = self.outer_oz if outer else self.inner_oz
        if copper_config:
            rows = [r for r in copper_config
                    if abs(r["systemCopperThickness"] - oz) < 0.01
                    and r["layerType"] == (1 if outer else 2)]
            if rows:
                row = next((r for r in rows if abs(r["baseCopperThickness"] - 0.5) < 0.01), rows[0])
                return float(row["traceWidthDelta"])
        return 0.7 if outer else 0.5

    def coverlay(self, index: int, coverlay_config: Sequence[dict[str, Any]] | None = None):
        """(C1, C2, C3)（mil）；内层返回 ``None``。"""
        if not self.is_outer(index):
            return None
        oz = self.outer_oz
        if coverlay_config:
            rows = [r for r in coverlay_config
                    if abs(r["systemCopperThickness"] - oz) < 0.01 and r["layerType"] == 1]
            if rows:
                row = next((r for r in rows if abs(r["baseCopperThickness"] - 0.5) < 0.01), rows[0])
                return (float(row["coatingAboveSubstrate"]),
                        float(row["coatingAboveTrace"]),
                        float(row["coatingBetweenTraces"]))
        return DEFAULT_COVERLAY["outer"]

    # ------------------------------------------------------------------ #
    @classmethod
    def from_template(cls, tpl: dict[str, Any]) -> "Stackup":
        """由嘉立创 ``selectPageImpedanceDefaultTemplate`` 的单条记录构造叠层。"""
        conductors: list[Conductor] = []
        dielectrics: list[Dielectric] = []

        def add_conductor(name: str, thick_mm: float) -> None:
            conductors.append(Conductor(name, float(thick_mm or 0.0)))

        def add_dielectric(thick_mm: float, dk: float, material: str) -> None:
            dielectrics.append(Dielectric(float(thick_mm or 0.0), float(dk or 0.0), material))

        for item in tpl.get("basicDataList") or []:
            mtype = item.get("materialType")
            name = item.get("material") or ""
            dk = item.get("dielectricConstant") or 0.0
            thick = item.get("dielectricThick") or 0.0
            if mtype == 1:                                    # 铜箔
                add_conductor(item.get("materialName") or "铜箔",
                              item.get("topConductorThick") or 0.0)
            elif mtype == 2:                                  # 芯板：上铜 + 介质 + 下铜
                add_conductor(item.get("materialName") or "芯板", item.get("topConductorThick") or 0.0)
                add_dielectric(thick, dk, name)
                dielectrics[-1].owner = (len(conductors) - 1, len(conductors))
                add_conductor(item.get("materialName") or "芯板", item.get("botConductorThick") or 0.0)
            elif mtype in (0, 4, 3):                          # PP / AD胶 / 光板
                add_dielectric(thick, dk, name)

        if len(conductors) - 1 != len(dielectrics):
            # 连续多张 PP（中间没有铜）在 SI9000 里就是**一层介质**：
            # 厚度相加、介电常数取算术平均（与官网 getThicknessBetween /
            # getAverageDielectricConstant 的做法一致）
            merged: list[Dielectric] = []
            for d in dielectrics:
                if merged and merged[-1].owner is None and d.owner is None:
                    prev = merged[-1]
                    t = prev.thickness_mm + d.thickness_mm
                    if t > 0:
                        dk = (prev.dk + d.dk) / 2.0
                    else:
                        dk = prev.dk
                    mat = '+'.join(x for x in (prev.material, d.material) if x)
                    merged[-1] = Dielectric(t, dk, mat)
                else:
                    merged.append(d)
            dielectrics = merged
        if len(conductors) - 1 != len(dielectrics):
            raise ValueError("叠层解析失败：导体 %d 个，介质 %d 层"
                             % (len(conductors), len(dielectrics)))

        outer_oz = conductors[0].oz or 1.0
        inner_oz = conductors[1].oz if len(conductors) > 2 else 0.5
        return cls(code=str(tpl.get("laminatedConstructionCode") or ""),
                   name=tpl.get("receptionDisplayName") or tpl.get("appointName") or "未命名",
                   conductors=conductors, dielectrics=dielectrics,
                   outer_oz=outer_oz, inner_oz=inner_oz or 0.5)

    # ------------------------------------------------------------------ #
    def describe(self) -> str:
        rows = []
        rows.append("%s  (%s)" % (self.name, self.code))
        order = ["L%d" % (i + 1) for i in range(self.copper_layers)]
        for i, c in enumerate(self.conductors):
            rows.append("  %-4s 铜厚 %-6s %.4f mm" % (order[i], "%goz" % c.oz, c.thickness_mm))
            if i < len(self.dielectrics):
                d = self.dielectrics[i]
                rows.append("       介质   %-10s %.4f mm  Dk=%.2f" % (d.material, d.thickness_mm, d.dk))
        return "\n".join(rows)

    @property
    def nominal_mm(self) -> float:
        """标称板厚 = 铜箔 + 介质（不含阻焊）。"""
        return (sum(c.thickness_mm for c in self.conductors)
                + sum(d.thickness_mm for d in self.dielectrics))


# --------------------------------------------------------------------------- #
#  内置叠层（离线可用，数值与官网 2026 年数据一致）
# --------------------------------------------------------------------------- #
def _builtin(code, name, diel, outer=1.0, inner=0.5, cu=(0.035, 0.0152), cores=None) -> Stackup:
    """``diel`` 是 (厚度mm, Dk) 列表，导体数 = len(diel) + 1；``cores`` 是芯板介质的下标。"""
    n = len(diel) + 1
    conductors = [Conductor("F.Cu" if i == 0 else ("B.Cu" if i == n - 1 else "In%d.Cu" % i),
                            cu[0] if i in (0, n - 1) else cu[1]) for i in range(n)]
    dielectrics = [Dielectric(t, dk, "") for t, dk in diel]
    if cores is None:
        cores = tuple(range(len(diel))) if len(diel) == 1 else ((1,) if len(diel) == 3 else ())
    for i in cores:
        dielectrics[i].owner = (i, i + 1)
    return Stackup(code, name, conductors, dielectrics, outer, inner)


BUILTIN_STACKUPS: dict[str, Stackup] = {
    "JLC04161H-3313": _builtin(
        "20211110053321", "JLC04161H-3313 (通用/成品板厚1.56mm±10%)",
        [(0.0994, 4.10), (1.2650, 4.42), (0.0994, 4.10)]),
    "JLC04161H-7628": _builtin(
        "20211110053231", "JLC04161H-7628 (通用/成品板厚1.59mm±10%)",
        [(0.2104, 4.40), (1.0650, 4.38), (0.2104, 4.40)]),
    "JLC04161H-2116": _builtin(
        "202407040505581973", "JLC04161H-2116 (1.60mm)",
        [(0.1164, 4.16), (1.2650, 4.42), (0.1164, 4.16)]),
    "JLC0216A": _builtin(
        "20220913081703606", "JLC0216A (2层 成品1.6mm)",
        [(1.4300, 4.42)], cu=(0.030, 0.030)),
}


def match_name(name: str, key: str) -> bool:
    """叠层名匹配。

    ``key`` 不以 ``(`` 结尾时要求**边界匹配**，否则 ``JLC04161H-3313`` 会错误命中
    ``JLC04161H-3313A(特殊...)``。``key`` 以 ``(`` 结尾（带了官方的标注前缀）时
    直接用前缀匹配。
    """
    name = (name or "").upper()
    key = (key or "").upper()
    if not name.startswith(key):
        return False
    if "(" in key:
        return True                 # key 已经进入括号注释，前缀匹配即可
    rest = name[len(key):]
    return rest == "" or rest.startswith("(")


def find(query: str, api=None, layers: int = 4, thickness: float = 1.6,
         outer_cu: float = 1.0, inner_cu: float = 0.5) -> Stackup:
    """先查内置叠层，再（``api`` 非空时）在线查官网叠层。"""
    key = query.strip()
    for name, st in BUILTIN_STACKUPS.items():
        if key.upper() == name.upper() or key.upper() in st.name.upper():
            return st
    if api is None:
        raise LookupError("内置叠层里没有 %r（可选：%s）；也可传 api 在线查询"
                          % (query, ", ".join(BUILTIN_STACKUPS)))
    for tpl in api.templates(layers, thickness, outer_cu, inner_cu):
        nm = tpl.get("receptionDisplayName") or tpl.get("appointName") or ""
        if match_name(nm, key):
            return Stackup.from_template(tpl)
    raise LookupError("官网叠层里没有 %r（%d 层 / %.2fmm）" % (query, layers, thickness))
