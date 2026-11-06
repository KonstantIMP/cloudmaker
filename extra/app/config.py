import os
from pathlib import Path

STORAGE = Path(os.getenv("CLOUDMAKER_STORAGE", "/var/lib/libvirt/images/cloudmaker"))
DATABASE = Path(os.getenv("CLOUDMAKER_DATABASE", "/data/cloudmaker.sqlite3"))
LIBVIRT_URI = os.getenv("LIBVIRT_URI", "qemu:///system")
ACCEL = os.getenv("CLOUDMAKER_ACCEL", "kvm")
if ACCEL not in {"kvm", "tcg"}:
    raise ValueError("CLOUDMAKER_ACCEL должен быть kvm или tcg")
NAMESPACE = "urn:cloudmaker:extra"
