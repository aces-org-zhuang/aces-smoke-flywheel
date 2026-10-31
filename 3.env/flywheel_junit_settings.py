"""被测仓的测试设置覆盖层：把测试结果落成 JUnit XML。

两个坑（都踩过，记在这里）：
  1. InvenTree 的 settings.py 显式设置了 TEST_RUNNER，环境变量 TEST_RUNNER 会被静默忽略；
  2. xmlrunner 的 Django 运行器只读 settings.TEST_OUTPUT_DIR，不认环境变量 / 构造参数，
     所以输出目录必须在 settings 层注入，否则 XML 会散落到工作目录（并污染 git 工作区）。
"""

import os

from InvenTree.settings import *  # noqa: F401,F403

TEST_RUNNER = "xmlrunner.extra.djangotestrunner.XMLTestRunner"
TEST_OUTPUT_DIR = os.environ.get("FLYWHEEL_JUNIT_DIR", "junit")
TEST_OUTPUT_VERBOSE = 2
TEST_OUTPUT_FILE_NAME = None
