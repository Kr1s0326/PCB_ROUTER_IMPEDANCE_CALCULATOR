"""纯 Python 的**离线**阻抗模型（不需要联网）。

分三层：

1. **物理基底**（本模块）：教科书公式
   Hammerstad-Jensen 微带线 / 等效高度带状线 / Ghione-Naldi 共面波导
   + 差分耦合系数。铜厚趋 0 时与 SI9000 只差 0.3%~0.6%。
2. **校准层**（:mod:`.calibration`）：``Z = Z_base · exp(Σ βᵢ φᵢ)``，
   系数由官网采样数据拟合，放在 ``_coefs.py``。
3. **反算**：二分法解 ``Z(W) = 目标``。

单位统一为 **mil**（1 mil = 0.0254 mm），铜厚 T1 也是 mil。

教材原式（不套校准时就是它）::

    微带线（IPC-2141 近似）
        Z0 = 87 / sqrt(Er + 1.41) * ln( 5.98 H / (0.8 W + T) )

    带状线（IPC-2141 近似）
        Z0 = 60 / sqrt(Er) * ln( 4 H / (0.67 pi (0.8 W + T)) )

本模块用的是精度更高的 Hammerstad-Jensen 版本（把 W/H 分段处理），
并带铜厚等效宽度修正。

.. warning::

   嘉立创后台用的是 Polar SI9000 一类的**边界元场求解器**，本模块是解析近似。
   套上校准层后：微带线约 ±0.6%~1.5%，带状线约 ±7%，共面约 ±3%~12%
   （每个结构的实测误差见 ``_coefs.py`` 的 ``width_holdout``）。
   要逐位一致请用 :class:`impedance_calculator.api.JlcApi`（在线模式）。
"""

import math
from collections.abc import Callable

from . import calibration, mom

# 铜厚修正的“折算系数”：SI9000 是真场解，教材公式需要一个经验因子才接近
THICKNESS_BLEND = 0.5
#: 偏置带状线的等效高度系数，见 :func:`stripline`（由采样数据定）
STRIPLINE_C = 2.44
#: 共面波导里「背地微带分量」占多少（1.0 = 全算，0 = 不算），由采样数据定
COPLANAR_BACK = 0.5


# --------------------------------------------------------------------------- #
#  工具
# --------------------------------------------------------------------------- #
def _k_of_ratio(r: float) -> float:
    r = min(max(r, 1e-9), 1 - 1e-9)
    return r


def elliptic_k(k: float) -> float:
    """第一类完全椭圆积分 K(k)，用 AGM（算术-几何平均）算。"""
    k = _k_of_ratio(abs(k))
    a, b = 1.0, math.sqrt(max(0.0, 1.0 - k * k))
    for _ in range(40):
        a, b = (a + b) / 2.0, math.sqrt(a * b)
        if abs(a - b) < 1e-15:
            break
    return math.pi / (2.0 * a)


def _microstrip_eeff(u: float, er: float, er_top: float = 1.0) -> float:
    if u <= 1.0:
        q = 1.0 / math.sqrt(1.0 + 12.0 / u) + 0.04 * (1.0 - u) ** 2
    else:
        q = 1.0 / math.sqrt(1.0 + 12.0 / u)
    return (er + er_top) / 2.0 + (er - er_top) / 2.0 * q


def _microstrip_z0_ratio(u: float, eeff: float) -> float:
    if u <= 1.0:
        return 60.0 / math.sqrt(eeff) * math.log(8.0 / u + u / 4.0)
    return (120.0 * math.pi) / (
        math.sqrt(eeff) * (u + 1.393 + 0.667 * math.log(u + 1.444)))


def _thickness_adjusted(W: float, T: float) -> float:
    if T is None or T <= 0 or W <= 0:
        return W
    return W + THICKNESS_BLEND * (T / math.pi) * (1.0 + math.log(4.0 * math.pi * W / T))


# --------------------------------------------------------------------------- #
#  单端模型
# --------------------------------------------------------------------------- #
def microstrip(H: float, Er: float, W1: float, W2: float, T: float,
               coated: tuple[float, float, float, float] | None = None) -> tuple[float, float]:
    """表层微带线。``coated=(C1, C2, C3, CEr)`` 时把阻焊也算进去。

    返回 ``(Z0, Er_eff)``。
    """
    W = _thickness_adjusted((W1 + W2) / 2.0, T)
    er_top = 1.0
    if coated:
        C1, C2, C3, cer = coated
        # 用“空气 + 阻焊”的并联等效介电常数近似阻焊层的影响
        d = (C2 or 0.0) + H
        er_top = ((cer * (C2 or 0.0)) + H) / d if d > 0 else 1.0
    eeff = _microstrip_eeff(W / H, Er, er_top)
    return _microstrip_z0_ratio(W / H, eeff), eeff


def _stripline_height(H1: float, Er1: float, H2: float,
                      Er2: float) -> tuple[float, float]:
    """偏置带状线的「等效微带高度」与「等效介电常数」。"""
    b = H1 + H2
    h_min = min(H1, H2)
    h_eff = h_min * b / (b + STRIPLINE_C * h_min)
    er_eff = (Er1 / H1 + Er2 / H2) / (1.0 / H1 + 1.0 / H2)
    return h_eff, er_eff


def stripline(H1: float, Er1: float, H2: float | None, Er2: float | None,
              W1: float, W2: float, T: float, **kw) -> tuple[float, float]:
    """带状线（内层）：**用 MoM 精确解**（T→0）+ 铜厚等效加宽。

    偏置带状线是标准二维场问题，``impedance_calculator.mom`` 里那套方法矩能算到
    **0.02%~0.14%**（与 SI9000 的 T=0.01 数据比），比原来「等效高度微带」
    硬凑（误差 13.5%）好两个数量级。
    """
    if H2 is None or Er2 is None:
        H2, Er2 = H1, Er1
    we = _thickness_adjusted((W1 + W2) / 2.0, T)
    return mom.z0_single(we, we, H1, Er1, H2, Er2, **kw)


def stripline_diff(H1: float, Er1: float, H2: float | None, Er2: float | None,
                   W1: float, W2: float, S1: float, T: float, **kw) -> tuple[float, float]:
    """差分带状线：**奇模 MoM 精确解**（T→0）+ 铜厚等效加宽。

    差分对在对称面是电壁（V1 = -V2），所以镜像电荷反号；
    ``Zdiff = 2·Z_odd``。自洽性校验：``S→∞`` 时趋于 ``2×Z0``。
    """
    if H2 is None or Er2 is None:
        H2, Er2 = H1, Er1
    we = _thickness_adjusted((W1 + W2) / 2.0, T)
    return mom.z0_diff(we, we, S1, H1, Er1, H2, Er2, **kw)


def coplanar(H: float, Er: float, W1: float, W2: float, D1: float, T: float,
             coated: tuple[float, float, float, float] | None = None,
             h_back: float | None = None,
             er_back: float | None = None) -> tuple[float, float]:
    """共面波导（带下地参考）。

    * ``D1``：线与同层地铜的间距；
    * ``H``：同层地/走线所在平面到**第一个参考面**的距离（用于 CPW 部分的共形映射）；
    * ``h_back`` / ``er_back``：下地平面的高度与介电常数（内层时用等效高度）。

    电容 = 纯 CPW 的电容 + ``COPLANAR_BACK`` 倍「线对下地的微带电容」。
    这个 0.5 是采样拟合出来的（同层地铜把背地电场遮掉了一半）。
    """
    W = (W1 + W2) / 2.0
    a = W / 2.0
    b = a + D1
    k = a / b
    kp = math.sqrt(max(0.0, 1.0 - k * k))
    k1 = (math.sinh(math.pi * a / (2 * H)) /
          math.sinh(math.pi * b / (2 * H))) if H > 0 else k
    k1p = math.sqrt(max(0.0, 1.0 - k1 * k1))
    kk, kkp = elliptic_k(k), elliptic_k(kp)
    q = (elliptic_k(k1) / elliptic_k(k1p)) * (kkp / kk)

    er_top = 1.0
    if coated:
        _c1, c2, _c3, cer = coated
        d = (c2 or 0.0) + H
        er_top = ((cer * (c2 or 0.0)) + H) / d if d > 0 else 1.0
    eeff_cpw = er_top + (Er - er_top) / 2.0 * q
    z_cpw = (30.0 * math.pi / math.sqrt(eeff_cpw)) * (kkp / kk)

    hb = h_back if h_back else H
    eb = er_back if er_back is not None else Er
    ub = _thickness_adjusted(W, T) / hb
    z_air_ms = _microstrip_z0_ratio(ub, 1.0)
    z_ms = _microstrip_z0_ratio(ub, _microstrip_eeff(ub, eb, er_top))

    z_air_cpw = z_cpw * math.sqrt(eeff_cpw)
    kc, km = 1.0 / z_air_cpw, COPLANAR_BACK / z_air_ms
    c0 = kc + km
    c = kc * (z_air_cpw / z_cpw) ** 2 + km * (z_air_ms / z_ms) ** 2
    er_eff = c / c0
    return (1.0 / c0) / math.sqrt(er_eff), er_eff


# --------------------------------------------------------------------------- #
#  差分模型（用耦合系数把两条单端线折起来）
# --------------------------------------------------------------------------- #
def _coupling(spacing: float, height: float, inner: bool) -> float:
    r = max(spacing, 1e-6) / max(height, 1e-6)
    return (0.347 * math.exp(-2.9 * r)) if inner else (0.48 * math.exp(-0.96 * r))


def differential(z_single: float, spacing: float, height: float, inner: bool) -> float:
    """由单端阻抗估算差分阻抗：``Zdiff = 2 Z0 (1 - k)``。"""
    return 2.0 * z_single * (1.0 - _coupling(spacing, height, inner))


# --------------------------------------------------------------------------- #
#  统一入口
# --------------------------------------------------------------------------- #
def estimate(impedance_type: str, params: dict[str, float],
             calibrated: bool = True) -> tuple[float, float]:
    """按模型名估算 ``(Z0, Er_eff)``。参数名与 :mod:`.structures` 一致。

    ``calibrated=True`` 会套用 :mod:`.calibration` 的校准系数（推荐）；
    拟合脚本内部取基底值时会传 ``False``。
    """
    z, eeff = estimate_base(impedance_type, params)
    if calibrated:
        z, _ = calibration.apply_correction(impedance_type, params, z)
    return z, eeff


def estimate_base(impedance_type: str, params: dict[str, float]) -> tuple[float, float]:
    """按模型名估算 ``(Z0, Er_eff)``。参数名与 :mod:`.structures` 一致。"""
    p = params
    T = float(p.get("T1", 1.6))
    W1, W2 = float(p["W1"]), float(p["W2"])
    H1, Er1 = float(p["H1"]), float(p["Er1"])
    kind = "diff" if impedance_type.startswith("Diff") else "single"
    is_coplanar = "Coplanar" in impedance_type
    coated = "Coated" in impedance_type

    coat = None
    if coated:
        coat = (float(p.get("C1") or 0), float(p.get("C2") or 0),
                float(p.get("C3") or 0), float(p.get("CEr") or 3.8))

    if "Stripline" in impedance_type:
        h2 = float(p.get("H2") or 0) or None
        er2 = float(p.get("Er2") or 0) or None
        if kind == "diff":                      # 带状线差分直接走奇模 MoM
            z, eeff = stripline_diff(H1, Er1, h2, er2, W1, W2,
                                     float(p.get("S1") or 8.0), T)
            return z, eeff
        z, eeff = stripline(H1, Er1, h2, er2, W1, W2, T)
        height = _stripline_height(H1, Er1, h2, er2)[0] if h2 else H1
        inner = True
    elif is_coplanar:
        h2 = float(p.get("H2") or 0) or None
        er2 = float(p.get("Er2") or 0) or None
        if h2:                      # 内层共面：背地用带状线的等效高度
            h_back, er_back = _stripline_height(H1, Er1, h2, er2)
        else:
            h_back, er_back = H1, Er1
        z, eeff = coplanar(H1, Er1, W1, W2, float(p.get("D1") or 10.0), T,
                           coated=coat, h_back=h_back, er_back=er_back)
        height = H1
        inner = "Offset" in impedance_type
    else:
        z, eeff = microstrip(H1, Er1, W1, W2, T, coat)
        height = H1
        inner = False

    if kind == "diff":
        z = differential(z, float(p.get("S1") or 8.0), height, inner)
    return z, eeff


def solve_width(impedance_type: str, params: dict[str, float], target: float,
                w2_delta: float = 0.5, lo: float = 1.0,
                hi: float = 200.0) -> float | None:
    """二分法反算线宽（W1，mil）；如果区间内无解返回 ``None``。"""
    def z_of(w1: float) -> float:
        q = dict(params)
        q["W1"] = w1
        q["W2"] = max(w1 - w2_delta, 0.05)
        return estimate(impedance_type, q)[0]

    lo = max(lo, w2_delta + 0.2)
    f_lo, f_hi = z_of(lo) - target, z_of(hi) - target
    if f_lo * f_hi > 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if (z_of(mid) - target) * f_lo > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def solve_spacing(impedance_type: str, params: dict[str, float], target: float,
                  lo: float = 2.5, hi: float = 100.0) -> float | None:
    """二分法反算差分间距（S1，mil）；无解返回 ``None``。"""
    def z_of(s: float) -> float:
        q = dict(params)
        q["S1"] = s
        return estimate(impedance_type, q)[0]

    f_lo, f_hi = z_of(lo) - target, z_of(hi) - target
    if f_lo * f_hi > 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if (z_of(mid) - target) * f_lo > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0
