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

from oslo_log import log as logging
from oslo_utils import units

from cinder import exception
from cinder.volume.drivers import lvm
from cinder.volume.targets import tgt

LOG = logging.getLogger(__name__)

# LUKS headers live in-band at the front of the block device, so the
# decrypted dm-crypt payload presented to the guest is smaller than the
# volume by the header size.  os-brick formats with an explicit
# ``--type``: provider ``luks`` -> LUKS1 (payload offset 4096 sectors),
# provider ``luks2`` -> LUKS2 (default 16 MiB metadata + keyslot area).
_LUKS1_HEADER_BYTES = 2 * units.Mi
_LUKS2_HEADER_BYTES = 16 * units.Mi

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
    the TGT helper.

    Volume sizing is deliberately identical to the upstream LVM driver.
    Upstream carves the LUKS header out of the requested size, so an
    encrypted volume's usable capacity is the nominal size minus the
    header (2 MiB for LUKS1).  This is documented and accepted upstream
    (spec "Sizing encrypted volumes", Xena) and every copy path in the
    manager (migrate, retype, backup, clone) assumes it.  Padding the LV
    to compensate makes the guest see *more* than nominal; growpart then
    consumes the excess and any later copy into an exact-size backend
    (e.g. retype to an unencrypted NetApp type) truncates the partition
    table.  See OSPC-2358.

    The one behavioural change is a fail-fast check when writing an image
    to an encrypted volume: if the image's virtual size cannot fit in the
    usable payload, raise ``ImageTooBig`` with the size the user
    needs to request, instead of downloading the image and failing later
    in ``qemu-img convert`` with "Cannot grow device files".
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
    # Fail-fast sizing check for encrypted image-to-volume copies
    # ------------------------------------------------------------------

    @staticmethod
    def _luks_header_bytes(provider):
        """Return the LUKS header size for an encryption provider string.

        Provider may be the short format name (``luks``, ``luks2``) or a
        legacy class path (``os_brick.encryptors.luks.Luks2Encryptor``).
        Anything not recognisably LUKS2 is treated as LUKS1, the smaller
        header, so the check never rejects an image that would fit.
        """
        if provider and "luks2" in provider.lower():
            return _LUKS2_HEADER_BYTES
        return _LUKS1_HEADER_BYTES

    def _check_image_fits_encrypted_volume(
        self, context, volume, image_service, image_id
    ):
        """Raise ImageTooBig if the image cannot fit the LUKS payload.

        Silently returns if the image's virtual size is unknown; upstream
        behaviour then applies.
        """
        try:
            image_meta = image_service.show(context, image_id)
            virtual_size = image_meta.get("virtual_size")
        except Exception:
            LOG.debug("Unable to read virtual_size for image %s; skipping "
                      "encrypted capacity pre-check.", image_id,
                      exc_info=True)
            return
        if not virtual_size:
            return
        virtual_size = int(virtual_size)

        try:
            encryption = self.db.volume_encryption_metadata_get(
                context, volume.id)
            provider = (encryption or {}).get("provider")
        except Exception:
            LOG.debug("Unable to read encryption metadata for volume %s; "
                      "assuming LUKS1 header.", volume.id, exc_info=True)
            provider = None
        header = self._luks_header_bytes(provider)

        nominal = int(volume["size"]) * units.Gi
        usable = nominal - header
        if virtual_size <= usable:
            return

        min_gib = int(math.ceil((virtual_size + header) / float(units.Gi)))
        reason = (
            "image virtual size %(vsize)d bytes exceeds the usable capacity "
            "of encrypted volume %(vol)s (%(nominal)d GiB requested minus "
            "%(header)d MiB LUKS header = %(usable)d bytes). Encrypted "
            "volumes lose the LUKS header from the requested size; request "
            "a volume of at least %(min_gib)d GiB for this image."
            % {"vsize": virtual_size, "vol": volume["id"],
               "nominal": int(volume["size"]), "header": header // units.Mi,
               "usable": usable, "min_gib": min_gib}
        )
        LOG.error("Image %(image)s does not fit encrypted volume: %(reason)s",
                  {"image": image_id, "reason": reason})
        raise exception.ImageTooBig(image_id=image_id, reason=reason)

    def copy_image_to_encrypted_volume(
        self, context, volume, image_service, image_id,
        disable_sparse=False,
    ):
        """Fetch image and write to an encrypted volume.

        Identical to upstream except for the pre-check.  The LV is never
        resized around the copy: the LUKS payload occupies everything
        after the header, so shrinking the LV truncates the image.
        """
        self._check_image_fits_encrypted_volume(
            context, volume, image_service, image_id)
        super(RXTLVM, self).copy_image_to_encrypted_volume(
            context, volume, image_service, image_id,
            disable_sparse=disable_sparse,
        )
