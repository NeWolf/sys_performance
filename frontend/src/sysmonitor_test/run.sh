#!/bin/bash
# ============================================================
# sysmonitor perf_test 一键测试脚本
# 用法:
#   ./run.sh start [间隔秒]   开始采集(默认间隔1秒), 后台常驻跑
#   ./run.sh stop             停止采集
#   ./run.sh pull [目录]      把数据拉到本地(默认 ./perf_out)
#   ./run.sh status           看是否在跑 + 当前数据文件
# 前提: 电脑已连 adb, 设备已 root(adb root 或设备支持 su)
# ============================================================
set -e
BIN=sysmonitor
DEV_BIN=/data/local/tmp/sysmonitor_test
PERF_DIR=/log/sys/perf

need_adb() {
  adb get-state >/dev/null 2>&1 || { echo "错误: 没连上设备, 先 adb connect / 插线"; exit 1; }
}

cmd_start() {
  need_adb
  local interval="${1:-1}"
  echo "[1/4] 推送二进制..."
  adb push "$BIN" "$DEV_BIN" >/dev/null
  adb shell chmod 755 "$DEV_BIN"
  echo "[2/4] 配置属性 (perf_test=1, 间隔=${interval}s, 写文件, 不刷logcat)..."
  adb shell setprop persist.sm.perf.test 1
  adb shell setprop persist.sm.perf.interval "$interval"
  adb shell setprop persist.sm.perf.tofile 1
  adb shell setprop persist.sm.perf.tologcat 0
  echo "[3/4] 停掉旧实例(若有)..."
  adb shell "su 0 pkill -f sysmonitor_test" 2>/dev/null || true
  sleep 1
  echo "[4/4] 后台启动..."
  adb shell "su 0 sh -c \"nohup $DEV_BIN >/dev/null 2>&1 &\""
  sleep 2
  local pid
  pid=$(adb shell "pgrep -f sysmonitor_test" 2>/dev/null | tr -d '\r' | head -1)
  echo "已启动, PID=$pid, 数据写入 $PERF_DIR/perf.log"
  echo "跑一段时间后:  ./run.sh stop   然后  ./run.sh pull"
}

cmd_stop() {
  need_adb
  adb shell "su 0 pkill -f sysmonitor_test" 2>/dev/null || true
  adb shell setprop persist.sm.perf.test 0
  echo "已停止采集"
}

cmd_pull() {
  need_adb
  local out="${1:-./perf_out}"
  mkdir -p "$out"
  # /log 需 root 读, 先 copy 到可 pull 的位置
  adb shell "su 0 sh -c \"cp $PERF_DIR/perf*.log /data/local/tmp/ 2>/dev/null; chmod 666 /data/local/tmp/perf*.log 2>/dev/null\"" || true
  adb pull /data/local/tmp/perf.log "$out/" 2>/dev/null || true
  for i in 1 2 3 4; do
    adb pull "/data/local/tmp/perf.$i.log" "$out/" 2>/dev/null || true
  done
  echo "已拉取到 $out/ :"
  ls -la "$out/"
}

cmd_status() {
  need_adb
  local pid
  pid=$(adb shell "pgrep -f sysmonitor_test" 2>/dev/null | tr -d '\r' | head -1)
  if [ -n "$pid" ]; then echo "运行中, PID=$pid"; else echo "未运行"; fi
  echo "perf_test 属性 = $(adb shell getprop persist.sm.perf.test | tr -d '\r')"
  echo "数据文件:"
  adb shell "su 0 ls -la $PERF_DIR/ 2>/dev/null" || echo "  (无)"
}

case "$1" in
  start)  cmd_start "$2" ;;
  stop)   cmd_stop ;;
  pull)   cmd_pull "$2" ;;
  status) cmd_status ;;
  *) echo "用法: $0 {start [间隔秒] | stop | pull [目录] | status}"; exit 1 ;;
esac
