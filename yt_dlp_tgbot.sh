#!/bin/bash
while true
do
    if ! pgrep -f "python3 main.py" > /dev/null
    then
        cd /root/
        exec python3 main.py &
    fi
    sleep 1
done
