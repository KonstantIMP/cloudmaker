import subprocess
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET

import libvirt
import pytest
import yaml
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app import config
from app.clean import clean
from app.main import app


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STORAGE", tmp_path / "storage")
    monkeypatch.setattr(config, "DATABASE", tmp_path / "state.sqlite3")
    monkeypatch.setattr(config, "LIBVIRT_URI", "test:///default")
    connection = libvirt.open("test:///default")
    with TestClient(app) as client:
        manager = app.state.manager
        manager.shutdown_timeout = 0
        raw = manager.storage / "images" / "test.raw"
        with raw.open("wb") as stream:
            stream.truncate(2 * 1024 * 1024)
        yield client, manager, connection
    connection.close()


def register(client, **kwargs):
    response = client.post("/images", json={"name": "test", "path": "test.raw", **kwargs})
    assert response.status_code == 201, response.text
    return response.json()


def create(client, image_id, **kwargs):
    response = client.post("/vms", json={"image_id": image_id, "disk_gb": 1, **kwargs})
    assert response.status_code == 201, response.text
    return response.json()


def test_lifecycle_cloud_init_and_metrics(environment, tmp_path):
    client, manager, connection = environment
    image = register(client)
    assert image["format"] == "raw"
    request_data = "users:\n  - name: cloudmaker\n    lock_passwd: false\nruncmd:\n  - [touch, /tmp/check]\n"
    vm = create(client, image["id"], name="XML <&> name", hostname="guest-check",
                ssh_keys=["ssh-ed25519 test-key"], user_data=request_data)
    domain = connection.lookupByUUIDString(vm["id"])
    assert domain.isPersistent()
    assert vm["status"] == "running"
    assert vm["name"] == "XML <&> name"
    assert client.get("/vms").json() == [vm]

    directory = manager.storage / "vms" / vm["id"]
    subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(directory / "seed.iso"),
                    "-extract", "/", str(tmp_path / "extracted")], check=True, capture_output=True)
    user_data = yaml.safe_load((tmp_path / "extracted/user-data").read_text())
    meta_data = yaml.safe_load((tmp_path / "extracted/meta-data").read_text())
    assert meta_data["instance-id"] == vm["id"]
    assert user_data["hostname"] == "guest-check"
    assert user_data["users"][0]["ssh_authorized_keys"] == ["ssh-ed25519 test-key"]
    assert user_data["users"][0]["lock_passwd"] is False
    assert user_data["runcmd"] == [["touch", "/tmp/check"]]

    response = client.get("/metrics")
    assert response.status_code == 200, response.text
    samples = [s for family in text_string_to_metric_families(response.text) for s in family.samples]
    assert any(s.name == "cloudmaker_vm_running" and s.value == 1 for s in samples)
    assert any(s.name == "cloudmaker_vm_cpu_seconds_total" for s in samples)
    assert any(s.name == "cloudmaker_vm_disk_allocated_bytes" and s.value > 0 for s in samples)

    marker = directory / "persistence"
    marker.write_text("preserved")
    for _ in range(2):
        assert client.post(f"/vms/{vm['id']}/stop").json()["status"] == "stopped"
    assert client.post(f"/vms/{vm['id']}/start").json()["status"] == "running"
    assert marker.read_text() == "preserved"
    assert client.delete(f"/vms/{vm['id']}").status_code == 204
    assert not directory.exists()
    assert client.get(f"/vms/{vm['id']}").status_code == 404
    assert vm["id"] not in client.get("/metrics").text


def test_recovery_and_host_side_changes(environment):
    client, manager, connection = environment
    image = register(client)
    running = create(client, image["id"])
    stopped = create(client, image["id"])
    client.post(f"/vms/{stopped['id']}/stop")
    domain = connection.lookupByUUIDString(running["id"])
    runtime_id = domain.ID()
    owner = manager.store.owner
    with TestClient(app) as restarted:
        assert app.state.manager.store.owner == owner
        assert restarted.get("/images").json() == [image]
        assert restarted.get(f"/vms/{running['id']}").json()["status"] == "running"
        assert restarted.get(f"/vms/{stopped['id']}").json()["status"] == "stopped"
        assert domain.ID() == runtime_id
        domain.suspend()
        assert restarted.get(f"/vms/{running['id']}").json()["status"] == "paused"
        assert restarted.post(f"/vms/{running['id']}/start").status_code == 409
        domain.resume()
    assert domain.isActive()  # API shutdown never shuts down the guest.


def test_bad_inputs_and_foreign_domains(environment):
    client, manager, connection = environment
    assert client.post("/images", json={"name": "bad", "path": "/etc/passwd"}).status_code == 422
    image = register(client)
    assert client.post("/images", json={"name": "test", "path": "test.raw"}).status_code == 409
    assert client.post("/vms", json={"image_id": image["id"], "user_data": "[1, 2]"}).status_code == 422
    assert client.post("/vms", json={"image_id": image["id"], "user_data": "[invalid"}).status_code == 422
    assert client.post("/vms", json={"image_id": image["id"], "vcpus": 0}).status_code == 422
    foreign = connection.lookupByName("test")
    assert client.delete(f"/vms/{foreign.UUIDString()}").status_code == 404
    assert foreign.isActive()
    assert not list((manager.storage / "vms").iterdir())


def test_reject_image_with_external_backing(environment):
    client, manager, _ = environment
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "raw", "-b",
                    str(manager.storage / "images/test.raw"),
                    str(manager.storage / "images/overlay.qcow2")], check=True, capture_output=True)
    assert client.post("/images", json={"name": "overlay", "path": "overlay.qcow2"}).status_code == 422


def test_failed_start_rolls_back(environment, monkeypatch):
    client, manager, connection = environment
    image = register(client)
    before = {d.UUIDString() for d in connection.listAllDomains()}

    def fail(*args, **kwargs):
        raise libvirt.libvirtError("injected start failure")

    monkeypatch.setattr(libvirt.virDomain, "create", fail)
    assert client.post("/vms", json={"image_id": image["id"]}).status_code == 503
    assert {d.UUIDString() for d in connection.listAllDomains()} == before
    assert not list((manager.storage / "vms").iterdir())


def test_clean_only_owned_domains(environment):
    client, manager, connection = environment
    image = register(client)
    vm = create(client, image["id"])
    clean(manager)
    assert connection.lookupByName("test").isActive()
    assert client.get(f"/vms/{vm['id']}").status_code == 404
    assert not (manager.storage / "vms").exists()
    assert not (manager.storage / "images").exists()


def test_clean_refuses_foreign_storage_reference(environment):
    client, manager, connection = environment
    image = register(client)
    vm = create(client, image["id"])
    xml = ET.fromstring(connection.lookupByName("test").XMLDesc(0))
    xml.find("name").text = "foreign-reference"
    xml.find("uuid").text = str(uuid4())
    disk = xml.find("devices/disk")
    disk.set("type", "file")
    source = disk.find("source")
    source.attrib.clear()
    source.set("file", str(manager.storage / "images/test.raw"))
    connection.defineXML(ET.tostring(xml, encoding="unicode"))
    with pytest.raises(RuntimeError, match="чужой ВМ"):
        clean(manager)
    assert connection.lookupByUUIDString(vm["id"]).isActive()
    assert (manager.storage / "images/test.raw").exists()


def test_libvirt_unavailable_is_not_an_empty_inventory(environment, monkeypatch):
    client, manager, _ = environment
    image = register(client)

    def unavailable(*args):
        raise libvirt.libvirtError("connection unavailable")

    monkeypatch.setattr(libvirt, "open", unavailable)
    assert client.get("/vms").status_code == 503
    assert client.get("/metrics").status_code == 503
    assert client.get("/images").json() == [image]
    assert client.post("/vms", json={"image_id": image["id"]}).status_code == 503
    assert not list((manager.storage / "vms").iterdir())
