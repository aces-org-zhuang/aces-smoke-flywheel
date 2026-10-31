"""Django 测试运行器：把每次冒烟运行落成可复算的 JUnit XML。

为什么不用 pytest：
  实测同一套 company 应用，pytest 416s / 77 tests（每次用例重建设置链），
  Django 原生 runner 8.3s / 75 tests。差 30 倍，不能作为飞轮的日常触发。
  这里用 xmlrunner 保留 Django 原生速度，同时产出结构化证据。

输出目录由 FLYWHEEL_JUNIT_DIR 指定，默认 <cwd>/junit。
"""

from __future__ import annotations

import os

from xmlrunner.extra.djangotestrunner import XMLTestRunner as _BaseRunner


class FlywheelXMLRunner(_BaseRunner):
    def __init__(self, *args, **kwargs):
        out = os.environ.get("FLYWHEEL_JUNIT_DIR", "junit")
        os.makedirs(out, exist_ok=True)
        kwargs.setdefault("output_dir", out)
        # 保留完整用例名，便于分类器回指到源码行
        kwargs.setdefault("verbosity", 2)
        super().__init__(*args, **kwargs)
