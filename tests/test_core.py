"""Unit tests for the pure logic in pve-r2-backup.py (no Proxmox or R2 needed).

Run:  python3 -m unittest discover -s tests -v
"""
import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

_path = Path(__file__).resolve().parent.parent / "pve-r2-backup.py"
_loader = importlib.machinery.SourceFileLoader("pve_r2_backup", str(_path))
_spec = importlib.util.spec_from_loader("pve_r2_backup", _loader)
m = importlib.util.module_from_spec(_spec)
_loader.exec_module(m)


def backup(gtype, vmid, ts, archive=True, ext="tar.zst"):
    base = f"vzdump-{gtype}-{vmid}-{ts}"
    files = [f"{base}.log"]
    if archive:
        files += [f"{base}.{ext}", f"{base}.{ext}.notes"]
    return files


class TestClasses(unittest.TestCase):
    JOBS = [
        {"schedule": "sun 01:00", "vmid": "108,1001", "enabled": 1},
        {"schedule": "monthly", "vmid": "1002,1003", "enabled": 1},
        {"schedule": "sun 01:15", "vmid": "100,101,1003"},          # 1003 also weekly
        {"schedule": "yearly", "vmid": "109", "enabled": 0},          # disabled
        {"schedule": "mon *-*-1..7 02:00", "vmid": "200", "enabled": 1},
    ]

    def test_classes(self):
        c = m.vmid_classes(self.JOBS, [])
        self.assertEqual(c["108"], "weekly")
        self.assertEqual(c["1002"], "monthly")
        self.assertEqual(c["1003"], "weekly", "a guest in a weekly and a monthly job is weekly")
        self.assertNotIn("109", c, "disabled jobs are ignored")
        self.assertEqual(c["200"], "monthly", "calendar event with day-of-month is monthly")

    def test_all_with_exclude(self):
        jobs = [{"schedule": "daily", "all": 1, "exclude": "101"}]
        self.assertEqual(m.vmid_classes(jobs, ["100", "101", "102"]), {"100": "weekly", "102": "weekly"})


class TestSelectFiles(unittest.TestCase):
    def test_latest_compares_dates_not_names(self):
        # vmid 106 was a VM in Feb and is a container now: 'qemu' sorts after 'lxc'
        names = backup("qemu", 106, "2026_02_22-01_06_20", ext="vma.zst") + backup("lxc", 106, "2026_07_12-01_18_53")
        got = m.select_files(names, "latest")
        self.assertTrue(all("lxc-106-2026_07_12" in n for n in got), got)
        self.assertEqual(len(got), 3)

    def test_latest_skips_failed_runs(self):
        # newer runs failed and left only a .log
        names = (backup("lxc", 106, "2026_07_12-01_18_53")
                 + backup("lxc", 106, "2026_10_11-01_17_21", archive=False)
                 + backup("lxc", 106, "2026_10_18-01_17_42", archive=False))
        got = m.select_files(names, "latest")
        self.assertEqual({n.split(".")[0] for n in got}, {"vzdump-lxc-106-2026_07_12-01_18_53"})

    def test_latest_one_per_guest(self):
        names = (backup("lxc", 100, "2026_10_11-01_15_00") + backup("lxc", 100, "2026_10_18-01_15_00")
                 + backup("qemu", 1001, "2026_10_18-01_03_29", ext="vma.zst"))
        got = m.select_files(names, "latest")
        self.assertEqual(len(got), 6)
        self.assertFalse(any("2026_10_11" in n for n in got))

    def test_ignores_partial_and_foreign_files(self):
        names = ["vzdump-lxc-100-2026_10_18-01_15_00.tar.dat", "notes.txt", "vzdump-lxc-100-bad.tar.zst"]
        self.assertEqual(m.select_files(names, "all"), [])

    def test_since(self):
        names = backup("lxc", 100, "2026_10_18-01_15_00")
        now = 1_000_000
        mtimes = {names[0]: now - 3600, names[1]: now - 3 * 86400, names[2]: now - 1 * 86400}
        self.assertEqual(m.select_files(names, "since", 2, mtimes, now), sorted([names[0], names[2]]))


class TestParseCopied(unittest.TestCase):
    def test_counts_multi_thread_copies(self):
        lines = [
            "2026/10/23 12:38:10 INFO  : vzdump-lxc-101-2026_10_11-01_15_04.log: Copied (new)",
            "2026/10/23 12:38:20 INFO  : vzdump-lxc-101-2026_10_11-01_15_04.tar.zst: Multi-thread Copied (new)",
            "2026/10/23 12:38:21 INFO  : vzdump-lxc-102-x.log: Copied (replaced existing)",
            "2026/10/23 12:38:22 NOTICE:   1.2 GiB / 2 GiB, 60%, 70 MiB/s, ETA 11s",
            "2026/10/23 12:38:23 INFO  : vzdump-lxc-103-y.log: Skipped copy as --dry-run is set",
        ]
        self.assertEqual(m.parse_copied(lines), ["vzdump-lxc-101-2026_10_11-01_15_04.log",
                                                 "vzdump-lxc-101-2026_10_11-01_15_04.tar.zst",
                                                 "vzdump-lxc-102-x.log"])


class TestEnv(unittest.TestCase):
    def test_reads_bash_printf_q_values(self):
        text = "# c\nR2_BUCKET=my-bucket\nR2_PATH=backups/pve/site1\nR2_CRYPT_PASSWORD=''\nR2_X=a\\ b\nBROKEN\n"
        env = m.parse_env(text)
        self.assertEqual(env["R2_BUCKET"], "my-bucket")
        self.assertEqual(env["R2_CRYPT_PASSWORD"], "")
        self.assertEqual(env["R2_X"], "a b")
        self.assertNotIn("BROKEN", env)

    def test_roundtrip(self):
        env = {k: "" for k in m.ENV_KEYS}
        env.update(R2_BUCKET="b", R2_PATH="x/y", R2_CRYPT_PASSWORD="p w'd$")
        self.assertEqual({k: v for k, v in m.parse_env(m.format_env(env)).items()}, env)


class TestRestoreHelpers(unittest.TestCase):
    VERSIONS = [{"ts": "2026_10_18-01_15_00"}, {"ts": "2026_10_11-01_15_00"}, {"ts": "2026_07_12-01_18_53"}]

    def test_pick_version(self):
        self.assertEqual(m.pick_version(self.VERSIONS, "latest")["ts"], "2026_10_18-01_15_00")
        self.assertEqual(m.pick_version(self.VERSIONS, "2")["ts"], "2026_10_11-01_15_00")
        self.assertEqual(m.pick_version(self.VERSIONS, "2026-07-12")["ts"], "2026_07_12-01_18_53")
        with self.assertRaises(m.Fatal):
            m.pick_version(self.VERSIONS, "2025_01_01")

    def test_restore_cmd(self):
        self.assertEqual(m.restore_cmd("lxc", "9100", "/d/a.tar.zst", "local-lvm", True, False),
                         ["pct", "restore", "9100", "/d/a.tar.zst", "--storage", "local-lvm", "--unique", "1"])
        self.assertEqual(m.restore_cmd("qemu", "1001", "/d/a.vma.zst", "local-lvm", False, True),
                         ["qmrestore", "/d/a.vma.zst", "1001", "--storage", "local-lvm", "--force", "1"])


if __name__ == "__main__":
    unittest.main()
