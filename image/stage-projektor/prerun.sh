#!/bin/bash -e
# Start from a fresh copy of stage2 (Lite) every time (build.sh sets CLEAN=1, so a
# --fast rebuild never runs on top of a previous result).
if [ ! -d "${ROOTFS_DIR}" ]; then
	copy_previous
fi
