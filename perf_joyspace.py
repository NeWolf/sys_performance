"""Safe bridge to the bundled JoySpace read/import tools (no npm dependency)."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

import perf_slate


# Read the templates in order: the first one that answers with content wins,
# the rest are only fallbacks for an unreachable or empty primary template.
TEMPLATE_URLS = (
    "https://joyspace.jd.com/pages/u4e8K20FZJF0SsDUG5fW",
    "https://joyspace.jd.com/pages/rXNrVj6wBpf16NbPkmFh",
)
TEMPLATE_URL = TEMPLATE_URLS[0]
READ_SCRIPT = "skills/joyspace-read-doc/scripts/read_joyspace_doc.js"
IMPORT_SCRIPT = "skills/markdown-to-joyspace/scripts/import_markdown_doc.js"
SLATE_SCRIPT = "skills/markdown-to-joyspace/scripts/import_slate_doc.mjs"
LIST_SCRIPT = "skills/joyspace-list/scripts/list_joyspace.mjs"
PAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")
MARKDOWN_PAGE = 13
PUBLISH_UNKNOWN = "发布结果未知，远端可能存在文档、临时页或图片残留；请先在 JoySpace 人工确认，不要自动重试。"
PUBLISH_RECOVERED = ("发布结果未收到确认，但在最近文档中找到同名页面；"
                     "该链接未经内容校验，请人工核对正文后再使用。")
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
    for name in ("SKILL.md", READ_SCRIPT, IMPORT_SCRIPT, SLATE_SCRIPT, LIST_SCRIPT,
                "skills/shared-auth.mjs",
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


def _invoke(node, args, skill, *, timeout, publish=False, probe=False, shape=dict):
    try:
        result = subprocess.run(
            [str(node), *args], cwd=str(skill), env=_environment(probe),
            shell=False, capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, check=False,
        )
        if result.returncode:
            raise ValueError("script failed")
        data = json.loads(result.stdout)
        if not isinstance(data, shape):
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
    """Read the template as a Slate block tree, not as Markdown.

    The report is written straight into this tree and uploaded unchanged, so
    the published page keeps the template's own tables, merges and colours.
    """
    node, skill = _check_runtime()
    failure = None
    for url in TEMPLATE_URLS:
        try:
            data = _invoke(node, [str(skill / READ_SCRIPT), "--url", url, "--raw"],
                           skill, timeout=90)
            raw = data.get("raw")
            content = raw.get("content") if isinstance(raw, dict) else None
            blocks = content.get("content") if isinstance(content, dict) else None
            try:
                return perf_slate.validate_tree(blocks)
            except perf_slate.SlateError as exc:
                raise JoySpaceError(f"JoySpace 模板结构不受支持：{exc}") from None
        except JoySpaceError as exc:
            # Never expose which template answered; only try the next fallback.
            failure = exc
    raise JoySpaceError(str(failure) if failure else "JoySpace 模板内容为空或格式无效。")


def report_snapshot(file_path):
    """Read and validate one image-free draft block tree without remote I/O."""
    try:
        content = Path(file_path).read_bytes()
        blocks = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise JoySpaceError("无法读取报告，请重新生成并预览。") from None
    try:
        # The allow-list refuses images, files and embeds along with anything
        # else the template never produces.
        perf_slate.validate_tree(blocks)
    except perf_slate.SlateError as exc:
        raise JoySpaceError(f"报告内容不受支持，暂不支持发布：{exc}"
                            "请检查完整正文后重新生成并预览。") from None
    return blocks, hashlib.sha256(content).hexdigest()


def report_sha256(file_path):
    return report_snapshot(file_path)[1]


def find_published(title, *, limit=100):
    """Look up recent documents by exact title; read-only, never writes remotely.

    Returns every markdown page whose title matches, so the caller can tell a
    single candidate apart from duplicates instead of guessing.
    """
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or "\x00" in title:
        raise JoySpaceError("文档标题不能为空且不能超过 200 个字符。")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise JoySpaceError("查询条数必须是 1 到 200 之间的整数。")
    wanted = title.strip()
    node, skill = _check_runtime()
    pages = _invoke(node, [str(skill / LIST_SCRIPT), "--json", "--limit", str(limit),
                           "--grep", wanted], skill, timeout=90, shape=list)
    found = []
    for page in pages:
        if not isinstance(page, dict) or page.get("page_type") != MARKDOWN_PAGE:
            continue
        name, link = page.get("title"), page.get("link")
        if not isinstance(name, str) or name.strip() != wanted:
            continue
        try:
            page_id = _page_id(link)
        except JoySpaceError:
            continue
        # The link must agree with the record's own identifier; never trust one alone.
        if page_id != page.get("id"):
            continue
        created = page.get("created_at")
        found.append({
            "pageId": page_id,
            "link": "https://joyspace.jd.com/pages/" + page_id,
            "createdAt": created if isinstance(created, str) and TIMESTAMP.fullmatch(created) else None,
        })
    return found


def _recover(title):
    """Never republish: only report what a read-only lookup can prove."""
    try:
        found = find_published(title)
    except JoySpaceError:
        raise JoySpaceError(PUBLISH_UNKNOWN) from None
    if not found:
        raise JoySpaceError(PUBLISH_UNKNOWN + "已查询最近文档，未发现同名页面。")
    if len(found) > 1:
        links = "、".join(item["link"] for item in found)
        raise JoySpaceError(PUBLISH_UNKNOWN +
                            f"已查询最近文档，发现 {len(found)} 个同名页面：{links}。")
    return dict(found[0], verified=False, message=PUBLISH_RECOVERED)


def publish_slate(file_path, title, placement="personal", parent_page=None, *, expected_sha256=None):
    """Upload the draft block tree as-is, so the page matches the template."""
    if placement not in ("personal", "child", "sibling"):
        raise JoySpaceError("发布位置必须是 personal、child 或 sibling。")
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or "\x00" in title:
        raise JoySpaceError("文档标题不能为空且不能超过 200 个字符。")
    try:
        path = Path(file_path).resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != ".json":
            raise ValueError("invalid report file")
    except (OSError, ValueError, TypeError):
        raise JoySpaceError("请选择存在的报告文件。") from None
    target = None
    if placement != "personal":
        target = _page_id(parent_page)
    elif parent_page:
        raise JoySpaceError("个人空间发布不接受目标页面。")
    blocks, source_sha256 = report_snapshot(path)
    if expected_sha256 is not None and source_sha256 != expected_sha256:
        raise JoySpaceError("报告内容已变化，请重新生成并预览后再发布。")
    node, skill = _check_runtime()
    args = [str(skill / SLATE_SCRIPT), "--file", str(path), "--title", title.strip(),
            "--image-free", "--source-sha256", source_sha256]
    if placement == "child":
        args.extend(["--parent-page-id", target])
    elif placement == "sibling":
        args.extend(["--page-url", "https://joyspace.jd.com/pages/" + target])
    try:
        data = _invoke(node, args, skill, timeout=180, publish=True)
        page_id = data.get("pageId")
        if (data.get("verified") is not True or data.get("imageFree") is not True
                or data.get("title") != title.strip() or data.get("sourceSha256") != source_sha256
                or not isinstance(page_id, str) or not PAGE_ID.fullmatch(page_id)):
            raise JoySpaceError(PUBLISH_UNKNOWN)
    except JoySpaceError:
        # The document may exist despite a lost confirmation; look it up instead of
        # republishing, and mark the link as not content-verified.
        return _recover(title.strip())
    # Do not forward arbitrary links, authentication metadata or local paths.
    return {"pageId": page_id, "link": "https://joyspace.jd.com/pages/" + page_id, "verified": True}