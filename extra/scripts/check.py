"""Full check on a Linux host with Docker, libvirt and KVM. Uses stdlib only."""

import json
import os
import pty
import re
import secrets
import select
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = f"http://127.0.0.1:{os.environ['API_PORT']}"
PROMETHEUS = f"http://127.0.0.1:{os.environ['PROMETHEUS_PORT']}"
STORAGE = Path(os.environ["STORAGE_DIR"])
IMAGE = "debian-12-genericcloud-amd64-20261026-2643.qcow2"


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def compose(*args):
    return command("docker", "compose", *args)


def virsh(*args):
    return command("virsh", "-c", "qemu:///system", *args)


def api(method, path, data=None):
    request = urllib.request.Request(API + path, method=method,
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read()
        return json.loads(body) if body else None


def wait_for(action, description, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = action()
            if result:
                return result
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(1)
    raise AssertionError(f"Таймаут: {description}")


def prom(query):
    url = PROMETHEUS + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)["data"]["result"]


class Console:
    def __init__(self, domain, password):
        self.domain = domain
        self.password = password
        self.buffer = ""

    def __enter__(self):
        self.master, slave = pty.openpty()
        self.process = subprocess.Popen(["virsh", "-c", "qemu:///system", "console", self.domain],
                                        stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        try:
            self.expect(r"Escape character is", 20)
            self.send("\n")
            self.expect(r"login:\s*$", 240)
            self.send("cloudmaker\n")
            self.expect(r"Password:\s*$", 30)
            self.send(self.password + "\n")
            self.expect(r"\$ ", 30)
            return self
        except Exception:
            self.close()
            raise

    def send(self, value):
        os.write(self.master, value.encode())

    def expect(self, pattern, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = re.search(pattern, self.buffer, re.MULTILINE)
            if match:
                output = self.buffer[:match.end()]
                self.buffer = self.buffer[match.end():]
                return output, match
            readable, _, _ = select.select([self.master], [], [], 1)
            if readable:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                self.buffer = (self.buffer + chunk.decode(errors="replace").replace("\r", ""))[-200000:]
        raise AssertionError(f"Консоль {self.domain}: не получено {pattern!r}\n{self.buffer[-2000:]}")

    def run(self, value, timeout=60):
        marker = "CHECK_" + secrets.token_hex(8)
        self.send(value + f"; printf '\\n%s:%s\\n' {marker} \"$?\"\n")
        output, match = self.expect(r"\r?\n" + marker + r":(\d+)\r?\n", timeout)
        assert match.group(1) == "0", output
        return output

    def boot_id(self):
        output = self.run("cat /proc/sys/kernel/random/boot_id")
        return re.search(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", output).group()

    def close(self):
        if self.process.poll() is None:
            self.send("exit\n")
            time.sleep(0.2)
            self.send("\x1d")
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=3)
        os.close(self.master)

    def __exit__(self, *args):
        self.close()


def check():
    vm_ids = []
    domains = []
    try:
        wait_for(lambda: api("GET", "/vms") is not None, "готовность API")
        image = api("POST", "/images", {"name": "debian-qcow2", "path": IMAGE, "description": "QCOW2 check"})
        compose("exec", "-T", "app", "qemu-img", "convert", "-f", "qcow2", "-O", "raw",
                str(STORAGE / "images" / IMAGE), str(STORAGE / "images/check.raw"))
        raw = api("POST", "/images", {"name": "debian-raw", "path": "check.raw", "description": "Raw check"})
        assert {item["format"] for item in api("GET", "/images")} == {"raw", "qcow2"}
        print("OK: регистрация QCOW2 и raw", flush=True)

        with tempfile.TemporaryDirectory(prefix="cloudmaker-key-") as directory:
            key = Path(directory) / "id_ed25519"
            command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
            public_key = key.with_suffix(".pub").read_text().strip()
        password = secrets.token_urlsafe(18)
        token = secrets.token_hex(16)
        user_data = "#cloud-config\n" + json.dumps({
            "users": ["default", {"name": "cloudmaker", "lock_passwd": False, "plain_text_passwd": password}],
            "write_files": [{"path": "/var/tmp/cloudmaker-user-data", "content": token}],
        })
        for index, selected in enumerate((image, raw)):
            vm = api("POST", "/vms", {"image_id": selected["id"], "name": f"check-{index}",
                                      "hostname": f"check-{index}", "ssh_keys": [public_key], "user_data": user_data})
            vm_ids.append(vm["id"])
            domains.append(vm["domain"])
            assert virsh("domuuid", vm["domain"]) == vm["id"]
            with Console(vm["domain"], password) as console:
                console.run("sudo cloud-init status --wait", timeout=240)
                console.run(f"test \"$(hostname)\" = check-{index}")
                console.run(f"test \"$(cat /var/tmp/cloudmaker-user-data)\" = {token}")
                console.run("grep -F -- " + shlex.quote(public_key) + " ~/.ssh/authorized_keys")
                console.run(f"echo {token} > ~/persistent; sync")
                if index == 0:
                    first_boot = console.boot_id()
        print("OK: две ВМ, cloud-init, hostname, user_data и SSH-ключ", flush=True)

        running_query = f'cloudmaker_vm_running{{vm_id="{vm_ids[0]}"}}'
        wait_for(lambda: prom(running_query), "Prometheus scrape")
        for metric in ("cpu_seconds_total", "memory_rss_bytes", "disk_allocated_bytes"):
            series = wait_for(lambda: prom(f'cloudmaker_vm_{metric}{{vm_id="{vm_ids[0]}"}}'), metric)
            assert float(series[0]["value"][1]) > 0, series
        print("OK: Prometheus собирает CPU, RAM и диск", flush=True)

        for _ in range(2):
            assert api("POST", f"/vms/{vm_ids[1]}/stop")["status"] == "stopped"
        runtime_id = virsh("domid", domains[0])
        compose("stop", "app")
        assert virsh("domid", domains[0]) == runtime_id
        with Console(domains[0], password) as console:
            assert console.boot_id() == first_boot
        compose("up", "-d", "--no-deps", "--force-recreate", "app")
        wait_for(lambda: api("GET", "/vms") is not None, "восстановление API")
        assert {vm["id"] for vm in api("GET", "/vms")} == set(vm_ids)
        assert {item["id"] for item in api("GET", "/images")} == {image["id"], raw["id"]}
        assert api("GET", f"/vms/{vm_ids[1]}")["status"] == "stopped"
        assert virsh("domid", domains[0]) == runtime_id
        with Console(domains[0], password) as console:
            assert console.boot_id() == first_boot
            console.run(f"test \"$(cat ~/persistent)\" = {token}")
        print("OK: контейнер пересоздан, работающая ВМ не перезагружалась, состояние сохранено", flush=True)

        assert api("POST", f"/vms/{vm_ids[0]}/stop")["status"] == "stopped"
        api("POST", f"/vms/{vm_ids[0]}/start")
        with Console(domains[0], password) as console:
            assert console.boot_id() != first_boot
            console.run(f"test \"$(cat ~/persistent)\" = {token}")
        for vm_id in vm_ids:
            api("DELETE", f"/vms/{vm_id}")
            assert not (STORAGE / "vms" / vm_id).exists()
        assert api("GET", "/vms") == []
        wait_for(lambda: not prom(running_query), "исчезновение метрик удалённой ВМ")
        print("OK: повторный запуск сохраняет диск; удаление работающей и остановленной ВМ", flush=True)

        # Leave a running VM to exercise clean.sh, including host-side resources.
        vm = api("POST", "/vms", {"image_id": image["id"], "name": "cleanup-check"})
        vm_ids.append(vm["id"])
        domains.append(vm["domain"])
    except Exception:
        subprocess.run(["docker", "compose", "logs", "--tail", "100"], check=False)
        raise
    finally:
        subprocess.run(["./clean.sh"], check=True)
    remaining = set(virsh("list", "--all", "--uuid").split())
    assert not remaining.intersection(vm_ids)
    assert not STORAGE.exists()
    assert not compose("ps", "-aq")
    volumes = command("docker", "volume", "ls", "-q", "--filter",
                      "label=com.docker.compose.project=" + os.environ["COMPOSE_PROJECT_NAME"])
    assert not volumes
    print("OK: очистка доменов, дисков, контейнеров и томов", flush=True)


if __name__ == "__main__":
    check()
