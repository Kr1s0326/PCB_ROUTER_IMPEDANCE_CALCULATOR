"""拟合离线校准层 → 生成 ``jlc_impedance/_coefs.py`` 与 ``_krrs.py``。

两级模型
--------
1. **线性级**：``ln(Z_官网/Z_base) = Σ βᵢφᵢ``
   基函数见 ``jlc_impedance/calibration.py``；前向选择挑特征（以 5 折 CV 为准）+ 岭回归。
2. **核级**：RBF 核岭回归修线性级的残差，``z = Σ αᵢexp(-γ‖x-Xᵢ‖²)``。

为什么必须有第二级：留出集实测表明，单线性级平均线宽误差 6.6%，
加上核级后降到 4.3%（``tools/experiments/holdout_compare.py``）。

评估口径
--------
* ``训练集 CV``：训练集内部 5 折。**偏乐观**，因为前向选择用了全量数据挑特征。
* ``留出集``：``data/holdout.jsonl``，从不参与任何拟合与特征选择 → **这才是可信数字**。

用法::

    python tools/fit_calibration.py            # 完整拟合 + 评估（几分钟）
    python tools/fit_calibration.py --no-eval  # 只拟合，跳过慢的留出集反算
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _common as C                                              # noqa: E402
from jlc_impedance.mlmodels import KRR                           # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='拟合离线校准层')
    ap.add_argument('--no-eval', action='store_true', help='跳过留出集反算评估（快）')
    ap.add_argument('--terms', type=int, default=10, help='线性级最多几项')
    args = ap.parse_args(argv)

    C.ensure_dirs()
    train = C.prepare(C.load(C.FILE_TRAIN))
    hold = C.prepare(C.load(C.FILE_HOLDOUT)) if os.path.exists(C.FILE_HOLDOUT) else []
    by_train: Dict[str, List[Any]] = {}
    by_hold: Dict[str, List[Any]] = {}
    for r in train:
        by_train.setdefault(r['mark'], []).append(r)
    for r in hold:
        by_hold.setdefault(r['mark'], []).append(r)
    print('训练 %d 条 / 留出 %d 条 / %d 个结构'
          % (len(train), len(hold), len(by_train)))

    result: Dict[str, Dict[str, Any]] = {}
    stores: Dict[str, Dict[str, Any]] = {}
    quality: Dict[str, Dict[str, Any]] = {}
    report: List[tuple] = []

    for mark, rs in sorted(by_train.items()):
        if len(rs) < 25:
            print('  跳过 %s（样本 %d）' % (mark, len(rs)))
            continue
        names, beta, _ = C.forward_select(rs, sorted(rs[0]['f'].keys()), args.terms)
        if beta is None:
            print('  跳过 %s（回归失败）' % mark)
            continue
        base_rms = C.rms([r['ly'] for r in rs])
        wcv = C.rms(C.width_cv(rs, names))

        # 第二级：对线性级的残差做 RBF 核岭回归
        Xc = [C.compact_inputs(r['params']) for r in rs]
        lin = [C.predict(beta, names, r['f']) for r in rs]
        resid = [r['ly'] - l for r, l in zip(rs, lin)]
        krr = KRR(reg=1e-2).fit(Xc, resid)
        store = {'mu': krr.mu, 'sd': krr.sd, 'gamma': krr.gamma,
                 'X': krr.Xs, 'alpha': krr.alpha}

        # 留出集：整条流水线的真实精度
        hw: List[float] = []
        if by_hold.get(mark) and not args.no_eval:
            hw, _ = C.width_errors(by_hold[mark], names, beta)
        h_rms, h_max, h_n = C.stats(hw)
        pv, _ = C.width_errors(rs, names, beta, C.PRACTICAL)
        p_rms = C.rms(pv)

        result[mark] = {'names': names, 'beta': beta}
        stores[mark] = store
        quality[mark] = {'width_holdout': h_rms if hw else None,
                         'width_train_cv': wcv, 'samples': len(rs)}
        report.append((mark, len(rs), names, beta, base_rms, wcv,
                       h_rms if hw else float('nan'), h_max, h_n, p_rms))
        print('  %-44s n=%3d base=%5.1f%%  CV=%5.2f%%  留出=%5.2f%%(max %5.2f%%,n=%d)'
              % (mark[:44], len(rs), 100 * base_rms, wcv, h_rms, h_max, h_n))

    C.write_coefs(result, quality)
    C.write_krrs(stores, {m: q['width_holdout'] for m, q in quality.items()})
    _write_report(report)
    print('\n→ %s\n→ %s\n→ %s' % (C.OUT_COEFS, C.OUT_KRRS, C.OUT_CALIB_REPORT))
    return 0


def _write_report(report: List[tuple]) -> None:
    lines = ['# 离线模型校准报告', '',
             '全部为**反算线宽**相对误差（给定目标阻抗解线宽，与官网解出的线宽比）。', '',
             '| 结构 | 样本 | 基底误差(lnZ) | 训练集 CV | **留出集** | 留出集最大 | n | 工作区 |',
             '|---|---|---|---|---|---|---|---|']
    for mark, n, names, beta, base_rms, wcv, h_rms, h_max, h_n, p_rms in report:
        lines.append('| `%s` | %d | %.1f%% | %.2f%% | **%.2f%%** | %.2f%% | %d | %.2f%% |'
                     % (mark, n, 100 * base_rms, wcv, h_rms, h_max, h_n, p_rms))
    lines += ['', '> 留出集（`data/holdout.jsonl`）从不参与拟合与特征选择，是**唯一无偏**的口径；',
              '> 训练集 CV 因为前向选择用过全量数据而偏乐观。', '',
              '## 线性级系数', '']
    for mark, n, names, beta, base_rms, wcv, h_rms, h_max, h_n, p_rms in report:
        lines += ['### %s' % mark, '', '| 特征 | 系数 |', '|---|---|']
        lines += ['| `%s` | %.6f |' % (nm, b) for nm, b in zip(names, beta)]
        lines.append('')
    with open(C.OUT_CALIB_REPORT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
