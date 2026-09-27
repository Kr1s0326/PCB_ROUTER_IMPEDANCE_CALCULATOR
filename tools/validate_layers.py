"""全层数校验：离线模型 vs 嘉立创官网，输出 HTML 报告。

覆盖 2 / 4 / 6 / 8 / 10 层，每种层数取官网的通用叠层，每一层铜都测：
单端 50Ω、单端 75Ω、差分 90Ω@S1=8、差分 100Ω@S1=8。

每个用例记录三个指标：
  * 反算线宽误差   （离线解出的线宽 vs 官网解出的线宽）—— 端到端
  * 正算阻抗误差   （在官网线宽处，离线算出的阻抗 vs 目标阻抗）—— 纯模型
  * 是否收敛

用法::

    python tools/validate_layers.py              # 全部，约 230 个请求
    python tools/validate_layers.py --quick      # 每层只测单端+差分
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import random
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C                                              # noqa: E402
from fetch_stackups import load_cache                            # noqa: E402
from jlc_impedance.api import JlcApi, JlcApiError                # noqa: E402
from jlc_impedance.calculator import ImpedanceCalculator         # noqa: E402
from jlc_impedance.stackup import Stackup                        # noqa: E402

OUT_JSON = os.path.join(C.REPORTS, 'validation_all.json')
OUT_HTML = os.path.join(C.REPORTS, 'validation_report.html')

# 每种层数选官网的「通用 / 免费 / 普通」叠层
SELECT = {
    2: ['JLC0216A'],
    4: ['JLC04161H-7628', 'JLC04161H-3313'],
    6: ['JLC06161H-7628', 'JLC06161H-3313'],
    8: ['JLC08161H-7628', 'JLC08161H-2116'],
    10: ['JLC10161H-7628', 'JLC10161H-2116'],
}
CASES_ALL = [('single', 50.0, None), ('single', 75.0, None),
             ('diff', 90.0, 8.0), ('diff', 100.0, 8.0)]
CASES_QUICK = [('single', 50.0, None), ('diff', 100.0, 8.0)]
#: 嘉立创 4 层板最小线宽约 0.09mm ≈ 3.5mil；比这更细的算例没有工艺意义，
#: 而且 dZ/dW 极陡会把反算误差放大，所以单独统计。
MIN_USABLE_MIL = 4.0


def pick_stackup(cache, n, key):
    """按名字前缀在缓存里找叠层。"""
    for it in cache.get(str(n), []):
        name = it.get('receptionDisplayName') or it.get('appointName') or ''
        if name.startswith(key):
            return Stackup.from_template(it), name
    return None, None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--quick', action='store_true')
    ap.add_argument('--layers', default=None, help='只测这些层数，如 2,4')
    ap.add_argument('--html-only', action='store_true', help='只从 JSON 重新生成 HTML')
    ap.add_argument('--min-delay', type=float, default=0.9)
    ap.add_argument('--max-delay', type=float, default=1.6)
    args = ap.parse_args(argv)
    cases = CASES_QUICK if args.quick else CASES_ALL

    if args.html_only:
        with open(OUT_JSON, encoding='utf-8') as fh:
            saved = json.load(fh)
        write_html(saved['rows'], saved['meta'])
        print('→ %s' % OUT_HTML)
        return 0

    cache = load_cache()
    api = JlcApi()
    copper = api.config_copper()
    coverlay = api.config_coverlay()
    sleep = lambda: time.sleep(random.uniform(args.min_delay, args.max_delay))   # noqa: E731

    want = None
    if args.layers:
        want = {int(x) for x in args.layers.split(',')}

    rows = []
    n_req = 0
    t0 = time.time()
    try:
        for n in sorted(SELECT):
            if want and n not in want:
                continue
            for key in SELECT[n]:
                st, name = pick_stackup(cache, n, key)
                if st is None:
                    print('找不到叠层 %s (%d层)' % (key, n), flush=True)
                    continue
                on = ImpedanceCalculator(st, api=api, copper_config=copper,
                                         coverlay_config=coverlay)
                off = ImpedanceCalculator(st, copper_config=copper,
                                          coverlay_config=coverlay)
                for li in range(1, n + 1):
                    layer = 'L%d' % li
                    outer = st.is_outer(li - 1)
                    for kind, target, spacing in cases:
                        rec = {'layers': n, 'stackup': name, 'key': key, 'layer': layer,
                               'outer': outer, 'kind': kind, 'target': target,
                               'spacing': spacing}
                        try:
                            ro = on.solve(layer, target, kind=kind, spacing=spacing)
                            n_req += 1
                            sleep()
                        except JlcApiError as exc:
                            rec['error'] = 'online: %s' % exc
                            rows.append(rec)
                            continue
                        if ro.width is None:
                            rec['error'] = 'online 无解 (status=%s) %s' % (ro.status, ro.error[:60])
                            rows.append(rec)
                            continue
                        rec['w_online'] = ro.width
                        rec['z_online'] = ro.impedance
                        # 离线：正算（在官网线宽处） + 反算
                        rf = off.forward(layer, width=ro.width, kind=kind,
                                         spacing=spacing)
                        rec['z_offline_at_wonline'] = rf.impedance
                        rec['z_err'] = (None if rf.impedance is None
                                        else 100.0 * (rf.impedance - target) / target)
                        rs = off.solve(layer, target, kind=kind, spacing=spacing)
                        if rs.width is None:
                            rec['error'] = 'offline 无解'
                            rows.append(rec)
                            continue
                        rec['w_offline'] = rs.width
                        rec['w_err'] = 100.0 * (rs.width - ro.width) / ro.width
                        rec['w_err_mil'] = rs.width - ro.width
                        rows.append(rec)
                        print('[%3d] %2d层 %-22s %-4s %-6s %5.0fΩ  W_on=%7.3f '
                              'W_off=%7.3f  Δ=%+6.2f%%  ΔZ=%+5.2f%%'
                              % (len(rows), n, key[:22], layer, kind, target,
                                 ro.width, rs.width, rec['w_err'],
                                 rec.get('z_err') or 0), flush=True)
    finally:
        api.close()

    meta = {'generated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'requests': n_req, 'seconds': round(time.time() - t0, 1),
            'cases_per_layer': len(cases)}
    with open(OUT_JSON, 'w', encoding='utf-8') as fh:
        json.dump({'meta': meta, 'rows': rows}, fh, ensure_ascii=False, indent=1)
    write_html(rows, meta)
    print('\n→ %s\n→ %s\n（%d 个请求，%.0f 秒）'
          % (OUT_JSON, OUT_HTML, n_req, time.time() - t0))
    return 0


# --------------------------------------------------------------------------- #
#  HTML 报告
# --------------------------------------------------------------------------- #
CSS = """
:root{--fg:#1a1a1a;--mut:#6b7280;--line:#e5e7eb;--bg:#fff;--ok:#059669;
--warn:#d97706;--bad:#dc2626;--acc:#2563eb;--card:#f9fafb}
*{box-sizing:border-box}
body{margin:0;padding:28px 32px;font:14px/1.55 -apple-system,"Segoe UI",
"Microsoft YaHei",system-ui,sans-serif;color:var(--fg);background:var(--bg);
max-width:1400px}
h1{font-size:23px;margin:0 0 4px}
h2{font-size:17px;margin:30px 0 10px;padding-bottom:6px;
border-bottom:1px solid var(--line)}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:9px;
padding:12px 16px;min-width:132px}
.card .k{font-size:12px;color:var(--mut)}
.card .v{font-size:21px;font-weight:600;margin-top:2px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 4px}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:right;
white-space:nowrap}
th{background:var(--card);font-weight:600;color:#374151;position:sticky;top:0}
th:first-child,td:first-child,th.l,td.l{text-align:left}
tr:hover td{background:#fbfdff}
.ok{color:var(--ok);font-weight:600}
.warn{color:var(--warn);font-weight:600}
.bad{color:var(--bad);font-weight:600}
.mut{color:var(--mut)}
details{margin:10px 0}
summary{cursor:pointer;font-weight:600;padding:6px 0}
.note{background:#fffbeb;border-left:3px solid var(--warn);padding:10px 14px;
border-radius:0 6px 6px 0;margin:14px 0;font-size:13px}
.legend{font-size:12px;color:var(--mut);margin:6px 0 0}
.card.hl{background:#eff6ff;border-color:#bfdbfe}
"""


def _cls(v, good=1.0, ok=3.0):
    if v is None:
        return 'mut'
    a = abs(v)
    return 'ok' if a <= good else ('warn' if a <= ok else 'bad')


def _f(v, n=3):
    return '—' if v is None else ('%.*f' % (n, v))


def _median(v):
    s = sorted(v)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _bar_chart(pairs, title, unit='%', w=780, h=210, good=1.0, ok=3.0):
    """内联 SVG 柱状图，无外部依赖。"""
    if not pairs:
        return ''
    pad_l, pad_b, pad_t = 46, 42, 16
    plot_h = h - pad_b - pad_t
    n = len(pairs)
    bw = (w - pad_l - 16) / n
    mx = max(v for _, v in pairs) or 1.0
    step = max(0.5, math.ceil(mx * 2) / 2 / 4)
    top = step * math.ceil(mx / step)
    out = ['<svg viewBox="0 0 %d %d" width="100%%" style="max-width:%dpx;'
           'font:11px system-ui,sans-serif">' % (w, h, w)]
    out.append('<text x="0" y="12" font-size="12" font-weight="600" fill="#374151">%s</text>'
               % html.escape(title))
    # 网格
    g = 0.0
    while g <= top + 1e-9:
        y = pad_t + plot_h - (g / top) * plot_h
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#eef1f5"/>'
                   % (pad_l, y, w - 10, y))
        out.append('<text x="%d" y="%.1f" text-anchor="end" fill="#9ca3af">%.1f%s</text>'
                   % (pad_l - 6, y + 4, g, unit))
        g += step
    for i, (label, v) in enumerate(pairs):
        x = pad_l + i * bw + bw * 0.18
        bh = (v / top) * plot_h if top else 0
        y = pad_t + plot_h - bh
        col = '#059669' if v <= good else ('#d97706' if v <= ok else '#dc2626')
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" fill="%s" '
                   'opacity="0.85"/>' % (x, y, bw * 0.64, bh, col))
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" fill="#374151" '
                   'font-weight="600">%.2f</text>' % (x + bw * 0.32, y - 4, v))
        out.append('<text x="%.1f" y="%d" text-anchor="middle" fill="#6b7280">%s</text>'
                   % (x + bw * 0.32, h - pad_b + 15, html.escape(label)))
    out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#d1d5db"/>'
               % (pad_l, pad_t + plot_h, w - 10, pad_t + plot_h))
    out.append('</svg>')
    return ''.join(out)


def write_html(rows, meta):
    good = [r for r in rows if r.get('w_err') is not None]
    usable = [r for r in good if (r.get('w_online') or 0) >= MIN_USABLE_MIL]
    thin = [r for r in good if (r.get('w_online') or 0) < MIN_USABLE_MIL]
    errs = [abs(r['w_err']) for r in good]
    uerrs = [abs(r['w_err']) for r in usable]
    zerrs = [abs(r['z_err']) for r in good if r.get('z_err') is not None]
    n_err = len(rows) - len(good)
    avg = sum(errs) / len(errs) if errs else 0
    med = _median(errs)
    mx = max(errs) if errs else 0
    uavg = sum(uerrs) / len(uerrs) if uerrs else 0
    p = []
    A = p.append
    A('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">')
    A('<title>离线模型 vs 嘉立创官网 · 全层数校验</title>')
    A('<style>%s</style></head><body>' % CSS)
    A('<h1>离线模型 vs 嘉立创官网 · 全层数校验</h1>')
    A('<div class="sub">生成时间 %s &nbsp;·&nbsp; %d 个在线请求 / %.0f 秒 '
      '&nbsp;·&nbsp; 覆盖 2 / 4 / 6 / 8 / 10 层 &nbsp;·&nbsp; 每一层铜都测'
      '（单端 50Ω、75Ω，差分 90Ω、100Ω @S1=8mil）</div>'
      % (html.escape(meta['generated']), meta['requests'], meta['seconds']))
    A('<div class="cards">')
    for k, v, c in (('用例总数', '%d' % len(rows), ''),
                    ('成功对比', '%d' % len(good), ''),
                    ('线宽误差 · 平均', '%.2f%%' % avg, _cls(avg)),
                    ('线宽误差 · 中位', '%.2f%%' % med, _cls(med)),
                    ('线宽误差 · 最大', '%.2f%%' % mx, _cls(mx)),
                    ('** 排除 W&lt;%gmil 后平均' % MIN_USABLE_MIL,
                     '%.2f%%' % uavg, _cls(uavg)),
                    ('阻抗误差 · 平均',
                     '%.2f%%' % (sum(zerrs) / len(zerrs) if zerrs else 0),
                     _cls(sum(zerrs) / len(zerrs) if zerrs else 0)),
                    ('无解/失败', '%d' % n_err, 'bad' if n_err else '')):
        cls = c + (' hl' if k.startswith('**') else '')
        kk = k.replace('** ', '')
        A('<div class="card%s"><div class="k">%s</div><div class="v %s">%s</div></div>'
          % (' hl' if k.startswith('**') else '', kk, c, v))
    A('</div>')
    if thin:
        A('<div class="note"><b>%d 个算例的官网解出线宽 &lt; %g mil</b>'
          '（75Ω 落在薄介质内层上），已经低于嘉立创的工艺下限'
          '（4 层约 3.5 mil），没有实际制造意义；而且那里 dZ/dW 极陡，'
          '会把反算误差放大。所以上表单列了「排除后」的平均误差。'
          '明细表里这些行标了 ⚠。</div>'
          % (len(thin), MIN_USABLE_MIL))


    # ---- 图表 ----
    by_layer = {}
    for r in good:
        by_layer.setdefault(r['layers'], []).append(abs(r['w_err']))
    A('<h2>误差图</h2>')
    A('<div style="display:flex;gap:24px;flex-wrap:wrap">')
    A('<div style="flex:1;min-width:340px">%s</div>'
      % _bar_chart([('%d 层' % n, sum(v) / len(v)) for n, v in sorted(by_layer.items())],
                   '按层数 · 平均线宽误差'))
    A('<div style="flex:1;min-width:340px">%s</div>'
      % _bar_chart([('单端 50Ω', sum(abs(r['w_err']) for r in good
                                    if r['kind'] == 'single' and r['target'] == 50)
                   / max(1, sum(1 for r in good if r['kind'] == 'single'
                                and r['target'] == 50))),
                   ('单端 75Ω', sum(abs(r['w_err']) for r in good
                                    if r['kind'] == 'single' and r['target'] == 75)
                   / max(1, sum(1 for r in good if r['kind'] == 'single'
                                and r['target'] == 75))),
                   ('差分 90Ω', sum(abs(r['w_err']) for r in good
                                    if r['kind'] == 'diff' and r['target'] == 90)
                   / max(1, sum(1 for r in good if r['kind'] == 'diff'
                                and r['target'] == 90))),
                   ('差分 100Ω', sum(abs(r['w_err']) for r in good
                                     if r['kind'] == 'diff' and r['target'] == 100)
                   / max(1, sum(1 for r in good if r['kind'] == 'diff'
                                and r['target'] == 100)))],
                   '按用例类型 · 平均线宽误差'))
    A('</div>')
    A('<div style="display:flex;gap:24px;flex-wrap:wrap">')
    A('<div style="flex:1;min-width:340px">%s</div>'
      % _bar_chart([('外层', sum(abs(r['w_err']) for r in good if r['outer'])
                     / max(1, sum(1 for r in good if r['outer']))),
                    ('内层', sum(abs(r['w_err']) for r in good if not r['outer'])
                     / max(1, sum(1 for r in good if not r['outer'])))],
                   '外层 vs 内层 · 平均线宽误差', w=380))
    A('<div style="flex:1;min-width:340px">%s</div>'
      % _bar_chart(sorted(('%s' % r['layer'], abs(r['w_err']))
                          for r in good if r['layers'] == max(by_layer))[:14],
                   '最大层数（%d 层）逐层误差' % max(by_layer)))
    A('</div>')

    # ---- 按层数汇总 ----
    A('<h2>按层数汇总</h2><table><tr><th class="l">层数</th><th>叠层</th>'
      '<th>用例</th><th>平均线宽误差</th><th>中位</th><th>最大</th>'
      '<th>排除超细线后</th><th>平均阻抗误差</th><th>失败</th></tr>')
    for n in sorted({r['layers'] for r in rows}):
        sub = [r for r in rows if r['layers'] == n]
        g = [r for r in sub if r.get('w_err') is not None]
        e = [abs(r['w_err']) for r in g]
        ue = [abs(r['w_err']) for r in g if (r.get('w_online') or 0) >= MIN_USABLE_MIL]
        ze = [abs(r['z_err']) for r in g if r.get('z_err') is not None]
        stacks = len({r['key'] for r in sub})
        A('<tr><td class="l"><b>%d 层</b></td><td>%d 个</td><td>%d</td>'
          '<td class="%s">%.2f%%</td><td>%.2f%%</td><td class="%s">%.2f%%</td>'
          '<td class="%s">%.2f%%</td><td>%.2f%%</td><td>%d</td></tr>'
          % (n, stacks, len(sub), _cls(sum(e) / len(e) if e else 0),
             sum(e) / len(e) if e else 0,
             _median(e), _cls(max(e) if e else 0),
             max(e) if e else 0, _cls(sum(ue) / len(ue) if ue else 0),
             sum(ue) / len(ue) if ue else 0, sum(ze) / len(ze) if ze else 0,
             len(sub) - len(g)))
    A('</table>')

    # ---- 按层别汇总 ----
    A('<h2>按层别汇总（外层 / 内层）</h2><table><tr><th class="l">类型</th>'
      '<th>用例</th><th>平均线宽误差</th><th>中位</th><th>最大</th></tr>')
    for tag, sel in (('外层（微带线）', True), ('内层（带状线）', False)):
        sub = [r for r in rows if r['outer'] == sel and r.get('w_err') is not None]
        e = [abs(r['w_err']) for r in sub]
        A('<tr><td class="l">%s</td><td>%d</td><td class="%s">%.2f%%</td>'
          '<td>%.2f%%</td><td class="%s">%.2f%%</td></tr>'
          % (tag, len(sub), _cls(sum(e) / len(e) if e else 0),
             sum(e) / len(e) if e else 0,
             _median(e), _cls(max(e) if e else 0),
             max(e) if e else 0))
    A('</table>')

    # ---- 误差分布 ----
    A('<h2>误差分布</h2><table><tr><th class="l">线宽误差区间</th><th>用例数</th>'
      '<th>占比</th></tr>')
    bins = [(0, 0.5), (0.5, 1), (1, 2), (2, 5), (5, 1e9)]
    for lo, hi in bins:
        c = sum(1 for e in errs if lo <= e < hi)
        label = '≤0.5%' if hi == 0.5 else ('0.5~1%' if lo == 0.5 else
                                           ('1~2%' if lo == 1 else
                                            ('2~5%' if lo == 2 else '>5%')))
        A('<tr><td class="l %s">%s</td><td>%d</td><td>%.1f%%</td></tr>'
          % (_cls((lo + min(hi, 9)) / 2), label, c,
             100.0 * c / len(errs) if errs else 0))
    A('</table>')

    # ---- 明细 ----
    A('<h2>明细（按层数 / 叠层 / 层别）</h2>')
    A('<div class="legend">线宽误差 = (离线线宽 − 官网线宽)/官网线宽；'
      '阻抗误差 = 在官网线宽处离线算出的阻抗与目标阻抗之差。</div>')
    for n in sorted({r['layers'] for r in rows}):
        for key in dict.fromkeys(r['key'] for r in rows if r['layers'] == n):
            sub = [r for r in rows if r['layers'] == n and r['key'] == key]
            name = sub[0]['stackup']
            e = [abs(r['w_err']) for r in sub if r.get('w_err') is not None]
            head = ('%.2f%%' % (sum(e) / len(e))) if e else '—'
            A('<details open><summary>%d 层 · %s &nbsp;<span class="mut">'
              '(%d 个用例，平均误差 %s)</span></summary>'
              % (n, html.escape(name), len(sub), head))
            A('<table><tr><th class="l">层</th><th class="l">位置</th>'
              '<th class="l">类型</th><th>目标 Ω</th><th>线距 mil</th>'
              '<th>官网线宽 mil</th><th>离线线宽 mil</th><th>Δ mil</th>'
              '<th>线宽误差</th><th>阻抗误差</th></tr>')
            for r in sub:
                if r.get('error'):
                    A('<tr><td class="l">%s</td><td class="l">%s</td>'
                      '<td class="l">%s</td><td>%s</td><td>%s</td>'
                      '<td colspan="5" class="bad">%s</td></tr>'
                      % (r['layer'], '外层' if r['outer'] else '内层',
                         '单端' if r['kind'] == 'single' else '差分',
                         _f(r['target'], 0), _f(r['spacing'], 0),
                         html.escape(r['error'][:80])))
                    continue
                warn = ' ⚠' if (r.get('w_online') or 0) < MIN_USABLE_MIL else ''
                A('<tr><td class="l">%s</td><td class="l">%s</td><td class="l">%s</td>'
                  '<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                  '<td class="%s">%+.2f%%</td><td class="%s">%+.2f%%</td></tr>'
                  % (r['layer'], '外层' if r['outer'] else '内层',
                     '单端' if r['kind'] == 'single' else '差分',
                     _f(r['target'], 0), _f(r['spacing'], 0),
                     _f(r.get('w_online')) + warn, _f(r.get('w_offline')),
                     '%+.4f' % (r.get('w_err_mil') or 0),
                     _cls(r.get('w_err')), r['w_err'],
                     _cls(r.get('z_err')), r.get('z_err') or 0))
            A('</table></details>')

    A('<div class="note"><b>怎么读这份报告：</b>'
      '线宽误差是端到端指标（离线解出的线宽 vs 官网解出的线宽）；'
      '阻抗误差只反映"阻抗模型"本身，不受反算放大影响。'
      '两者都小才说明离线模型可靠。'
      '绿色 ≤1%，黄色 1~3%，红色 &gt;3%。</div>')
    A('</body></html>')
    with open(OUT_HTML, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(p))


if __name__ == '__main__':
    raise SystemExit(main())
