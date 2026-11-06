import shutil
from pathlib import Path
from xml.etree import ElementTree as ET

from . import config
from .manager import Manager
from .store import Store


def clean(manager):
    with manager.lock, manager.connection() as connection:
        domains = connection.listAllDomains()
        # Refuse to remove storage referenced by a domain outside this installation.
        for domain in domains:
            if manager.metadata(domain) is not None:
                continue
            root = ET.fromstring(domain.XMLDesc(0))
            for source in root.findall(".//source[@file]"):
                if Path(source.get("file")).resolve().is_relative_to(manager.storage):
                    raise RuntimeError(f"Каталог используется чужой ВМ: {domain.name()}")
        for domain in domains:
            if manager.metadata(domain) is not None:
                if domain.isActive():
                    domain.destroy()
                domain.undefine()
        for name in ("vms", "images"):
            path = manager.storage / name
            if path.exists():
                shutil.rmtree(path)


if __name__ == "__main__":
    clean(Manager(Store(config.DATABASE), config.STORAGE, config.LIBVIRT_URI))
