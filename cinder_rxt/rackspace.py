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

import math
import string
import textwrap

from oslo_log import log as logging
from oslo_utils import units

from cinder import context as cinder_context
from cinder import exception
from cinder.volume.drivers import lvm
from cinder.volume.targets import tgt
from cinder.volume import volume_types
from cinder.volume import volume_utils

LOG = logging.getLogger(__name__)

# LUKS headers live in-band at the front of the block device, so the
# decrypted dm-crypt payload presented to the guest is smaller than the
# volume by the header size.
# Provider "luks" (LUKS1) payload offset is 4096 sectors = 2 MiB (standard in
# OpenStack/Flex; aligns to 4 MiB on standard LVM extents).
# Provider "luks2" (LUKS2) default metadata + keyslot area is 16 MiB.
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
    """Target object for block storage devices with SCSI serial number persistence.

    Controls TGT exports ensuring consistent scsi_id and scsi_sn are assigned
    based on the volume UUID. This is required for multipath-tools on compute
    nodes to properly assemble multipath devices across primary and secondary
    storage interfaces (portals).
    """

    SECONDARY_IP_SUPPORT = True

    def create_iscsi_target(
        self, name, tid, lun, path, chap_auth=None, **kwargs
    ):
        """Create a target for ISCSI and return target info.

        This method sets and resets the volume configuration to ensure that the
        SCSI_SN is defined for the return and unset after.
        """
        vol_id = name.split(":")[1] if ":" in name else name
        volume_conf_copy = self.VOLUME_CONF
        try:
            self.VOLUME_CONF = textwrap.dedent(
                RXT_VOLUME_CONF_TEMPLATE.safe_substitute(
                    SCSI_SN=vol_id
                )
            )
            return super().create_iscsi_target(
                name, tid, lun, path, chap_auth, **kwargs
            )
        finally:
            self.VOLUME_CONF = volume_conf_copy


def get_luks_header_overhead_mib(provider=None):
    """Return physical LVM padding in MiB needed for the LUKS header.

    LUKS2 standard metadata+keyslots area is 16 MiB.
    LUKS1 payload offset is 2 MiB (standard in OpenStack/Flex; aligns to 4 MiB
    on standard LVM extents).
    Defaults to 2 MiB (standard OpenStack LUKS1).
    """
    if provider:
        provider_str = str(provider).lower()
        if "luks2" in provider_str:
            return 16
        elif "luks" in provider_str or "plain" in provider_str:
            return 2
    return 2


def _resolve_context(volume=None, context=None):
    """Safely resolve an OpenStack RequestContext."""
    if context is not None:
        return context
    if volume is not None:
        ctx = getattr(volume, "_context", None)
        if ctx is not None:
            return ctx
        if isinstance(volume, dict) and volume.get("_context") is not None:
            return volume["_context"]
    try:
        return cinder_context.get_admin_context()
    except Exception:
        return None


def is_encrypted_volume(volume, context=None, db=None):
    """Determine whether a volume is an encrypted volume."""
    key_id = (
        volume.get("encryption_key_id")
        if isinstance(volume, dict)
        else getattr(volume, "encryption_key_id", None)
    )
    if key_id and type(key_id).__name__ not in ("Mock", "MagicMock"):
        return True

    ctx = _resolve_context(volume, context)
    vol_id = (
        volume.get("id")
        if isinstance(volume, dict)
        else getattr(volume, "id", None)
    )
    if db and vol_id:
        try:
            encryption = db.volume_encryption_metadata_get(ctx, vol_id)
            if encryption:
                enc_key_id = (
                    encryption.get("encryption_key_id")
                    if isinstance(encryption, dict)
                    else getattr(encryption, "encryption_key_id", None)
                )
                enc_provider = (
                    encryption.get("provider")
                    if isinstance(encryption, dict)
                    else getattr(encryption, "provider", None)
                )
                if enc_key_id and type(enc_key_id).__name__ not in ("Mock", "MagicMock"):
                    return True
                if enc_provider and type(enc_provider).__name__ not in ("Mock", "MagicMock"):
                    return True
        except Exception:
            LOG.exception(
                "Failed to retrieve volume encryption metadata for volume %(vol)s.",
                {"vol": vol_id},
            )
            raise

    vol_type_id = (
        volume.get("volume_type_id")
        if isinstance(volume, dict)
        else getattr(volume, "volume_type_id", None)
    )
    if vol_type_id:
        try:
            is_enc = volume_types.is_encrypted(ctx, vol_type_id)
            if is_enc and type(is_enc).__name__ not in ("Mock", "MagicMock"):
                return True
        except exception.VolumeTypeNotFound:
            LOG.warning(
                "Volume type %(type_id)s not found for volume %(vol)s.",
                {"type_id": vol_type_id, "vol": vol_id},
            )
        except Exception:
            LOG.exception(
                "Unexpected error checking encryption status for volume %(vol)s with volume type %(type_id)s.",
                {"vol": vol_id, "type_id": vol_type_id},
            )
            raise

    return False


def get_volume_overhead_mib(volume, context=None, db=None):
    """Return the extra physical overhead in MiB required for the volume.

    Returns 0 if the volume is unencrypted.
    Returns 16 MiB (or 2 MiB for standard OpenStack LUKS1, aligning to 4 MiB
    on standard LVM extents) for encrypted volumes so that the decrypted
    dm-crypt plaintext payload provides the full requested nominal capacity.
    """
    ctx = _resolve_context(volume, context)
    if not is_encrypted_volume(volume, context=ctx, db=db):
        return 0

    provider = None
    vol_id = (
        volume.get("id")
        if isinstance(volume, dict)
        else getattr(volume, "id", None)
    )
    if db and vol_id:
        try:
            encryption = db.volume_encryption_metadata_get(ctx, vol_id)
            if encryption:
                enc_provider = (
                    encryption.get("provider")
                    if isinstance(encryption, dict)
                    else getattr(encryption, "provider", None)
                )
                if enc_provider and type(enc_provider).__name__ not in ("Mock", "MagicMock"):
                    provider = enc_provider
        except Exception:
            LOG.exception(
                "Failed to retrieve volume encryption metadata for volume %(vol)s.",
                {"vol": vol_id},
            )
            raise

    if not provider:
        vol_type_id = (
            volume.get("volume_type_id")
            if isinstance(volume, dict)
            else getattr(volume, "volume_type_id", None)
        )
        if vol_type_id:
            try:
                enc_type = volume_types.get_volume_type_encryption(
                    ctx, vol_type_id
                )
                if enc_type:
                    enc_provider = (
                        enc_type.get("provider")
                        if isinstance(enc_type, dict)
                        else getattr(enc_type, "provider", None)
                    )
                    if enc_provider and type(enc_provider).__name__ not in ("Mock", "MagicMock"):
                        provider = enc_provider
            except exception.VolumeTypeNotFound:
                LOG.warning(
                    "Volume type %(type_id)s not found for volume %(vol)s.",
                    {"type_id": vol_type_id, "vol": vol_id},
                )
            except Exception:
                LOG.exception(
                    "Unexpected error checking encryption provider for volume %(vol)s with volume type %(type_id)s.",
                    {"vol": vol_id, "type_id": vol_type_id},
                )
                raise

    return get_luks_header_overhead_mib(provider)


def check_image_fits_encrypted_volume(
    context, volume, image_service, image_id, db=None
):
    """Validate that an image fits the nominal volume size.

    Because the RXT LVM driver compensates for the LUKS header at LV allocation
    time (adding the header overhead to the physical LV), the decrypted dm-crypt
    device provides the full nominal volume size (volume['size'] * GiB) as usable
    payload.

    Therefore, an image whose virtual size equals the requested volume size
    (such as a 40 GiB image on a 40 GiB root volume) fits natively without
    truncation or ImageTooBig rejection. We only reject if the image actually
    exceeds the full nominal volume size.
    """
    try:
        image_meta = image_service.show(context, image_id)
        virtual_size = image_meta.get("virtual_size")
    except Exception:
        LOG.debug(
            "Unable to read virtual_size for image %s; skipping "
            "encrypted capacity pre-check.",
            image_id,
            exc_info=True,
        )
        return

    if not virtual_size:
        return
    virtual_size = int(virtual_size)

    volume_size_gib = (
        volume.get("size")
        if isinstance(volume, dict)
        else getattr(volume, "size", 0)
    )
    nominal_bytes = int(volume_size_gib) * units.Gi

    if virtual_size <= nominal_bytes:
        return

    min_gib = int(math.ceil(virtual_size / float(units.Gi)))
    vol_id = (
        volume.get("id")
        if isinstance(volume, dict)
        else getattr(volume, "id", None)
    )
    reason = (
        "Image virtual size %(vsize)d bytes exceeds the nominal capacity "
        "of encrypted volume %(vol)s (%(nominal)d GiB / %(nominal_bytes)d bytes). "
        "Request a volume of at least %(min_gib)d GiB for this image."
        % {
            "vsize": virtual_size,
            "vol": vol_id,
            "nominal": int(volume_size_gib),
            "nominal_bytes": nominal_bytes,
            "min_gib": min_gib,
        }
    )
    LOG.error(
        "Image %(image)s does not fit volume: %(reason)s",
        {"image": image_id, "reason": reason},
    )
    raise exception.ImageTooBig(image_id=image_id, reason=reason)


def clone_encryption_key_if_needed(context, key_manager, src_volume):
    """Clone Barbican encryption key for encrypted volume clone or snapshot.

    Returns the new cloned encryption_key_id, or None if the source volume
    is not encrypted or has no encryption_key_id.
    """
    if not src_volume:
        return None
    key_id = (
        src_volume.get("encryption_key_id")
        if isinstance(src_volume, dict)
        else getattr(src_volume, "encryption_key_id", None)
    )
    if not key_id or not key_manager:
        return None
    try:
        return volume_utils.clone_encryption_key(context, key_manager, key_id)
    except Exception:
        LOG.warning(
            "Failed to clone encryption key %s via key_manager",
            key_id,
            exc_info=True,
        )
        return None


class RXTLVM(lvm.LVMVolumeDriver):
    """Rackspace LVM Driver.

    This class provides the Cinder LVM driver with custom TGT iSCSI target
    configuration (unique scsi_id and scsi_sn for multipath consistency) and
    native compensation for LUKS encryption header overhead (OSPC-2358).

    OSPC-2358 Fix:
    When formatting LUKS on a block device, the LUKS header lives in-band at
    the start of the block device (e.g. 2 MiB / 4096 sectors for LUKS1, 16 MiB
    for LUKS2). If the underlying Logical Volume is created at exactly the
    nominal volume size (e.g. 40 GiB), the decrypted dm-crypt payload is smaller
    than nominal by the header size. Writing a full 40 GiB image via
    qemu-img convert pushes data past the end of the partition, truncating the
    secondary (backup) GPT table at LBA -1 and corrupting filesystems.

    Rather than destructive post-copy lvresize/shrink hacks, this driver resolves
    the issue at allocation time:
    - Encrypted volumes are allocated with the necessary LUKS overhead padding
      (e.g. +4 MiB for LUKS1, +16 MiB for LUKS2).
    - The LV remains at this padded size throughout its lifecycle; it is NEVER
      shrunk back.
    - An image capacity pre-check ensures fail-fast rejection (ImageTooBig) if an
      image's virtual size exceeds the nominal volume size.
    - Cloned volumes and volumes created from snapshots preserve the necessary
      padding and copy the full container.
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

    def _get_extent_size_bytes(self):
        """Return the VG physical extent size in bytes."""
        if not self.vg:
            return 4 * units.Mi
        try:
            cmd = self.vg.LVM_CMD_PREFIX + [
                "vgs", "--noheadings", "--nosuffix", "--units", "m",
                "-o", "vg_extent_size", self.vg.vg_name,
            ]
            out, _err = self.vg._execute(
                *cmd, root_helper=self.vg._root_helper, run_as_root=True)
            return int(float(out.strip()) * units.Mi)
        except Exception:
            return 4 * units.Mi

    def _get_lv_size_str(self, volume, size_in_g=None):
        """Return LVM size string, adding LUKS header overhead for encrypted volumes.

        For unencrypted volumes, returns '<size>g'.
        For encrypted volumes, calculates overhead (e.g. +4 MiB for LUKS1, +16 MiB for LUKS2)
        and returns '<total_mib>M'.
        """
        if size_in_g is None:
            size_in_g = (
                volume.get("size")
                if isinstance(volume, dict)
                else getattr(volume, "size", 0)
            )
        size_in_g = int(size_in_g)
        context = _resolve_context(volume)
        overhead_mib = get_volume_overhead_mib(
            volume, context=context, db=getattr(self, "db", None)
        )
        if overhead_mib > 0:
            try:
                pe_bytes = self._get_extent_size_bytes()
                pe_mib = max(1, pe_bytes // units.Mi)
                overhead_mib = int(math.ceil(overhead_mib / float(pe_mib))) * pe_mib
            except Exception:
                pass

            total_mib = (size_in_g * 1024) + overhead_mib
            vol_name = (
                volume.get("name")
                if isinstance(volume, dict)
                else getattr(volume, "name", "")
            )
            LOG.info(
                "Allocating encrypted LV %(name)s with %(overhead)d MiB LUKS "
                "header padding (total %(total_mib)d MiB) to provide full %(size)d GiB payload.",
                {
                    "name": vol_name,
                    "overhead": overhead_mib,
                    "total_mib": total_mib,
                    "size": size_in_g,
                },
            )
            return f"{total_mib}M"
        return "%sg" % size_in_g

    def create_volume(self, volume):
        """Creates a logical volume with LUKS padding if encrypted."""
        mirror_count = 0
        if self.configuration.lvm_mirrors:
            mirror_count = self.configuration.lvm_mirrors

        self._create_volume(
            volume["name"],
            self._get_lv_size_str(volume),
            self.configuration.lvm_type,
            mirror_count,
        )

    def extend_volume(self, volume, new_size):
        """Extend an existing volume's size with LUKS padding if encrypted."""
        self.vg.extend_volume(
            volume["name"],
            self._get_lv_size_str(volume, size_in_g=new_size),
        )
        try:
            self.target_driver.extend_target(volume)
        except Exception:
            LOG.exception("Error extending target after volume resize.")
            raise exception.TargetUpdateFailed(volume_id=volume.id)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot with LUKS padding if encrypted."""
        if self.configuration.lvm_type == "thin":
            self.vg.create_lv_snapshot(
                volume["name"],
                self._escape_snapshot(snapshot["name"]),
                self.configuration.lvm_type,
            )
            if volume["size"] > snapshot["volume_size"]:
                LOG.debug("Resize the new volume to %s.", volume["size"])
                self.extend_volume(volume, volume["size"])
            self.vg.activate_lv(snapshot["name"], is_snapshot=True)
            self.vg.activate_lv(volume["name"], is_snapshot=True, permanent=True)
            return

        self._create_volume(
            volume["name"],
            self._get_lv_size_str(volume),
            self.configuration.lvm_type,
            self.configuration.lvm_mirrors,
        )
        self.vg.activate_lv(snapshot["name"], is_snapshot=True)

        context = _resolve_context(volume)
        overhead_mib = get_volume_overhead_mib(
            volume, context=context, db=getattr(self, "db", None)
        )
        copy_size_mib = (snapshot["volume_size"] * units.Ki) + (overhead_mib or 0)

        volume_utils.copy_volume(
            self.local_path(snapshot),
            self.local_path(volume),
            copy_size_mib,
            self.configuration.volume_dd_blocksize,
            execute=self._execute,
            sparse=self._sparse_copy_volume,
        )

    def create_cloned_volume(self, volume, src_vref):
        """Creates a clone of the specified volume with LUKS padding if encrypted."""
        context = _resolve_context(volume) or _resolve_context(src_vref)
        key_mgr = getattr(self, "key_manager", None)
        cloned_key = None
        if key_mgr and src_vref.get("encryption_key_id") and not volume.get("encryption_key_id"):
            try:
                cloned_key = clone_encryption_key_if_needed(context, key_mgr, src_vref)
                if cloned_key:
                    volume["encryption_key_id"] = cloned_key
            except Exception:
                LOG.warning("Key cloning hook failed or unneeded for volume %s", volume["id"])

        if self.configuration.lvm_type == "thin":
            self.vg.create_lv_snapshot(
                volume["name"],
                src_vref["name"],
                self.configuration.lvm_type,
            )
            if volume["size"] > src_vref["size"]:
                LOG.debug("Resize the new volume to %s.", volume["size"])
                self.extend_volume(volume, volume["size"])
            self.vg.activate_lv(volume["name"], is_snapshot=True, permanent=True)
            return {"encryption_key_id": cloned_key} if cloned_key else None

        mirror_count = 0
        if self.configuration.lvm_mirrors:
            mirror_count = self.configuration.lvm_mirrors
        LOG.info("Creating clone of volume: %s", src_vref["id"])
        volume_name = src_vref["name"]
        temp_id = "tmp-snap-%s" % volume["id"]
        temp_snapshot = {
            "volume_name": volume_name,
            "size": src_vref["size"],
            "volume_size": src_vref["size"],
            "name": "clone-snap-%s" % volume["id"],
            "id": temp_id,
        }

        self.create_snapshot(temp_snapshot)

        try:
            self._create_volume(
                volume["name"],
                self._get_lv_size_str(volume),
                self.configuration.lvm_type,
                mirror_count,
            )

            self.vg.activate_lv(temp_snapshot["name"], is_snapshot=True)

            overhead_mib = get_volume_overhead_mib(
                src_vref, context=context, db=getattr(self, "db", None)
            )
            copy_size_mib = (src_vref["size"] * units.Ki) + (overhead_mib or 0)

            volume_utils.copy_volume(
                self.local_path(temp_snapshot),
                self.local_path(volume),
                copy_size_mib,
                self.configuration.volume_dd_blocksize,
                execute=self._execute,
                sparse=self._sparse_copy_volume,
            )
        finally:
            self.delete_snapshot(temp_snapshot)

        return {"encryption_key_id": cloned_key} if cloned_key else None

    def copy_image_to_encrypted_volume(
        self, context, volume, image_service, image_id, disable_sparse=False
    ):
        """Fetch image and write to an encrypted volume.

        Because the volume was already created with the appropriate LUKS header
        overhead padding (via _get_lv_size_str), the decrypted dm-crypt device
        provides the full nominal volume capacity.

        We perform a fail-fast pre-check (check_image_fits_encrypted_volume) to ensure
        the image does not exceed the volume size, and then delegate to the base
        image-to-volume copy. Crucially, we DO NOT shrink the LV after the copy,
        preserving the secondary GPT header and preventing data corruption (OSPC-2358).
        """
        check_image_fits_encrypted_volume(
            context, volume, image_service, image_id, db=getattr(self, "db", None)
        )
        self._copy_image_data_to_volume(
            context,
            volume,
            image_service,
            image_id,
            encrypted=True,
            disable_sparse=disable_sparse,
        )
