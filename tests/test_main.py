import io
import stat
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile

from app import main


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dest = self.root / "extract"
        self.dest.mkdir()

    def tar(self, members):
        path = self.root / "package.tar"
        with tarfile.open(path, "w") as tf:
            for member, data in members:
                tf.addfile(member, io.BytesIO(data) if data else None)
        return path

    def test_tar_rejects_links_and_special_members_before_extracting(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind):
                harmless = tarfile.TarInfo("harmless.txt")
                harmless.size = 4
                member = tarfile.TarInfo("link")
                member.type = kind
                member.linkname = str(self.root / "outside")
                path = self.tar([(harmless, b"safe"), (member, b"")])
                self.assertFalse(main._extract_archive(path, self.dest, []))
                self.assertFalse((self.dest / "harmless.txt").exists())

    def test_tar_rejects_parent_and_absolute_paths(self):
        for name in ("../outside.txt", str(self.root / "outside.txt"), r"..\outside.txt"):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                member.size = 4
                self.assertFalse(main._extract_archive(
                    self.tar([(member, b"evil")]), self.dest, []))

    def test_zip_rejects_nonportable_and_parent_paths(self):
        for name in ("../outside.txt", r"..\outside.txt", "C:/outside.txt", "C:outside.txt", r"\\server\share\file"):
            with self.subTest(name=name):
                path = self.root / "package.zip"
                with zipfile.ZipFile(path, "w") as zf:
                    zf.writestr(name, b"evil")
                self.assertFalse(main._extract_archive(path, self.dest, []))

    def test_zip_rejects_symlink_members(self):
        path = self.root / "package.zip"
        with zipfile.ZipFile(path, "w") as zf:
            member = zipfile.ZipInfo("link")
            member.create_system = 3
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(member, str(self.root / "outside"))
        self.assertFalse(main._extract_archive(path, self.dest, []))

    def test_existing_symlink_cannot_redirect_archive_writes(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.dest / "redirect").symlink_to(outside, target_is_directory=True)
        path = self.root / "package.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("redirect/written.txt", "evil")
        self.assertFalse(main._extract_archive(path, self.dest, []))
        self.assertFalse((outside / "written.txt").exists())

    def test_safe_tar_with_root_directory_extracts(self):
        directory = tarfile.TarInfo(".")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        member = tarfile.TarInfo("./nested/demo")
        member.size = 7
        path = self.tar([(directory, b""), (member, b"program")])
        self.assertTrue(main._extract_archive(path, self.dest, []))
        self.assertEqual((self.dest / "nested" / "demo").read_bytes(), b"program")

    def test_zip_member_count_and_total_size_limits(self):
        path = self.root / "package.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("one", b"a" * 100)
            zf.writestr("two", b"b" * 100)
        limits = main.archive.ArchiveLimits(max_members=1, max_total_size=1000)
        with self.assertRaises(ValueError):
            main.archive.extract_zip(path, self.dest, limits)
        limits = main.archive.ArchiveLimits(max_members=10, max_total_size=150)
        with self.assertRaises(ValueError):
            main.archive.extract_zip(path, self.dest, limits)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_zip_compression_ratio_limit_rejects_bomb(self):
        path = self.root / "bomb.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("zeroes", b"\0" * (17 * 1024 * 1024))
        limits = main.archive.ArchiveLimits(
            max_members=10,
            max_total_size=64 * 1024 * 1024,
            max_ratio=2,
        )
        with self.assertRaises(ValueError):
            main.archive.inspect_zip(path, limits)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.apps = self.home / ".aac" / "apps"
        self.agent = {"id": "demo", "name": "Demo", "command": "demo", "sources": {}}
        self.installs = []
        self.jobs = []
        self.addCleanup(lambda: [coro.close() for coro in self.jobs])
        for target, name, value in (
            (Path, "home", self.home),
            (main.registry, "get_agent", self.agent),
            (main.detect, "find_installs", self.installs),
            (main.installer, "available_source", None),
            (main.installer, "prefix_writable", True),
            (main.installer, "build_uninstall_argv", (["not-executed"], False)),
        ):
            mocked = patch.object(target, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        for name, value in (("ARCHIVES_APPS", self.apps), ("TASKS", {})):
            mocked = patch.object(main, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        mocked = patch.object(main.installer, "run", AsyncMock(return_value=0))
        mocked.start()
        self.addCleanup(mocked.stop)

    async def uninstall(self, prefix):
        with patch.object(main.asyncio, "create_task", side_effect=self.jobs.append):
            return await main.api_uninstall("demo", main.ActionReq(prefix=str(prefix)))

    async def upload(self, filename, data):
        with patch.object(main.asyncio, "create_task", side_effect=self.jobs.append):
            result = await main.api_install_archive(
                "demo", UploadFile(filename=filename, file=io.BytesIO(data)))
        for coro in self.jobs:
            await coro
        return main.TASKS[result["task_id"]]

    async def test_update_uses_detected_primary_prefix(self):
        self.agent["sources"] = {"npm": "demo-pkg"}
        prefix = self.home / ".local"
        self.installs[:] = [{"prefix": str(prefix)}]
        with patch.object(
                main.installer,
                "build_update_argv",
                return_value=(["npm", "install", "-g"], False),
        ) as build_argv, patch.object(
                main.asyncio, "create_task", side_effect=self.jobs.append):
            await main.api_update("demo", main.ActionReq())
            for coro in self.jobs:
                await coro
        build_argv.assert_called_once_with(self.agent, "user", str(prefix))

    def zip_data(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zf:
            zf.writestr("nested/demo", b"program")
        return stream.getvalue()

    async def test_uninstall_rejects_undetected_archive_sibling(self):
        prefix = self.apps.with_name("apps-backup")
        prefix.mkdir(parents=True)
        with self.assertRaises(HTTPException) as caught:
            await self.uninstall(prefix)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self.jobs, [])
        self.assertTrue(prefix.is_dir())

    async def test_uninstall_never_deletes_known_archive_sibling(self):
        prefix = self.apps.with_name("apps-backup")
        prefix.mkdir(parents=True)
        self.installs.append({"prefix": str(prefix)})
        with self.assertRaises(HTTPException):
            await self.uninstall(prefix)
        self.assertEqual(self.jobs, [])
        self.assertTrue(prefix.is_dir())

    async def test_uninstall_rejects_undetected_script_layout(self):
        self.agent["sources"] = {"npm": "demo-pkg"}
        prefix = self.home / ".demo"
        (prefix / "node_modules" / "demo-pkg").mkdir(parents=True)
        (prefix / "bin").mkdir()
        (prefix / "bin" / "demo").write_bytes(b"program")
        with self.assertRaises(HTTPException) as caught:
            await self.uninstall(prefix)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self.jobs, [])

    async def test_declared_native_script_install_uses_safe_remove_path(self):
        self.agent.update({
            "sources": {"npm": "demo-pkg"},
            "script_prefix": "~/.demo",
        })
        prefix = self.home / ".demo"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin" / "demo").write_text("binary", encoding="utf-8")
        package = prefix / "node_modules" / "@demo" / "plugin"
        package.mkdir(parents=True)
        (package / "index.js").write_text("plugin", encoding="utf-8")
        (prefix / "settings.json").write_text("keep", encoding="utf-8")
        self.installs.append({"prefix": str(prefix)})
        main.installer.run.reset_mock()
        result = await self.uninstall(prefix)
        for coro in self.jobs:
            await coro
        self.assertEqual(main.TASKS[result["task_id"]]["status"], "done")
        self.assertFalse((prefix / "bin" / "demo").exists())
        self.assertFalse((prefix / "node_modules").exists())
        self.assertEqual((prefix / "settings.json").read_text(encoding="utf-8"), "keep")
        main.installer.run.assert_not_awaited()

    async def test_uninstall_refuses_archive_root_and_other_agents_directory(self):
        for prefix in (self.apps, self.apps / "another-agent"):
            with self.subTest(prefix=prefix):
                prefix.mkdir(parents=True, exist_ok=True)
                self.installs[:] = [{"prefix": str(prefix)}]
                with self.assertRaises(HTTPException):
                    await self.uninstall(prefix)
                self.assertTrue(prefix.is_dir())
        self.assertEqual(self.jobs, [])

    async def test_detected_archive_is_removed_for_npm_and_non_npm_agents(self):
        for sources in ({}, {"npm": "demo-pkg"}):
            with self.subTest(sources=sources):
                self.jobs.clear()
                self.agent["sources"] = sources
                prefix = self.apps / "demo"
                prefix.mkdir(parents=True, exist_ok=True)
                (prefix / "demo").write_bytes(b"program")
                config = self.home / "settings.json"
                config.write_bytes(b"config")
                self.installs[:] = [{"prefix": str(prefix)}]
                result = await self.uninstall(prefix)
                for coro in self.jobs:
                    await coro
                self.assertEqual(main.TASKS[result["task_id"]]["status"], "done")
                self.assertFalse(prefix.exists())
                self.assertEqual(config.read_bytes(), b"config")

    async def test_archive_uninstall_refuses_symlinked_installation(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.apps.mkdir(parents=True)
        prefix = self.apps / "demo"
        prefix.symlink_to(outside, target_is_directory=True)
        self.installs[:] = [{"prefix": str(prefix)}]
        with self.assertRaises(HTTPException):
            await self.uninstall(prefix)
        self.assertEqual(self.jobs, [])
        self.assertTrue(outside.is_dir())

    async def test_upload_filename_cannot_overwrite_arbitrary_file(self):
        sentinel = self.root / "uploaded.zip"
        sentinel.write_bytes(b"keep")
        task = await self.upload(str(sentinel), self.zip_data())
        self.assertEqual(task["status"], "done")
        self.assertTrue(sentinel.is_file())
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual((self.apps / "demo" / "bin" / "demo").read_bytes(), b"program")

    async def test_invalid_archive_does_not_destroy_existing_install(self):
        prefix = self.apps / "demo"
        prefix.mkdir(parents=True)
        (prefix / "keep").write_bytes(b"old installation")
        task = await self.upload("bad.zip", b"not an archive")
        self.assertEqual(task["status"], "failed")
        self.assertTrue((prefix / "keep").is_file())
        self.assertEqual((prefix / "keep").read_bytes(), b"old installation")

    async def test_upload_filename_matching_executable_does_not_delete_it(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zf:
            zf.writestr("demo", b"program")
        task = await self.upload("demo", stream.getvalue())
        self.assertEqual(task["status"], "done")
        self.assertEqual((self.apps / "demo" / "bin" / "demo").read_bytes(), b"program")

    async def test_upload_limit_rejects_request_and_removes_temporary_file(self):
        with patch.object(main, "ARCHIVE_UPLOAD_LIMIT", 3):
            with self.assertRaises(HTTPException) as caught:
                await main.api_install_archive(
                    "demo", UploadFile(filename="too-large.zip", file=io.BytesIO(b"1234")))
        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(list(self.apps.parent.glob(".aac-archive-*")), [])
        self.assertEqual(self.jobs, [])

    async def test_safe_update_aborts_when_patch_detection_fails(self):
        self.agent["sources"] = {"npm": "demo-pkg"}
        main.installer.run.reset_mock()
        with patch.object(main.catmod, "detect_patches", return_value={"error": "boom"}), \
                patch.object(main.asyncio, "create_task", side_effect=self.jobs.append):
            result = await main.api_safe_update("demo", main.ActionReq())
            for coro in self.jobs:
                await coro
        self.assertEqual(main.TASKS[result["task_id"]]["status"], "failed")
        main.installer.run.assert_not_awaited()

    async def test_safe_update_aborts_when_snapshot_fails(self):
        self.agent["sources"] = {"npm": "demo-pkg"}
        patches = {
            "modified": ["index.js"],
            "added": [],
            "pkg": "demo-pkg",
            "pkg_version": "1.0.0",
        }
        main.installer.run.reset_mock()
        with patch.object(main.catmod, "detect_patches", return_value=patches), \
                patch.object(main.patchwork, "snapshot", return_value=False), \
                patch.object(main.asyncio, "create_task", side_effect=self.jobs.append):
            result = await main.api_safe_update("demo", main.ActionReq())
            for coro in self.jobs:
                await coro
        self.assertEqual(main.TASKS[result["task_id"]]["status"], "failed")
        main.installer.run.assert_not_awaited()

    async def test_safe_update_reports_partial_when_reapply_fails(self):
        self.agent["sources"] = {"npm": "demo-pkg"}
        patches = {
            "modified": ["index.js"],
            "added": [],
            "pkg": "demo-pkg",
            "pkg_version": "1.0.0",
        }
        main.installer.run.reset_mock()
        with patch.object(
                main.catmod, "detect_patches", side_effect=[patches, {"modified": [], "added": []}]
        ), patch.object(main.patchwork, "snapshot", return_value=True), \
                patch.object(
                    main.patchwork,
                    "reapply",
                    return_value={"status": "partial", "ok": 0, "ported": 0, "failed": 1},
                ), patch.object(main.asyncio, "create_task", side_effect=self.jobs.append):
            result = await main.api_safe_update("demo", main.ActionReq())
            for coro in self.jobs:
                await coro
        task = main.TASKS[result["task_id"]]
        self.assertEqual(task["status"], "partial", "".join(task["log"]))
        main.installer.run.assert_awaited_once()


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        mocked = patch.object(main, "TASKS", {})
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_finished_tasks_expire_after_ttl(self):
        main.TASKS["old"] = {
            "status": "done",
            "log": [],
            "finished_at": time.monotonic() - main.TASK_TTL_SECONDS - 1,
        }
        main.TASKS["fresh"] = {
            "status": "done",
            "log": [],
            "finished_at": time.monotonic(),
        }
        main._prune_tasks()
        self.assertNotIn("old", main.TASKS)
        self.assertIn("fresh", main.TASKS)

    def test_task_pruning_drops_oldest_completed_tasks_first(self):
        for index in range(3):
            main.TASKS[f"done-{index}"] = {
                "status": "done",
                "log": [],
                "finished_at": float(index),
            }
        main.TASKS["running"] = {
            "status": "running",
            "log": [],
            "created_at": 0.0,
        }
        with patch.object(main, "TASK_MAX_COUNT", 2):
            main._prune_tasks()
        self.assertNotIn("done-0", main.TASKS)
        self.assertIn("running", main.TASKS)


class CloudPullDecisionTests(unittest.TestCase):
    def test_simultaneous_local_and_remote_changes_are_a_conflict(self):
        decision = main._cloud_pull_decision(
            {"local_sha256": "old", "remote_fingerprint": "old-remote"},
            {"sha256": "new-local"},
            "new-remote",
        )
        self.assertEqual(decision, "conflict")

    def test_remote_only_change_is_pulled(self):
        decision = main._cloud_pull_decision(
            {"local_sha256": "same", "remote_fingerprint": "old-remote"},
            {"sha256": "same"},
            "new-remote",
        )
        self.assertEqual(decision, "pull")

    def test_untracked_local_file_is_not_silently_overwritten(self):
        decision = main._cloud_pull_decision({}, {"sha256": "local"}, "remote")
        self.assertEqual(decision, "conflict")


class CliTests(unittest.TestCase):
    def setUp(self):
        main._OPEN_URL = None
        self.addCleanup(setattr, main, "_OPEN_URL", None)

    def test_cli_binds_loopback_and_preserves_custom_port(self):
        for arguments, port in ((["aac"], 8600), (["aac", "--port", "8754"], 8754)):
            with self.subTest(arguments=arguments):
                with patch.object(sys, "argv", arguments), patch.object(main.webbrowser, "open"), patch.object(main.uvicorn, "run") as run, patch("builtins.print"):
                    main.cli()
                self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
                self.assertEqual(run.call_args.kwargs["port"], port)
                self.assertEqual(main._OPEN_URL, f"http://127.0.0.1:{port}")

    def test_cli_no_browser_flag_disables_auto_open(self):
        with patch.object(sys, "argv", ["aac", "--no-browser"]), \
                patch.object(main.uvicorn, "run"), patch("builtins.print"):
            main.cli()
        self.assertIsNone(main._OPEN_URL)
