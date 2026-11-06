import logging
import subprocess
from contextlib import asynccontextmanager
from uuid import UUID

import libvirt
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CollectorRegistry, CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from . import config
from .manager import APIError, Manager
from .models import ImageCreate, VMCreate
from .store import Store


@asynccontextmanager
async def lifespan(app):
    app.state.manager = Manager(
        Store(config.DATABASE), config.STORAGE, config.LIBVIRT_URI, accel=config.ACCEL
    )
    yield
    # Host libvirt owns the VMs; stopping the API must not stop them.


app = FastAPI(title="Cloud Maker", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.exception_handler(APIError)
async def api_error(request, error):
    return JSONResponse(status_code=error.status, content={"detail": error.detail})


@app.exception_handler(libvirt.libvirtError)
async def libvirt_error(request, error):
    logging.exception("libvirt operation failed")
    return JSONResponse(status_code=503, content={"detail": str(error)})


@app.exception_handler(subprocess.CalledProcessError)
async def command_error(request, error):
    logging.exception("Command failed")
    return JSONResponse(status_code=422, content={"detail": (error.stderr or str(error))[-2000:]})


@app.exception_handler(yaml.YAMLError)
async def yaml_error(request, error):
    return JSONResponse(status_code=422, content={"detail": "Некорректный YAML в user_data"})


@app.post("/images", status_code=201)
def register_image(body: ImageCreate, request: Request):
    return request.app.state.manager.register_image(body)


@app.get("/images")
def images(request: Request):
    return request.app.state.manager.store.images()


@app.post("/vms", status_code=201)
def create_vm(body: VMCreate, request: Request):
    return request.app.state.manager.create_vm(body)


@app.get("/vms")
def vms(request: Request):
    return request.app.state.manager.list_vms()


@app.get("/vms/{vm_id}")
def vm(vm_id: UUID, request: Request):
    return request.app.state.manager.get_vm(vm_id)


@app.post("/vms/{vm_id}/start")
def start_vm(vm_id: UUID, request: Request):
    return request.app.state.manager.start_vm(vm_id)


@app.post("/vms/{vm_id}/stop")
def stop_vm(vm_id: UUID, request: Request):
    return request.app.state.manager.stop_vm(vm_id)


@app.delete("/vms/{vm_id}", status_code=204)
def delete_vm(vm_id: UUID, request: Request):
    request.app.state.manager.delete_vm(vm_id)
    return Response(status_code=204)


class VMCollector:
    def __init__(self, rows):
        self.rows = rows

    def collect(self):
        for key, help_text, metric_type in [
            ("running", "VM running state", GaugeMetricFamily),
            ("cpu_seconds", "QEMU CPU time in seconds", CounterMetricFamily),
            ("memory_rss_bytes", "QEMU resident memory in bytes", GaugeMetricFamily),
            ("memory_configured_bytes", "Configured guest memory in bytes", GaugeMetricFamily),
            ("disk_allocated_bytes", "Allocated overlay disk blocks in bytes", GaugeMetricFamily),
        ]:
            family = metric_type("cloudmaker_vm_" + key, help_text, labels=["vm_id"])
            for row in self.rows:
                if key in row:
                    family.add_metric([row["id"]], row[key])
            yield family


@app.get("/metrics")
def metrics(request: Request):
    registry = CollectorRegistry()
    registry.register(VMCollector(request.app.state.manager.metrics()))
    return Response(generate_latest(registry), headers={"Content-Type": CONTENT_TYPE_LATEST})
