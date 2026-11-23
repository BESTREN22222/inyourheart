"""Weibo API access: direct access_token or optional weibo-cli."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import paths

API = "https://api.weibo.com/2"
ROOT = paths.BASE


class ApiError(RuntimeError):
    pass


def _get(path: str, params: dict[str, Any], token: str, timeout: int = 30) -> dict[str, Any]:
    query = {k: v for k, v in params.items() if v not in (None, "")}
    query["access_token"] = token
    url = f"{API}/{path}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"User-Agent": "inyourheart/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            data = json.loads(body)
            message = data.get("error") or data.get("error_code") or body
        except ValueError:
            message = body
        raise ApiError(f"HTTP {exc.code}: {message}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ApiError(str(exc)) from exc


def verify(token: str) -> dict[str, Any]:
    data = _get("account/get_uid.json", {}, token)
    uid = data.get("uid")
    if not uid:
        raise ApiError("授权码无效或过期，请重新获取")
    return {
        "uid": str(uid),
        "screen_name": "",
        "followers_count": 0,
    }


def extract_uid(value: str) -> str:
    """Accept 7117031969, https://weibo.com/u/7117031969, or a profile link."""
    text = str(value or "").strip()
    if text.isdigit():
        return text
    for pattern in (r"/u/(\d+)", r"weibo\.com/(\d+)", r"(\d{5,})"):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def resolve_uid(account: dict[str, Any], value: str) -> str:
    uid = extract_uid(value)
    if uid:
        return uid
    name = str(value or "").strip()
    if not name:
        raise ApiError("请填写微博号或主页链接")
    if account.get("mode") != "cli":
        raise ApiError("请填数字微博号，或带数字的主页链接")
    data = _cli(["users", "show_batch/other", "--screen_name", name])
    users = data.get("users") or []
    if not users:
        raise ApiError("没找到这个微博号")
    return str(users[0].get("id"))


# ---- one-click CLI device login -------------------------------------------

_LOGIN_LOCK = threading.Lock()
_LOGIN: dict[str, Any] = {
    "status": "idle", "phase": "idle", "message": "",
    "url": "", "code": "", "output": "", "error": "", "me": None,
}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def login_status() -> dict[str, Any]:
    with _LOGIN_LOCK:
        return dict(_LOGIN)


def _login_set(**fields: Any) -> None:
    with _LOGIN_LOCK:
        _LOGIN.update(fields)


def login_start() -> dict[str, Any]:
    global _LOGIN
    with _LOGIN_LOCK:
        if _LOGIN["status"] == "running":
            return dict(_LOGIN)

    if not cli_available():
        with _LOGIN_LOCK:
            _LOGIN = {
                "status": "error", "phase": "error", "message": "没有安装高级工具",
                "url": "", "code": "", "output": "",
                "error": "没有安装高级工具，请先安装 Node.js 和 weibo-cli", "me": None,
            }
        return login_status()

    with _LOGIN_LOCK:
        _LOGIN = {
            "status": "running", "phase": "checking", "message": "正在检查登录状态…",
            "url": "", "code": "", "output": "", "error": "", "me": None,
        }
    threading.Thread(target=_login_worker, daemon=True, name="cli-login").start()
    return login_status()


def _login_worker() -> None:
    path = cli_path()
    if not path:
        _login_set(status="error", phase="error", message="没有安装高级工具", error="没有安装高级工具")
        return

    try:
        me = cli_me(timeout=15)
        _login_set(status="done", phase="done", message=f"登录成功：{me.get('screen_name') or me.get('uid')}", me=me)
        return
    except Exception:  # noqa: BLE001
        pass

    output: list[str] = []
    _login_set(phase="starting", message="正在启动微博登录…")
    try:
        process = subprocess.Popen(
            ["cmd", "/c", path, "auth", "login", "--device"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None

        def _kill() -> None:
            try:
                process.kill()
            except Exception:
                pass

        watchdog = threading.Timer(180, _kill)
        watchdog.daemon = True
        watchdog.start()

        for line in process.stdout:
            clean = _ANSI.sub("", line).strip()
            if not clean:
                continue
            output.append(clean)
            output = output[-40:]
            url_match = re.search(r"https?://[^\s\"'<>]+", clean)
            code_match = re.search(r"(?:code|验证码|代码)[：:\s]*([A-Za-z0-9-]{4,16})", clean, re.I)
            fields: dict[str, Any] = {"output": "\n".join(output), "phase": "waiting"}
            if url_match:
                fields["url"] = url_match.group(0).rstrip(".,)")
                fields["message"] = "已获取授权链接，请打开浏览器确认"
            elif not _LOGIN.get("url"):
                fields["message"] = "正在获取授权链接…"
            if code_match and not _LOGIN.get("code"):
                fields["code"] = code_match.group(1)
            _login_set(**fields)
        process.wait()
        watchdog.cancel()
    except Exception as exc:  # noqa: BLE001
        _login_set(status="error", phase="error", message="启动登录失败", error=str(exc)[:300],
                   output="\n".join(output[-40:]))
        return

    _login_set(phase="verifying", message="正在确认登录结果…")
    try:
        me = cli_me(timeout=20)
        _login_set(status="done", phase="done",
                   message=f"登录成功：{me.get('screen_name') or me.get('uid')}",
                   me=me, output="\n".join(output[-40:]))
    except Exception as exc:  # noqa: BLE001
        _login_set(status="error", phase="error", message="没有完成登录",
                   error=str(exc)[:300], output="\n".join(output[-40:]))


def fetch(source: str, token: str, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch one page for a source. Returns {items, total?}."""
    common = {
        "count": params.get("count", 50),
        "page": params.get("page", 1),
        "since_id": params.get("since_id", 0),
        "max_id": params.get("max_id", 0),
    }
    if source == "by_me":
        data = _get("comments/by_me.json", {**common, "uid": params.get("uid")}, token)
        return {"items": data.get("comments") or [], "total": data.get("total_number")}
    if source == "to_me":
        data = _get(
            "comments/to_me.json",
            {
                **common,
                "filter_by_author": params.get("filter_by_author", 0),
                "filter_by_source": params.get("filter_by_source", 0),
            },
            token,
        )
        return {"items": data.get("comments") or [], "total": data.get("total_number")}
    if source == "show":
        data = _get(
            "comments/show.json",
            {
                **common,
                "id": params.get("post_id"),
                "filter_by_author": params.get("filter_by_author", 0),
            },
            token,
        )
        return {"items": data.get("comments") or [], "total": data.get("total_number")}
    if source == "mentions":
        data = _get("comments/mentions.json", common, token)
        return {"items": data.get("comments") or [], "total": data.get("total_number")}
    if source == "user":
        uid = params.get("uid")
        if not uid:
            raise ApiError("请填写 UID")
        data = _get("users/show.json", {"uid": uid}, token)
        return {"items": [data] if data.get("id") else [], "total": 1}
    if source in {"by_me_other", "to_me_other", "mentions_other", "timeline_other"}:
        raise ApiError("查其他OTA&小偶像的评论需要高级工具，请绑定高级工具账号")
    raise ApiError(f"未知接口：{source}")


# ---- optional CLI adapter --------------------------------------------------

def cli_path() -> str | None:
    env = os.environ.get("WEIBO_CLI")
    if env and Path(env).exists():
        return env
    bundled = ROOT / "runtime" / "cli" / "wb.cmd"
    if bundled.exists():
        return str(bundled)
    local = ROOT / "tools" / "weibo-cli" / "wb.cmd"
    if local.exists():
        return str(local)
    found = shutil.which("wb") or shutil.which("wb.cmd")
    return found


def cli_available() -> bool:
    return bool(cli_path())


def cli_logout() -> None:
    path = cli_path()
    if not path:
        raise ApiError("没有找到高级工具")
    result = subprocess.run(
        ["cmd", "/c", path, "auth", "logout"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if result.returncode != 0:
        raise ApiError((result.stderr or result.stdout).strip()[:400] or "退出登录失败")


def _cli(args: list[str], timeout: int = 240) -> dict[str, Any]:
    path = cli_path()
    if not path:
        raise ApiError("没有找到高级工具，请先安装并登录")
    result = subprocess.run(
        ["cmd", "/c", path, *args, "--output", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if result.returncode != 0:
        raise ApiError((result.stderr or result.stdout).strip()[:400])
    return json.loads(result.stdout)


def cli_me(timeout: int = 30) -> dict[str, Any]:
    data = _cli(["me"], timeout=timeout)
    user = data.get("user") or {}
    return {
        "uid": str(user.get("id") or ""),
        "screen_name": user.get("username") or user.get("screen_name") or "",
        "balance": user.get("balance"),
    }


def cli_fetch(source: str, params: dict[str, Any]) -> dict[str, Any]:
    uid = params.get("uid")
    count = params.get("count", 50)
    page = params.get("page", 1)
    since_id = params.get("since_id", 0)
    max_id = params.get("max_id", 0)
    common = ["--count", str(count), "--page", str(page)]
    if since_id:
        common += ["--since_id", str(since_id)]
    if max_id:
        common += ["--max_id", str(max_id)]

    if source == "by_me":
        data = _cli(["comments", "by_me/other", "--uid", str(uid), *common])
        return {"items": data.get("comments") or [], "total": data.get("total_number")}
    if source == "by_me_other":
        data = _cli(["comments", "by_me/other", "--uid", str(uid), *common])
        return {"items": data.get("comments") or [], "total": data.get("total_number")}
    if source == "to_me":
        data = _cli([
            "comments", "to_me/other", "--uid", str(uid), *common,
            "--filter_by_author", str(params.get("filter_by_author", 0)),
        ])
        return {"items": data.get("comments") or [], "total": data.get("total_number")}
    if source == "to_me_other":
        data = _cli([
            "comments", "to_me/other", "--uid", str(uid), *common,
            "--filter_by_author", str(params.get("filter_by_author", 0)),
        ])
        return {"items": data.get("comments") or [], "total": data.get("total_number")}
    if source == "mentions_other":
        data = _cli(["comments", "mentions/other", "--uid", str(uid), *common])
        return {"items": data.get("comments") or [], "total": data.get("total_number")}
    if source == "timeline_other":
        data = _cli(["comments", "timeline/other", "--uid", str(uid), *common])
        return {"items": data.get("comments") or [], "total": data.get("total_number")}
    if source == "show":
        data = _cli(["comments", "show/all", "--id", str(params.get("post_id")), *common])
        return {"items": data.get("comments") or [], "total": data.get("total_number")}
    if source == "mentions":
        data = _cli(["comments", "mentions/other", "--uid", str(uid), *common])
        return {"items": data.get("comments") or [], "total": data.get("total_number")}
    if source == "user":
        data = _cli(["users", "show_batch/other", "--uids", str(uid)])
        return {"items": data.get("users") or [], "total": data.get("total_number")}
    raise ApiError(f"CLI 模式不支持：{source}")


def flatten(item: dict[str, Any]) -> dict[str, Any]:
    user = item.get("user") or {}
    status = item.get("status") or {}
    status_user = status.get("user") or {}
    reply = item.get("reply_comment") or {}
    reply_user = reply.get("user") or {}
    return {
        "id": item.get("id") or item.get("idstr"),
        "time": item.get("created_at", ""),
        "text": item.get("text", ""),
        "author": user.get("screen_name", ""),
        "author_uid": user.get("id", ""),
        "target": reply_user.get("screen_name") or status_user.get("screen_name", ""),
        "post_id": status.get("id", ""),
        "post_text": status.get("text", ""),
        "raw": item,
    }
