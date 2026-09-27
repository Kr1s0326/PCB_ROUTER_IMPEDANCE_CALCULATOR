# -*- coding: utf-8 -*-
"""把 ``reports/validation_all.json`` 画成一张 SVG 校验图。

**零依赖**：不引入 matplotlib，直接按需拼 SVG 字符串。这样报告图能在任何
环境（含 CI）里确定性重建，也符合本项目「运行时零依赖」的一贯做法。

四个面板：

* ① **正算 parity** —— 同一线宽下，离线阻抗 vs JLC 阻抗，附 ±1% / ±2% 参考带
* ② **误差累计分布** —— 正算与反算的 |相对误差| CDF
* ③ **反算误差直方图** —— 离线解出的线宽与 JLC 解出的线宽之差
* ④ **按层数** —— 2/4/6/8/10 层的平均反算误差

用法::

    python tools/make_validation_figure.py
"""

from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402

OUT_DIR = os.path.join(C.REPORTS, 'figures')
OUT_SVG = os.path.join(OUT_DIR, 'validation.svg')

# ---- 配色 ----------------------------------------------------------------
INK = '#1f2937'
MUTED = '#6b7280'
GRID = '#e5e7eb'
PANEL_BG = '#fbfbfe'
PANEL_EDGE = '#e3e4ee'
BLUE = '#2563eb'
ORANGE = '#ea580c'
RED = '#dc2626'
GREEN = '#059669'
BAND1 = '#2563eb'
BAND2 = '#93c5fd'


# =========================================================================
# 极简 SVG 画布
# =========================================================================
def _esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


class Canvas:
    """够用就好的 SVG 画布：矩形、折线、圆点、文字。"""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.parts = []

    def rect(self, x, y, w, h, fill='none', stroke='none', sw=1, rx=0, op=None):
        s = '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" rx="%.1f"' % (
            x, y, max(w, 0), max(h, 0), rx)
        s += ' fill="%s"' % fill
        if stroke != 'none':
            s += ' stroke="%s" stroke-width="%.2f"' % (stroke, sw)
        if op is not None:
            s += ' opacity="%.3f"' % op
        self.parts.append(s + '/>')
        return self

    def line(self, x1, y1, x2, y2, stroke=GRID, sw=1, dash=None, op=None):
        s = '<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="%s" stroke-width="%.2f"' % (
            x1, y1, x2, y2, stroke, sw)
        if dash:
            s += ' stroke-dasharray="%s"' % dash
        if op is not None:
            s += ' opacity="%.3f"' % op
        self.parts.append(s + '/>')
        return self

    def poly(self, pts, stroke=BLUE, sw=1.6, fill='none', close=False, op=None):
        d = ' '.join('%.2f,%.2f' % (x, y) for x, y in pts)
        tag = 'polygon' if close else 'polyline'
        s = '<%s points="%s" fill="%s" stroke="%s" stroke-width="%.2f"' % (
            tag, d, fill, stroke, sw)
        s += ' stroke-linejoin="round"'
        if op is not None:
            s += ' opacity="%.3f"' % op
        self.parts.append(s + '/>')
        return self

    def circle(self, cx, cy, r, fill=BLUE, stroke='none', sw=1, op=None):
        s = '<circle cx="%.2f" cy="%.2f" r="%.2f" fill="%s"' % (cx, cy, r, fill)
        if stroke != 'none':
            s += ' stroke="%s" stroke-width="%.2f"' % (stroke, sw)
        if op is not None:
            s += ' opacity="%.3f"' % op
        self.parts.append(s + '/>')
        return self

    def text(self, x, y, s, size=11, fill=INK, anchor='start', weight='normal',
             family='sans-serif', op=None):
        t = ('<text x="%.2f" y="%.2f" font-size="%.1f" fill="%s" '
             'font-family="%s" text-anchor="%s"' % (x, y, size, fill, family, anchor))
        if weight != 'normal':
            t += ' font-weight="%s"' % weight
        if op is not None:
            t += ' opacity="%.3f"' % op
        self.parts.append(t + '>%s</text>' % _esc(s))
        return self

    def render(self):
        head = ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
                'viewBox="0 0 %d %d">' % (self.w, self.h, self.w, self.h))
        style = (
            '<style>text{font-family:-apple-system,"Segoe UI","Microsoft YaHei",'
            '"PingFang SC","Noto Sans CJK SC",sans-serif;}</style>')
        return (head + '<rect width="100%" height="100%" fill="#ffffff"/>' + style
                + ''.join(self.parts) + '</svg>')


# =========================================================================
# 坐标与刻度
# =========================================================================
class Lin:
    """线性映射 data -> pixel。"""

    def __init__(self, d0, d1, r0, r1):
        self.d0, self.d1, self.r0, self.r1 = d0, d1, r0, r1
        self.k = (r1 - r0) / (d1 - d0) if d1 != d0 else 0.0

    def __call__(self, v):
        return self.r0 + (v - self.d0) * self.k

    def inv(self, p):
        return self.d0 + (p - self.r0) / self.k if self.k else self.d0


def nice_ticks(lo, hi, target=5):
    """返回落在 [lo, hi] 内的「好看」刻度值。"""
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if raw <= step:
            break
    start = math.ceil(lo / step) * step
    out, v = [], start
    while v <= hi + step * 1e-6:
        out.append(round(v, 10))
        v += step
    return out


def fmt(v, nd=0):
    s = ('%.*f' % (nd, v))
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or '0'


# =========================================================================
# 面板
# =========================================================================
def panel(c, x, y, w, h, title, pad_l=54, pad_r=16, pad_t=34, pad_b=42):
    """画一个带标题的面板，返回内部绘图区 (px, py, pw, ph)。"""
    c.rect(x, y, w, h, fill=PANEL_BG, stroke=PANEL_EDGE, sw=1, rx=8)
    c.rect(x, y, 3.5, h, fill=BLUE, rx=2)
    c.text(x + 14, y + 22, title, size=12.5, weight='600', fill=INK)
    px, py = x + pad_l, y + pad_t
    pw, ph = w - pad_l - pad_r, h - pad_t - pad_b
    return px, py, pw, ph


def axes(c, px, py, pw, ph, xscale, yscale, xticks, yticks, xfmt, yfmt,
         xlabel='', ylabel=''):
    """网格 + 刻度 + 轴标签。"""
    for t in yticks:
        yy = yscale(t)
        c.line(px, yy, px + pw, yy, stroke=GRID, sw=1)
        c.text(px - 7, yy + 3.5, yfmt(t), size=10, fill=MUTED, anchor='end')
    for t in xticks:
        xx = xscale(t)
        c.line(xx, py, xx, py + ph, stroke=GRID, sw=1)
        c.text(xx, py + ph + 15, xfmt(t), size=10, fill=MUTED, anchor='middle')
    c.line(px, py, px, py + ph, stroke=MUTED, sw=1)
    c.line(px, py + ph, px + pw, py + ph, stroke=MUTED, sw=1)
    if xlabel:
        c.text(px + pw / 2, py + ph + 33, xlabel, size=11, fill=INK, anchor='middle')
    if ylabel:
        c.text(px - 40, py + ph / 2, ylabel, size=11, fill=INK, anchor='middle')


def cdf(vals, xmax):
    """返回 (x, y%) 阶梯折线，用于 CDF。分母是整个样本，超出 xmax 的部分在末端截断。"""
    a = sorted(abs(v) for v in vals)
    n = len(a)
    pts = [(0.0, 0.0)]
    for i, v in enumerate(a, 1):
        if v > xmax:
            break
        pts.append((v, 100.0 * i / n))
    pts.append((xmax, pts[-1][1]))          # 水平收尾到右边界
    return pts


def histogram(vals, xmax, binw):
    nb = max(int(math.ceil(xmax / binw)), 1)
    bins = [0] * nb
    for v in vals:
        i = int(abs(v) // binw)
        bins[min(i, nb - 1)] += 1
    return bins, binw


# =========================================================================
# 主流程
# =========================================================================
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='生成离线 vs JLC 的校验图（SVG）')
    ap.add_argument('--out', default=OUT_SVG, help='输出 SVG 路径')
    ap.add_argument('--data', default=os.path.join(C.REPORTS, 'validation_all.json'),
                    help='输入的校验结果 JSON')
    args = ap.parse_args(argv)

    with open(args.data, encoding='utf-8') as fh:
        data = json.load(fh)
    rows = [r for r in data['rows'] if 'w_err' in r]
    skipped = len(data['rows']) - len(rows)

    z_online = [r['z_online'] for r in rows]
    z_off = [r['z_offline_at_wonline'] for r in rows]
    z_err = [r['z_err'] for r in rows]
    w_err = [r['w_err'] for r in rows]
    kinds = [r['kind'] for r in rows]

    n = len(rows)
    mean = lambda v: sum(abs(x) for x in v) / len(v)  # noqa: E731
    median = lambda v: sorted(abs(x) for x in v)[len(v) // 2]  # noqa: E731
    pct_le = lambda v, t: 100.0 * sum(1 for x in v if abs(x) <= t) / len(v)  # noqa: E731

    W, H = 1140, 800
    c = Canvas(W, H)

    # --- 大标题 ---------------------------------------------------------
    c.text(24, 34, '离线引擎 vs 嘉立创在线引擎 — 多层实测校验', size=17, weight='700')
    c.text(24, 55, ('真实叠层：2 / 4 / 6 / 8 / 10 层  ·  单端与差分  ·  50 / 75 / 90 / 100 Ω  ·  '
                    '有效样本 %d 组（另有 %d 组 JLC 引擎自身无解，已剔除）' % (n, skipped)),
           size=11, fill=MUTED)

    ML, MR, MT, MB, GX, GY = 26, 26, 78, 24, 24, 22
    pw = (W - ML - MR - GX) / 2.0
    ph = (H - MT - MB - GY) / 2.0

    # =====================================================================
    # ① 正算 parity
    # =====================================================================
    x0, y0 = ML, MT
    px, py, iw, ih = panel(c, x0, y0, pw, ph,
                           '① 正算一致性：同一线宽下 离线阻抗 vs JLC 阻抗')
    lo = min(min(z_online), min(z_off)) * 0.96
    hi = max(max(z_online), max(z_off)) * 1.04
    sx = Lin(lo, hi, px, px + iw)
    sy = Lin(lo, hi, py + ih, py)
    ticks = nice_ticks(lo, hi, 5)
    axes(c, px, py, iw, ih, sx, sy, ticks, ticks, fmt, fmt,
         xlabel='JLC 在线引擎  Z (Ω)', ylabel='离线引擎  Z (Ω)')

    # ±2% / ±1% 参考带（以 y=x 为中心的比例带）
    for frac, col, op in ((0.02, BAND2, 0.30), (0.01, BAND1, 0.20)):
        up = [(sx(z), sy(z * (1 + frac))) for z in ticks]
        dn = [(sx(z), sy(z * (1 - frac))) for z in reversed(ticks)]
        c.poly(up + dn, stroke='none', fill=col, close=True, op=op)
    c.line(sx(lo), sy(lo), sx(hi), sy(hi), stroke=MUTED, sw=1.2, dash='5,4')

    for z, zo, k in zip(z_online, z_off, kinds):
        c.circle(sx(z), sy(zo), 2.6,
                 fill=BLUE if k == 'single' else ORANGE, op=0.62)

    lx, ly = px + 12, py + 14
    for i, (col, lab) in enumerate(((BLUE, '单端'), (ORANGE, '差分'))):
        c.circle(lx, ly + i * 16, 4, fill=col, op=0.8)
        c.text(lx + 9, ly + i * 16 + 3.5, lab, size=10.5, fill=INK)
    c.text(px + iw - 6, py + 18,
           '±1%%: %.0f%%   ±2%%: %.0f%%   平均 %.2f%%'
           % (pct_le(z_err, 1), pct_le(z_err, 2), mean(z_err)),
           size=10, fill=MUTED, anchor='end')

    # =====================================================================
    # ② CDF
    # =====================================================================
    x0, y0 = ML + pw + GX, MT
    px, py, iw, ih = panel(c, x0, y0, pw, ph, '② 误差累计分布（|相对误差|）')
    xmax = 8.0
    sx = Lin(0, xmax, px, px + iw)
    sy = Lin(0, 100, py + ih, py)
    axes(c, px, py, iw, ih, sx, sy,
         nice_ticks(0, xmax, 4), [0, 20, 40, 60, 80, 100],
         lambda t: fmt(t) + '%', lambda t: fmt(t) + '%',
         xlabel='|误差|', ylabel='累计占比')

    for v, lab, col in ((median(z_err), '正算', BLUE), (median(w_err), '反算', RED)):
        c.line(sx(v), sy(0), sx(v), sy(100), stroke=col, sw=1, dash='3,3', op=0.5)

    for vals, lab, col in ((z_err, '正算 Z', BLUE), (w_err, '反算线宽', RED)):
        pts = [(sx(x), sy(y)) for x, y in cdf(vals, xmax)]
        c.poly(pts, stroke=col, sw=2.2)

    def x_at_y(vals, y_target):
        for x, y in cdf(vals, xmax):
            if y >= y_target:
                return x
        return xmax

    c.text(sx(x_at_y(z_err, 46)) + 6, sy(46) - 4, '正算 Z', size=10.5, fill=BLUE,
           weight='600')
    c.text(sx(x_at_y(w_err, 88)) + 6, sy(88) - 4, '反算线宽', size=10.5, fill=RED,
           weight='600')

    ly = py + ih - 44
    for i, (vals, lab, col) in enumerate(((z_err, '正算', BLUE), (w_err, '反算', RED))):
        c.line(px + 12, ly + i * 22, px + 34, ly + i * 22, stroke=col, sw=2.4)
        c.text(px + 40, ly + i * 22 + 3.5,
               '%s  中位 %.2f%%   ≤2%%: %.0f%%' % (lab, median(vals), pct_le(vals, 2)),
               size=10.5, fill=INK)

    # =====================================================================
    # ③ 直方图
    # =====================================================================
    x0, y0 = ML, MT + ph + GY
    px, py, iw, ih = panel(c, x0, y0, pw, ph, '③ 反算线宽误差分布')
    binw = 0.5
    bins, bw = histogram(w_err, xmax, binw)
    ymax = max(bins) * 1.18
    sx = Lin(0, xmax, px, px + iw)
    sy = Lin(0, ymax, py + ih, py)
    axes(c, px, py, iw, ih, sx, sy,
         nice_ticks(0, xmax, 4), nice_ticks(0, ymax, 4),
         lambda t: fmt(t) + '%', fmt,
         xlabel='|反算线宽误差|', ylabel='案例数')

    for i, cnt in enumerate(bins):
        if not cnt:
            continue
        bx = sx(i * bw)
        bwpx = sx((i + 1) * bw) - bx
        col = BLUE if (i + 0.5) * bw <= 2.0 else ORANGE
        c.rect(bx + 0.8, sy(cnt), max(bwpx - 1.6, 1), py + ih - sy(cnt),
               fill=col, op=0.55, rx=2)
    mv = median(w_err)
    c.line(sx(mv), py, sx(mv), py + ih, stroke=RED, sw=1.4, dash='4,3')
    c.text(sx(mv) + 5, py + 13, '中位 %.2f%%' % mv, size=10.5, fill=RED, weight='600')
    inside = sum(cnt for i, cnt in enumerate(bins) if (i + 0.5) * bw <= 2.0)
    c.text(px + iw - 6, py + 14, '≤2%% 占 %.0f%%' % (100.0 * inside / n),
           size=10.5, fill=MUTED, anchor='end')

    # =====================================================================
    # ④ 按层数
    # =====================================================================
    x0, y0 = ML + pw + GX, MT + ph + GY
    px, py, iw, ih = panel(c, x0, y0, pw, ph, '④ 分层表现（反算线宽误差）')
    layers = sorted({r['layers'] for r in rows})
    stat = []
    for L in layers:
        v = [r['w_err'] for r in rows if r['layers'] == L]
        stat.append((L, mean(v), median(v), len(v)))
    ymax = max(s[1] for s in stat) * 1.35
    sx = Lin(0, len(stat), px, px + iw)
    sy = Lin(0, ymax, py + ih, py)
    axes(c, px, py, iw, ih, sx, sy, [],
         nice_ticks(0, ymax, 4), fmt, lambda t: fmt(t, 1) + '%',
         xlabel='板层数', ylabel='平均 |误差|')

    for i, (L, m, med, cnt) in enumerate(stat):
        wpx = iw / len(stat)
        bx = px + i * wpx + wpx * 0.24
        bwd = wpx * 0.52
        c.rect(bx, sy(m), bwd, py + ih - sy(m), fill=BLUE, op=0.55, rx=3)
        c.rect(bx, sy(m), bwd, 2.5, fill=BLUE, rx=1)
        c.text(bx + bwd / 2, sy(m) - 6, '%.2f%%' % m, size=10.5,
               fill=INK, anchor='middle', weight='600')
        c.text(bx + bwd / 2, py + ih + 15, '%d 层' % L, size=10, fill=MUTED,
               anchor='middle')
        c.text(bx + bwd / 2, py + ih + 28, 'n=%d' % cnt, size=9, fill=MUTED,
               anchor='middle')

    c.text(W - 26, H - 8,
           '数据：reports/validation_all.json  ·  由 tools/make_validation_figure.py 生成',
           size=9.5, fill=MUTED, anchor='end')

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as fh:
        fh.write(c.render())

    print('正算  平均 %.3f%%  中位 %.3f%%  ≤2%%: %.0f%%  n=%d'
          % (mean(z_err), median(z_err), pct_le(z_err, 2), n))
    print('反算  平均 %.3f%%  中位 %.3f%%  ≤2%%: %.0f%%  n=%d'
          % (mean(w_err), median(w_err), pct_le(w_err, 2), n))
    print('→ %s (%.1f KB)' % (args.out, os.path.getsize(args.out) / 1024.0))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
