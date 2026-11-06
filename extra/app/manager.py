import json
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

import libvirt

from .config import NAMESPACE
from .guest import cloud_config, domain_xml, seed_iso


class APIError(Exception):
    def __init__(self, status, detail):
        self.status = status
        self.detail = detail
        super().__init__(detail)


class Manager:
    def __init__(self, store, storage, uri, shutdown_timeout=20, *, accel="kvm"):
        self.accel = accel
        self.store = store
        self.storage = Path(storage).resolve()
        self.uri = uri
        self.shutdown_timeout = shutdown_timeout
        self.lock = threading.RLock()
        for name in ("images", "vms"):
            (self.storage / name).mkdir(parents=True, exist_ok=True, mode=0o755)

    @contextmanager
    def connection(self):
        connection = libvirt.open(self.uri)
        try:
            yield connection
        finally:
            connection.close()

    def metadata(self, domain):
        root = ET.fromstring(domain.XMLDesc(0))
        marker = root.find(f"metadata/{{{NAMESPACE}}}vm")
        if marker is not None and marker.get("owner") == self.store.owner:
            return marker.attrib
        return None

    def find(self, connection, vm_id):
        try:
            domain = connection.lookupByUUIDString(str(vm_id))
        except libvirt.libvirtError as error:
            if error.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                raise APIError(404, "ВМ не найдена") from error
            raise
        if self.metadata(domain) is None:
            raise APIError(404, "ВМ не найдена")
        return domain

    def view(self, domain):
        metadata = self.metadata(domain)
        state, memory_kib, _, vcpus, _ = domain.info()
        states = {libvirt.VIR_DOMAIN_RUNNING: "running", libvirt.VIR_DOMAIN_BLOCKED: "running",
                  libvirt.VIR_DOMAIN_PAUSED: "paused", libvirt.VIR_DOMAIN_SHUTDOWN: "stopping",
                  libvirt.VIR_DOMAIN_SHUTOFF: "stopped", libvirt.VIR_DOMAIN_CRASHED: "failed"}
        return {"id": domain.UUIDString(), "domain": domain.name(), "name": metadata["name"],
                "image_id": metadata["image_id"], "hostname": metadata["hostname"],
                "vcpus": vcpus, "memory_mb": memory_kib // 1024,
                "status": states.get(state, "unknown")}

    def list_vms(self):
        with self.connection() as connection:
            result = []
            for domain in connection.listAllDomains():
                try:
                    if self.metadata(domain) is not None:
                        result.append(self.view(domain))
                except libvirt.libvirtError as error:
                    if error.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                        raise
            return sorted(result, key=lambda vm: vm["id"])

    def get_vm(self, vm_id):
        with self.connection() as connection:
            return self.view(self.find(connection, vm_id))

    def register_image(self, request):
        with self.lock:
            base = (self.storage / "images").resolve()
            path = (base / request.path).resolve()
            if not path.is_relative_to(base) or not path.is_file():
                raise APIError(422, "Образ должен быть файлом в каталоге images")
            info = json.loads(subprocess.run(["qemu-img", "info", "--output=json", str(path)],
                                             check=True, capture_output=True, text=True).stdout)
            if info["format"] not in ("qcow2", "raw") or info.get("backing-filename"):
                raise APIError(422, "Нужен самостоятельный образ QCOW2 или raw")
            try:
                return self.store.add_image(request.name, path, request.description,
                                            info["format"], info["virtual-size"])
            except sqlite3.IntegrityError as error:
                raise APIError(409, "Имя образа уже используется") from error

    def create_vm(self, request):
        with self.lock:
            image = self.store.image(request.image_id)
            if image is None:
                raise APIError(404, "Образ не найден")
            if request.disk_gb * 1024 ** 3 < image["virtual_size"]:
                raise APIError(422, "Размер диска меньше размера базового образа")
            vm_id = str(uuid4())
            hostname = request.hostname or "vm-" + vm_id[:8]
            try:
                user_data = cloud_config(request, hostname)
            except (ValueError, TypeError) as error:
                raise APIError(422, str(error)) from error
            directory = self.storage / "vms" / vm_id
            with self.connection() as connection:
                domain = None
                directory.mkdir(mode=0o755)
                (directory / ".owner").write_text(self.store.owner)
                try:
                    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", image["format"],
                                    "-b", image["path"], str(directory / "disk.qcow2"),
                                    f"{request.disk_gb}G"], check=True, capture_output=True, text=True)
                    seed_iso(directory, vm_id, hostname, user_data)
                    domain = connection.defineXML(domain_xml(
                        vm_id, self.store.owner, request, directory, hostname,
                        accel=self.accel,
                    ))
                    domain.create()
                    return self.view(domain)
                except Exception:
                    # Files are removed only after libvirt has released the domain.
                    if domain is not None:
                        if domain.isActive():
                            domain.destroy()
                        domain.undefine()
                    shutil.rmtree(directory)
                    raise

    def start_vm(self, vm_id):
        with self.lock, self.connection() as connection:
            domain = self.find(connection, vm_id)
            if not domain.isActive():
                domain.create()
            elif domain.state()[0] == libvirt.VIR_DOMAIN_PAUSED:
                raise APIError(409, "ВМ приостановлена через libvirt")
            return self.view(domain)

    def poweroff(self, domain):
        if not domain.isActive():
            return
        domain.shutdown()
        deadline = time.monotonic() + self.shutdown_timeout
        while domain.isActive() and time.monotonic() < deadline:
            time.sleep(0.2)
        if domain.isActive():
            domain.destroy()

    def stop_vm(self, vm_id):
        with self.lock, self.connection() as connection:
            domain = self.find(connection, vm_id)
            self.poweroff(domain)
            return self.view(domain)

    def delete_vm(self, vm_id):
        with self.lock, self.connection() as connection:
            domain = self.find(connection, vm_id)
            directory = self.storage / "vms" / str(UUID(str(vm_id)))
            self.poweroff(domain)
            domain.undefine()
            if directory.exists():
                shutil.rmtree(directory)

    def metrics(self):
        with self.connection() as connection:
            rows = []
            for domain in connection.listAllDomains():
                try:
                    if self.metadata(domain) is None:
                        continue
                    vm_id = domain.UUIDString()
                    state, memory_kib, _, _, cpu_ns = domain.info()
                    active = domain.isActive()
                    row = {"id": vm_id, "running": int(state in (libvirt.VIR_DOMAIN_RUNNING,
                                                                          libvirt.VIR_DOMAIN_BLOCKED)),
                           "memory_configured_bytes": memory_kib * 1024}
                    if active:
                        row["cpu_seconds"] = cpu_ns / 1_000_000_000
                        rss = domain.memoryStats().get("rss")
                        if rss is not None:
                            row["memory_rss_bytes"] = rss * 1024
                    disk = self.storage / "vms" / vm_id / "disk.qcow2"
                    row["disk_allocated_bytes"] = disk.stat().st_blocks * 512
                    rows.append(row)
                except FileNotFoundError:
                    continue
                except libvirt.libvirtError as error:
                    if error.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                        raise
            return rows
