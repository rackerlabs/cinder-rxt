# Copyright 2024 Cloudnull <kevin@cloudnull.com>
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

import math
import string
import textwrap

from oslo_concurrency import processutils as putils
from oslo_log import log as logging
from oslo_utils import units

from cinder.volume.drivers import lvm
from cinder.volume.targets import tgt

LOG = logging.getLogger(__name__)

# LUKS1 header is 2 MiB; LUKS2 header is 16 MiB.  We use 16 MiB to
# cover both formats.  This is the minimum extra space the LV needs so
# that the decrypted dm-crypt device is at least as large as the
# requested volume size.
_LUKS_HEADER_BYTES = 16 * units.Mi

RXT_VOLUME_CONF_TEMPLATE = string.Template(
    """
<target %(name)s>
    backing-store %(path)s
    driver %(driver)s
    %(chap_auth)s
    %(target_flags)s
    scsi_sn $SCSI_SN
    scsi_id $SCSI_SN
    write-cache %(write_cache)s
</target>
"""
)


class RXTTgtAdm(tgt.TgtAdm):
    """Target object for block storage devices.

    Base class for target object, where target
    is data transport mechanism (target) specific calls.
    This includes things like create targets, attach, detach
    etc.
    """

    def create_iscsi_target(
        self, name, tid, lun, path, chap_auth=None, **kwargs
    ):
        """Create a target for ISCSI and return target info.

        This method sets and resets the volume configuration to ensure that the
        SCSI_SN is defined for the return and unset after.
        """

        volume_conf_copy = self.VOLUME_CONF
        try:
            self.VOLUME_CONF = textwrap.dedent(
                RXT_VOLUME_CONF_TEMPLATE.safe_substitute(
                    SCSI_SN=name.split(":")[1]
                )
            )
            return super().create_iscsi_target(
                name, tid, lun, path, chap_auth, **kwargs
            )
        finally:
            self.VOLUME_CONF = volume_conf_copy


class RXTLVM(lvm.LVMVolumeDriver):
    """Rackspace LVM Driver.

    This class is used to create a new Cinder driver that gives us control of
    the TGT helper and to work around a sizing issue when writing images to
    encrypted LVM volumes.

    When cinder writes an image to an encrypted volume the LUKS header
    consumes space on the LV, leaving the decrypted dm-crypt device
    smaller than the requested volume size.  For example a 40 GiB LV
    yields only ~39.998 GiB of usable space after LUKS1 formatting
    (or ~39.984 GiB with LUKS2).  ``qemu-img convert`` then fails with
    "Cannot grow device files" because the image's virtual size exceeds
    the decrypted device.

    The fix is minimal: before writing the image we extend the LV by
    enough VG physical extents to cover the LUKS header, then shrink it
    back after the write completes so that backup tools that capture the
    raw block device see the correct size.
    """

    def __init__(self, vg_obj=None, *args, **kwargs):

        super(RXTLVM, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(lvm.volume_opts)
        self.hostname = lvm.socket.gethostname()
        self.vg = vg_obj
        self.backend_name = (
            self.configuration.safe_get("volume_backend_name") or "LVM"
        )

        LOG.debug(
            "Attempting to initialize LVM driver with the "
            "following target_driver: RXTTgtAdm"
        )

        self.target_driver = lvm.importutils.import_object(
            "cinder_rxt.rackspace.RXTTgtAdm",
            configuration=self.configuration,
            executor=self._execute,
        )

        self.protocol = (
            self.target_driver.storage_protocol or self.target_driver.protocol
        )

        self._sparse_copy_volume = False

    # ------------------------------------------------------------------
    # LUKS header compensation for encrypted image-to-volume copies
    # ------------------------------------------------------------------

    def _get_extent_size_bytes(self):
        """Return the VG physical extent size in bytes.

        Uses the same LVM_CMD_PREFIX as the brick LVM class to ensure
        rootwrap filter compatibility.
        """
        cmd = self.vg.LVM_CMD_PREFIX + [
            "vgs", "--noheadings", "--nosuffix", "--units", "m",
            "-o", "vg_extent_size", self.vg.vg_name,
        ]
        out, _err = self.vg._execute(
            *cmd, root_helper=self.vg._root_helper, run_as_root=True)
        return int(float(out.strip()) * units.Mi)

    def _lvresize(self, volume, size_str):
        """Resize an LV using lvresize (supports both grow and shrink).

        This is needed because os-brick's LVM class only exposes
        ``extend_volume`` (lvextend) which cannot shrink.

        The LV is deactivated before shrinking because dm-crypt or
        iSCSI holders may not have been fully released yet.  After
        the resize the LV is reactivated.

        Commands use bare invocations (no env prefix) matched by
        rootwrap ``CommandFilter`` entries.
        """
        lv_path = "%s/%s" % (self.vg.vg_name, volume["name"])
        _exec = putils.execute
        rh = self.vg._root_helper

        _exec("lvchange", "-an", lv_path,
              run_as_root=True, root_helper=rh)
        try:
            _exec("lvresize", "-f", "-L", size_str, lv_path,
                  run_as_root=True, root_helper=rh)
        finally:
            try:
                _exec("lvchange", "-ay", "-K", lv_path,
                      run_as_root=True, root_helper=rh)
            except putils.ProcessExecutionError:
                LOG.exception(
                    "Failed to reactivate LV %s after resize attempt. "
                    "Manual intervention may be required.", lv_path)

    def copy_image_to_encrypted_volume(
        self, context, volume, image_service, image_id,
        disable_sparse=False,
    ):
        """Fetch image and write to an encrypted volume.

        Before writing, temporarily extend the LV by enough extents to
        cover the LUKS header so it does not eat into the usable space.
        After the write succeeds, shrink the LV back to the original
        size to prevent backup tools from capturing the oversized block
        device.
        """
        extent_bytes = self._get_extent_size_bytes()
        # LUKS2 header is 16 MiB and the default PE is 4 MiB, so we
        # may need up to 4 extents.  Calculate the minimum number of
        # extents to cover the LUKS header.
        extra_extents = int(math.ceil(_LUKS_HEADER_BYTES / extent_bytes))
        extra_bytes = extra_extents * extent_bytes

        original_bytes = int(volume["size"]) * units.Gi
        extended_bytes = original_bytes + extra_bytes
        original_str = self._sizestr(volume["size"])

        LOG.info(
            "Extending LV for encrypted volume %(vol)s by %(extra)d bytes "
            "(%(extents)d extents) to accommodate LUKS header before "
            "image copy.",
            {"vol": volume["id"], "extra": extra_bytes,
             "extents": extra_extents},
        )

        self.extend_volume(volume, extended_bytes / float(units.Gi))

        try:
            # Delegate to the base driver which handles attaching,
            # LUKS encryptor setup, fetch_to_raw, and detaching.
            self._copy_image_data_to_volume(
                context, volume, image_service, image_id,
                encrypted=True, disable_sparse=disable_sparse,
            )
        finally:
            # Always attempt to shrink back, even on failure, so we
            # don't leave an oversized LV.
            try:
                self._lvresize(volume, original_str)
                LOG.info(
                    "LV for volume %(vol)s shrunk back to %(size)s after "
                    "encrypted image copy.",
                    {"vol": volume["id"], "size": original_str},
                )
            except putils.ProcessExecutionError as e:
                LOG.warning(
                    "Failed to shrink LV for volume %(vol)s back to "
                    "%(size)s. The LV will remain at the extended size. "
                    "stderr=%(err)s",
                    {"vol": volume["id"], "size": original_str,
                     "err": e.stderr},
                )
