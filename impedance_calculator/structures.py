"""嘉立创（JLC）阻抗计算器 —— 结构与参数定义。

本模块中的 ``impedance_type`` 就是嘉立创后台送给 Polar SI9000 的模型名，
含义可以在 :class:`Structure` 的注释里看到。

结构名对照（Polar SI9000 命名法）::

    Surface Microstrip 1B                     表层微带线（无阻焊）
    Coated  Microstrip 1B                     表层微带线（带阻焊/绿油）
    Offset  Stripline   1B1A                  偏置带状线（内层，上下参考面距离不等）
    Surface Coplanar Waveguide w/ Lower Gnd   表层共面波导（下地参考，无阻焊）
    Coated  Coplanar Waveguide w/ Lower Gnd   表层共面波导（下地参考，带阻焊）
    Offset  Coplanar Waveguide 1B1A           内层共面波导
    Diff Edge-Coupled ...                     以上结构的两线差分版本

参数名含义（单位 mil，1 mil = 0.0254 mm）::

    H1 / H2      基材厚度（不含铜）
    Er1 / Er2    基材介电常数
    W1           线底宽度（贴着基材那一面）
    W2           线顶宽度（阻焊开窗那一面），W2 = W1 - 蚀刻线宽增量
    S1           差分线间距（线边到线边）
    D1           线与同层地铜的间距（共面结构）
    T1           铜厚
    C1/C2/C3     基材上 / 走线上 / 走线间的阻焊厚度
    CEr          阻焊的介电常数
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

# 所有可能的参数（按 SI9000 的顺序）
ALL_PARAMS: Tuple[str, ...] = (
    "H1", "Er1", "H2", "Er2", "W1", "W2", "S1", "D1", "T1",
    "C1", "C2", "C3", "CEr",
)


@dataclass(frozen=True)
class Structure:
    """一个 SI9000 截面模型。"""

    impedance_type: str          # 嘉立创后台使用的模型名
    name_cn: str                 # 界面上的名字
    kind: str                    # "single" | "diff"
    layer: str                   # "outer" | "inner"
    coplanar: bool = False       # 是否共面（有 D1）
    coated: bool = False         # 是否有阻焊层（C1/C2/C3/CEr）

    @property
    def params(self) -> Tuple[str, ...]:
        """该模型实际参与计算的参数（顺序固定，便于发送给后台）。"""
        p = ["H1", "Er1"]
        if self.layer == "inner":
            p += ["H2", "Er2"]
        p += ["W1", "W2"]
        if self.kind == "diff":
            p += ["S1"]
        if self.coplanar:
            p += ["D1"]
        p += ["T1"]
        if self.coated:
            p += ["C1", "C2"]
            if self.kind == "diff":
                p += ["C3"]
            p += ["CEr"]
        return tuple(p)

    @property
    def can_goal_seek(self) -> Tuple[str, ...]:
        """后台支持反算（给定阻抗求几何）的参数。"""
        out = ["W2"]
        if self.kind == "diff":
            out += ["S1"]
        if self.coplanar:
            out += ["D1"]
        return tuple(out)


def _s(impedance_type, name_cn, kind, layer, coplanar=False, coated=False):
    return Structure(impedance_type, name_cn, kind, layer, coplanar, coated)


#: 嘉立创阻抗计算器里全部 12 个模型（顺序与官网一致）
STRUCTURES: Tuple[Structure, ...] = (
    _s("SurfaceMicrostrip1B", "单端阻抗（不带防焊）", "single", "outer"),
    _s("CoatedMicrostrip1B", "单端阻抗（外层）", "single", "outer", coated=True),
    _s("OffsetStripline1B1A", "单端阻抗（内层）", "single", "inner"),
    _s("SurfaceCoplanarWaveguideWithLowerGnd1B", "共面单端（不带防焊）", "single", "outer", coplanar=True),
    _s("CoatedCoplanarWaveguideWithLowerGnd1B", "共面单端（外层）", "single", "outer", coplanar=True, coated=True),
    _s("OffsetCoplanarWaveguide1B1A", "共面单端阻抗（内层）", "single", "inner", coplanar=True),
    _s("DiffEdgeCoupledSurfaceMicrostrip1B", "差分阻抗（不带防焊）", "diff", "outer"),
    _s("DiffEdgeCoupledCoatedMicrostrip1B", "差分阻抗（外层）", "diff", "outer", coated=True),
    _s("DiffOffsetStripline1B1A", "差分阻抗（内层）", "diff", "inner"),
    _s("DiffSurfaceCoplanarWaveguideWithLowerGnd1B", "共面差分阻抗（不带防焊）", "diff", "outer", coplanar=True),
    _s("DiffCoatedCoplanarWaveguideWithLowerGnd1B", "共面差分阻抗（外层）", "diff", "outer", coplanar=True, coated=True),
    _s("DiffOffsetCoplanarWaveguide1B1A", "共面差分阻抗（内层）", "diff", "inner", coplanar=True),
)

BY_TYPE: Dict[str, Structure] = {s.impedance_type: s for s in STRUCTURES}


def pick(kind: str, layer: str, coplanar: bool = False, coated: bool = True) -> Structure:
    """按用途挑选模型。

    :param kind: ``single`` 或 ``diff``
    :param layer: ``outer`` 或 ``inner``
    :param coplanar: 是否需要共面模型（带同层地铜）
    :param coated: 是否有阻焊层；表层走线一般 True
    """
    if kind not in ("single", "diff"):
        raise ValueError("kind 只能是 single / diff")
    if layer not in ("outer", "inner"):
        raise ValueError("layer 只能是 outer / inner")
    if layer == "inner":
        coated = False  # 内层没有阻焊
    for s in STRUCTURES:
        if s.kind == kind and s.layer == layer and s.coplanar == coplanar and s.coated == coated:
            return s
    raise LookupError("找不到匹配的模型: %s/%s/coplanar=%s/coated=%s" % (kind, layer, coplanar, coated))
