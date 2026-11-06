.DEFAULT_GOAL := run
.PHONY: disk seed run

IMAGE := cirros-0.6.3-x86_64-disk.img

disk: build/vm.qcow2

seed: build/seed.iso

build:
	mkdir -p build

build/vm.qcow2: | build images/$(IMAGE)
	qemu-img create -f qcow2 -F qcow2 -b "../images/$(IMAGE)" build/vm.qcow2

build/seed.iso: seed/meta-data | build
	xorriso -as mkisofs -quiet -V cidata -o build/seed.iso seed

run: disk seed
	qemu-system-x86_64 -enable-kvm -m 256 -smp 1 -nographic \
		-drive file=build/vm.qcow2,format=qcow2,if=virtio \
		-drive file=build/seed.iso,format=raw,media=cdrom,readonly=on \
		-nic user,model=virtio-net-pci -device virtio-rng-pci
