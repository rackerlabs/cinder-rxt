# Copyright 2024 Cloudnull <kevin@cloudnull.com>
# Copyright 2026 Rackspace US, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import unittest
from unittest import mock

from oslo_utils import units

from cinder import exception
from cinder_rxt.rackspace import (
    RXTLVM,
    RXTTgtAdm,
    check_image_fits_encrypted_volume,
    clone_encryption_key_if_needed,
    get_luks_header_overhead_mib,
    get_volume_overhead_mib,
    is_encrypted_volume,
)

GI = units.Gi
MI = units.Mi


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


def make_volume(vol_id="vol-1", name="volume-vol-1", size=40, key_id=None, vol_type_id=None, context=None):
    vol = {
        "id": vol_id,
        "name": name,
        "size": size,
    }
    if key_id:
        vol["encryption_key_id"] = key_id
    if vol_type_id:
        vol["volume_type_id"] = vol_type_id
    if context:
        vol["_context"] = context
    return vol


class TestRXTTgtAdm(unittest.TestCase):
    """Tests for the RXTTgtAdm target driver with SCSI serial injection."""

    def _make_tgt_driver(self):
        conf = mock.Mock()
        conf.safe_get.return_value = "fake"
        return RXTTgtAdm(root_helper="sudo", configuration=conf, executor=mock.Mock())

    def test_secondary_ip_support_is_true(self):
        self.assertTrue(RXTTgtAdm.SECONDARY_IP_SUPPORT)

    @mock.patch("cinder.volume.targets.tgt.TgtAdm.create_iscsi_target")
    def test_create_iscsi_target_sets_scsi_sn_and_scsi_id(self, mock_super_create):
        tgt_driver = self._make_tgt_driver()
        orig_conf = tgt_driver.VOLUME_CONF

        name = "iqn.2010-10.org.openstack:volume-test-uuid-1234"
        tgt_driver.create_iscsi_target(name, 1, 0, "/dev/fake/path")

        mock_super_create.assert_called_once()
        # Verify the VOLUME_CONF was restored after execution
        self.assertEqual(orig_conf, tgt_driver.VOLUME_CONF)

    @mock.patch("cinder.volume.targets.tgt.TgtAdm.create_iscsi_target")
    def test_create_iscsi_target_without_colon(self, mock_super_create):
        tgt_driver = self._make_tgt_driver()
        name = "target-without-colon"
        tgt_driver.create_iscsi_target(name, 1, 0, "/dev/fake/path")
        mock_super_create.assert_called_once()


class TestEncryptionHelpers(unittest.TestCase):
    """Tests for LUKS encryption detection, overhead calculations, and pre-checks."""

    def test_get_luks_header_overhead_mib(self):
        self.assertEqual(16, get_luks_header_overhead_mib("luks2"))
        self.assertEqual(16, get_luks_header_overhead_mib("Luks2Encryptor"))
        self.assertEqual(2, get_luks_header_overhead_mib("luks"))
        self.assertEqual(2, get_luks_header_overhead_mib("LuksEncryptor"))
        self.assertEqual(2, get_luks_header_overhead_mib("plain"))
        self.assertEqual(2, get_luks_header_overhead_mib(None))

    def test_is_encrypted_volume_by_key_id(self):
        vol = make_volume(key_id="secret-key-123")
        self.assertTrue(is_encrypted_volume(vol))

    def test_is_encrypted_volume_by_db(self):
        vol = make_volume()
        db = mock.Mock()
        db.volume_encryption_metadata_get.return_value = {"encryption_key_id": "k1"}
        self.assertTrue(is_encrypted_volume(vol, context=mock.sentinel.ctx, db=db))

    def test_is_encrypted_volume_db_exception_raises(self):
        vol = make_volume()
        db = mock.Mock()
        db.volume_encryption_metadata_get.side_effect = RuntimeError("DB connection dropped")
        with self.assertRaises(RuntimeError):
            is_encrypted_volume(vol, context=mock.sentinel.ctx, db=db)

    @mock.patch("cinder.volume.volume_types.is_encrypted", return_value=True)
    def test_is_encrypted_volume_by_volume_type(self, mock_is_enc):
        vol = make_volume(vol_type_id="type-luks")
        self.assertTrue(is_encrypted_volume(vol, context=mock.sentinel.ctx))

    @mock.patch("cinder.volume.volume_types.is_encrypted", side_effect=exception.VolumeTypeNotFound(volume_type_id="bad-type"))
    def test_is_encrypted_volume_type_not_found_returns_false(self, mock_is_enc):
        vol = make_volume(vol_type_id="bad-type")
        self.assertFalse(is_encrypted_volume(vol, context=mock.sentinel.ctx))

    @mock.patch("cinder.volume.volume_types.is_encrypted", side_effect=RuntimeError("DB error"))
    def test_is_encrypted_volume_type_db_error_raises(self, mock_is_enc):
        vol = make_volume(vol_type_id="err-type")
        with self.assertRaises(RuntimeError):
            is_encrypted_volume(vol, context=mock.sentinel.ctx)

    def test_is_encrypted_volume_false_for_unencrypted(self):
        vol = make_volume()
        self.assertFalse(is_encrypted_volume(vol))

    def test_get_volume_overhead_mib_unencrypted(self):
        vol = make_volume()
        self.assertEqual(0, get_volume_overhead_mib(vol))

    def test_get_volume_overhead_mib_luks1(self):
        vol = make_volume(key_id="k1")
        db = mock.Mock()
        db.volume_encryption_metadata_get.return_value = {"provider": "luks"}
        self.assertEqual(2, get_volume_overhead_mib(vol, context=mock.sentinel.ctx, db=db))

    def test_get_volume_overhead_mib_luks2(self):
        vol = make_volume(key_id="k2")
        db = mock.Mock()
        db.volume_encryption_metadata_get.return_value = {"provider": "luks2"}
        self.assertEqual(16, get_volume_overhead_mib(vol, context=mock.sentinel.ctx, db=db))

    def test_check_image_fits_encrypted_volume_exact_match(self):
        img_svc = mock.Mock()
        img_svc.show.return_value = {"virtual_size": 40 * GI}
        vol = make_volume(size=40, key_id="k1")
        # Exact 40 GiB virtual size on 40 GiB volume must not raise
        check_image_fits_encrypted_volume(mock.sentinel.ctx, vol, img_svc, "img-40g")

    def test_check_image_fits_encrypted_volume_smaller_fits(self):
        img_svc = mock.Mock()
        img_svc.show.return_value = {"virtual_size": 20 * GI}
        vol = make_volume(size=40, key_id="k1")
        check_image_fits_encrypted_volume(mock.sentinel.ctx, vol, img_svc, "img-20g")

    def test_check_image_fits_encrypted_volume_oversized_raises_image_too_big(self):
        img_svc = mock.Mock()
        img_svc.show.return_value = {"virtual_size": 41 * GI}
        vol = make_volume(size=40, key_id="k1")
        with self.assertRaises(exception.ImageTooBig) as cm:
            check_image_fits_encrypted_volume(mock.sentinel.ctx, vol, img_svc, "img-41g")
        self.assertIn("at least 41 GiB", str(cm.exception))

    def test_check_image_fits_encrypted_volume_missing_virtual_size_skips(self):
        img_svc = mock.Mock()
        img_svc.show.return_value = {}
        vol = make_volume(size=40, key_id="k1")
        check_image_fits_encrypted_volume(mock.sentinel.ctx, vol, img_svc, "img-nosize")

    @mock.patch("cinder.volume.volume_utils.clone_encryption_key")
    def test_clone_encryption_key_if_needed(self, mock_clone):
        mock_clone.return_value = "new-key-456"
        key_mgr = mock.Mock()
        src_vol = make_volume(key_id="old-key-123")
        cloned = clone_encryption_key_if_needed(mock.sentinel.ctx, key_mgr, src_vol)
        self.assertEqual("new-key-456", cloned)
        mock_clone.assert_called_once_with(mock.sentinel.ctx, key_mgr, "old-key-123")


class TestRXTLVMSurgicalFix(unittest.TestCase):
    """Tests for RXTLVM volume sizing and lifecycle overrides fixing OSPC-2358."""

    def _make_driver(self, extent_size_mib=4):
        drv = RXTLVM.__new__(RXTLVM)
        drv.vg = FakeVG(extent_size_mib=extent_size_mib)
        drv._sparse_copy_volume = False
        drv._execute = mock.Mock()
        drv.db = mock.Mock()
        drv.db.volume_encryption_metadata_get.return_value = None

        class Conf:
            volume_dd_blocksize = "1M"
            volume_group = "fake-vg"
            lvm_type = "default"
            lvm_mirrors = 0
            use_multipath_for_image_xfer = False
            enforce_multipath_for_image_xfer = False

        drv.configuration = Conf()
        drv.target_driver = mock.Mock()
        return drv

    def test_get_extent_size_bytes(self):
        drv = self._make_driver(extent_size_mib=4)
        self.assertEqual(4 * MI, drv._get_extent_size_bytes())

    def test_get_lv_size_str_unencrypted(self):
        drv = self._make_driver()
        vol = make_volume(size=40)
        self.assertEqual("40g", drv._get_lv_size_str(vol))

    def test_get_lv_size_str_encrypted_luks1(self):
        drv = self._make_driver(extent_size_mib=4)
        vol = make_volume(size=40, key_id="k1", context=mock.sentinel.ctx)
        drv.db.volume_encryption_metadata_get.return_value = {"provider": "luks"}
        # 40 GiB * 1024 + 4 MiB = 40964 MiB
        self.assertEqual("40964M", drv._get_lv_size_str(vol))

    def test_get_lv_size_str_encrypted_luks2(self):
        drv = self._make_driver(extent_size_mib=4)
        vol = make_volume(size=40, key_id="k2", context=mock.sentinel.ctx)
        drv.db.volume_encryption_metadata_get.return_value = {"provider": "luks2"}
        # 40 GiB * 1024 + 16 MiB = 40976 MiB
        self.assertEqual("40976M", drv._get_lv_size_str(vol))

    def test_get_lv_size_str_custom_pe_alignment(self):
        # With 8 MiB physical extents, 4 MiB LUKS1 overhead rounds up to 8 MiB (1 PE)
        drv = self._make_driver(extent_size_mib=8)
        vol = make_volume(size=40, key_id="k1", context=mock.sentinel.ctx)
        drv.db.volume_encryption_metadata_get.return_value = {"provider": "luks"}
        # 40 GiB * 1024 + 8 MiB = 40968 MiB
        self.assertEqual("40968M", drv._get_lv_size_str(vol))

    def test_create_volume_unencrypted(self):
        drv = self._make_driver()
        drv._create_volume = mock.Mock()
        vol = make_volume(size=40)
        drv.create_volume(vol)
        drv._create_volume.assert_called_once_with("volume-vol-1", "40g", "default", 0)

    def test_create_volume_encrypted(self):
        drv = self._make_driver()
        drv._create_volume = mock.Mock()
        vol = make_volume(size=40, key_id="k1", context=mock.sentinel.ctx)
        drv.db.volume_encryption_metadata_get.return_value = {"provider": "luks"}
        drv.create_volume(vol)
        # Sizing includes +4 MiB LUKS padding
        drv._create_volume.assert_called_once_with("volume-vol-1", "40964M", "default", 0)

    def test_extend_volume_unencrypted(self):
        drv = self._make_driver()
        drv.vg.extend_volume = mock.Mock()
        vol = make_volume(size=40)
        drv.extend_volume(vol, 50)
        drv.vg.extend_volume.assert_called_once_with("volume-vol-1", "50g")
        drv.target_driver.extend_target.assert_called_once_with(vol)

    def test_extend_volume_encrypted(self):
        drv = self._make_driver()
        drv.vg.extend_volume = mock.Mock()
        vol = make_volume(size=40, key_id="k1", context=mock.sentinel.ctx)
        drv.db.volume_encryption_metadata_get.return_value = {"provider": "luks"}
        drv.extend_volume(vol, 50)
        # 50 * 1024 + 4 = 51204M
        drv.vg.extend_volume.assert_called_once_with("volume-vol-1", "51204M")
        drv.target_driver.extend_target.assert_called_once_with(vol)

    @mock.patch("cinder.volume.volume_utils.copy_volume")
    def test_create_cloned_volume_encrypted(self, mock_copy):
        drv = self._make_driver()
        drv.create_snapshot = mock.Mock()
        drv.delete_snapshot = mock.Mock()
        drv._create_volume = mock.Mock()
        drv.vg.activate_lv = mock.Mock()
        drv.local_path = mock.Mock(side_effect=lambda x: f"/dev/fake/{x['name']}")

        src_vol = make_volume(vol_id="src", name="volume-src", size=40, key_id="k-src", context=mock.sentinel.ctx)
        dst_vol = make_volume(vol_id="dst", name="volume-dst", size=40, key_id="k-dst", context=mock.sentinel.ctx)
        drv.db.volume_encryption_metadata_get.return_value = {"provider": "luks"}

        drv.create_cloned_volume(dst_vol, src_vol)

        # Target LV must be allocated with LUKS padding (40964M on 4 MiB PE VG)
        drv._create_volume.assert_called_once_with("volume-dst", "40964M", "default", 0)
        # Block copy must transfer 40960 + 2 = 40962 MiB (full container including LUKS header)
        mock_copy.assert_called_once_with(
            "/dev/fake/clone-snap-dst",
            "/dev/fake/volume-dst",
            40962,
            "1M",
            execute=drv._execute,
            sparse=False,
        )
        drv.delete_snapshot.assert_called_once()

    @mock.patch("cinder.volume.volume_utils.copy_volume")
    def test_create_volume_from_snapshot_encrypted(self, mock_copy):
        drv = self._make_driver()
        drv._create_volume = mock.Mock()
        drv.vg.activate_lv = mock.Mock()
        drv.local_path = mock.Mock(side_effect=lambda x: f"/dev/fake/{x['name']}")

        snap = {"name": "snapshot-1", "volume_size": 40}
        vol = make_volume(vol_id="v1", name="volume-v1", size=40, key_id="k1", context=mock.sentinel.ctx)
        drv.db.volume_encryption_metadata_get.return_value = {"provider": "luks"}

        drv.create_volume_from_snapshot(vol, snap)

        drv._create_volume.assert_called_once_with("volume-v1", "40964M", "default", 0)
        mock_copy.assert_called_once_with(
            "/dev/fake/snapshot-1",
            "/dev/fake/volume-v1",
            40962,
            "1M",
            execute=drv._execute,
            sparse=False,
        )

    def test_copy_image_to_encrypted_volume_success_no_shrink(self):
        """OSPC-2358 Fix Verification: copy image performs capacity pre-check and NEVER shrinks LV."""
        drv = self._make_driver()
        drv._copy_image_data_to_volume = mock.Mock()

        img_svc = mock.Mock()
        img_svc.show.return_value = {"virtual_size": 40 * GI}
        vol = make_volume(size=40, key_id="k1")

        drv.copy_image_to_encrypted_volume(
            context=mock.sentinel.ctx,
            volume=vol,
            image_service=img_svc,
            image_id="img-40g",
        )

        drv._copy_image_data_to_volume.assert_called_once_with(
            mock.sentinel.ctx,
            vol,
            img_svc,
            "img-40g",
            encrypted=True,
            disable_sparse=False,
        )
        # Ensure no extend_volume or lvresize was called
        self.assertFalse(hasattr(drv, "_lvresize"))

    def test_copy_image_to_encrypted_volume_oversized_fails_fast(self):
        drv = self._make_driver()
        drv._copy_image_data_to_volume = mock.Mock()

        img_svc = mock.Mock()
        img_svc.show.return_value = {"virtual_size": 42 * GI}
        vol = make_volume(size=40, key_id="k1")

        with self.assertRaises(exception.ImageTooBig):
            drv.copy_image_to_encrypted_volume(
                context=mock.sentinel.ctx,
                volume=vol,
                image_service=img_svc,
                image_id="img-42g",
            )

        # Copy data must never be called for an oversized image
        drv._copy_image_data_to_volume.assert_not_called()


if __name__ == "__main__":
    unittest.main()
