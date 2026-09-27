"""第 1 项：测嘉立创后台（SI9000）的**重复性** —— 决定模型精度的天花板。

三件事：
  A. 同一组参数重复请求 N 次 → 看 Z 的抖动（噪声地板）
  B. 正算/反算自洽：goal-seek 解出 W 后，再用正算回代看是否等于目标阻抗
  C. 采一批 **T=0.01（准零铜厚）** 的数据，用来验证 MoM（单独存，不参与拟合）

用法::

    python tools/check_engine.py            # 全做
    python tools/check_engine.py --only A
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C                                              # noqa: E402
from impedance_calculator.api import JlcApi, JlcApiError                # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_ZERO = C.FILE_ZEROCOPPER

# 覆盖不同结构的代表几何（互相差别很大，避免都落在同一区域）
REPEAT_CASES = [
    ('CoatedMicrostrip1B', dict(H1=3.9134, Er1=4.10, W1=6.232, W2=5.732, T1=1.6,
                                C1=1.0, C2=0.6, CEr=3.8)),
    ('CoatedMicrostrip1B', dict(H1=8.2835, Er1=4.40, W1=13.840, W2=13.340, T1=1.6,
                                C1=1.0, C2=0.6, CEr=3.8)),
    ('SurfaceMicrostrip1B', dict(H1=60.236, Er1=4.42, W1=111.735, W2=111.235, T1=1.6)),
    ('OffsetStripline1B1A', dict(H1=41.9291, Er1=4.38, H2=8.8819, Er2=4.40,
                                 W1=10.879, W2=10.379, T1=0.6)),
    ('OffsetStripline1B1A', dict(H1=49.803, Er1=4.42, H2=4.5118, Er2=4.10,
                                 W1=5.407, W2=4.907, T1=0.6)),
    ('OffsetStripline1B1A', dict(H1=20.0, Er1=4.30, H2=20.0, Er2=4.30,
                                 W1=8.0, W2=7.5, T1=0.6)),
    ('DiffOffsetStripline1B1A', dict(H1=41.9291, Er1=4.38, H2=8.8819, Er2=4.40,
                                     W1=6.57, W2=6.07, S1=8.0, T1=0.6)),
    ('DiffEdgeCoupledCoatedMicrostrip1B', dict(H1=8.2835, Er1=4.40, W1=8.82, W2=8.32,
                                               S1=8.0, T1=1.6, C1=1.0, C2=0.6,
                                               C3=1.0, CEr=3.8)),
    ('SurfaceCoplanarWaveguideWithLowerGnd1B', dict(H1=8.2835, Er1=4.40, W1=10.0,
                                                    W2=9.5, D1=10.0, T1=1.6)),
    ('CoatedCoplanarWaveguideWithLowerGnd1B', dict(H1=3.9134, Er1=4.10, W1=6.07,
                                                   W2=5.57, D1=10.0, T1=1.6,
                                                   C1=1.0, C2=0.6, CEr=3.8)),
    ('OffsetCoplanarWaveguide1B1A', dict(H1=49.803, Er1=4.42, H2=4.5118, Er2=4.10,
                                         W1=6.0, W2=5.5, D1=8.0, T1=0.6)),
    ('DiffCoatedCoplanarWaveguideWithLowerGnd1B', dict(H1=8.2835, Er1=4.40, W1=8.0,
                                                       W2=7.5, S1=8.0, D1=10.0, T1=1.6,
                                                       C1=1.0, C2=0.6, C3=1.0, CEr=3.8)),
]

# 准零铜厚验证点（T=0.01），用来验 MoM
ZERO_CASES = []
for W in (5, 8, 12, 20):
    ZERO_CASES.append(('OffsetStripline1B1A', dict(H1=20, Er1=4.3, H2=20, Er2=4.3,
                                                   W1=W, W2=W - 0.01, T1=0.01)))
for h1, h2 in ((5, 20), (20, 5), (10, 30), (30, 10), (4, 40)):
    ZERO_CASES.append(('OffsetStripline1B1A', dict(H1=h1, Er1=4.3, H2=h2, Er2=4.3,
                                                   W1=8, W2=7.99, T1=0.01)))
for S in (4, 8, 16, 30):
    ZERO_CASES.append(('DiffOffsetStripline1B1A', dict(H1=20, Er1=4.3, H2=20, Er2=4.3,
                                                       W1=8, W2=7.99, S1=S, T1=0.01)))
for h1, h2 in ((20, 20), (10, 40)):
    ZERO_CASES.append(('DiffOffsetStripline1B1A', dict(H1=h1, Er1=4.3, H2=h2, Er2=4.3,
                                                       W1=8, W2=7.99, S1=8, T1=0.01)))
    ZERO_CASES.append(('DiffOffsetCoplanarWaveguide1B1A',
                       dict(H1=h1, Er1=4.3, H2=h2, Er2=4.3, W1=8, W2=7.99,
                            S1=8, D1=10, T1=0.01)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', default=None)
    ap.add_argument('--repeat', type=int, default=3)
    ap.add_argument('--min-delay', type=float, default=0.9)
    ap.add_argument('--max-delay', type=float, default=1.6)
    args = ap.parse_args(argv)

    api = JlcApi()
    sleep = lambda: time.sleep(random.uniform(args.min_delay, args.max_delay))   # noqa: E731
    try:
        if args.only in (None, 'A'):
            print('=== A. 重复性（同一参数请求 %d 次）===' % args.repeat)
            worst = 0.0
            rows = []
            for mark, p in REPEAT_CASES:
                zs = []
                for _ in range(args.repeat):
                    r = api.calculate(mark, dict(p, dCalculateMode=3))
                    zs.append(r.impedance)
                    sleep()
                if any(v is None for v in zs):
                    print('  %-42s 计算失败' % mark)
                    continue
                lo, hi = min(zs), max(zs)
                rel = 100 * (hi - lo) / lo
                worst = max(worst, rel)
                rows.append((mark, zs, rel))
                print('  %-42s Z=%.6f  极差=%.6f (%.4f%%)'
                      % (mark, sum(zs) / len(zs), hi - lo, rel))
            print('  → 最大相对抖动 %.4f%%   ← 这是精度的噪音地板' % worst)
            if rows:
                print('  → 平均相对抖动 %.4f%%'
                      % (sum(r[2] for r in rows) / len(rows)))

        if args.only in (None, 'B'):
            print()
            print('=== B. 正算/反算自洽（goal-seek 解出 W 后回代）===')
            for mark, p, target in (('CoatedMicrostrip1B',
                                     dict(H1=8.2835, Er1=4.40, W1=13.0, W2=12.5, T1=1.6,
                                          C1=1.0, C2=0.6, CEr=3.8), 50.0),
                                    ('OffsetStripline1B1A',
                                     dict(H1=41.9291, Er1=4.38, H2=8.8819, Er2=4.40,
                                          W1=10.0, W2=9.5, T1=0.6), 50.0),
                                    ('DiffOffsetStripline1B1A',
                                     dict(H1=41.9291, Er1=4.38, H2=8.8819, Er2=4.40,
                                          W1=6.0, W2=5.5, S1=8.0, T1=0.6), 100.0)):
                res = api.solve(mark, 'W2', dict(p, dCalculateMode=3), target, 2.0, 150.0)
                sleep()
                s = res.solved
                if not s.get('W1'):
                    print('  %-42s 反算失败' % mark)
                    continue
                back = api.calculate(mark, dict(p, W1=s['W1'], W2=s['W2'], dCalculateMode=3))
                sleep()
                print('  %-42s 目标%.0fΩ → W1=%.4f  回代=%.5fΩ  偏差=%+.5fΩ'
                      % (mark, target, s['W1'], back.impedance,
                         back.impedance - target))

        if args.only in (None, 'C'):
            print()
            print('=== C. 采准零铜厚（T=0.01）验证点 %d 组 ===' % len(ZERO_CASES))
            with open(OUT_ZERO, 'w', encoding='utf-8') as fh:
                for mark, p in ZERO_CASES:
                    r = api.calculate(mark, dict(p, dCalculateMode=3))
                    fh.write(json.dumps({'mark': mark, 'params': p, 'z': r.impedance,
                                         'status': r.status}, ensure_ascii=False) + '\n')
                    print('  %-40s %s → %s' % (mark, {k: p[k] for k in ('H1', 'H2', 'W1', 'S1')
                                                      if k in p}, r.impedance))
                    sleep()
            print('  → 写入', OUT_ZERO)
    except JlcApiError as exc:
        print('在线引擎出错:', exc, file=sys.stderr)
        return 2
    finally:
        api.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
