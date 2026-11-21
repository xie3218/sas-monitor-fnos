# SAS 硬盘管理 · 飞牛 fnOS

在飞牛桌面查看 SAS 硬盘的 SMART 状态、温度、增长缺陷和错误趋势。应用面向能够访问物理 SAS 盘的飞牛 NAS，支持多盘切换、手动采集和 CSV 导出。

**当前版本 v0.1.6 · [下载飞牛安装包](downloads/sas-monitor-fnos-0.1.6.zip)**

ZIP 包中包含 `sas-monitor.fpk`、后台采集服务和源码。

## 功能

| 功能 | 能查看或执行的操作 |
|---|---|
| 硬盘概览 | 查看型号、序列号、WWN、固件、容量、扇区大小、温度和通电时间 |
| SAS 健康监控 | 查看 G-list、SCSI 读写与校验错误、非介质错误和 SMART 自检历史 |
| 错误趋势 | 查看本次新增非介质错误、最近观测到增长的时间和触发状态的原因 |
| 多盘管理 | 搜索设备、按健康状态筛选、点击列表或使用下拉框切换硬盘 |
| SMART 采集 | 后台在上次采集结束约 5 分钟后再次运行。点击「立即采集」可主动读取硬盘 |
| 数据导出 | 下载单盘原始 SMART 文本，或把全部硬盘和判定原因导出为 CSV |
| 飞牛桌面适配 | 浅色毛玻璃界面。宽窗口并排显示列表和详情，窄窗口把列表变成卡片 |

## 安装

### 环境

- 飞牛 fnOS，系统版本需支持应用统一网关。
- 飞牛管理员账号、SSH 和 sudo 权限。
- NAS 上需有 `python3`、`smartmontools` 和 systemd。
- LSI/Broadcom IT 模式 HBA 等可直通 SAS 物理盘的环境。部分 MegaRAID 控制器可通过 smartctl 的物理盘枚举访问，实际取决于驱动和固件。
- 在 PVE 虚拟机中运行飞牛时，请确认**飞牛虚拟机本身**能查询物理盘的 SAS SMART 数据。

### 首次安装

1. 解压下载的 ZIP，在飞牛「应用中心 → 手动安装」上传 `sas-monitor.fpk`。
2. 将包内 `collector/` 目录上传到 NAS，SSH 进入该目录。
3. 缺少依赖时先安装依赖，再执行安装脚本。

   ```bash
   sudo apt install smartmontools python3
   sudo bash ./install.sh
   ```

4. 以飞牛管理员身份打开桌面上的「SAS 硬盘管理」。首次采集完成后会出现硬盘列表。

### 从旧版升级

先在应用中心上传新版 `sas-monitor.fpk`，然后关闭并重新打开 App。已经运行 v0.1.5 采集服务的用户可以直接使用 v0.1.6。更早的版本需要将新版 `collector/` 上传到 NAS 并在该目录再次执行 `sudo bash ./install.sh`，以启用新版告警规则。

## 如何看懂告警

| 状态 | 判定 |
|---|---|
| 健康 | SMART 总体状态通过，当前无告警条件 |
| 需关注 | SMART 总体状态通过，且 G-list 大于零、存在不可纠正错误、自检日志有失败记录，或过去 24 小时观测到非介质错误增加 |
| SMART 故障 | 硬盘或 smartctl 报告 SMART 总体状态失败 |
| 未知 | 无法确认 SMART 总体状态 |

首次成功读取非介质错误时，应用会把累计值作为基线。例如 **535 → 535** 表示新增 0，**535 → 536** 表示新增 1。观测到新增长后，非介质错误会触发 **24 小时**的提示。基线按 WWN 跟踪，缺少 WWN 时使用型号和序列号。设备名变化或 NAS 重启后，保存的基线仍可用于对比。

缺少的 SMART 指标在界面中显示为「—」。查看某块盘的详情即可确认是哪个指标触发了提示。

## 常见问题

| 现象 | 排查 |
|---|---|
| 页面一直显示「等待首次采集」 | 在 NAS 上运行新版 `collector/install.sh`，再检查采集日志 |
| 页面没有显示某块 SAS 盘 | 在飞牛的 SSH 里运行 `sudo smartctl --scan-open`。核对 HBA、磁盘透传和实际返回的设备类型 |
| 「立即采集」超时 | 检查 systemd 的请求监听单元和采集服务日志 |
| 列表一直显示旧结果 | 核对页面的采集时间，在 NAS 上手动启动采集服务并查看日志 |
| 非介质错误持续上涨 | 对比同一序列号的日志和 SAS PHY 计数。多盘同步上涨时，优先核对它们共用的线缆、背板或 HBA |

```bash
sudo smartctl --scan-open
sudo systemctl status sas-monitor-collector.path sas-monitor-collector.timer --no-pager
sudo systemctl start sas-monitor-collector.service
sudo journalctl -u sas-monitor-collector.service -n 50 --no-pager
```

针对单盘查看原始报告时，设备名和驱动类型以 `--scan-open` 的输出为准。例如扫描结果为 `/dev/sda -d scsi` 时，可运行 `sudo smartctl -x -d scsi /dev/sda`。

## 源码与构建

| 路径 | 用途 |
|---|---|
| [`fpk/`](fpk/) | 飞牛清单、权限配置、桌面入口、页面及本地服务 |
| [`collector/`](collector/) | SMART 采集脚本及 systemd 安装、定时采集和即时请求配置 |
| [`downloads/`](downloads/) | 已打包的版本，可直接下载到 NAS 安装 |

在装有飞牛官方 `fnpack` 的环境中打包：

```bash
cd fpk
fnpack build
```

采集器在 NAS 上执行只读 SMART 查询，飞牛应用通过本地 Unix Socket 复用管理员登录态。采集结果和告警基线保存在 NAS 本机。定时 SMART 查询可能唤醒休眠盘。

卸载时，可先在 NAS 的 `collector/` 目录运行 `sudo bash ./uninstall.sh` 移除采集服务和告警基线，再从飞牛应用中心卸载桌面 App。
