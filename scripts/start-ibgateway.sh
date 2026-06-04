#!/bin/bash
# Start IBGateway via IBC. Runs in a loop so it restarts after daily auto-restart at 00:05.
# Uses -inline so the loop correctly blocks until IBC exits (not a forced 60s cycle).

IBC_PATH=/opt/solo-trader/ibc
GATEWAY_PATH=/opt/solo-trader/gateway
DISPLAY_NUM=${DISPLAY_NUM:-1}

pkill -f "$IBC_PATH" 2>/dev/null
pkill -f "Xvfb :${DISPLAY_NUM}" 2>/dev/null
sleep 2

mv "${GATEWAY_PATH}/ibgateway/1045/ibgateway1" "${GATEWAY_PATH}/ibgateway/1045/ibgateway" 2>/dev/null

Xvfb ":${DISPLAY_NUM}" -screen 0 1024x768x24 &
export DISPLAY=":${DISPLAY_NUM}"
sleep 2

cd "$IBC_PATH"
while true; do
    mv "${GATEWAY_PATH}/ibgateway/1045/ibgateway1" "${GATEWAY_PATH}/ibgateway/1045/ibgateway" 2>/dev/null
    pkill -f "$IBC_PATH" 2>/dev/null
    sleep 2
    bash gatewaystart.sh -inline
    echo "Gateway exited, waiting 60s before restart..."
    sleep 60
done
