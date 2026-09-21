---
name: bambu-print
description: 拓竹打印：用 Bambu Studio CLI 准备模型，通过已登录云端账号启动 P1S 打印或查询设备状态。适用于 Linux、0.4 mm 喷嘴、外置 PLA 料盘。
---

用本 skill 的 `scripts/bambu_print.py` 执行固定流程；参数详情读 `--help`，故障时再读脚本。

```bash
# 查询当前选择的打印机；也可用 --printer 指定名称或序列号
python scripts/bambu_print.py status

# STL 保留原朝向并放到热床；开启顶面熨烫，仅准备文件
python scripts/bambu_print.py prepare panel.stl --ironing topmost --support auto --out ./print-job

# 启动已经准备好的任务，使用外置 PLA
python scripts/bambu_print.py print ./print-job --printer HL-P1S
```

在 skill 目录执行以上命令，或使用脚本的绝对路径。含模型的已切片单盘 `.3mf` 使用内嵌模型和原工艺重新切片、检查；除支撑开关外的工艺变更使用 STL。`print` 可直接接受模型，省去单独的 `prepare`。

悬空风险以 Bambu CLI 为准。三种 `--support auto|on|off` 模式都先关闭支撑预切片，读取官方结果和日志；`auto` 发现悬空警告后启用支撑重切，`on` 始终启用，`off` 保持关闭。报告保存在 `support_report.json` / `job.json`；风险未处理或最终切片仍有警告时阻止下发。旧任务缺少官方检查记录时，重新 `prepare` 源模型。

用户要求启动打印时执行 `print`，已有授权沿用；只询问打印方法、准备文件或创建 skill 时执行 `prepare` / `--dry-run`。脚本会加载已登录会话、初始化官方证书并核对设备状态，无需收集账号密码或局域网 IP。

完成标准：回执 `printer_start_confirmed=true` 才报告已启动，并说明设备当前阶段和预计耗时。超时或返回不确定结果时先查状态、保留原任务目录，确认结果后再处理；同一目录的下发标记会阻止重复打印。
