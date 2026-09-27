"""把「叠层 + 目标阻抗」变成「线宽 / 线距」的高层接口。"""

from dataclasses import dataclass, field
from collections.abc import Sequence
from typing import Any, Optional

from . import analytic, calibration
from .api import CalcResult, JlcApi
from .stackup import Stackup
from .structures import Structure, pick

MM_PER_MIL = 0.0254


@dataclass
class Solution:
    impedance_type: str
    layer: str
    target: float | None = None
    kind: str = "single"
    width: float | None = None          # W1，mil
    spacing: float | None = None        # S1，mil（差分）
    gap: float | None = None            # D1，mil（共面）
    impedance: float | None = None      # 实际阻抗，Ω
    engine: str = ""                       # online / analytic
    params: dict[str, float] = field(default_factory=dict)
    status: int = -1
    error: str = ""
    note: str = ""

    # -- 便于阅读的输出 --
    @property
    def width_mm(self) -> float | None:
        return None if self.width is None else self.width * MM_PER_MIL

    @property
    def spacing_mm(self) -> float | None:
        return None if self.spacing is None else self.spacing * MM_PER_MIL

    def as_dict(self) -> dict[str, Any]:
        return {
            "impedance_type": self.impedance_type,
            "layer": self.layer,
            "kind": self.kind,
            "target_ohm": self.target,
            "width_mil": None if self.width is None else round(self.width, 4),
            "width_mm": None if self.width_mm is None else round(self.width_mm, 4),
            "spacing_mil": None if self.spacing is None else round(self.spacing, 4),
            "spacing_mm": None if self.spacing_mm is None else round(self.spacing_mm, 4),
            "gap_mil": None if self.gap is None else round(self.gap, 4),
            "impedance_ohm": None if self.impedance is None else round(self.impedance, 4),
            "engine": self.engine,
            "note": self.note,
        }

    def __str__(self) -> str:
        parts = ["%s  %s" % (self.layer, self.impedance_type)]
        if self.width is not None:
            parts.append("线宽 W1 = %.3f mil (%.4f mm)" % (self.width, self.width_mm))
        if self.spacing is not None:
            parts.append("线距 S1 = %.3f mil (%.4f mm)" % (self.spacing, self.spacing_mm))
        if self.gap is not None:
            parts.append("线铜距 D1 = %.3f mil" % self.gap)
        if self.impedance is not None and self.impedance == self.impedance:
            if self.target is None:
                parts.append("阻抗 = %.3f Ω" % self.impedance)
            else:
                parts.append("实际阻抗 = %.3f Ω (目标 %.3f)" % (self.impedance, self.target))
        parts.append("[%s]" % self.engine)
        if self.note:
            parts.append("(%s)" % self.note)
        if self.error:
            parts.append("错误: %s" % self.error)
        return "  ".join(parts)


class ImpedanceCalculator:
    """在某个叠层上做阻抗正算 / 反算。

    :param stackup: :class:`~impedance_calculator.stackup.Stackup`
    :param api: 传 :class:`~impedance_calculator.api.JlcApi` 则使用**在线**引擎（与官网一致）
    :param copper_config / coverlay_config: 嘉立创的线宽增量 / 阻焊厚度配置表，
        不传则用官方文档默认值（外层 1oz：T1=1.6mil，C1/C2/C3=1.2/0.6/1.2，CEr=3.8）
    """

    def __init__(self, stackup: Stackup, api: JlcApi | None = None,
                 copper_config: Sequence[dict[str, Any]] | None = None,
                 coverlay_config: Sequence[dict[str, Any]] | None = None,
                 cer: float = 3.8):
        self.stackup = stackup
        self.api = api
        self.copper_config = copper_config
        self.coverlay_config = coverlay_config
        self.cer = cer

    # ------------------------------------------------------------------ #
    @property
    def engine(self) -> str:
        return "online" if self.api is not None else "analytic"

    def _geometry(self, layer: str) -> dict[str, float]:
        return self.stackup.si9000_geometry(layer)

    def _build_params(self, layer: str, st: Structure, width: float,
                      spacing: float | None = None,
                      gap: float | None = None) -> dict[str, float]:
        i = self.stackup.index_of(layer)
        g = dict(self._geometry(layer))
        delta = self.stackup.w2_delta(i, self.copper_config)
        p: dict[str, float] = {
            "H1": g["H1"], "Er1": g["Er1"],
            "W1": float(width), "W2": float(width) - delta,
            "T1": g["T1"],
        }
        if "H2" in g:
            p["H2"] = g["H2"]
            p["Er2"] = g["Er2"]
        if "S1" in st.params:
            p["S1"] = float(spacing if spacing is not None else 8.0)
        if "D1" in st.params:
            p["D1"] = float(gap if gap is not None else 10.0)
        if "C1" in st.params:
            c1, c2, c3 = self.stackup.coverlay(i, self.coverlay_config) or (1.2, 0.6, 1.2)
            p["C1"], p["C2"] = c1, c2
            if "C3" in st.params:
                p["C3"] = c3
            p["CEr"] = self.cer
        return p

    # ------------------------------------------------------------------ #
    def forward(self, layer: str, width: float = 8.0, kind: str = "single",
                coplanar: bool = False, spacing: float | None = None,
                gap: float | None = None, coated: bool = True) -> Solution:
        """正算：给定线宽，算阻抗。"""
        st = pick(kind, "outer" if self.stackup.is_outer(self.stackup.index_of(layer)) else "inner",
                  coplanar=coplanar, coated=coated)
        params = self._build_params(layer, st, width, spacing, gap)
        if self.api is not None:
            res = self.api.calculate(st.impedance_type, params)
            return Solution(st.impedance_type, layer, None, kind,
                            width=params["W1"], spacing=params.get("S1"),
                            gap=params.get("D1"), impedance=res.impedance,
                            engine="online", params=params,
                            status=res.status, error=res.error)
        z, _ = analytic.estimate(st.impedance_type, params)
        return Solution(st.impedance_type, layer, None, kind, width=params["W1"],
                        spacing=params.get("S1"), gap=params.get("D1"),
                        impedance=z, engine="analytic", params=params,
                        note=calibration.quality_note(st.impedance_type))

    def solve(self, layer: str, target: float, kind: str = "single",
              coplanar: bool = False, spacing: float | None = None,
              gap: float | None = None, coated: bool = True,
              width: float | None = None,
              bounds: tuple[float, float] = (1.0, 200.0)) -> Solution:
        """反算线宽；差分时 ``spacing`` 必须给定（也可给 ``width`` 反算线距）。

        返回的 :class:`Solution` 中 ``impedance`` 是后台回代（或公式回代）的实际值。
        """
        outer = self.stackup.is_outer(self.stackup.index_of(layer))
        st = pick(kind, "outer" if outer else "inner", coplanar=coplanar, coated=coated)

        # --- 已知线宽，反算线距 ---
        if kind == "diff" and width is not None and spacing is None:
            params = self._build_params(layer, st, width, 8.0, gap)
            if self.api is not None:
                res = self.api.solve(st.impedance_type, "S1", params, target, 2.5, 100.0)
                s = res.solved
                return Solution(st.impedance_type, layer, target, kind,
                                width=s.get("W1"), spacing=s.get("S1"), gap=s.get("D1"),
                                impedance=res.impedance, engine="online",
                                params=params, status=res.status, error=res.error)
            sp = analytic.solve_spacing(st.impedance_type, params, target)
            note = calibration.quality_note(st.impedance_type)
            if sp is None:
                return Solution(st.impedance_type, layer, target, kind,
                                width=params["W1"], engine="analytic", params=params,
                                note=note, error="离线公式在给定范围内无解，请用在线模式")
            params2 = dict(params, S1=sp, W2=params["W1"] - self.stackup.w2_delta(
                self.stackup.index_of(layer), self.copper_config))
            z, _ = analytic.estimate(st.impedance_type, params2)
            return Solution(st.impedance_type, layer, target, kind, width=params["W1"],
                            spacing=sp, impedance=z, engine="analytic", params=params2,
                            note=note)

        # --- 反算线宽 ---
        base_width = float(width if width is not None else 8.0)
        params = self._build_params(layer, st, base_width, spacing, gap)
        if self.api is not None:
            res = self.api.solve(st.impedance_type, "W2", params, target, *bounds)
            s = res.solved
            return Solution(st.impedance_type, layer, target, kind,
                            width=s.get("W1"), spacing=s.get("S1", params.get("S1")),
                            gap=s.get("D1", params.get("D1")),
                            impedance=res.impedance, engine="online",
                            params=params, status=res.status, error=res.error)
        delta = self.stackup.w2_delta(self.stackup.index_of(layer), self.copper_config)
        w = analytic.solve_width(st.impedance_type, params, target, delta, *bounds)
        note = calibration.quality_note(st.impedance_type)
        if w is None:
            return Solution(st.impedance_type, layer, target, kind,
                            engine="analytic", params=params, note=note,
                            error="离线公式在给定范围内无解，请用在线模式")
        p2 = dict(params, W1=w, W2=w - delta)
        z, _ = analytic.estimate(st.impedance_type, p2)
        return Solution(st.impedance_type, layer, target, kind, width=w,
                        spacing=p2.get("S1"), gap=p2.get("D1"), impedance=z,
                        engine="analytic", params=p2, note=note)
