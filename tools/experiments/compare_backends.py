"""离线后端对照实验：线性 / 核岭 / 高斯过程 / MLP / 堆叠 / 分层收缩 / 符号回归。

为什么要有这个脚本
------------------
「离线模型该用什么后端」不是拍脑袋决定的，是用**独立留出集**量出来的。
结论（见 ``reports/backend_comparison.md``）：

* **堆叠（线性 + 核岭修残差）最好**，平均线宽误差比纯线性低 ~36%
* 单用核岭 / 高斯过程也不错；MLP 明显更差（样本量不够）
* **分层收缩失败**（直接对系数做收缩没有意义，各结构特征语义不同）
* 符号回归能给出**可读公式**，但精度不如核方法

⚠️ 必须看留出集那一列：``forward_select`` 用全量数据挑特征，所以
「训练集 CV」对线性/堆叠偏乐观，实测两者结论会**反过来**。

用法::

    python tools/experiments/compare_backends.py                 # 默认（含 GP，几分钟）
    python tools/experiments/compare_backends.py --fast          # 跳过 GP/MLP/符号回归
    python tools/experiments/compare_backends.py --symbolic      # 额外跑符号回归（慢）
"""

import argparse
import json
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)
sys.path.insert(0, os.path.dirname(TOOLS))
os.makedirs(os.path.join(TOOLS, 'experiments'), exist_ok=True)

import _common as C                                              # noqa: E402
from impedance_calculator import analytic                               # noqa: E402
from impedance_calculator.calibration import compact_inputs, features   # noqa: E402
from impedance_calculator.mlmodels import GP, KRR, MLP, hierarchical_shrink   # noqa: E402

OUT = os.path.join(C.REPORTS, 'backend_comparison.md')
OUT_JSON = os.path.join(C.REPORTS, 'backend_comparison.json')
#: 固定展示顺序（字典插入顺序会随开关变化，不能拿来当表头）
BACKEND_ORDER = ['linear', 'krr', 'gp', 'mlp', 'stack', 'shrink', 'symbolic']
WIDTH_ROWS = 25          # 每折最多评估多少行反算（MoM 会拖慢带状线）


# --------------------------------------------------------------------------- #
def build_backends(train, beta, names, with_gp, with_mlp):
    """返回 ``{名字: dy(params)}``。``dy`` 是 ln(Z/Z_base) 的预测。"""
    Xc = [compact_inputs(r['params']) for r in train]
    y = [r['ly'] for r in train]

    def dy_linear(p):
        fv = features(p)
        return sum(b * fv.get(n, 0.0) for b, n in zip(beta, names))

    out = {'linear': dy_linear}
    krr = KRR(reg=1e-2).fit(Xc, y)
    out['krr'] = lambda p, _m=krr: _m.predict(compact_inputs(p))

    resid = [y[i] - dy_linear(train[i]['params']) for i in range(len(train))]
    krr_res = KRR(reg=1e-2).fit(Xc, resid)
    out['stack'] = lambda p, _m=krr_res: dy_linear(p) + _m.predict(compact_inputs(p))

    if with_gp:
        gp = GP().fit(Xc, y)
        out['gp'] = lambda p, _m=gp: _m.predict(compact_inputs(p))
    if with_mlp:
        mlp = MLP().fit(Xc, y)
        out['mlp'] = lambda p, _m=mlp: _m.predict(compact_inputs(p))
    return out


def eval_backend(fn, rows, mark, width_mask=True):
    """返回 ``(lnZ_rms, width_rms, width_max, fails)``。"""
    e = [fn(r['params']) - r['ly'] for r in rows]
    ln_rms = C.rms(e)
    wr, fails = _width(fn, rows[:WIDTH_ROWS], mark)
    return ln_rms, C.rms(wr), (max(abs(x) for x in wr) if wr else float('nan')), fails


def _width(fn, rows, mark, steps=40):
    errs, fails = [], 0
    for r in rows:
        p = r['params']
        d = p['W1'] - p['W2']
        lo, hi = d + 0.2, 250.0

        def z_of(w):
            q = dict(p, W1=w, W2=max(w - d, 0.05))
            zb, _ = analytic.estimate_base(mark, q)
            return zb * math.exp(max(-1.0, min(1.0, fn(q))))

        flo, fhi = z_of(lo) - r['z'], z_of(hi) - r['z']
        if flo * fhi > 0:
            fails += 1
            continue
        for _ in range(steps):
            mid = (lo + hi) / 2.0
            if (z_of(mid) - r['z']) * flo > 0:
                lo = mid
            else:
                hi = mid
        errs.append(100.0 * ((lo + hi) / 2.0 - p['W1']) / p['W1'])
    return errs, fails


def main(argv=None):
    ap = argparse.ArgumentParser(description='离线后端对照实验')
    ap.add_argument('--fast', action='store_true', help='只跑 linear/krr/stack')
    ap.add_argument('--symbolic', action='store_true', help='额外跑符号回归（慢）')
    args = ap.parse_args(argv)
    with_gp = not args.fast
    with_mlp = not args.fast

    C.ensure_dirs()
    train = C.prepare(C.load(C.FILE_TRAIN))
    hold = C.prepare(C.load(C.FILE_HOLDOUT))
    by_t, by_h = {}, {}
    for r in train:
        by_t.setdefault(r['mark'], []).append(r)
    for r in hold:
        by_h.setdefault(r['mark'], []).append(r)

    names_all = sorted(by_t)
    # 多任务分层收缩：需要统一特征集
    sel, betas, counts = {}, {}, {}
    for mark in names_all:
        rs = by_t[mark]
        if len(rs) < 40:
            continue
        sel[mark] = C.forward_select(rs, sorted(rs[0]['f'].keys()))[0]
        betas[mark] = dict(zip(sel[mark], C.fit_beta(rs, sel[mark]) or []))
        counts[mark] = len(rs)
    union = sorted({n for v in sel.values() for n in v})
    shrunk = hierarchical_shrink(betas, counts, n0=60.0) if betas else {}

    rows_out = []
    acc = {}
    print('%-42s %s' % ('结构', '  '.join('%-15s' % b for b in BACKEND_ORDER)),
          flush=True)
    for mark in names_all:
        rs, hs = by_t[mark], by_h.get(mark, [])
        if len(rs) < 40:
            continue
        names = sel[mark]
        beta = C.fit_beta(rs, names)
        backends = build_backends(rs, beta, names, with_gp, with_mlp)

        if mark in shrunk and shrunk[mark]:
            sb = shrunk[mark]
            backends['shrink'] = (lambda p, _b=sb: sum(
                v * features(p).get(k, 0.0) for k, v in _b.items()))

        if args.symbolic:
            try:
                import symbolic_regression as SR
                Xs = [SR.features_vec(r['params']) for r in rs]
                ys = [r['ly'] for r in rs]
                _, tree, a, b = SR.evolve(Xs, ys, random.Random(7),
                                          pop_size=200, gens=25)
                backends['symbolic'] = (lambda p, _t=tree, _a=a, _b=b:
                                        _a * SR.ev(_t, SR.features_vec(p)) + _b)
            except Exception as exc:                              # noqa: BLE001
                print('  符号回归失败: %s' % exc, file=sys.stderr)

        line, per = [], {}
        for name in BACKEND_ORDER:
            fn = backends.get(name)
            if fn is None:
                line.append('%-15s' % '—')
                continue
            r = eval_backend(fn, hs, mark)
            per[name] = r
            acc.setdefault(name, []).append((r[1], r[0]))
            line.append('%-15s' % ('%.2f%%/%.2f%%' % (r[1], r[0] * 100)))
        print('%-42s %s' % (mark[:42], '  '.join(line)), flush=True)
        rows_out.append((mark, per))

    # ---- 汇总 ----
    lines = ['# 离线后端对照实验', '',
             '数值为**反算线宽误差%**（端到端）与 lnZ 误差%。',
             '留出集（`data/holdout.jsonl`）从不参与拟合与特征选择，是**唯一无偏**的口径。', '',
             '| 后端 | 线宽·平均 | 线宽·中位 | 线宽·RMS | lnZ·平均 | 结构数 |',
             '|---|---|---|---|---|---|']
    for name in BACKEND_ORDER:
        if name not in acc:
            continue
        w = [x[0] for x in acc[name]]
        l = [x[1] for x in acc[name]]
        lines.append('| **%s** | %.2f%% | %.2f%% | %.2f%% | %.2f%% | %d |'
                     % (name, sum(w) / len(w), C.median(w), C.rms(w),
                        100 * sum(l) / len(l), len(w)))
    lines += ['', '## 逐结构（留出集反算线宽误差）', '',
              '| 结构 | ' + ' | '.join(BACKEND_ORDER) + ' |',
              '|---' * (len(BACKEND_ORDER) + 1) + '|']
    for mark, r in rows_out:
        cells = ['%.2f%%' % r[n][1] if n in r else '—' for n in BACKEND_ORDER]
        lines.append('| `%s` | %s |' % (mark, ' | '.join(cells)))
    with open(OUT, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines) + '\n')
    with open(OUT_JSON, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump({m: {k: list(v) for k, v in r.items()} for m, r in rows_out},
                  fh, ensure_ascii=False, indent=1)
    print('\n→ %s\n→ %s' % (OUT, OUT_JSON))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
