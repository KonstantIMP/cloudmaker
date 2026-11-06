#!/bin/sh
set -eu
exec virsh -c qemu:///system console "cloudmaker-$1"
