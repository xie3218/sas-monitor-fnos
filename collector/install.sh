#!/bin/bash
set -euo pipefail
if [[ "$EUID" != 0 ]]; then
    echo "请使用 sudo bash collector/install.sh 运行。" >&2
    exit 1
fi
cd -- "$(dirname -- "$0")"
if ! getent group sas-monitor >/dev/null; then
    echo "先在飞牛应用中心安装 SAS 硬盘管理，再安装采集服务。" >&2
    exit 1
fi
if [[ ! -x /usr/bin/python3 ]]; then
    echo "请先安装 python3。" >&2
    exit 1
fi
if [[ ! -x /usr/sbin/smartctl && ! -x /usr/bin/smartctl ]]; then
    echo "请先安装 smartmontools。" >&2
    exit 1
fi
install -d -o root -g root -m 0755 /usr/local/lib/sas-monitor
install -o root -g root -m 0644 collect.py scan.py /usr/local/lib/sas-monitor/
install -o root -g root -m 0644 sas-monitor-collector.{service,timer,path} /etc/systemd/system/
install -o root -g root -m 0644 sas-monitor-scan.{socket,service} /etc/systemd/system/
install -o root -g root -m 0644 sas-monitor-refresh.conf /etc/tmpfiles.d/sas-monitor-refresh.conf
systemd-tmpfiles --create /etc/tmpfiles.d/sas-monitor-refresh.conf
systemctl daemon-reload
systemctl enable --now sas-monitor-collector.timer sas-monitor-collector.path
systemctl enable --now sas-monitor-scan.socket
# An existing worker must pick up the installed code. A scan in progress becomes interrupted.
systemctl try-restart sas-monitor-scan.service
systemctl start --no-block sas-monitor-collector.service
echo "采集已开始。完成后从飞牛桌面打开「SAS 硬盘管理」。"
