"""``jlc_impedance`` 的离线测试套件。

原则：**全部离线、零依赖**（只用 ``unittest``），可随时 ``python -m unittest`` 跑。
涉及官网的测试都在 ``@unittest.skipUnless(RUN_ONLINE)`` 后面，默认不执行。

    python -m unittest discover -s tests -v          # 离线
    JLC_ONLINE=1 python -m unittest discover -s tests # 连官网（慢）
"""
