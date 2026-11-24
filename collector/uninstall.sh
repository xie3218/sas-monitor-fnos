#!/bin/bash
set -euo pipefail
if [[ "$EUID" != 0 ]]; then
    echo "请使用 sudo bash collector/uninstall.sh 运行。" >&2
    exit 1
fi
if [[ -f /etc/systemd/system/sas-monitor-collector.path ]]; then
    systemctl disable --now sas-monitor-collector.path
fi
if [[ -f /etc/systemd/system/sas-monitor-scan.socket ]]; then
    systemctl disable --now sas-monitor-scan.socket
    systemctl stop sas-monitor-scan.service
fi
if [[ -f /etc/systemd/system/sas-monitor-collector.timer ]]; then
    systemctl disable --now sas-monitor-collector.timer
    systemctl stop sas-monitor-collector.service
fi
rm -f -- /etc/systemd/system/sas-monitor-collector.{service,timer,path}
rm -f -- /etc/systemd/system/sas-monitor-scan.{socket,service}
rm -f -- /etc/tmpfiles.d/sas-monitor-refresh.conf /run/sas-monitor-refresh/request
rmdir /run/sas-monitor-refresh 2>/dev/null || true
rm -f -- /usr/local/lib/sas-monitor/{collect,scan}.py /run/sas-monitor/snapshot.json
rmdir /usr/local/lib/sas-monitor 2>/dev/null || true
rmdir /run/sas-monitor 2>/dev/null || true
rm -f -- /var/lib/sas-monitor/non-medium.json
rmdir /var/lib/sas-monitor 2>/dev/null || true
rm -f -- /var/lib/sas-monitor-scan/results.json
rmdir /var/lib/sas-monitor-scan 2>/dev/null || true
systemctl daemon-reload
