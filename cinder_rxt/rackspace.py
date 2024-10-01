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

import textwrap

from cinder.volume.drivers import lvm
from cinder.volume.targets import tgt


class RXTTgtAdm(tgt.TgtAdm):
    """Target object for block storage devices.

    Base class for target object, where target
    is data transport mechanism (target) specific calls.
    This includes things like create targets, attach, detach
    etc.
    """

    VOLUME_CONF = textwrap.dedent(
        """
                <target %(name)s>
                    backing-store %(path)s
                    driver %(driver)s
                    %(chap_auth)s
                    %(target_flags)s
                    write-cache %(write_cache)s
                    scsi_sn %(scsi_sn)s
                    scsi_id %(scsi_sn)s
                </target>
                  """
    )

    def create_iscsi_target(
        self, name, tid, lun, path, chap_auth=None, **kwargs
    ):
        volume_conf = self.VOLUME_CONF % {
            "name": name,
            "path": path,
            "driver": driver,
            "chap_auth": chap_str,
            "target_flags": target_flags,
            "write_cache": write_cache,
            "scsi_sn": vol_id,
            "scsi_id": vol_id,
        }
        return tid


class RXTLVM(lvm.LVMVolumeDriver):
    """Rackspace LVM Driver.

    This class is used to create a new Cinder driver that gives us control of
    the TGT helper.
    """

    def __init__(self, vg_obj=None, *args, **kwargs):

        super(RXTLVM, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(lvm.volume_opts)
        self.hostname = lvm.socket.gethostname()
        self.vg = vg_obj
        self.backend_name = (
            self.configuration.safe_get("volume_backend_name") or "LVM"
        )

        lvm.LOG.debug(
            "Attempting to initialize LVM driver with the "
            "following target_driver: RXTTgtAdm"
        )

        self.target_driver = RXTTgtAdm
        self.protocol = (
            self.target_driver.storage_protocol or self.target_driver.protocol
        )

        self._sparse_copy_volume = False
