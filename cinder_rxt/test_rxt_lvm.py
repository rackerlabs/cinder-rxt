import unittest
from unittest import mock

from oslo_utils import units

from cinder import exception

from cinder_rxt import rackspace
from cinder_rxt.rackspace import RXTLVM
from cinder_rxt.rackspace import _LUKS1_HEADER_BYTES
from cinder_rxt.rackspace import _LUKS2_HEADER_BYTES


GI = units.Gi
MI = units.Mi


class FakeVolume(dict):
    """dict with attribute access, like a cinder Volume object."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def enc_vol(size=40):
    return FakeVolume(id="vol-enc", name="vol-enc", size=size,
                      encryption_key_id="key-1")


class Base(unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.drv = RXTLVM.__new__(RXTLVM)
        self.drv.vg = mock.Mock()
        self.drv.db = mock.Mock()
        self.drv.db.volume_encryption_metadata_get.return_value = {
            "provider": "luks"}
        self.image_service = mock.Mock()
        self.ctx = mock.sentinel.ctx

    def set_image(self, virtual_size):
        self.image_service.show.return_value = {"id": "img-1",
                                                "virtual_size": virtual_size}


class TestUpstreamSizing(unittest.TestCase):
    """The driver must not override any LV sizing path."""

    def test_no_sizing_overrides(self):
        for name in ("create_volume", "extend_volume",
                     "create_volume_from_snapshot", "create_cloned_volume",
                     "migrate_volume", "manage_existing_get_size",
                     "manage_existing_object_get_size",
                     "copy_image_to_volume", "_copy_image_data_to_volume"):
            self.assertIs(getattr(RXTLVM, name),
                          getattr(rackspace.lvm.LVMVolumeDriver, name),
                          "%s must be upstream" % name)

    def test_no_resize_helpers(self):
        for name in ("_lvresize", "_encryption_pad_bytes",
                     "_lv_size_str_for", "_copy_lv"):
            self.assertFalse(hasattr(RXTLVM, name))


class TestHeaderBytes(unittest.TestCase):

    def test_luks1(self):
        for p in ("luks", "LUKS", "nova.volume.encryptors.luks.LuksEncryptor",
                  "os_brick.encryptors.luks.LuksEncryptor"):
            self.assertEqual(_LUKS1_HEADER_BYTES,
                             RXTLVM._luks_header_bytes(p))

    def test_luks2(self):
        for p in ("luks2", "LUKS2", "os_brick.encryptors.luks.Luks2Encryptor"):
            self.assertEqual(_LUKS2_HEADER_BYTES,
                             RXTLVM._luks_header_bytes(p))

    def test_unknown_is_smallest_header(self):
        # Never reject an image that would actually fit.
        self.assertEqual(_LUKS1_HEADER_BYTES, RXTLVM._luks_header_bytes(None))
        self.assertEqual(_LUKS1_HEADER_BYTES, RXTLVM._luks_header_bytes(""))
        self.assertEqual(_LUKS1_HEADER_BYTES,
                         RXTLVM._luks_header_bytes("plain"))


class TestPreCheck(Base):

    def check(self, vol=None):
        self.drv._check_image_fits_encrypted_volume(
            self.ctx, vol or enc_vol(), self.image_service, "img-1")

    def test_small_image_passes(self):
        self.set_image(4 * GI)
        self.check()

    def test_image_exactly_usable_passes(self):
        self.set_image(40 * GI - 2 * MI)
        self.check()

    def test_full_size_image_rejected(self):
        """OSPC-2358: 40 GiB Nova local-disk snapshot into 40 GiB volume."""
        self.set_image(40 * GI)
        with self.assertRaises(exception.ImageTooBig) as cm:
            self.check()
        msg = str(cm.exception)
        self.assertIn("img-1", msg)
        self.assertIn("vol-enc", msg)
        self.assertIn("2 MiB LUKS header", msg)
        self.assertIn("at least 41 GiB", msg)

    def test_one_byte_over_rejected(self):
        self.set_image(40 * GI - 2 * MI + 1)
        self.assertRaises(exception.ImageTooBig, self.check)

    def test_full_size_image_fits_with_extra_gib(self):
        self.set_image(40 * GI)
        self.check(enc_vol(size=41))

    def test_luks2_provider_uses_larger_header(self):
        self.drv.db.volume_encryption_metadata_get.return_value = {
            "provider": "luks2"}
        # Fits LUKS1 (2 MiB) but not LUKS2 (16 MiB)
        self.set_image(40 * GI - 8 * MI)
        with self.assertRaises(exception.ImageTooBig) as cm:
            self.check()
        self.assertIn("16 MiB LUKS header", str(cm.exception))

    def test_min_gib_accounts_for_header(self):
        # Image is exactly 41 GiB; needs 41 GiB + 2 MiB -> 42 GiB
        self.set_image(41 * GI)
        with self.assertRaises(exception.ImageTooBig) as cm:
            self.check(enc_vol(size=41))
        self.assertIn("at least 42 GiB", str(cm.exception))

    def test_missing_virtual_size_skips_check(self):
        self.image_service.show.return_value = {"id": "img-1"}
        self.check()
        self.image_service.show.return_value = {"id": "img-1",
                                                "virtual_size": None}
        self.check()

    def test_image_show_failure_skips_check(self):
        self.image_service.show.side_effect = RuntimeError("glance down")
        self.check()

    def test_encryption_metadata_failure_assumes_luks1(self):
        self.drv.db.volume_encryption_metadata_get.side_effect = (
            RuntimeError("db down"))
        self.set_image(40 * GI - 2 * MI)
        self.check()
        self.set_image(40 * GI)
        self.assertRaises(exception.ImageTooBig, self.check)

    def test_encryption_metadata_none_assumes_luks1(self):
        self.drv.db.volume_encryption_metadata_get.return_value = None
        self.set_image(40 * GI - 2 * MI)
        self.check()


class TestCopyImageToEncryptedVolume(Base):

    def test_precheck_then_upstream(self):
        self.set_image(4 * GI)
        vol = enc_vol()
        with mock.patch.object(rackspace.lvm.LVMVolumeDriver,
                               "copy_image_to_encrypted_volume") as up:
            self.drv.copy_image_to_encrypted_volume(
                self.ctx, vol, self.image_service, "img-1",
                disable_sparse=True)
        up.assert_called_once_with(self.ctx, vol, self.image_service,
                                   "img-1", disable_sparse=True)

    def test_rejected_image_never_reaches_upstream(self):
        self.set_image(40 * GI)
        with mock.patch.object(rackspace.lvm.LVMVolumeDriver,
                               "copy_image_to_encrypted_volume") as up:
            self.assertRaises(
                exception.ImageTooBig,
                self.drv.copy_image_to_encrypted_volume,
                self.ctx, enc_vol(), self.image_service, "img-1")
        up.assert_not_called()

    def test_lv_never_resized(self):
        self.set_image(4 * GI)
        self.drv._copy_image_data_to_volume = mock.Mock()
        self.drv.copy_image_to_encrypted_volume(
            self.ctx, enc_vol(), self.image_service, "img-1")
        self.drv.vg.extend_volume.assert_not_called()
        self.drv._copy_image_data_to_volume.assert_called_once()


if __name__ == "__main__":
    unittest.main()
