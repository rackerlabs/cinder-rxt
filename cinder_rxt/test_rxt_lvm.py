import unittest
from unittest import mock

from oslo_concurrency import processutils as putils
from oslo_utils import units

from cinder_rxt.rackspace import RXTLVM, _LUKS_HEADER_BYTES


class FakeVG:
    LVM_CMD_PREFIX = ["env", "LC_ALL=C"]

    def __init__(self, extent_size_mib=4):
        self._extent_size_mib = extent_size_mib
        self.vg_name = "fake-vg"
        self._root_helper = "sudo cinder-rootwrap /etc/cinder/rootwrap.conf"

    def _execute(self, *args, **kwargs):
        """Simulate vgs command for extent size queries."""
        if "vg_extent_size" in args:
            return ("  %s\n" % self._extent_size_mib, "")
        return ("", "")


class TestCopyImageToEncryptedVolume(unittest.TestCase):
    """Tests for the LUKS header compensation in copy_image_to_encrypted_volume."""

    def _make_driver(self):
        """Create an RXTLVM instance without running the real __init__."""
        drv = RXTLVM.__new__(RXTLVM)
        drv.vg = FakeVG()
        drv._sparse_copy_volume = False
        drv._execute = lambda *a, **k: None
        drv._sizestr = lambda sz: "%sg" % sz

        class Conf:
            volume_dd_blocksize = 1
            volume_group = "fake-vg"
            use_multipath_for_image_xfer = False
            enforce_multipath_for_image_xfer = False

        drv.configuration = Conf()
        return drv

    def test_get_extent_size_bytes(self):
        drv = self._make_driver()
        # Default FakeVG has 4 MiB extents
        self.assertEqual(4 * units.Mi, drv._get_extent_size_bytes())

    def test_get_extent_size_bytes_custom(self):
        drv = self._make_driver()
        drv.vg = FakeVG(extent_size_mib=8)
        self.assertEqual(8 * units.Mi, drv._get_extent_size_bytes())

    @mock.patch("cinder_rxt.rackspace.putils.execute")
    def test_lvresize_calls_correct_commands(self, mock_execute):
        drv = self._make_driver()
        volume = {"id": "v1", "name": "vol-1", "size": 10}

        drv._lvresize(volume, "10g")

        self.assertEqual(mock_execute.call_count, 3)
        calls = mock_execute.call_args_list

        # 1. deactivate
        self.assertEqual(
            calls[0][0], ("lvchange", "-an", "fake-vg/vol-1"))
        # 2. resize
        self.assertEqual(
            calls[1][0],
            ("lvresize", "-f", "-L", "10g", "fake-vg/vol-1"))
        # 3. reactivate
        self.assertEqual(
            calls[2][0],
            ("lvchange", "-ay", "-K", "fake-vg/vol-1"))

    @mock.patch("cinder_rxt.rackspace.putils.execute")
    def test_extends_lv_before_image_copy_and_shrinks_after(
        self, mock_lvresize_execute
    ):
        drv = self._make_driver()
        volume = {"id": "v1", "name": "vol-enc", "size": 40}

        drv.extend_volume = mock.Mock()
        drv._copy_image_data_to_volume = mock.Mock()

        drv.copy_image_to_encrypted_volume(
            context=mock.sentinel.ctx,
            volume=volume,
            image_service=mock.sentinel.img_svc,
            image_id="img-1",
        )

        # LV should be extended before the copy
        drv.extend_volume.assert_called_once()
        _, extended_gb = drv.extend_volume.call_args[0]
        # With 4 MiB extents and 16 MiB LUKS header: 4 extra extents
        expected_extra = 4 * 4 * units.Mi  # 16 MiB
        expected_gb = (40 * units.Gi + expected_extra) / float(units.Gi)
        self.assertAlmostEqual(extended_gb, expected_gb, places=6)

        # Image copy should have been called with encrypted=True
        drv._copy_image_data_to_volume.assert_called_once_with(
            mock.sentinel.ctx, volume, mock.sentinel.img_svc, "img-1",
            encrypted=True, disable_sparse=False,
        )

        # LV should be shrunk back after the copy (deactivate, resize, activate)
        self.assertEqual(mock_lvresize_execute.call_count, 3)
        resize_args = mock_lvresize_execute.call_args_list[1][0]
        self.assertEqual("lvresize", resize_args[0])
        # Target size should be the original volume size
        self.assertEqual("40g", resize_args[3])

    @mock.patch("cinder_rxt.rackspace.putils.execute")
    def test_shrinks_lv_even_when_image_copy_fails(
        self, mock_lvresize_execute
    ):
        drv = self._make_driver()
        volume = {"id": "v2", "name": "vol-fail", "size": 10}

        drv.extend_volume = mock.Mock()
        drv._copy_image_data_to_volume = mock.Mock(
            side_effect=Exception("image copy failed")
        )

        with self.assertRaises(Exception, msg="image copy failed"):
            drv.copy_image_to_encrypted_volume(
                context=mock.sentinel.ctx,
                volume=volume,
                image_service=mock.sentinel.img_svc,
                image_id="img-2",
            )

        # LV should still be shrunk back despite the failure
        # (deactivate, resize, activate = 3 calls)
        self.assertEqual(mock_lvresize_execute.call_count, 3)

    @mock.patch("cinder_rxt.rackspace.putils.execute")
    def test_lvresize_failure_is_non_fatal(self, mock_execute):
        """If lvresize fails after image copy, log warning but don't raise."""
        def _selective_fail(*args, **kwargs):
            if "lvresize" in args:
                raise putils.ProcessExecutionError(
                    exit_code=1, stderr="lvresize failed")

        mock_execute.side_effect = _selective_fail
        drv = self._make_driver()
        volume = {"id": "v3", "name": "vol-shrink-fail", "size": 10}

        drv.extend_volume = mock.Mock()
        drv._copy_image_data_to_volume = mock.Mock()

        # Should not raise
        drv.copy_image_to_encrypted_volume(
            context=mock.sentinel.ctx,
            volume=volume,
            image_service=mock.sentinel.img_svc,
            image_id="img-3",
        )

        # Image copy should have succeeded
        drv._copy_image_data_to_volume.assert_called_once()

    @mock.patch("cinder_rxt.rackspace.putils.execute")
    def test_extra_extents_scale_with_pe_size(self, mock_lvresize_execute):
        """With larger PE sizes, fewer extents are needed."""
        drv = self._make_driver()
        # 16 MiB extents: only 1 extent needed for 16 MiB LUKS header
        drv.vg = FakeVG(extent_size_mib=16)
        volume = {"id": "v4", "name": "vol-big-pe", "size": 10}

        drv.extend_volume = mock.Mock()
        drv._copy_image_data_to_volume = mock.Mock()

        drv.copy_image_to_encrypted_volume(
            context=mock.sentinel.ctx,
            volume=volume,
            image_service=mock.sentinel.img_svc,
            image_id="img-4",
        )

        _, extended_gb = drv.extend_volume.call_args[0]
        # 1 extent of 16 MiB
        expected_gb = (10 * units.Gi + 16 * units.Mi) / float(units.Gi)
        self.assertAlmostEqual(extended_gb, expected_gb, places=6)


if __name__ == "__main__":
    unittest.main()
