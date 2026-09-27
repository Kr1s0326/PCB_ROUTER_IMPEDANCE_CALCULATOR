"""诊断：采样分布是否贴合实际用量。

第一轮 750 组是纯空间填充（H1/H2 独立取），结果内层结构只有 ~15% 的样本
落在真实叠层区间，等于**大部分算力花在了永远用不到的参数区**。
这个脚本把这个事实量化出来，也是第二轮 ``stackup`` regime 补采的依据。

用法::

    python tools/experiments/analyze_coverage.py
"""

import collections
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import _common as C                                              # noqa: E402

INNER = ['OffsetStripline1B1A', 'DiffOffsetStripline1B1A',
         'OffsetCoplanarWaveguide1B1A', 'DiffOffsetCoplanarWaveguide1B1A']
#: 真实 4 层叠层里 ln(H2/H1) 的落点：3313 −2.40、2116 −2.26、7628 −1.55
REAL_BAND = (-2.7, -1.2)


def main():
    rows = C.load(C.FILE_TRAIN)
    print('训练样本 %d 条\n' % len(rows))

    print('内层结构的 ln(H2/H1) 分布（真实叠层落在 %.2f ~ %.2f）' % REAL_BAND)
    for mark in INNER:
        v = sorted(math.log(r['params']['H2'] / r['params']['H1'])
                   for r in rows if r['mark'] == mark)
        if not v:
            continue
        inside = sum(1 for x in v if REAL_BAND[0] <= x <= REAL_BAND[1])
        print('  %-38s n=%3d  中位 %+5.2f  区间内 %2d 条 (%.0f%%)'
              % (mark, len(v), v[len(v) // 2], inside, 100.0 * inside / len(v)))

    print('\n阻抗落点')
    z = [r['z'] for r in rows if r.get('z')]
    inb = sum(1 for v in z if 40 <= v <= 110)
    print('  40~110Ω（实际工作区）: %d / %d = %.0f%%' % (inb, len(z), 100.0 * inb / len(z)))

    print('\n线宽落点')
    w = sorted(r['params']['W1'] for r in rows)
    print('  W1: min %.1f  中位 %.1f  max %.1f（mil）' % (w[0], w[len(w) // 2], w[-1]))
    print('  W1 ≤ 15mil: %.0f%%' % (100.0 * sum(1 for x in w if x <= 15) / len(w)))

    print('\n按 regime 分组')
    c = collections.Counter(r.get('regime', 'fill') for r in rows)
    for k, v in sorted(c.items()):
        print('  %-10s %4d' % (k, v))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
