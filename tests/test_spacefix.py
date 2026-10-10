import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import spacefix  # noqa: E402

# Lines taken from a real failing Finder restore log (iPhone 15 Pro Max,
# iOS 27.0.1), trimmed to the ones the parser relies on.
FAILING_LOG = """\
[12:36:47.6508] Successfully read minSystemPartitionSize : 9379 MB
[12:36:47.6508] SpaceNeededForRestore: 6042 MB ContainerFreeSpace: 6339 MBSufficient space available for upgrade install
[12:36:47.6508] Space usage: DataVolumeSpaceUsed: 24317 MB ContainerFreeSpace: 15912 MB
[12:36:47.6508] ASR:     apfs_invert_asr_img: dstream_id=16, size=10403971072
[12:36:47.6508] Transfer: Failed preallocation of 6031409152 bytes for /mnt9/cryptex1/current/os.dmg: Unknown error: -1.
[12:36:47.6508] Transfer: Failed preallocation due to space. No hope in this succeeding, bailing.
[12:36:47.6508] [09:36:33.0911-GMT]{3>6} CHECKPOINT FAILURE:(FAILURE:100) (null):[0x06A6] install_splat [0]D(failed to install splat)
"""


def identity(variant, behavior, minsys, model="d84ap"):
    return {"Info": {"DeviceClass": model, "Variant": variant,
                     "RestoreBehavior": behavior,
                     "MinimumSystemPartition": minsys}}


def manifest(*identities):
    return {"BuildIdentities": list(identities),
            "SupportedProductTypes": ["iPhone16,2"]}


STANDARD = manifest(
    identity("Customer Erase Install (IPSW)", "Erase", 9379),
    identity("Customer Upgrade Install (IPSW)", "Update", 9379),
    identity("Recovery Customer Install", "Erase", 488),
)


class ParseLogTests(unittest.TestCase):
    def test_extracts_space_failure_evidence(self):
        ev = spacefix.parse_restore_log(FAILING_LOG)
        self.assertTrue(ev["space_failure"])
        self.assertTrue(ev["splat_failure"])
        self.assertEqual(ev["prealloc_bytes"], 6031409152)
        self.assertEqual(ev["min_system_partition_mb"], 9379)
        self.assertEqual((ev["space_needed_mb"], ev["free_before_mb"]),
                         (6042, 6339))

    def test_shortfall_matches_hand_calculation(self):
        ev = spacefix.parse_restore_log(FAILING_LOG)
        # 6031 MB needed - (15912 MB free after wipe - 10404 MB system) = ~523
        self.assertEqual(spacefix.estimate_shortfall_mb(ev), 523)

    def test_unrelated_log_is_not_a_space_failure(self):
        ev = spacefix.parse_restore_log("Restore failed (error 4013)\n")
        self.assertFalse(ev["space_failure"])
        self.assertIsNone(spacefix.estimate_shortfall_mb(ev))

    def test_diagnose_reads_log_folder(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "iPhoneUpdater-1.log").write_text("nothing relevant\n")
            Path(d, "iPhoneUpdater-2.log").write_text(FAILING_LOG)
            result = spacefix.diagnose(Path(d))
        self.assertEqual(result["logs_checked"], 2)
        self.assertEqual([p.name for p, _ in result["findings"]],
                         ["iPhoneUpdater-2.log"])


class PatchManifestTests(unittest.TestCase):
    def write_fw(self, d, data):
        Path(d, "BuildManifest.plist").write_bytes(plistlib.dumps(data))
        return Path(d)

    def infos(self, d):
        data = plistlib.loads(Path(d, "BuildManifest.plist").read_bytes())
        return [bi["Info"] for bi in data["BuildIdentities"]]

    def test_only_upgrade_identity_is_raised(self):
        with tempfile.TemporaryDirectory() as d:
            fw = self.write_fw(d, STANDARD)
            result = spacefix.patch_manifest(fw, "D84AP", 1387)
            infos = self.infos(d)
        self.assertEqual(result, {"variant": "Customer Upgrade Install (IPSW)",
                                  "old": 9379, "new": 10766})
        self.assertEqual([i["MinimumSystemPartition"] for i in infos],
                         [9379, 10766, 488])

    def test_patching_twice_does_not_stack(self):
        with tempfile.TemporaryDirectory() as d:
            fw = self.write_fw(d, STANDARD)
            spacefix.patch_manifest(fw, "d84ap", 1387)
            second = spacefix.patch_manifest(fw, "d84ap", 2000)
            self.assertTrue(Path(d, "BuildManifest.original.plist").exists())
        self.assertEqual((second["old"], second["new"]), (9379, 11379))

    def test_refuses_when_no_upgrade_identity_for_board(self):
        with tempfile.TemporaryDirectory() as d:
            fw = self.write_fw(d, STANDARD)
            with self.assertRaises(spacefix.SpaceFixError):
                spacefix.patch_manifest(fw, "d73ap", 1387)

    def test_refuses_when_upgrade_identity_is_ambiguous(self):
        data = manifest(
            identity("Customer Upgrade Install (IPSW)", "Update", 9379),
            identity("Developer Upgrade Install (IPSW)", "Update", 9379),
        )
        with tempfile.TemporaryDirectory() as d:
            fw = self.write_fw(d, data)
            with self.assertRaises(spacefix.SpaceFixError):
                spacefix.patch_manifest(fw, "d84ap", 1387)


class NeverEraseTests(unittest.TestCase):
    def test_restore_command_has_no_erase_flag(self):
        cmd = spacefix.build_restore_args(
            "Customer Upgrade Install (IPSW)", Path("/fw"), Path("/log"))
        self.assertFalse(spacefix.ERASE_FLAGS & set(cmd))
        self.assertIn("Customer Upgrade Install (IPSW)", cmd)

    def test_refuses_an_erase_variant(self):
        with self.assertRaises(spacefix.SpaceFixError):
            spacefix.build_restore_args(
                "Customer Erase Install (IPSW)", Path("/fw"), Path("/log"))


if __name__ == "__main__":
    unittest.main()
