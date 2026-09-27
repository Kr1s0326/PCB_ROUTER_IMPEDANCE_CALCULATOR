"""校验 PCB TEMPLATE 里的 KiCad 模板文件结构是否自洽。

检查项：
  * 铜层数 = 文件名里的层数
  * 铜 / 介质必须交替
  * ``general thickness`` = 铜 + 介质 + 上下阻焊之和
  * ``.kicad_pro`` 的 ``meta.filename`` 与文件名一致
  * ``.kicad_dru`` 与参考模板逐字一致

用法::

    python tools/check_templates.py
"""

import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import _common as C
REPO = os.path.dirname(ROOT)
# 目录由 --out 指定（见 main）
REF_DRU = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kicad_ref', 'template.kicad_dru')

RE_CU = re.compile(r'\(layer "([^"]+)"\s*\n\s*\(type "copper"\)\s*\n\s*\(thickness ([\d.]+)\)')
RE_DI = re.compile(r'\(layer "(dielectric \d+)"\s*\n\s*\(type "(prepreg|core)"\)'
                   r'\s*\n\s*\(color "[^"]*"\)\s*\n\s*\(thickness ([\d.]+)\)'
                   r'\s*\n\s*\(material "([^"]+)"\)\s*\n\s*\(epsilon_r ([\d.]+)\)')
RE_MASK = re.compile(r'\(type "(?:Top|Bottom) Solder Mask"\)\s*\n'
                     r'\s*\(color "Green"\)\s*\n\s*\(thickness ([\d.]+)\)')
RE_THICK = re.compile(r'\(general\s*\n\s*\(thickness ([\d.]+)\)')
RE_SEQ = re.compile(r'\(layer "([^"]+)"\s*\n\s*\(type "(copper|prepreg|core)"\)')


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='复验 KiCad 模板结构')
    ap.add_argument('--out', default=None,
                    help='模板目录（默认 ../PCB TEMPLATE 或包内 kicad_templates/）')
    args = ap.parse_args(argv)
    tpl_dir = args.out or C.default_template_dir()
    ref_dru_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'kicad_ref', 'template.kicad_dru')
    if not os.path.isdir(tpl_dir):
        print('目录不存在: %s' % tpl_dir)
        return 1
    with io.open(ref_dru_path, encoding='utf-8') as fh:
        ref_dru = fh.read()
    bad = 0
    for fname in sorted(f for f in os.listdir(tpl_dir) if f.endswith('.kicad_pcb')):
        s = io.open(os.path.join(tpl_dir, fname), encoding='utf-8').read()
        cu = RE_CU.findall(s)
        di = RE_DI.findall(s)
        mask = RE_MASK.findall(s)
        thick = float(RE_THICK.search(s).group(1))
        cu_sum = sum(float(t) for _, t in cu)
        di_sum = sum(float(d[2]) for d in di)
        mask_sum = sum(float(m) for m in mask)
        total = cu_sum + di_sum + mask_sum

        seq = [x for x in RE_SEQ.findall(s) if x[1] in ('copper', 'prepreg', 'core')]
        alt = all((seq[i][1] == 'copper') != (seq[i + 1][1] == 'copper')
                  for i in range(len(seq) - 1))

        m = re.match(r'JLC_(\d+)L_', fname)
        want = int(m.group(1)) if m else len(cu)
        base = fname[:-len('.kicad_pcb')]
        pro_path = os.path.join(tpl_dir, base + '.kicad_pro')
        dru_path = os.path.join(tpl_dir, base + '.kicad_dru')
        pro_ok = dru_ok = False
        if os.path.exists(pro_path):
            with io.open(pro_path, encoding='utf-8') as fh:
                pro_ok = json.load(fh)['meta']['filename'] == base + '.kicad_pro'
        if os.path.exists(dru_path):
            with io.open(dru_path, encoding='utf-8') as fh:
                dru_ok = fh.read() == ref_dru

        ok = (len(cu) == want and len(di) == want - 1 and alt
              and abs(total - thick) < 1e-6 and pro_ok and dru_ok
              and len(mask) == 2)
        if not ok:
            bad += 1
        print('%s %-26s 铜层%d/%d 介质%d 交替:%s 板厚 %.4f=%.4f pro:%s dru:%s'
              % ('OK ' if ok else 'BAD', fname, len(cu), want, len(di),
                 '是' if alt else '否', thick, total, pro_ok, dru_ok))
        print('     ' + ' | '.join('%s(%s) %s/%s Dk%s' % (c[0], c[1], d[2], d[1], d[4])
                                   for c, d in zip(cu, list(di) + [None] * 9) if d))
    print('\n%s（%d 个不正常）' % ('全部通过' if bad == 0 else '有问题', bad))
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
