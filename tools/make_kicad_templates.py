"""按嘉立创官网叠层生成 KiCad 叠层 / 设计规则模板（`.kicad_pcb` + `.kicad_pro` + `.kicad_dru`）。

数据来源：`POST /api/jlcTools/impedance/selectPageImpedanceDefaultTemplate`
（就是阻抗计算神器加载叠层的那个接口），已缓存到 `tools/stackups_cache.json`。

生成的 3 个文件与手写模板格式完全一致：
* ``.kicad_pcb`` —— 铜层数、物理叠层、板级设置（按叠层逐层生成）
* ``.kicad_pro``  —— 从已有模板复制，只改 ``meta.filename``（其余字段本来就与层数无关）
* ``.kicad_dru``  —— 逐字复制（空规则模板）

用法::

    python tools/make_kicad_templates.py            # 生成 6/8/10 层
    python tools/make_kicad_templates.py --dry-run  # 只看会写什么
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(ROOT)
#: 内置的参考模板（.kicad_pro 的字段与层数无关，只改 meta.filename；.kicad_dru 逐字复制）
REF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kicad_ref')
REF_NAME = 'template'
REF_PRO = os.path.join(REF_DIR, REF_NAME + '.kicad_pro')
REF_DRU = os.path.join(REF_DIR, REF_NAME + '.kicad_dru')
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C
from fetch_stackups import load_cache
from impedance_calculator.stackup import match_name

#: (层数, 官网叠层名前缀, 模板文件名, .kicad_pro/.dru 参考模板)
#: 参考模板写自己 = 原地重写（保留原有 .kicad_pro 内容，只更新 .kicad_pcb）
TARGETS = [
    (2, 'JLC0216A', 'JLC_2L_1mm6', 'JLC_2L_1mm6'),
    (4, 'JLC04161H-3313', 'JLC_4L_1mm56_3313', 'JLC_4L_1mm56_3313'),
    (4, 'JLC04161H-7628', 'JLC_4L_1mm59_7628', 'JLC_4L_1mm59_7628'),
    (6, 'JLC06161H-3313(免费', 'JLC_6L_1mm54_3313', 'JLC_4L_1mm56_3313'),
    (8, 'JLC08161H-2116(通用免费', 'JLC_8L_1mm60_2116', 'JLC_4L_1mm56_3313'),
    (10, 'JLC10161H-2116(通用免费', 'JLC_10L_1mm57_2116', 'JLC_4L_1mm56_3313'),
]
LOSS_TANGENT = 0.017
MASK_THICKNESS = 0.01

# KiCad 铜层编号：F.Cu=0，B.Cu=2，内层 4,6,8,...
INNER_LAYER_IDS = [4, 6, 8, 10, 12, 14, 16, 18]


def num(v, nd=4):
    """简洁数字：去掉多余尾零（KiCad 里写 0.035 而不是 0.0350）。"""
    s = ('%.*f' % (nd, v)).rstrip('0').rstrip('.')
    return s or '0'


def parse_sequence(tpl):
    """把 basicDataList 展平成 ``[('cu', thick, name) | ('diel', thick, dk, name, is_core)]``。"""
    seq = []
    for b in tpl.get('basicDataList') or []:
        mt = b.get('materialType')
        name = b.get('materialName') or ''
        dk = float(b.get('dielectricConstant') or 0)
        thick = float(b.get('dielectricThick') or 0)
        if mt == 1:                                     # 铜箔
            seq.append(('cu', float(b.get('topConductorThick') or 0), name))
        elif mt == 2:                                   # 芯板：上铜 + 介质 + 下铜
            seq.append(('cu', float(b.get('topConductorThick') or 0), name))
            seq.append(('diel', thick, dk, name, True))
            seq.append(('cu', float(b.get('botConductorThick') or 0), name))
        elif mt in (0, 3, 4):                           # PP / 光板 / AD胶
            seq.append(('diel', thick, dk, name, False))
    return seq


def merge_dielectrics(seq):
    """连续多张 PP 在 KiCad 里合并成一层：厚度相加、Dk 取**厚度加权调和平均**。

    叠层里的介质是**串联**电容，等效介电常数应为 ``t_total / Σ(tᵢ/εᵢ)``
    （不是算术平均）。实测这几个叠层的合并间隙都是同种 PP，所以两种算法结果相同。
    """
    out, i = [], 0
    while i < len(seq):
        if seq[i][0] == 'cu':
            out.append(seq[i])
            i += 1
            continue
        j, parts = i, []
        while j < len(seq) and seq[j][0] == 'diel':
            parts.append(seq[j])
            j += 1
        t = sum(p[1] for p in parts)
        dk = t / sum(p[1] / p[2] for p in parts) if t > 0 and all(p[2] > 0 for p in parts) \
            else parts[0][2]
        is_core = any(p[4] for p in parts)
        name = '+'.join(sorted({p[3].split()[0] for p in parts}))
        out.append(('diel', t, dk, name, is_core))
        i = j
    return out


def material_of(name, is_core):
    """从 materialName 里取材料代号：3313 / 7628 / 2116 / 1080 / FR4。"""
    if is_core:
        return 'FR4'
    tok = (name or '').split()
    return tok[0] if tok and tok[0][:1].isdigit() else (tok[0] if tok else 'FR4')


def build_pcb(tpl, fname, n_layers):
    seq = merge_dielectrics(parse_sequence(tpl))
    cus = [s for s in seq if s[0] == 'cu']
    diels = [s for s in seq if s[0] == 'diel']
    assert len(cus) == n_layers, '铜层数不符：%d != %d' % (len(cus), n_layers)
    assert len(diels) == n_layers - 1, '介质数不符'

    # 铜层名
    names = ['F.Cu'] + ['In%d.Cu' % i for i in range(1, n_layers - 1)] + ['B.Cu']
    # 层号：F.Cu=0, B.Cu=2, 内层 4,6,8...
    ids = [0] + INNER_LAYER_IDS[:n_layers - 2] + [2]

    cu_sum = sum(c[1] for c in cus)
    diel_sum = sum(d[1] for d in diels)
    nominal = cu_sum + diel_sum
    total = nominal + 2 * MASK_THICKNESS

    L = []
    A = L.append
    A('(kicad_pcb')
    A('\t(version 20260206)')
    A('\t(generator "pcbnew")')
    A('\t(generator_version "10.0")')
    A('\t(general')
    A('\t\t(thickness %s)' % num(total, 6))
    A('\t\t(legacy_teardrops no)')
    A('\t)')
    A('\t(paper "A4")')
    A('\t(title_block')
    A('\t\t(title "%s")' % fname)
    name = (tpl.get('receptionDisplayName') or '').replace('(', ' ').replace(')', ' ')
    A('\t\t(comment 1 "JLCPCB %d-layer stackup %s, %.2fmm (copper+dielectric)")'
      % (n_layers, (tpl.get('appointment') or name.split()[0]), nominal))
    parts = []
    for i, c in enumerate(cus):
        parts.append('%s %s' % (names[i], num(c[1])))
        if i < len(diels):
            d = diels[i]
            parts.append('%s %s' % (material_of(d[3], d[4]), num(d[1])))
    line, lines = '', []
    for p in parts:
        if len(line) + len(p) + 3 > 100:
            lines.append(line)
            line = ''
        line = (line + ' / ' + p) if line else p
    lines.append(line)
    cn = 2
    for ln in lines[:3]:
        A('\t\t(comment %d "%s")' % (cn, ln))
        cn += 1
    A('\t\t(comment %d "Via: drill 0.3mm / diameter 0.4mm, min annular ring 0.05mm")' % cn)
    cn += 1
    A('\t\t(comment %d "Dielectric Dk %.2f~%.2f / Df %.3f")'
      % (cn, min(d[2] for d in diels), max(d[2] for d in diels), LOSS_TANGENT))
    A('\t)')
    A('\t(layers')
    for i, nm in enumerate(names):
        A('\t\t(%d "%s" signal)' % (ids[i], nm))
    for line in ('\t\t(9 "F.Adhes" user "F.Adhesive")',
                 '\t\t(11 "B.Adhes" user "B.Adhesive")',
                 '\t\t(13 "F.Paste" user)',
                 '\t\t(15 "B.Paste" user)',
                 '\t\t(5 "F.SilkS" user "F.Silkscreen")',
                 '\t\t(7 "B.SilkS" user "B.Silkscreen")',
                 '\t\t(1 "F.Mask" user)',
                 '\t\t(3 "B.Mask" user)',
                 '\t\t(17 "Dwgs.User" user "User.Drawings")',
                 '\t\t(19 "Cmts.User" user "User.Comments")',
                 '\t\t(21 "Eco1.User" user "User.Eco1")',
                 '\t\t(23 "Eco2.User" user "User.Eco2")',
                 '\t\t(25 "Edge.Cuts" user)',
                 '\t\t(27 "Margin" user)',
                 '\t\t(31 "F.CrtYd" user "F.Courtyard")',
                 '\t\t(29 "B.CrtYd" user "B.Courtyard")',
                 '\t\t(35 "F.Fab" user)',
                 '\t\t(33 "B.Fab" user)',
                 '\t\t(39 "User.1" user)', '\t\t(41 "User.2" user)',
                 '\t\t(43 "User.3" user)', '\t\t(45 "User.4" user)',
                 '\t\t(47 "User.5" user)', '\t\t(49 "User.6" user)',
                 '\t\t(51 "User.7" user)', '\t\t(53 "User.8" user)',
                 '\t\t(55 "User.9" user)'):
        A(line)
    A('\t)')
    A('\t(setup')
    A('\t\t(stackup')
    A('\t\t\t(layer "F.SilkS"\n\t\t\t\t(type "Top Silk Screen")\n\t\t\t\t(color "White")\n\t\t\t)')
    A('\t\t\t(layer "F.Paste"\n\t\t\t\t(type "Top Solder Paste")\n\t\t\t)')
    A('\t\t\t(layer "F.Mask"\n\t\t\t\t(type "Top Solder Mask")\n'
      '\t\t\t\t(color "Green")\n\t\t\t\t(thickness %.2f)\n\t\t\t)' % MASK_THICKNESS)
    for i, c in enumerate(cus):
        A('\t\t\t(layer "%s"\n\t\t\t\t(type "copper")\n\t\t\t\t(thickness %s)\n\t\t\t)'
          % (names[i], num(c[1])))
        if i < len(diels):
            d = diels[i]
            A('\t\t\t(layer "dielectric %d"' % (i + 1))
            A('\t\t\t\t(type "%s")' % ('core' if d[4] else 'prepreg'))
            A('\t\t\t\t(color "FR4 natural")')
            A('\t\t\t\t(thickness %s)' % num(d[1]))
            A('\t\t\t\t(material "%s")' % material_of(d[3], d[4]))
            A('\t\t\t\t(epsilon_r %.2f)' % d[2])
            A('\t\t\t\t(loss_tangent %.3f)' % LOSS_TANGENT)
            A('\t\t\t)')
    A('\t\t\t(layer "B.Mask"\n\t\t\t\t(type "Bottom Solder Mask")\n'
      '\t\t\t\t(color "Green")\n\t\t\t\t(thickness %.2f)\n\t\t\t)' % MASK_THICKNESS)
    A('\t\t\t(layer "B.Paste"\n\t\t\t\t(type "Bottom Solder Paste")\n\t\t\t)')
    A('\t\t\t(layer "B.SilkS"\n\t\t\t\t(type "Bottom Silk Screen")\n'
      '\t\t\t\t(color "White")\n\t\t\t)')
    A('\t\t\t(copper_finish "None")')
    A('\t\t\t(dielectric_constraints yes)')
    A('\t\t)')
    for line in ('\t\t(pad_to_mask_clearance 0)',
                 '\t\t(solder_mask_min_width 0.1)',
                 '\t\t(allow_soldermask_bridges_in_footprints no)',
                 '\t\t(tenting\n\t\t\t(front yes)\n\t\t\t(back yes)\n\t\t)',
                 '\t\t(covering\n\t\t\t(front no)\n\t\t\t(back no)\n\t\t)',
                 '\t\t(plugging\n\t\t\t(front no)\n\t\t\t(back no)\n\t\t)',
                 '\t\t(capping no)',
                 '\t\t(filling no)'):
        A(line)
    A('\t)')
    A(')')
    A('')
    return '\n'.join(L), dict(nominal=nominal, total=total, cus=cus, diels=diels)


def find_stackup(cache, n, key):
    """按名字找叠层（复用 ``impedance_calculator.stackup.match_name`` 的边界匹配规则）。"""
    for it in cache.get(str(n), []):
        nm = it.get('receptionDisplayName') or it.get('appointName') or ''
        if match_name(nm, key):
            return it
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--out', default=None, help='模板输出目录（默认 ../PCB TEMPLATE 或包内 kicad_templates/）')
    args = ap.parse_args(argv)
    global TEMPLATES
    TEMPLATES = args.out or C.default_template_dir()
    if not args.dry_run:
        os.makedirs(TEMPLATES, exist_ok=True)
    cache = load_cache()
    for n, key, fname, ref in TARGETS:
        tpl = find_stackup(cache, n, key)
        if tpl is None:
            print('!! 没找到 %d 层叠层 %s' % (n, key), file=sys.stderr)
            continue
        text, info = build_pcb(tpl, fname, n)
        print('%-22s ← %s' % (fname, (tpl.get('receptionDisplayName') or '')[:44]))
        print('   铜层 %d，介质 %d 层，标称(铜+介质) %.4f mm，KiCad 板厚 %.4f mm'
              % (n, n - 1, info['nominal'], info['total']))
        print('   Dk %s' % ', '.join('%.2f' % d[2] for d in info['diels']))
        if args.dry_run:
            continue
        with open(os.path.join(TEMPLATES, fname + '.kicad_pcb'), 'w',
                  encoding='utf-8', newline='\n') as fh:
            fh.write(text)
        # .kicad_pro / .kicad_dru 从内置参考模板派生（不依赖外面的 PCB TEMPLATE/）
        with open(REF_PRO, encoding='utf-8') as fh:
            pro = fh.read()
        pro = pro.replace(json.dumps(REF_NAME + '.kicad_pro'),
                          json.dumps(fname + '.kicad_pro'))
        with open(os.path.join(TEMPLATES, fname + '.kicad_pro'), 'w',
                  encoding='utf-8', newline='\n') as fh:
            fh.write(pro)
        with open(REF_DRU, encoding='utf-8') as fh:
            dru = fh.read()
        with open(os.path.join(TEMPLATES, fname + '.kicad_dru'), 'w',
                  encoding='utf-8', newline='\n') as fh:
            fh.write(dru)
        print('   → 写入 3 个文件')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
