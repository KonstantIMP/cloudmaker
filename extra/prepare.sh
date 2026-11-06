#!/bin/sh
set -eu
cd "$(dirname "$0")"

storage=${STORAGE_DIR:-/var/lib/libvirt/images/cloudmaker}
version=20261026-2643
filename=debian-12-genericcloud-amd64-$version.qcow2
checksum=0464b5d4ae3aa4f658468d88da83af7857b71548b30d46dcb6282770955a747fb95a983476db90d25afd0653af607224c7ebd05122ab71faa106fd712aaad652

install -d -m 755 "$storage/images" "$storage/vms"
cd "$storage/images"
if [ ! -f "$filename" ]; then
    curl -fL --retry 3 "https://cloud.debian.org/images/cloud/bookworm/$version/$filename" -o "$filename.part"
    echo "$checksum  $filename.part" | sha512sum -c -
    mv "$filename.part" "$filename"
fi
echo "$checksum  $filename" | sha512sum -c -
chmod 644 "$filename"
