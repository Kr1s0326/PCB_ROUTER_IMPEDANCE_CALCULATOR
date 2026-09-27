"""诊断：物理基底在「真实叠层窗口」里的误差有多大。

用来回答「误差到底来自基底还是来自校准」——
如果基底的形状错了，校准救不回来，必须先把公式改对
（带状线换成 MoM 精确解、共面把背地系数从 1.0 降到 0.5 就是这么来的）。

关键事实：**真实叠层窗口里旧基底只有 6.5%，13.5% 是被非物理样本拉高的**。

用法::

    python tools/experiments/check_base_models.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import _common as C                                              # noqa: E402
from impedance_calculator import analytic                               # noqa: E402

#: 真实叠层窗口：内层 H1 是芯板（大）、H2 是 prepreg+铜（小），T=0.6
WINDOWS = [
    ('全部样本', lambda p: True),
    ('真实窗口 H1∈[35,55] H2∈[4,10] T=0.6',
     lambda p: p['T1'] == 0.6 and 35 <= p['H1'] <= 55 and 4 <= p.get('H2', 0) <= 10),
    ('真实窗口 + W≤15mil',
     lambda p: p['T1'] == 0.6 and 35 <= p['H1'] <= 55 and 4 <= p.get('H2', 0) <= 10
     and p['W1'] <= 15),
]


def main():
    rows = C.prepare(C.load(C.FILE_TRAIN), verbose=False)
    by = {}
    for r in rows:
        by.setdefault(r['mark'], []).append(r)

    for mark, rs in sorted(by.items()):
        print('=== %s' % mark)
        for tag, flt in WINDOWS:
            sub = [r for r in rs if flt(r['params'])]
            if not sub:
                continue
            e = [r['ly'] for r in sub]          # ly = ln(Z_官网 / Z_base)
            print('   %-38s n=%3d  基底误差 %5.2f%%  最大 %5.2f%%'
                  % (tag, len(sub), 100 * C.rms(e),
                     100 * max(abs(x) for x in e)))
        print()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
