"""全局测试护栏：测试永远不能弹权限框，也不能改到本机真实程序。

之前跑一次全量测试卡了 1 小时 36 分 —— 有个用例走到真机的提权分支，
系统弹出授权框在等点击。补丁引擎是要动系统目录里 271MB 二进制的东西，
测试里一律禁掉，谁要测提权就自己 monkeypatch。
"""

import os

import pytest

os.environ["AAC_NO_ELEVATION"] = "1"


@pytest.fixture(autouse=True)
def _block_elevation(monkeypatch):
    from app import localize

    monkeypatch.setattr(localize, "_elevated", lambda *a, **k: (1, "测试环境禁止提权"))
