"""从官网取各层数的叠层方案，缓存到 ``data/stackups_cache.json``。

数据来源：``POST /api/jlcTools/impedance/selectPageImpedanceDefaultTemplate``
（就是阻抗计算神器页面加载叠层用的接口）。

用法::

    python tools/fetch_stackups.py            # 有缓存就用缓存
    python tools/fetch_stackups.py --refresh  # 重新拉取
    python tools/fetch_stackups.py --show     # 只打印缓存内容
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C                                              # noqa: E402
from impedance_calculator.api import JlcApi                             # noqa: E402
from impedance_calculator.stackup import Stackup                        # noqa: E402

LAYERS = [2, 4, 6, 8, 10]
THICKNESS = 1.6
OUTER_CU = 1.0
INNER_CU = 0.5


#: 缓存里只保留这些字段（官网原始响应 1.7MB → 精简后 ~100KB，仓库更轻）
TPL_FIELDS = ('laminatedConstructionCode', 'receptionDisplayName', 'appointName',
              'plateLayerNumber', 'plateThickness', 'cuprumThickness',
              'innerCopperThickness', 'boardType')
ITEM_FIELDS = ('materialType', 'material', 'materialName', 'dielectricThick',
               'dielectricConstant', 'topConductorThick', 'botConductorThick')


def compact(tpl):
    """裁掉官网响应里用不到的字段（残铜率、供应商、审核状态…）。"""
    out = {k: tpl.get(k) for k in TPL_FIELDS}
    out['basicDataList'] = [{k: b.get(k) for k in ITEM_FIELDS}
                            for b in (tpl.get('basicDataList') or [])]
    return out


def fetch():
    out = {}
    with JlcApi() as api:
        for n in LAYERS:
            try:
                lst = api.templates(n, THICKNESS, OUTER_CU, INNER_CU)
            except Exception as exc:                              # noqa: BLE001
                print('  取 %d 层失败: %s' % (n, exc), file=sys.stderr)
                continue
            out[str(n)] = [compact(t) for t in lst]
            print('  %2d 层: %d 个叠层' % (n, len(lst)))
    C.ensure_dirs()
    with open(C.FILE_STACKUPS, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    return out


def load_cache():
    if not os.path.exists(C.FILE_STACKUPS):
        return fetch()
    with open(C.FILE_STACKUPS, encoding='utf-8') as fh:
        return json.load(fh)


def show(data):
    for n in LAYERS:
        lst = data.get(str(n)) or []
        ok = 0
        print('=== %d 层（%d 个）===' % (n, len(lst)))
        for it in lst:
            name = (it.get('receptionDisplayName') or it.get('appointName') or '?')[:48]
            try:
                st = Stackup.from_template(it)
                ok += 1
                geo = ' '.join('L%d %.1f' % (i + 1, st.si9000_geometry('L%d' % (i + 1))['H1'])
                               for i in range(min(st.copper_layers, 4)))
                print('   %-48s %d 层铜  标称 %.4fmm  H1: %s'
                      % (name, st.copper_layers, st.nominal_mm, geo))
            except Exception as exc:                              # noqa: BLE001
                print('   %-48s 解析失败: %s' % (name, str(exc)[:40]))
        print('   小计：%d/%d 可解析\n' % (ok, len(lst)))


def main(argv=None):
    ap = argparse.ArgumentParser(description='取官网叠层并缓存')
    ap.add_argument('--show', action='store_true')
    ap.add_argument('--refresh', action='store_true')
    args = ap.parse_args(argv)
    data = fetch() if (args.refresh or not os.path.exists(C.FILE_STACKUPS)) else load_cache()
    show(data)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
