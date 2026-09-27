"""逐步走一遍**当前**的离线计算流程（对着代码看最直观）。

以 JLC04161H-7628 内层 L2 单端 50Ω 为例，把每一步的中间量都打印出来。

用法::

    python examples/walkthrough.py
"""

from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from jlc_impedance import analytic, calibration, mom               # noqa: E402
from jlc_impedance.stackup import BUILTIN_STACKUPS                 # noqa: E402

MARK = 'OffsetStripline1B1A'
STACK = 'JLC04161H-7628'
LAYER = 'L2'
TARGET = 50.0
T1 = 0.6                     # 内层 0.5oz → 0.6 mil


def rule(ch):
    print(ch * 74)


def main():
    st = BUILTIN_STACKUPS[STACK]
    g = st.si9000_geometry(LAYER)
    p = dict(g, T1=T1, W1=10.669, W2=10.169)

    rule('=')
    print('① 叠层 → SI9000 参数（stackup.si9000_geometry）')
    rule('-')
    print(st.describe())
    print('   →', {k: round(v, 4) for k, v in p.items()})
    print('   规则：H1 = 该层所属芯板的介质；H2 = 另一侧介质 + 该层铜厚')

    W = (p['W1'] + p['W2']) / 2.0
    We = analytic._thickness_adjusted(W, T1)
    rule('=')
    print('② 铜厚 → 等效加宽（analytic._thickness_adjusted）')
    rule('-')
    print('   W  = (%.3f + %.3f)/2      = %.4f mil' % (p['W1'], p['W2'], W))
    print("   W' = W + 0.5·(T/π)(1+ln(4πW/T)) = %.4f mil" % We)

    rule('=')
    print('③ T→0 的精确解（mom.py：方法矩 + Galerkin/Chebyshev）')
    rule('-')
    c0 = mom.capacitance_vacuum(We, p['H1'], p['H2'])
    zv = mom.z0_vacuum(We, p['H1'], p['H2'])
    print('   真空电容 C0 = %.6e F/m' % c0)
    print('   真空阻抗 Zvac = 1/(c0·C0) = %.4f Ω' % zv)
    er_eff = mom.effective_er(p['Er1'], p['H1'], p['Er2'], p['H2'])
    print('   等效介电常数 Er = (Er1/H1+Er2/H2)/(1/H1+1/H2) = %.4f' % er_eff)
    z_t0 = zv / math.sqrt(er_eff)
    print('   T→0 阻抗 = Zvac/√Er = %.4f Ω' % z_t0)

    z_base, _ = analytic.estimate_base(MARK, p)
    rule('=')
    print('④ 物理基底 Z_base（analytic.estimate_base）')
    rule('-')
    print('   Z_base = %.4f Ω   （与 ③ 一致，即 MoM + 铜厚等效加宽）' % z_base)

    rule('=')
    print('⑤ 线性级修正：ln(Z/Z_base) = Σβᵢ·φᵢ（calibration.features）')
    rule('-')
    cal = calibration.CALIBRATION[MARK]
    f = calibration.features(p)
    k_lin = 0.0
    for name, beta in zip(cal['names'], cal['beta']):
        v = f.get(name, 0.0)
        if abs(v) < 1e-12:
            continue
        k_lin += beta * v
        print('   β(%-6s) = %+9.6f   φ = %-11.5f  →  %+.6f'
              % (name, beta, v, beta * v))
    print('   小计 = %+.6f' % k_lin)

    rule('=')
    print('⑥ 核级修正：RBF 核岭回归修残差（calibration.krr_predict）')
    rule('-')
    store = calibration.KRRS[MARK]
    k_krr = calibration.krr_predict(store, p)
    print('   支持点 n=%d，γ=%.4g' % (len(store['alpha']), store['gamma']))
    print('   核修正 = %+.6f' % k_krr)

    k = max(-1.0, min(1.0, k_lin + k_krr))
    rule('=')
    print('⑦ 合成')
    rule('-')
    print('   Z = Z_base · exp(线性 + 核)')
    print('     = %.4f · exp(%+.6f + %+.6f)' % (z_base, k_lin, k_krr))
    print('     = %.4f Ω' % (z_base * math.exp(k)))
    print('   官网值 = 50.000 Ω')

    rule('=')
    print('⑧ 反算：二分求 W1 使 Z = %.0f（analytic.solve_width）' % TARGET)
    rule('-')
    w = analytic.solve_width(MARK, dict(p, W1=8.0, W2=7.5), TARGET, 0.5)
    print('   解得 W1 = %.4f mil' % w)
    print('   官网解 = 10.879 mil   误差 = %+.2f%%' % (100 * (w - 10.879) / 10.879))
    rule('=')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
