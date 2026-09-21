"""Explicit-device ADB controls; never execute a host shell or restart adbd."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid

REMOTE_BIN = "/data/local/tmp/sysmonitor_test"
PERF_DIR = "/log/sys/perf"
LOG_NAMES = ["perf.log"] + [f"perf.{i}.log" for i in range(1, 5)]
PROPERTIES = ("test", "interval", "tofile", "tologcat", "async")
MAX_FILE_BYTES = 1024 * 1024 * 1024


class AdbError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class AdbController:
    def __init__(self, archive, executable=None):
        self.archive = Path(archive)
        self.executable = executable or os.environ.get("SYSMONITOR_ADB") or shutil.which("adb")
        self.binary = Path(__file__).parent / "frontend/src/sysmonitor_test/sysmonitor"
        self.lock = threading.Lock()

    def run(self, *args, timeout=10, output=None):
        if not self.executable:
            raise AdbError("未找到 adb，请安装 Android platform-tools 并加入 PATH", 503)
        try:
            result = subprocess.run([self.executable, *args], stdin=subprocess.DEVNULL,
                                    stdout=output if output is not None else subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout, shell=False)
        except subprocess.TimeoutExpired as exc:
            raise AdbError("ADB 操作超时；设备操作可能已生效，请刷新状态后重试", 504) from exc
        except OSError as exc:
            raise AdbError("无法执行 ADB，请检查安装及本机文件权限", 503) from exc
        if result.returncode:
            message = result.stderr.decode("utf-8", "replace").strip()[:600]
            raise AdbError("ADB 执行失败：" + (message or "请检查设备授权、连接及权限"), 502)
        return result.stdout.decode("utf-8", "replace").strip() if output is None else ""

    def devices(self):
        rows = []
        for line in self.run("devices", "-l").splitlines():
            parts = line.split()
            if len(parts) < 2 or line.startswith(("List of devices", "*")):
                continue
            details = dict(item.split(":", 1) for item in parts[2:] if ":" in item)
            rows.append({"serial": parts[0], "state": parts[1], "model": details.get("model", "")})
        return rows

    @contextmanager
    def device(self, serial):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,199}", serial):
            raise AdbError("无效的设备序列号")
        if not self.lock.acquire(blocking=False):
            raise AdbError("已有设备操作正在执行，请稍后重试", 409)
        try:
            device = next((row for row in self.devices() if row["serial"] == serial), None)
            if not device or device["state"] != "device":
                raise AdbError("设备未连接、离线或尚未授权，请检查设备上的 USB 调试提示", 409)
            yield
        finally:
            self.lock.release()

    def shell(self, serial, script, root=False, output=None):
        command = "su 0 sh -c " + shlex.quote(script) if root else "sh -c " + shlex.quote(script)
        return self.run("-s", serial, "shell", "-T", command,
                        timeout=40 if output is not None else 10, output=output)

    def root_mode(self, serial):
        if self.shell(serial, "id -u") == "0":
            return False
        if self.shell(serial, "id -u", True) != "0":
            raise AdbError("需要 root adbd 或支持 su 0 的设备；不会自动执行 adb root", 403)
        return True

    def pids(self, serial, root):
        script = ('for p in /proc/[0-9]*/exe; do '
                  f'if [ "$(readlink "$p")" = "{REMOTE_BIN}" ]; then '
                  'v=${p#/proc/}; echo "${v%/exe}"; fi; done')
        values = self.shell(serial, script, root).split()
        if any(not value.isdecimal() for value in values):
            raise AdbError("设备进程查询返回无效结果", 502)
        return values

    def properties(self, serial):
        return {key: self.shell(serial, "getprop persist.sm.perf." + key) for key in PROPERTIES}

    def setprop(self, serial, key, value, root):
        self.shell(serial, "setprop persist.sm.perf." + key + " " + shlex.quote(value), root)
        if self.shell(serial, "getprop persist.sm.perf." + key) != value:
            raise AdbError("属性设置未生效：" + key, 502)

    def status(self, serial):
        with self.device(serial):
            root = self.root_mode(serial)
            return self.snapshot(serial, root)

    def is_deployed(self, serial, root):
        return self.shell(serial, f'if [ -f {REMOTE_BIN} ] && [ -x {REMOTE_BIN} ]; then echo yes; fi', root) == "yes"

    def snapshot(self, serial, root):
        files = self.shell(serial, f'if [ -d {PERF_DIR} ]; then ls -ln {PERF_DIR}; fi', root)
        return {"serial": serial, "pids": self.pids(serial, root),
                "properties": self.properties(serial), "files": files,
                "privilege": "su 0" if root else "root adbd", "deployed": self.is_deployed(serial, root)}

    def start(self, serial, interval, tologcat=False, async_write=False):
        with self.device(serial):
            root = self.root_mode(serial)
            if self.pids(serial, root):
                raise AdbError("测试实例已运行，请先停止再更改配置", 409)
            if "arm64-v8a" not in self.shell(serial, "getprop ro.product.cpu.abilist").split(","):
                raise AdbError("附带程序仅支持 Android ARM64，设备架构不兼容")
            previous = self.properties(serial)
            if previous["test"] == "1":
                raise AdbError("设备已有性能采集开启，请先停止；共享属性会影响系统实例", 409)
            if not self.is_deployed(serial, root):
                if not self.binary.is_file():
                    raise AdbError("本机缺少附带的 sysmonitor 二进制", 503)
                self.run("-s", serial, "push", str(self.binary), REMOTE_BIN + ".new", timeout=30)
                self.shell(serial, f"chmod 755 {REMOTE_BIN}.new && mv {REMOTE_BIN}.new {REMOTE_BIN}", root)
                if not self.is_deployed(serial, root):
                    raise AdbError("采集程序部署失败，请检查设备文件与执行权限", 502)
            try:
                for key, value in (("interval", str(interval)), ("tofile", "1"),
                                   ("tologcat", str(int(tologcat))), ("async", str(int(async_write))), ("test", "1")):
                    self.setprop(serial, key, value, root)
                self.shell(serial, f"nohup {REMOTE_BIN} >/data/local/tmp/sysmonitor_test.stderr 2>&1 </dev/null &", root)
                time.sleep(2)
                if not self.pids(serial, root):
                    raise AdbError("程序启动失败，请检查设备 /data/local/tmp/sysmonitor_test.stderr 与动态库兼容性", 502)
                return self.snapshot(serial, root)
            except AdbError as exc:
                # Disable collection after partial start; never report a rollback we cannot confirm.
                try:
                    self.setprop(serial, "test", "0", root)
                except AdbError:
                    raise AdbError(str(exc) + "；关闭采集也失败，请立即检查设备状态", 502) from exc
                raise AdbError(str(exc) + "；采集开关已关闭，其余配置可能已修改，请刷新状态", exc.status) from exc

    def stop(self, serial):
        with self.device(serial):
            root = self.root_mode(serial)
            self._stop(serial, root)
            return self.snapshot(serial, root)

    def _stop(self, serial, root):
        """Caller holds the device lock throughout stop, pull or deletion."""
        self.setprop(serial, "test", "0", root)
        # Recheck the executable immediately before signalling each PID.
        for pid in self.pids(serial, root):
            self.shell(serial, f'[ "$(readlink /proc/{pid}/exe)" != "{REMOTE_BIN}" ] || kill -TERM {pid}', root)
        for _ in range(10):
            if not self.pids(serial, root):
                return
            time.sleep(0.2)
        raise AdbError("采集开关已关闭，但测试进程尚未退出；未强制杀进程，请刷新状态", 409)

    def delete_logs(self, serial):
        with self.device(serial):
            root = self.root_mode(serial)
            # Do not trust a potentially stale browser status or release the lock.
            self._stop(serial, root)
            if self.properties(serial)["test"] != "0" or self.pids(serial, root):
                raise AdbError("采集未确认停止，未删除设备日志，请刷新状态后重试", 409)
            paths = " ".join(shlex.quote(PERF_DIR + "/" + name) for name in LOG_NAMES)
            # Fixed allowlist only: no caller paths, globs or recursive deletion.
            try:
                self.shell(serial, "rm -f -- " + paths, root)
                remaining = self.shell(
                    serial, f'for f in {paths}; do if [ -e "$f" ] || [ -L "$f" ]; '
                    'then echo "$f"; fi; done', root)
                if remaining:
                    raise AdbError("设备仍有未删除的性能日志，请检查权限后重试", 502)
            except AdbError as exc:
                raise AdbError("采集已停止；日志删除未完成，可能已部分删除：" + str(exc), exc.status) from exc
            return self.snapshot(serial, root)

    def pull(self, serial, store, title=""):
        with self.device(serial):
            root = self.root_mode(serial)
            if self.properties(serial)["test"] != "0" or self.pids(serial, root):
                self._stop(serial, root)
            if self.properties(serial)["test"] != "0" or self.pids(serial, root):
                raise AdbError("采集未确认停止，未拉取日志，请刷新状态后重试", 409)
            folder = self.archive / uuid.uuid4().hex
            folder.mkdir(parents=True)
            paths = []
            for name in LOG_NAMES:
                remote = PERF_DIR + "/" + name
                exists = self.shell(serial, f'if [ -f {remote} ]; then echo yes; fi', root)
                if exists != "yes":
                    continue
                path = folder / name
                with path.open("wb") as output:
                    self.shell(serial, f"head -c {MAX_FILE_BYTES + 1} {remote}", root, output)
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise AdbError("设备日志超过单文件 1 GB 限制；本次未导入", 413)
                paths.append(path)
            if not paths:
                raise AdbError("设备没有可拉取的性能日志")
            with ExitStack() as stack:
                sources = [(path.name, stack.enter_context(path.open("rb"))) for path in paths]
                result = store.import_files(sources, title or "ADB 采集 " + serial)
            return dict(result, archive=str(folder), files=[path.name for path in paths])