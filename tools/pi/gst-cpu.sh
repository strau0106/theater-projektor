#!/bin/sh
# usage: cpu.sh "<pipeline>" -> avg CPU% over 12 s, shmem delta, warnings
export GST_GL_WINDOW=gbm GST_GL_PLATFORM=egl
cd /tmp
s0=$(awk '/^Shmem:/{print $2}' /proc/meminfo)
gst-launch-1.0 -q $1 > /tmp/c.log 2>&1 &
P=$!
sleep 12
cpu=$(ps -o pcpu= -p $P); s1=$(awk '/^Shmem:/{print $2}' /proc/meminfo)
kill $P 2>/dev/null; wait $P 2>/dev/null
echo "cpu=${cpu}% shmem+=$(( (s1-s0)/1024 ))MB drops=$(grep -c dropped /tmp/c.log) err=$(grep -m1 -i error /tmp/c.log | cut -c1-120)"
