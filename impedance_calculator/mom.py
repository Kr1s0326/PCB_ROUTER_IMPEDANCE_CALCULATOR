"""T→0（零铜厚）截面的**精确准静态解**：方法矩（MoM）+ Galerkin/Chebyshev。

为什么需要它
------------
嘉立创后台（SI9000）是边界元场求解器。解析公式在铜厚趋 0 时只能算到 ~0.5%，
而**偏置带状线**用「等效高度微带」硬凑，基底误差有 13.5%。

本模块用**和 SI9000 同一套物理**（准 TEM + 2D 拉普拉斯）把零铜厚截面算准。
实测（与 SI9000 的 T=0.01 数据比）::

    对称带状线   +0.03% ~ +0.13%
    偏置带状线   +0.11% ~ +0.20%

数学
----
两块地（y=0、y=l）之间、位于 y=h1 的线电荷，格林函数是**解析的**::

    G(x, y) = 1/(4·pi·eps) · ln[ (cosh(pi·x/l) - cos(pi·(y+y')/l))
                                / (cosh(pi·x/l) - cos(pi·(y-y')/l)) ]

微带线是它的 ``h2 -> ∞`` 极限（等于镜像法）::

    G(x) = 1/(2·pi·eps) · ln( sqrt(x² + 4h²) / |x| )

导体电荷密度在边缘 ~ ``1/sqrt(1-ξ²)``（ξ = 2(x-xc)/W），因此用

    sigma(ξ) = Σ_n c_n · T_n(ξ) / sqrt(1-ξ²)

做 Galerkin 展开 + Gauss-Chebyshev 求积（正好吸收那个 1/sqrt 权重），
用 ``Z0 = Z_vac / sqrt(Er_eff)`` 得到阻抗。

差分对用**奇模**（V1 = -V2 → x=0 处是电壁 → 镜像电荷反号）::

    K_odd(x, x') = K(x - x') - K(x + x')        Zdiff = 2 · Z0_odd

校验：``S -> ∞`` 时 ``Zdiff -> 2·Z0``；``S -> 0`` 时耦合最强。
"""

import math
from collections.abc import Sequence

C_LIGHT = 299792458.0
EPS0 = 8.8541878128e-12
MIL = 25.4e-6

#: 默认求积节点数 / 基函数个数（收敛性实测：再大反而因近奇异求积退化）
M_DEFAULT = 56
N_DEFAULT = 7


def _gc_nodes(m: int) -> tuple[list[float], list[float]]:
    """Gauss-Chebyshev 第一类节点（含 1/sqrt(1-ξ²) 权）。"""
    return ([math.cos((2 * i + 1) * math.pi / (2 * m)) for i in range(m)],
            [math.pi / m] * m)


def _kernels(h1m: float, h2m: float | None):
    """返回 ``(K(Δ), C_m)``，其中 ``K(Δ) ≈ [ln(C_m) - ln|Δ|]/(2πε₀)``。"""
    if h2m is None:
        def ker(dx):
            return math.log(math.sqrt(dx * dx + 4 * h1m * h1m) / abs(dx)) / (2 * math.pi * EPS0)
        return ker, 2.0 * h1m

    l = h1m + h2m

    def ker(dx):
        ch = math.cosh(math.pi * dx / l)
        return math.log((ch - math.cos(2 * math.pi * h1m / l)) / (ch - 1.0)) / (4 * math.pi * EPS0)

    return ker, 2.0 * l * math.sin(math.pi * h1m / l) / math.pi


def _solve(A: Sequence[Sequence[float]], b: Sequence[float]) -> list[float] | None:
    n = len(A)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-300:
            return None
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for r in range(n):
            if r == col:
                continue
            fac = M[r][col] / pv
            if fac:
                for c in range(col, n + 1):
                    M[r][c] -= fac * M[col][c]
    return [M[i][n] / M[i][i] for i in range(n)]


def capacitance_vacuum(W: float, h1: float, h2: float | None = None,
                       offset: float = 0.0, wall: str | None = None,
                       m: int = M_DEFAULT, n: int = N_DEFAULT) -> float:
    """零铜厚、均匀介质的**单位长度电容**（F/m，V=1）。

    :param W: 线宽（mil）
    :param h1: 下/唯一参考面距离（mil）
    :param h2: 上参考面距离（mil）；``None`` → 微带线（单参考面）
    :param offset: 导体中心到对称面 x=0 的距离（mil，差分对用）
    :param wall: ``None`` 无对称面；``'electric'`` 电壁（奇模，镜像反号）；
                 ``'magnetic'`` 磁壁（偶模，镜像同号）
    """
    a = W * MIL / 2.0
    h1m = h1 * MIL
    xi, wq = _gc_nodes(m)
    ker, cm = _kernels(h1m, h2 * MIL if h2 is not None else None)
    xc = offset * MIL

    T = [[math.cos(k * math.acos(t)) for t in xi] for k in range(n)]
    xs = [xc + a * t for t in xi]

    # 自项（Δ=0 的对数奇点）：用单元平均解析积分  <ln|δ|> = ln(2h) - 1.5
    diag = []
    for i in range(m):
        if i == 0:
            hh = (xi[0] - xi[1]) / 2.0
        elif i == m - 1:
            hh = (xi[m - 2] - xi[m - 1]) / 2.0
        else:
            hh = (xi[i - 1] - xi[i + 1]) / 4.0
        hh = max(hh, 1e-12)
        diag.append((math.log(cm / a) - math.log(2 * hh) + 1.5) / (2 * math.pi * EPS0))

    sign = -1.0 if wall == 'electric' else (1.0 if wall == 'magnetic' else 0.0)
    G = []
    for i in range(m):
        row = []
        for j in range(m):
            v = diag[i] if i == j else ker(xs[i] - xs[j])
            if sign:
                v += sign * ker(xs[i] + xs[j])
            row.append(wq[i] * wq[j] * v)
        G.append(row)

    TG = [[sum(T[k][i] * G[i][j] for i in range(m)) for j in range(m)] for k in range(n)]
    A = [[(a * a) * sum(TG[k][j] * T[kk][j] for j in range(m)) for kk in range(n)]
         for k in range(n)]
    b = [(a * math.pi) if k == 0 else 0.0 for k in range(n)]      # V = 1
    for k in range(n):
        A[k][k] += 1e-12 * (1.0 + abs(A[k][k]))
    c = _solve(A, b)
    if c is None:
        raise ValueError('MoM 矩阵奇异')
    return a * math.pi * c[0]


def z0_vacuum(W: float, h1: float, h2: float | None = None, **kw) -> float:
    """零铜厚、真空的阻抗（Ω）。"""
    return 1.0 / (C_LIGHT * capacitance_vacuum(W, h1, h2, **kw))


def effective_er(er1: float, h1: float, er2: float, h2: float) -> float:
    """按 1/h 加权的等效介电常数。"""
    return (er1 / h1 + er2 / h2) / (1.0 / h1 + 1.0 / h2)


def z0_single(W1: float, W2: float, h1: float, er1: float,
              h2: float | None = None, er2: float | None = None, **kw) -> tuple[float, float]:
    """零铜厚单端阻抗（Ω）与等效介电常数。"""
    W = (W1 + W2) / 2.0
    zv = z0_vacuum(W, h1, h2, **kw)
    if h2 is None:
        return zv / math.sqrt(er1), er1
    eeff = effective_er(er1, h1, er2 if er2 is not None else er1, h2)
    return zv / math.sqrt(eeff), eeff


def z0_diff(W1: float, W2: float, S: float, h1: float, er1: float,
            h2: float | None = None, er2: float | None = None, **kw) -> tuple[float, float]:
    """零铜厚差分阻抗（Ω）与等效介电常数（奇模）::

        Zdiff = 2 · Z_odd,   Z_odd = Z_vac,odd / sqrt(Er_eff)

    ``S`` 是线边到线边的间距（mil）。
    """
    W = (W1 + W2) / 2.0
    off = S / 2.0 + W / 2.0
    zv = z0_vacuum(W, h1, h2, offset=off, wall='electric', **kw)
    if h2 is None:
        return 2.0 * zv / math.sqrt(er1), er1
    eeff = effective_er(er1, h1, er2 if er2 is not None else er1, h2)
    return 2.0 * zv / math.sqrt(eeff), eeff
