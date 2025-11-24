import unittest
from unittest import mock

from cinder_rxt.rackspace import RXTLVM


class FakeVG:
    def __init__(self, extent_size_mib=4):
        # Match typical LVM extent size (e.g., 4 MiB)
        self.vg_extent_size = extent_size_mib

    def activate_lv(self, name, is_snapshot=False, permanent=False):
        # No-op for tests
        return


class RXTLVMTests(unittest.TestCase):
    def _make_driver(self, **conf_overrides):
        """Create a RXTLVM instance without running the real __init__.

        We only need minimal attributes for the new sizing logic.
        """
        drv = RXTLVM.__new__(RXTLVM)
        drv.vg = FakeVG()

        class Conf:
            safe_size_margin_gb = 0.25
            safe_size_margin_blank_gb = None
            lvm_mirrors = 0
            lvm_type = "default"
            volume_dd_blocksize = 1
            # Needed by LVMVolumeDriver.local_path
            volume_group = "fake-vg"

        cfg = Conf()
        for k, v in conf_overrides.items():
            setattr(cfg, k, v)
        drv.configuration = cfg

        drv._sparse_copy_volume = False
        drv._execute = lambda *a, **k: None  # stub for driver exec
        # Reuse the same _sizestr semantics as base LVM
        drv._sizestr = lambda sz: f"{sz}g"

        return drv

    def test_plain_volume_create_uses_margin_by_default(self):
        drv = self._make_driver()
        volume = {"id": "v1", "name": "vol-plain", "size": 10}

        drv._create_volume = mock.Mock()
        drv._record_safe_size_margin = mock.Mock()

        drv.create_volume(volume)

        drv._create_volume.assert_called_once()
        name, size_str, lvm_type, mirrors = drv._create_volume.call_args[0]
        self.assertEqual("vol-plain", name)
        lv_size_gb = int(size_str.rstrip("g"))
        self.assertGreater(lv_size_gb, volume["size"])
        self.assertEqual("default", lvm_type)
        self.assertEqual(0, mirrors)

        drv._record_safe_size_margin.assert_called_once()
        args, kwargs = drv._record_safe_size_margin.call_args
        _, req_gb, backend_gb, backend_bytes = args
        self.assertEqual(10.0, req_gb)
        self.assertGreater(backend_gb, req_gb)
        self.assertGreater(backend_bytes, 10 * 1024 * 1024 * 1024)
        self.assertEqual("blank", kwargs.get("margin_type"))

    def test_plain_volume_create_can_disable_margin_via_blank_override(self):
        drv = self._make_driver(safe_size_margin_gb=0.5,
                                safe_size_margin_blank_gb=0.0)
        volume = {"id": "v1b", "name": "vol-plain-blank0", "size": 10}

        drv._create_volume = mock.Mock()
        drv._record_safe_size_margin = mock.Mock()

        drv.create_volume(volume)

        drv._create_volume.assert_called_once()
        _name, size_str, _lvm_type, _mirrors = drv._create_volume.call_args[0]
        lv_size_gb = int(size_str.rstrip("g"))
        self.assertEqual(lv_size_gb, volume["size"])
        drv._record_safe_size_margin.assert_not_called()

    @mock.patch("cinder_rxt.rackspace.lvm.volume_utils.copy_volume")
    def test_create_volume_from_snapshot_uses_margin(self, mock_copy_volume):
        drv = self._make_driver(safe_size_margin_gb=0.25,
                                safe_size_margin_blank_gb=0.0)
        volume = {"id": "v2", "name": "vol-from-snap", "size": 10}
        snapshot = {"name": "snap-001", "volume_size": 10}

        drv._create_volume = mock.Mock()
        drv._record_safe_size_margin = mock.Mock()

        drv.create_volume_from_snapshot(volume, snapshot)

        drv._create_volume.assert_called_once()
        _name, size_str, _lvm_type, _mirrors = drv._create_volume.call_args[0]
        # LV size should be strictly larger than requested 10 GiB
        self.assertEqual("vol-from-snap", _name)
        lv_size_gb = int(size_str.rstrip("g"))
        self.assertGreater(lv_size_gb, 10)

        drv._record_safe_size_margin.assert_called_once()
        args, kwargs = drv._record_safe_size_margin.call_args
        _, req_gb, backend_gb, backend_bytes = args
        self.assertEqual(10.0, req_gb)
        self.assertGreater(backend_gb, req_gb)
        self.assertGreater(backend_bytes, 10 * 1024 * 1024 * 1024)
        self.assertEqual("snapshot", kwargs.get("margin_type"))

        # copy_volume should be called with snapshot size in MiB
        mock_copy_volume.assert_called_once()
        src, dst, size_mib, blocksize = mock_copy_volume.call_args[0][:4]
        self.assertEqual(snapshot["volume_size"] * 1024, size_mib)

    @mock.patch("cinder_rxt.rackspace.lvm.volume_utils.copy_volume")
    def test_create_cloned_volume_uses_margin(self, mock_copy_volume):
        drv = self._make_driver(safe_size_margin_gb=0.25,
                                safe_size_margin_blank_gb=0.0)
        volume = {"id": "v3", "name": "vol-clone", "size": 10}
        src_vref = {"id": "src1", "name": "src-vol", "size": 10}

        drv._create_volume = mock.Mock()
        drv._record_safe_size_margin = mock.Mock()
        drv.create_snapshot = mock.Mock()
        drv.delete_snapshot = mock.Mock()

        drv.create_cloned_volume(volume, src_vref)

        drv._create_volume.assert_called_once()
        _name, size_str, _lvm_type, _mirrors = drv._create_volume.call_args[0]
        self.assertEqual("vol-clone", _name)
        lv_size_gb = int(size_str.rstrip("g"))
        self.assertGreater(lv_size_gb, 10)

        drv._record_safe_size_margin.assert_called_once()
        _args, kwargs = drv._record_safe_size_margin.call_args
        self.assertEqual("clone", kwargs.get("margin_type"))
        mock_copy_volume.assert_called_once()

    @mock.patch("cinder_rxt.rackspace.cinder_db.volume_admin_metadata_update")
    @mock.patch("cinder_rxt.rackspace.cinder_context.get_admin_context")
    def test_record_safe_size_margin_writes_expected_admin_metadata_blank(
        self, mock_get_admin_context, mock_volume_admin_metadata_update
    ):
        drv = self._make_driver(safe_size_margin_gb=0.25,
                                safe_size_margin_blank_gb=0.25)
        volume = {"id": "v-meta-blank", "name": "vol-meta-blank", "size": 10}

        drv._create_volume = mock.Mock()

        drv.create_volume(volume)

        mock_get_admin_context.assert_called_once()
        mock_volume_admin_metadata_update.assert_called_once()
        _ctxt, vol_id, metadata, update = \
            mock_volume_admin_metadata_update.call_args[0]

        self.assertEqual("v-meta-blank", vol_id)
        # Margin must be positive and recorded
        margin_str = metadata.get("safe_size_margin_gb")
        self.assertIsNotNone(margin_str)
        self.assertGreater(float(margin_str), 0.0)
        self.assertEqual("blank", metadata.get("safe_size_margin_type"))
        self.assertFalse(update)

    @mock.patch("cinder_rxt.rackspace.cinder_db.volume_admin_metadata_update")
    @mock.patch("cinder_rxt.rackspace.cinder_context.get_admin_context")
    def test_record_safe_size_margin_writes_expected_admin_metadata_snapshot(
        self, mock_get_admin_context, mock_volume_admin_metadata_update
    ):
        drv = self._make_driver(safe_size_margin_gb=0.25,
                                safe_size_margin_blank_gb=0.0)
        volume = {"id": "v-meta-snap", "name": "vol-meta-snap", "size": 10}
        snapshot = {"name": "snap-meta", "volume_size": 10}

        drv._create_volume = mock.Mock()

        drv.create_volume_from_snapshot(volume, snapshot)

        mock_get_admin_context.assert_called_once()
        mock_volume_admin_metadata_update.assert_called_once()
        _ctxt, vol_id, metadata, update = \
            mock_volume_admin_metadata_update.call_args[0]

        self.assertEqual("v-meta-snap", vol_id)
        margin_str = metadata.get("safe_size_margin_gb")
        self.assertIsNotNone(margin_str)
        self.assertGreater(float(margin_str), 0.0)
        self.assertEqual("snapshot", metadata.get("safe_size_margin_type"))
        self.assertFalse(update)

    @mock.patch("cinder_rxt.rackspace.lvm.volume_utils.copy_volume")
    @mock.patch("cinder_rxt.rackspace.cinder_db.volume_admin_metadata_update")
    @mock.patch("cinder_rxt.rackspace.cinder_context.get_admin_context")
    def test_record_safe_size_margin_writes_expected_admin_metadata_clone(
        self,
        mock_get_admin_context,
        mock_volume_admin_metadata_update,
        mock_copy_volume,
    ):
        drv = self._make_driver(safe_size_margin_gb=0.25,
                                safe_size_margin_blank_gb=0.0)
        volume = {"id": "v-meta-clone", "name": "vol-meta-clone", "size": 10}
        src_vref = {"id": "src-meta", "name": "src-vol-meta", "size": 10}

        drv._create_volume = mock.Mock()
        drv.create_snapshot = mock.Mock()
        drv.delete_snapshot = mock.Mock()

        drv.create_cloned_volume(volume, src_vref)

        mock_get_admin_context.assert_called_once()
        mock_volume_admin_metadata_update.assert_called_once()
        _ctxt, vol_id, metadata, update = \
            mock_volume_admin_metadata_update.call_args[0]

        self.assertEqual("v-meta-clone", vol_id)
        margin_str = metadata.get("safe_size_margin_gb")
        self.assertIsNotNone(margin_str)
        self.assertGreater(float(margin_str), 0.0)
        self.assertEqual("clone", metadata.get("safe_size_margin_type"))
        self.assertFalse(update)

    @mock.patch("cinder_rxt.rackspace.lvm.image_utils.fetch_to_raw")
    @mock.patch("cinder_rxt.rackspace.cinder_db.volume_admin_metadata_update")
    @mock.patch("cinder_rxt.rackspace.cinder_context.get_admin_context")
    def test_record_safe_size_margin_writes_expected_admin_metadata_image(
        self,
        mock_get_admin_context,
        mock_volume_admin_metadata_update,
        mock_fetch,
    ):
        drv = self._make_driver(safe_size_margin_gb=0.25,
                                safe_size_margin_blank_gb=0.0)
        volume = {"id": "v-meta-img", "name": "vol-meta-img", "size": 10}

        drv.extend_volume = mock.Mock()

        drv.copy_image_to_volume(
            context=mock.sentinel.ctx,
            volume=volume,
            image_service=mock.sentinel.img_svc,
            image_id="img-meta",
            disable_sparse=False,
        )

        mock_get_admin_context.assert_called_once()
        mock_volume_admin_metadata_update.assert_called_once()
        _ctxt, vol_id, metadata, update = \
            mock_volume_admin_metadata_update.call_args[0]

        self.assertEqual("v-meta-img", vol_id)
        margin_str = metadata.get("safe_size_margin_gb")
        self.assertIsNotNone(margin_str)
        self.assertGreater(float(margin_str), 0.0)
        self.assertEqual("image", metadata.get("safe_size_margin_type"))
        self.assertFalse(update)

    @mock.patch("cinder_rxt.rackspace.lvm.image_utils.fetch_to_raw")
    def test_copy_image_to_volume_extends_and_records_margin(self, mock_fetch):
        drv = self._make_driver(safe_size_margin_gb=0.25,
                                safe_size_margin_blank_gb=0.0)
        volume = {"id": "v4", "name": "vol-img", "size": 10}

        drv.extend_volume = mock.Mock()
        drv._record_safe_size_margin = mock.Mock()

        drv.copy_image_to_volume(
            context=mock.sentinel.ctx,
            volume=volume,
            image_service=mock.sentinel.img_svc,
            image_id="img-123",
            disable_sparse=False,
        )

        # Since margin is applied, extend_volume should be called
        drv.extend_volume.assert_called_once()
        _vol_arg, new_size = drv.extend_volume.call_args[0]
        self.assertIs(_vol_arg, volume)
        self.assertGreater(new_size, volume["size"])

        drv._record_safe_size_margin.assert_called_once()
        _args, kwargs = drv._record_safe_size_margin.call_args
        self.assertEqual("image", kwargs.get("margin_type"))

        mock_fetch.assert_called_once()
        args, kwargs = mock_fetch.call_args
        _ctx, _svc, _img_id, dest_path, blocksize = args[:5]
        # size argument passed to fetch_to_raw must be the requested size
        self.assertEqual(volume["size"], kwargs.get("size"))


if __name__ == "__main__":
    unittest.main()
