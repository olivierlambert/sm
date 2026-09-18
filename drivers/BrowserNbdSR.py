#!/usr/bin/python3
# SPDX-License-Identifier: LGPL-2.1-only
"""Experimental browser ISO using stock nbd-client and normal SM activation.

No direct_nbd bypass, WebSocket dependency, or custom tapdisk backend. The export
capability uses the conventional password key so SM's existing log redaction
applies. Never log nbd-client argv or exception text.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid

import SR
import VDI
import util
from lock import Lock

sys.path.insert(0, '/opt/xensource/libexec')

DRIVER_INFO = {
    'name': 'Browser NBD ISO',
    'description': 'Experimental read-only browser media using native NBD/TLS',
    'vendor': 'Vates', 'copyright': '(C) 2026 Vates',
    'driver_version': '0.1', 'required_api_version': '1.0',
    'capabilities': ['SR_ATTACH', 'SR_DETACH', 'SR_SCAN', 'VDI_CREATE',
                     'VDI_DELETE', 'VDI_ATTACH', 'VDI_DETACH'],
    'configuration': [['host', 'XO NBD host'], ['port', 'XO NBD port'],
                      ['password', 'Ephemeral export capability'], ['size', 'ISO byte size']],
}
RUNTIME = '/run/sm-browsernbd'


def validate(config):
    size = int(config.get('size', '0'))
    host = config.get('host', '')
    port = int(config.get('port', '10809'))
    if (not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.:-]*', host) or
            not 1 <= port <= 65535 or not re.fullmatch(r'[a-f0-9]{64}', config.get('password', '')) or
            not 32768 <= size <= 128 * 1024 ** 3 or size % 512):
        raise util.SMException('Invalid native browser NBD configuration')
    return size


def command(config, device):
    args = ['/usr/sbin/nbd-client', config['host'], config.get('port', '10809'), device,
            '-name', config['password'], '-readonly', '-timeout', '30']
    if config.get('allow_plaintext') != 'true':
        args += ['-enable-tls', '-cacertfile', config.get('ca_file', '/etc/pki/tls/certs/ca-bundle.crt'),
                 '-tlshostname', config['host']]
    return args


def run(args, timeout=35):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        if result.returncode:
            raise ValueError('Command failed')
        return result.stdout
    except Exception:
        raise util.SMException('Native NBD operation failed') from None


def identity(device):
    if not re.fullmatch(r'/dev/nbd[0-9]+', device):
        raise util.SMException('Invalid NBD device path')
    try:
        with open('/sys/block/' + os.path.basename(device) + '/pid') as source:
            return source.read().strip()
    except FileNotFoundError:
        return None


class BrowserNbdSR(SR.SR):
    @staticmethod
    def handles(sr_type):
        return sr_type == 'browsernbd'

    def load(self, sr_uuid):
        self.sr_vditype = 'iso'
        self.lock = Lock('sr', sr_uuid)
        self.ops_exclusive = ['sr_scan', 'vdi_create', 'vdi_delete', 'vdi_attach', 'vdi_detach']
        self.nbd_config = dict(self.dconf)
        if 'password_secret' in self.dconf:
            self.nbd_config['password'] = util.get_secret(self.session, self.dconf['password_secret'])
        self.media_size = validate(self.nbd_config)

    def create(self, sr_uuid, size):
        self.attach(sr_uuid)

    def attach(self, sr_uuid):
        if shutil.which('nbd-client') is None:
            raise util.SMException('Stock nbd-client is required')
        # Connection and actual export size are checked at VDI attach, before
        # the standard wrapper activates tapdisk. Do not allocate at PBD plug.

    def detach(self, sr_uuid):
        pass

    def delete(self, sr_uuid):
        pass

    def scan(self, sr_uuid):
        self.physical_size = self.media_size
        self.physical_utilisation = self.virtual_allocation = 0
        for ref, record in util.list_VDI_records_in_sr(self).items():
            disk = self.vdi(record['uuid'])
            disk.label = record['name_label']
            self.vdis[disk.uuid] = disk
        return super(BrowserNbdSR, self).scan(sr_uuid)

    def vdi(self, vdi_uuid):
        return BrowserNbdVDI(self, vdi_uuid)


class BrowserNbdVDI(VDI.VDI):
    def load(self, vdi_uuid):
        self.uuid = str(uuid.UUID(vdi_uuid))
        self.location = self.uuid
        self.vdi_type = 'iso'
        self.read_only = True
        self.size = self.sr.media_size
        self.utilisation = 0
        self.sm_config = {}
        self.state_path = os.path.join(RUNTIME, self.uuid + '.json')
        state = self.state()
        self.path = state['device'] if state else os.path.join(RUNTIME, self.uuid + '.unattached')

    def create(self, sr_uuid, vdi_uuid, size):
        if size != self.size or not self.read_only:
            raise util.SMException('Browser ISO size mismatch')
        self._db_introduce()
        return self.get_params()

    def delete(self, sr_uuid, vdi_uuid):
        self.detach(sr_uuid, vdi_uuid)
        self._db_forget()

    def state(self):
        try:
            with open(self.state_path) as source:
                return json.load(source)
        except FileNotFoundError:
            return None

    def attach(self, sr_uuid, vdi_uuid):
        import nbd_client_manager as manager
        run(['modprobe', 'nbd', 'nbds_max=24'])
        run(['udevadm', 'settle', '--timeout=30'])
        fingerprint = hashlib.sha256(json.dumps(self.sr.nbd_config, sort_keys=True).encode()).hexdigest()
        # Share XAPI's allocation lock instead of racing its own NBD users.
        with manager.FILE_LOCK:
            state = self.state()
            if state is not None and identity(state['device']) == state['pid']:
                if state['fingerprint'] != fingerprint:
                    raise util.SMException('Existing browser NBD attachment differs')
                self.path = state['device']
                return super(BrowserNbdVDI, self).attach(sr_uuid, vdi_uuid)
            device = manager._find_unused_nbd_device()
            try:
                run(command(self.sr.nbd_config, device))
                pid = identity(device)
                if pid is None or int(run(['blockdev', '--getsize64', device])) != self.size:
                    raise util.SMException('Browser NBD export size mismatch')
                os.makedirs(RUNTIME, mode=0o700, exist_ok=True)
                with open(os.open(self.state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as out:
                    json.dump({'device': device, 'pid': pid, 'fingerprint': fingerprint}, out)
                self.path = device
                return super(BrowserNbdVDI, self).attach(sr_uuid, vdi_uuid)
            except Exception:
                # This device was free while holding the shared allocation lock.
                # Bound cleanup even when negotiation or activation fails.
                run(['/usr/sbin/nbd-client', '-disconnect', device], timeout=10)
                raise util.SMException('Could not attach native browser NBD') from None

    def detach(self, sr_uuid, vdi_uuid):
        import nbd_client_manager as manager
        with manager.FILE_LOCK:
            state = self.state()
            if state is None:
                return
            # Never disconnect a device that another NBD user has since reused.
            if identity(state['device']) == state['pid']:
                run(['/usr/sbin/nbd-client', '-disconnect', state['device']], timeout=10)
            os.unlink(self.state_path)


SR.registerSR(BrowserNbdSR)

if __name__ == '__main__':
    import SRCommand
    SRCommand.run(BrowserNbdSR, DRIVER_INFO)
