import unittest
from unittest import mock

from app import mirrors


class ModeTests(unittest.TestCase):
    def test_default_is_cn(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(mirrors.mode(), "cn")

    def test_official(self):
        with mock.patch.dict("os.environ", {"AAC_MIRROR": "official"}):
            self.assertEqual(mirrors.mode(), "official")

    def test_unknown_falls_back_to_cn(self):
        with mock.patch.dict("os.environ", {"AAC_MIRROR": "whatever"}):
            self.assertEqual(mirrors.mode(), "cn")


class FlagTests(unittest.TestCase):
    def test_npm_flags(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(mirrors.with_flags(["npm", "install", "-g", "x"]),
                             ["npm", "install", "-g", "x", "--registry=https://registry.npmmirror.com"])

    def test_pip_flags(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            argv = mirrors.with_flags(["pip", "install", "pipx"])
            self.assertIn("-i", argv)
            self.assertIn("https://pypi.tuna.tsinghua.edu.cn/simple", argv)

    def test_python_m_pip_flags(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            argv = mirrors.with_flags(["python3", "-m", "pip", "install", "pipx"])
            self.assertIn("-i", argv)

    def test_official_adds_nothing(self):
        with mock.patch.dict("os.environ", {"AAC_MIRROR": "official"}):
            self.assertEqual(mirrors.with_flags(["npm", "install", "-g", "x"]),
                             ["npm", "install", "-g", "x"])
            self.assertEqual(mirrors.env_for(["brew", "install", "git"]), {})

    def test_input_not_mutated(self):
        argv = ["npm", "install"]
        with mock.patch.dict("os.environ", {}, clear=True):
            mirrors.with_flags(argv)
        self.assertEqual(argv, ["npm", "install"])


class EnvTests(unittest.TestCase):
    def test_brew_env(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            env = mirrors.env_for(["brew", "install", "git"])
            self.assertIn("HOMEBREW_API_DOMAIN", env)
            self.assertIn("ustc", env["HOMEBREW_BOTTLE_DOMAIN"])

    def test_npm_env(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            env = mirrors.env_for(["npm", "install"])
            self.assertEqual(env["npm_config_registry"], "https://registry.npmmirror.com")

    def test_info_shape(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            info = mirrors.info()
            self.assertEqual(info["mode"], "cn")
            self.assertTrue(info["label"])
            self.assertTrue(info["npm"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
