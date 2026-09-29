"""Safe bridge to the bundled JoySpace read/import tools (no npm dependency)."""
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit


TEMPLATE_URL = "https://joyspace.jd.com/pages/rXNrVj6wBpf16NbPkmFh"
READ_SCRIPT = "skills/joyspace-read-doc/scripts/read_joyspace_doc.js"
IMPORT_SCRIPT = "skills/markdown-to-joyspace/scripts/import_markdown_doc.js"
PAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
PUBLISH_UNKNOWN = "发布结果未知，远端可能存在文档、临时页或图片残留；请先在 JoySpace 人工确认，不要自动重试。"
PROBE = "console.log(JSON.stringify({version:process.version,platform:process.platform,arch:process.arch}))"


class JoySpaceError(RuntimeError):
    """An intentionally safe message suitable for display to the user."""


def _root():
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent / "_internal"))
    return Path(__file__).resolve().parent


def _runtime():
    root = _root()
    skill = root / "joyspace_assets/joyspace-kit"
    for name in ("SKILL.md", READ_SCRIPT, IMPORT_SCRIPT, "skills/shared-auth.mjs",
                 "skills/hioffice-auth/scripts/hioffice-auth.mjs"):
        if not (skill / name).is_file():
            raise JoySpaceError("内置 JoySpace 资源不完整，请重新安装应用。")
    node = root / "joyspace_assets/node" / ("node.exe" if os.name == "nt" else "node")
    if not node.is_file():
        if getattr(sys, "frozen", False):
            raise JoySpaceError("内置 Node 运行时缺失，请重新安装应用。")
        found = shutil.which("node")
        if not found:
            raise JoySpaceError("开发环境需要 Node 22.7 或更高版本。")
        node = Path(found)
    if os.name != "nt" and not os.access(node, os.X_OK):
        raise JoySpaceError("Node 运行时不可执行，请重新安装应用。")
    return node.resolve(), skill.resolve()


def _environment(probe=False):
    if probe:
        # No auth helper is invoked, and no credentials are inherited by probes.
        env = {k: v for k, v in os.environ.items()
               if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    else:
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("NODE_", "DYLD_", "LD_"))}
    env["PATH"] = ""
    return env


def _invoke(node, args, skill, *, timeout, publish=False, probe=False):
    try:
        result = subprocess.run(
            [str(node), *args], cwd=str(skill), env=_environment(probe),
            shell=False, capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, check=False,
        )
        if result.returncode:
            raise ValueError("script failed")
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError("invalid result")
        return data
    except subprocess.TimeoutExpired:
        raise JoySpaceError(PUBLISH_UNKNOWN if publish else "JoySpace 读取超时，请检查内网连接和 HiOffice 登录状态。") from None
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        raise JoySpaceError(PUBLISH_UNKNOWN if publish else "JoySpace 操作失败，请检查内网连接和 HiOffice 登录状态。") from None


def _check_runtime():
    node, skill = _runtime()
    data = _invoke(node, ["-e", PROBE], skill, timeout=10, probe=True)
    version = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", str(data.get("version", "")))
    arches = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}
    systems = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}
    if (not version or tuple(map(int, version.groups())) < (22, 7, 0)
            or data.get("platform") != systems.get(platform.system())
            or data.get("arch") != arches.get(platform.machine().lower())):
        raise JoySpaceError("Node 运行时版本、平台或架构不兼容，请重新安装应用。")
    return node, skill


def status():
    """Only readiness, never credentials or an implicit authentication attempt."""
    try:
        _check_runtime()
    except JoySpaceError as exc:
        return {"ready": False, "message": str(exc)}
    return {"ready": True, "message": "JoySpace 运行时就绪；尚未验证认证，请保持 HiOffice 登录及内网连接。"}


def _page_id(value):
    if not isinstance(value, str) or value != value.strip():
        raise JoySpaceError("请选择有效的 JoySpace 页面链接或页面 ID。")
    if PAGE_ID.fullmatch(value):
        return value
    try:
        url = urlsplit(value)
        match = re.fullmatch(r"/pages/([A-Za-z0-9_-]{1,128})/?", url.path)
        if (url.scheme != "https" or url.netloc != "joyspace.jd.com"
                or url.query or url.fragment or not match):
            raise ValueError("untrusted page URL")
        return match.group(1)
    except ValueError:
        raise JoySpaceError("仅支持 https://joyspace.jd.com/pages/ 页面链接或页面 ID。") from None


def read_template():
    node, skill = _check_runtime()
    data = _invoke(node, [str(skill / READ_SCRIPT), "--url", TEMPLATE_URL], skill, timeout=90)
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise JoySpaceError("JoySpace 模板内容为空或格式无效。")
    return content


def publish_markdown(file_path, title, placement="personal", parent_page=None):
    if placement not in ("personal", "child", "sibling"):
        raise JoySpaceError("发布位置必须是 personal、child 或 sibling。")
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or "\x00" in title:
        raise JoySpaceError("文档标题不能为空且不能超过 200 个字符。")
    try:
        path = Path(file_path).resolve(strict=True)
        if not path.is_file() or path.suffix.lower() not in (".md", ".markdown"):
            raise ValueError("invalid markdown file")
    except (OSError, ValueError, TypeError):
        raise JoySpaceError("请选择存在的 Markdown 文件。") from None
    target = None
    if placement != "personal":
        target = _page_id(parent_page)
    elif parent_page:
        raise JoySpaceError("个人空间发布不接受目标页面。")
    node, skill = _check_runtime()
    args = [str(skill / IMPORT_SCRIPT), "--file", str(path), "--title", title.strip()]
    if placement == "child":
        args.extend(["--parent-page-id", target])
    elif placement == "sibling":
        args.extend(["--page-url", "https://joyspace.jd.com/pages/" + target])
    data = _invoke(node, args, skill, timeout=180, publish=True)
    page_id = data.get("pageId")
    if (data.get("verified") is not True or not isinstance(page_id, str)
            or not PAGE_ID.fullmatch(page_id)):
        raise JoySpaceError(PUBLISH_UNKNOWN)
    # Do not forward arbitrary links, authentication metadata or local paths.
    return {"pageId": page_id, "link": "https://joyspace.jd.com/pages/" + page_id}