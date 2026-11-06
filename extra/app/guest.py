import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import yaml

from .config import NAMESPACE


def cloud_config(request, hostname):
    config = yaml.safe_load(request.user_data) or {}
    if not isinstance(config, dict):
        raise ValueError("user_data должен содержать YAML cloud-config")
    config["hostname"] = hostname
    config.setdefault("manage_etc_hosts", True)
    config.setdefault("ssh_pwauth", False)
    users = config.setdefault("users", ["default"])
    if not isinstance(users, list):
        raise ValueError("users в user_data должен быть списком")
    account = next((u for u in users if isinstance(u, dict) and u.get("name") == "cloudmaker"), None)
    if account is None:
        account = {"name": "cloudmaker"}
        users.append(account)
    account.setdefault("shell", "/bin/bash")
    account.setdefault("sudo", "ALL=(ALL) NOPASSWD:ALL")
    account.setdefault("lock_passwd", True)
    keys = account.setdefault("ssh_authorized_keys", [])
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ValueError("ssh_authorized_keys должен быть списком строк")
    keys.extend(key for key in request.ssh_keys if key not in keys)
    return "#cloud-config\n" + yaml.safe_dump(config, sort_keys=False)


def seed_iso(directory: Path, vm_id: str, hostname: str, user_data: str):
    seed = directory / "seed"
    seed.mkdir(mode=0o700)
    (seed / "meta-data").write_text(yaml.safe_dump({"instance-id": vm_id, "local-hostname": hostname}))
    (seed / "user-data").write_text(user_data)
    subprocess.run(["xorriso", "-as", "mkisofs", "-quiet", "-V", "cidata", "-o",
                    str(directory / "seed.iso"), str(seed)], check=True, capture_output=True, text=True)


def domain_xml(vm_id, owner, request, directory, hostname, *, accel="kvm"):
    domain_type = {"kvm": "kvm", "tcg": "qemu"}[accel]
    root = ET.Element("domain", type=domain_type)

    def node(parent, tag, text=None, **attrs):
        result = ET.SubElement(parent, tag, attrs)
        if text is not None:
            result.text = str(text)
        return result

    node(root, "name", "cloudmaker-" + vm_id)
    node(root, "uuid", vm_id)
    node(root, "title", request.name)
    node(node(root, "metadata"), f"{{{NAMESPACE}}}vm", owner=owner,
         image_id=str(request.image_id), name=request.name, hostname=hostname)
    node(root, "memory", request.memory_mb, unit="MiB")
    node(root, "vcpu", request.vcpus)
    os_node = node(root, "os")
    node(os_node, "type", "hvm", arch="x86_64", machine="pc")
    node(os_node, "boot", dev="hd")
    features = node(root, "features")
    node(features, "acpi")
    node(features, "apic")
    if accel == "kvm":
        node(root, "cpu", mode="host-model")
    node(root, "on_poweroff", "destroy")
    node(root, "on_reboot", "restart")
    node(root, "on_crash", "destroy")
    devices = node(root, "devices")
    disk = node(devices, "disk", type="file", device="disk")
    node(disk, "driver", name="qemu", type="qcow2")
    node(disk, "source", file=str(directory / "disk.qcow2"))
    node(disk, "target", dev="vda", bus="virtio")
    cdrom = node(devices, "disk", type="file", device="cdrom")
    node(cdrom, "driver", name="qemu", type="raw")
    node(cdrom, "source", file=str(directory / "seed.iso"))
    node(cdrom, "target", dev="hda", bus="ide")
    node(cdrom, "readonly")
    node(node(devices, "interface", type="user"), "model", type="virtio")
    node(node(devices, "serial", type="pty"), "target", port="0")
    node(node(devices, "console", type="pty"), "target", type="serial", port="0")
    node(node(devices, "rng", model="virtio"), "backend", "/dev/urandom", model="random")
    return ET.tostring(root, encoding="unicode")
