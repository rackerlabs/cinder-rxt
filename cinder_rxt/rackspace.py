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

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import units

from oslo_concurrency import processutils as putils

from cinder import context as cinder_context
from cinder import db as cinder_db
from cinder.volume.drivers import lvm
from cinder.volume.targets import tgt

LOG = logging.getLogger(__name__)


# Extra configuration options for the RXTLVM driver.
# These are in addition to the base LVM driver's options.
rxt_volume_opts = [
    cfg.FloatOpt(
        "safe_size_margin_gb",
        default=0.25,
        min=0.0,
        help="""
Additional backend space (in GiB) to allocate when creating LVM-backed
volumes from images, snapshots or clones. This margin is **not**
reflected in Cinder-reported volume size or quota accounting; it only
affects the underlying LVM LV size.

The actual margin used is aligned to the VG extent size. Set to 0.0 to
disable the configured margin for from-image / from-snapshot /
from-volume flows.
""",
    ),
    cfg.FloatOpt(
        "safe_size_margin_blank_gb",
        default=0.25,
        help="""
Additional backend space (in GiB) to allocate when creating blank
(empty) LVM-backed volumes. If unset, the driver falls back to using
safe_size_margin_gb. Set to 0.0 to disable the margin for blank
volumes only.

The actual margin used is aligned to the VG extent size.
""",
    ),
]


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
    the TGT helper and implement safer LV sizing for all volume creation
    paths (blank volumes as well as from-image / from-snapshot /
    from-volume use cases).
    """

    def __init__(self, vg_obj=None, *args, **kwargs):

        super(RXTLVM, self).__init__(*args, **kwargs)
        # Base LVM options + RXT-specific options
        self.configuration.append_config_values(lvm.volume_opts)
        self.configuration.append_config_values(rxt_volume_opts)
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
        self._vg_extent_size_mib = None  # Cache for VG extent size

    # ------------------------------------------------------------------
    # Safe size margin helpers
    # ------------------------------------------------------------------

    def _get_vg_extent_size_mib(self):
        """Fetch the VG extent size in MiB.

        Caches the result since extent size doesn't change for a VG.
        """
        if self._vg_extent_size_mib is not None:
            return self._vg_extent_size_mib

        cmd = ['env', 'LC_ALL=C', 'vgs', '--noheadings', '--unit=m',
               '-o', 'vg_extent_size', '--nosuffix', self.vg.vg_name]
        try:
            (out, _err) = putils.execute(*cmd, run_as_root=True,
                                         root_helper=self.vg._root_helper)
            self._vg_extent_size_mib = float(out.strip())
        except putils.ProcessExecutionError:
            LOG.warning("Failed to query VG extent size, using default 4 MiB")
            self._vg_extent_size_mib = 4.0  # LVM default extent size
        return self._vg_extent_size_mib

    def _calculate_backend_size_gb(self, volume, margin_gb):
        """Compute LV size (GiB) including configured safety margin.

        - Base is requested volume['size'] in GiB.
        - We always align to whole VG extents.
        - ``margin_gb`` is the configured extra backend space (in GiB) for
          this operation. It may be 0.0 to disable extra space.
        - End users and quota accounting still see only the requested size;
          the extra space only exists on the backend LV.
        """
        base_gb = float(volume["size"])
        base_bytes = int(base_gb * units.Gi)

        # VG extent size (MiB -> bytes)
        extent_bytes = int(self._get_vg_extent_size_mib() * units.Mi)

        # Normalise and clamp the configured margin
        if margin_gb is None:
            margin_gb = 0.0
        margin_gb = max(float(margin_gb), 0.0)
        cfg_margin_bytes = int(margin_gb * units.Gi)

        margin_bytes = 0
        if cfg_margin_bytes > 0:
            margin_bytes = cfg_margin_bytes
            # Ensure at least one extent worth of extra space
            if margin_bytes < extent_bytes:
                margin_bytes = extent_bytes

        total_bytes = base_bytes + margin_bytes

        # Round up to whole extents
        extents = int(math.ceil(float(total_bytes) / float(extent_bytes)))
        backend_bytes = extents * extent_bytes
        backend_gb = backend_bytes / float(units.Gi)

        return backend_gb, backend_bytes, margin_bytes

    def _get_blank_margin_gb(self):
        """Return safety margin (GiB) for blank volumes.

        If ``safe_size_margin_blank_gb`` is unset, fall back to
        ``safe_size_margin_gb``.
        """
        cfg = self.configuration
        blank = getattr(cfg, "safe_size_margin_blank_gb", None)
        if blank is not None:
            return blank
        return getattr(cfg, "safe_size_margin_gb", 0.0) or 0.0

    def _get_from_source_margin_gb(self):
        """Return safety margin (GiB) for snapshot/clone/image flows."""
        return getattr(self.configuration, "safe_size_margin_gb", 0.0) or 0.0

    def _record_safe_size_margin(self, volume, requested_size_gb,
                                 backend_size_gb, backend_bytes,
                                 *, margin_type=None):
        """Log and persist the extra backend margin, if any.

        This uses volume admin_metadata so operators can see when margin was
        applied. End users still see the original requested size.

        ``margin_type`` (if provided) indicates which configured margin was
        used for this volume, e.g. "blank", "snapshot", "clone" or "image".
        """
        if backend_size_gb <= requested_size_gb:
            return

        used_margin_gb = backend_size_gb - requested_size_gb
        LOG.warning(
            "Safe size margin applied to volume %(vol)s: "
            "%(req).3f GiB requested, %(backend).3f GiB allocated "
            "in LVM (margin %(margin).3f GiB, raw bytes %(bytes)d).",
            {
                "vol": volume["id"],
                "req": requested_size_gb,
                "backend": backend_size_gb,
                "margin": used_margin_gb,
                "bytes": backend_bytes,
            },
        )

        metadata = {"safe_size_margin_gb": f"{used_margin_gb:.3f}"}
        if margin_type:
            metadata["safe_size_margin_type"] = margin_type

        try:
            admin_ctxt = cinder_context.get_admin_context()
            cinder_db.volume_admin_metadata_update(
                admin_ctxt,
                volume["id"],
                metadata,
                False,
            )
        except Exception:
            LOG.exception(
                "Failed to update admin_metadata for volume %s "
                "with safe_size_margin_gb.",
                volume["id"],
            )

    # ------------------------------------------------------------------
    # Overridden methods to use safe size margin
    # ------------------------------------------------------------------

    def create_volume(self, volume):
        """Creates a logical volume.

        Plain (blank) volumes receive a configurable safety margin so that the
        usable space on the LV is at least the requested size. The extra
        backend space is not reflected in the Cinder-reported size.
        """
        mirror_count = 0
        if self.configuration.lvm_mirrors:
            mirror_count = self.configuration.lvm_mirrors

        requested_size_gb = float(volume["size"])
        margin_gb = self._get_blank_margin_gb()
        backend_gb, backend_bytes, _ = self._calculate_backend_size_gb(
            volume, margin_gb
        )
        lv_size_gb = int(math.ceil(backend_gb))

        self._create_volume(
            volume["name"],
            self._sizestr(lv_size_gb),
            self.configuration.lvm_type,
            mirror_count,
        )

        self._record_safe_size_margin(
            volume,
            requested_size_gb,
            float(lv_size_gb),
            backend_bytes,
            margin_type="blank",
        )

        # Base LVM driver returns None; keep that behavior
        return None

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot.

        For non-thin volumes we allocate with a safety margin.
        Thin volumes keep upstream behavior.
        """
        if self.configuration.lvm_type == "thin":
            self.vg.create_lv_snapshot(
                volume["name"],
                self._escape_snapshot(snapshot["name"]),
                self.configuration.lvm_type,
            )
            if volume["size"] > snapshot["volume_size"]:
                LOG.debug("Resize the new volume to %s.", volume["size"])
                self.extend_volume(volume, volume["size"])
            # Some configurations of LVM do not automatically activate
            # ThinLVM snapshot LVs.
            self.vg.activate_lv(snapshot["name"], is_snapshot=True)
            self.vg.activate_lv(volume["name"], is_snapshot=True, permanent=True)
            return

        requested_size_gb = float(volume["size"])
        margin_gb = self._get_from_source_margin_gb()
        backend_gb, backend_bytes, _ = self._calculate_backend_size_gb(
            volume, margin_gb
        )
        lv_size_gb = int(math.ceil(backend_gb))

        self._create_volume(
            volume["name"],
            self._sizestr(lv_size_gb),
            self.configuration.lvm_type,
            self.configuration.lvm_mirrors,
        )

        self._record_safe_size_margin(
            volume,
            requested_size_gb,
            float(lv_size_gb),
            backend_bytes,
            margin_type="snapshot",
        )

        # Some configurations of LVM do not automatically activate
        # ThinLVM snapshot LVs.
        self.vg.activate_lv(snapshot["name"], is_snapshot=True)

        # copy_volume expects sizes in MiB, we store integer GiB
        volume_utils = lvm.volume_utils
        volume_utils.copy_volume(
            self.local_path(snapshot),
            self.local_path(volume),
            snapshot["volume_size"] * units.Ki,
            self.configuration.volume_dd_blocksize,
            execute=self._execute,
            sparse=self._sparse_copy_volume,
        )

    def create_cloned_volume(self, volume, src_vref):
        """Creates a clone of the specified volume.

        For non-thin volumes we allocate with a safety margin.
        Thin volumes keep upstream behavior.
        """
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
            return

        mirror_count = 0
        if self.configuration.lvm_mirrors:
            mirror_count = self.configuration.lvm_mirrors
        LOG.info("Creating clone of volume: %s", src_vref["id"])
        volume_name = src_vref["name"]
        temp_id = f"tmp-snap-{volume['id']}"
        temp_snapshot = {
            "volume_name": volume_name,
            "size": src_vref["size"],
            "volume_size": src_vref["size"],
            "name": f"clone-snap-{volume['id']}",
            "id": temp_id,
        }

        self.create_snapshot(temp_snapshot)

        volume_utils = lvm.volume_utils
        try:
            requested_size_gb = float(volume["size"])
            margin_gb = self._get_from_source_margin_gb()
            backend_gb, backend_bytes, _ = self._calculate_backend_size_gb(
                volume, margin_gb
            )
            lv_size_gb = int(math.ceil(backend_gb))

            self._create_volume(
                volume["name"],
                self._sizestr(lv_size_gb),
                self.configuration.lvm_type,
                mirror_count,
            )

            self._record_safe_size_margin(
                volume,
                requested_size_gb,
                float(lv_size_gb),
                backend_bytes,
                margin_type="clone",
            )

            self.vg.activate_lv(temp_snapshot["name"], is_snapshot=True)
            volume_utils.copy_volume(
                self.local_path(temp_snapshot),
                self.local_path(volume),
                src_vref["size"] * units.Ki,
                self.configuration.volume_dd_blocksize,
                execute=self._execute,
                sparse=self._sparse_copy_volume,
            )
        finally:
            self.delete_snapshot(temp_snapshot)

    def copy_image_to_volume(
        self,
        context,
        volume,
        image_service,
        image_id,
        disable_sparse=False,
    ):
        """Fetch the image and write it to the volume with safety margin.

        This method is only used for from-image / reimage flows, so we apply
        the configured margin here (without changing Cinder's reported size).
        """
        requested_size_gb = float(volume["size"])
        margin_gb = self._get_from_source_margin_gb()
        backend_gb, backend_bytes, _ = self._calculate_backend_size_gb(
            volume, margin_gb
        )
        lv_size_gb = int(math.ceil(backend_gb))

        # If backend needs to be larger, extend LV only (DB size unchanged)
        if lv_size_gb > volume["size"]:
            self.extend_volume(volume, lv_size_gb)
            self._record_safe_size_margin(
                volume,
                requested_size_gb,
                float(lv_size_gb),
                backend_bytes,
                margin_type="image",
            )

        image_utils = lvm.image_utils
        image_utils.fetch_to_raw(
            context,
            image_service,
            image_id,
            self.local_path(volume),
            self.configuration.volume_dd_blocksize,
            size=volume["size"],  # image/virtual size constraint
            disable_sparse=disable_sparse,
        )
