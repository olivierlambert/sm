#!/usr/bin/env python3
"""Install only the native NBD SR on XCP-ng 8.3 (run as root).

Usage: python3 install-browser-nbd-client.py /path/to/BrowserNbdSR.py
Requires the stock nbd-client package and nbd_client_manager helper.
Does not patch SRCommand, blktap2, tapdisk, xenopsd, or QEMU.
"""
import os
from pathlib import Path
import shutil
import sys

if os.geteuid() != 0:
    raise SystemExit('Run as root')
if not shutil.which('nbd-client') or not Path('/opt/xensource/libexec/nbd_client_manager.py').is_file():
    raise SystemExit('Stock nbd-client and nbd_client_manager are required')

source = Path(sys.argv[1]).read_text()
compile(source, 'BrowserNbdSR.py', 'exec')
config_path = Path('/etc/xapi.conf')
config = config_path.read_text()
lines = config.splitlines()
indices = [i for i, line in enumerate(lines) if line.startswith('sm-plugins=')]
if len(indices) != 1:
    raise SystemExit('Expected one sm-plugins entry in /etc/xapi.conf')
index = indices[0]
if 'browsernbd' not in lines[index].split('=', 1)[1].split():
    lines[index] += ' browsernbd'

backup = Path('/root/browser-nbd-client-backup')
backup.mkdir(mode=0o700, exist_ok=True)

def install(path, content, mode):
    saved = backup / path.name
    if path.exists() and not saved.exists():
        shutil.copy2(str(path), str(saved))
    temporary = path.with_name(path.name + '.browser-nbd-new')
    temporary.write_text(content)
    temporary.chmod(mode)
    os.replace(str(temporary), str(path))

root = Path('/opt/xensource/sm')
install(root / 'BrowserNbdSR.py', source, 0o644)
install(root / 'BrowserNbdSR', '''#!/usr/bin/python3
import SRCommand
from BrowserNbdSR import BrowserNbdSR, DRIVER_INFO
SRCommand.run(BrowserNbdSR, DRIVER_INFO)
''', 0o755)
install(config_path, '\n'.join(lines) + '\n', config_path.stat().st_mode & 0o777)
print('Installed browsernbd. Restart XAPI for initial SR type discovery.')
print('No existing SM dispatcher or tapdisk files were modified.')
