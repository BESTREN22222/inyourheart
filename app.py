"""Local HTTP server for INYOURHEART (127.0.0.1)."""

from __future__ import annotations

import csv
import io
import json
import mimetypes
import re
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import jobs
import paths
import store
import weibo_api

ROOT = paths.BASE
WEB = paths.WEB
DATA = paths.DATA
CONFIG_PATH = ROOT / "config.json"
TZ = timezone.utc
VERSION = "3"

SOURCES = {
    "by_me", "to_me", "show", "mentions", "user",
    "by_me_other", "to_me_other", "mentions_other", "timeline_other",
}
OTHER_SOURCES = {"by_me_other", "to_me_other", "mentions_other", "timeline_other"}


def load_config() -> dict[str, Any]:
    config = {"port": 8790, "host": "127.0.0.1", "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": ""}}
    for path in (ROOT / "config.example.json", CONFIG_PATH):
        if path.exists():
            try:
                config.update(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                pass
    return config


def write_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def parse_post_id(value: str) -> str:
    value = (value or "").strip()
    match = re.search(r"(?:status/|detail/)?(\d{6,})", value)
    return match.group(1) if match else value


class Handler(BaseHTTPRequestHandler):
    server_version = "INYOURHEART/1.0"
    config: dict[str, Any] = {}

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/state":
                conn = store.connect()
                store.init_db(conn)
                try:
                    accounts = store.list_accounts(conn)
                    schedules = store.list_schedules(conn)
                    ntfy = store.get_setting(conn, "ntfy", self.config.get("ntfy", {}))
                finally:
                    conn.close()
                write_json(self, 200, {
                    "version": VERSION,
                    "accounts": accounts,
                    "schedules": schedules,
                    "ntfy": ntfy,
                    "cli": weibo_api.cli_available(),
                })
                return
            if path == "/api/comments":
                account_id = int((params.get("account_id") or ["0"])[0])
                source = (params.get("source") or [""])[0]
                limit = min(200, max(1, int((params.get("limit") or ["50"])[0])))
                offset = max(0, int((params.get("offset") or ["0"])[0]))
                conn = store.connect()
                store.init_db(conn)
                try:
                    payload = store.list_comments(conn, account_id, source, limit, offset)
                finally:
                    conn.close()
                items = []
                for item in payload["items"]:
                    flat = weibo_api.flatten(item)
                    flat["_source"] = item.get("_source", source)
                    items.append(flat)
                write_json(self, 200, {"total": payload["total"], "items": items})
                return
            if path == "/api/jobs":
                write_json(self, 200, {"items": [jobs.get_job(key) for key in list(jobs.JOBS)]})
                return
            if path.startswith("/api/jobs/"):
                job = jobs.get_job(path.rsplit("/", 1)[-1])
                if not job:
                    write_json(self, 404, {"error": "任务不存在"})
                else:
                    write_json(self, 200, job)
                return
            if path == "/api/export.csv":
                self._export_csv(params)
                return
            if path == "/api/cli/status":
                available = weibo_api.cli_available()
                me = None
                error = None
                if available:
                    try:
                        me = weibo_api.cli_me(timeout=10)
                    except Exception as exc:  # noqa: BLE001
                        error = str(exc)
                write_json(self, 200, {"available": available, "me": me, "error": error})
                return
            if path == "/api/cli/login/status":
                write_json(self, 200, weibo_api.login_status())
                return
            if path == "/api/history":
                limit = int((params.get("limit") or ["50"])[0])
                conn = store.connect()
                store.init_db(conn)
                try:
                    items = store.list_queries(conn, limit)
                finally:
                    conn.close()
                write_json(self, 200, {"items": items})
                return
            self._static(path)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            write_json(self, 500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._json_body()
            if path == "/api/cli/login":
                write_json(self, 200, weibo_api.login_start())
                return
            if path == "/api/cli/logout":
                try:
                    weibo_api.cli_logout()
                except Exception as exc:  # noqa: BLE001
                    raise ValueError(str(exc)) from exc
                write_json(self, 200, {"ok": True})
                return
            if path == "/api/balance":
                conn = store.connect()
                store.init_db(conn)
                try:
                    accounts = store.list_accounts(conn)
                    me = None
                    error = None
                    if any(a.get("mode") == "cli" for a in accounts):
                        try:
                            me = weibo_api.cli_me(timeout=12)
                        except Exception as exc:  # noqa: BLE001
                            error = str(exc)
                    if me:
                        for account in accounts:
                            if account.get("mode") == "cli" and str(account.get("uid")) == str(me.get("uid")):
                                store.set_account_balance(conn, int(account["id"]), str(me.get("balance") or ""))
                    accounts = store.list_accounts(conn)
                finally:
                    conn.close()
                write_json(self, 200, {"accounts": accounts, "error": error})
                return
            if path == "/api/accounts":
                write_json(self, 200, self._add_account(body))
                return
            if path == "/api/pull":
                source = body.get("source")
                if source not in SOURCES:
                    raise ValueError("未知接口")
                params = body.get("params") or {}
                pages = min(20, max(1, int(body.get("pages") or 1)))
                account_id = int(body.get("account_id") or 0)
                conn = store.connect()
                store.init_db(conn)
                try:
                    account = store.get_account(conn, account_id)
                finally:
                    conn.close()
                if not account:
                    raise ValueError("账号不存在，先绑定一下")
                if source in OTHER_SOURCES and account.get("mode") != "cli":
                    raise ValueError("查其他OTA&小偶像的评论需要高级工具账号，请先绑定高级工具")
                if source == "show":
                    params["post_id"] = parse_post_id(str(params.get("post_id") or ""))
                else:
                    if not str(params.get("uid") or "").strip():
                        params["uid"] = account.get("uid") or ""
                    params["uid"] = weibo_api.resolve_uid(account, str(params.get("uid") or ""))
                job_id = jobs.start_pull(account_id, source, params, pages)
                write_json(self, 200, {"job_id": job_id})
                return
            if path == "/api/user/lookup":
                account_id = int(body.get("account_id") or 0)
                uid = str(body.get("uid") or "").strip()
                conn = store.connect()
                store.init_db(conn)
                try:
                    account = store.get_account(conn, account_id)
                finally:
                    conn.close()
                if not account:
                    raise ValueError("账号不存在")
                uid = weibo_api.resolve_uid(account, uid)
                if account["mode"] == "cli":
                    data = weibo_api.cli_fetch("user", {"uid": uid})
                else:
                    data = weibo_api.fetch("user", account.get("token", ""), {"uid": uid})
                write_json(self, 200, {"items": data.get("items") or []})
                return
            if path == "/api/schedules":
                conn = store.connect()
                store.init_db(conn)
                try:
                    account_id = int(body.get("account_id") or 0)
                    account = store.get_account(conn, account_id)
                    if not account:
                        raise ValueError("账号不存在，先绑定一下")
                    source = str(body.get("source") or "by_me")
                    if source in OTHER_SOURCES and account.get("mode") != "cli":
                        raise ValueError("查其他OTA&小偶像的评论需要高级工具账号，请先绑定高级工具")
                    params = body.get("params") or {}
                    if source != "show":
                        if not str(params.get("uid") or "").strip():
                            params["uid"] = account.get("uid") or ""
                        params["uid"] = weibo_api.resolve_uid(account, str(params.get("uid") or ""))
                    else:
                        params["post_id"] = parse_post_id(str(params.get("post_id") or ""))
                    schedule_id = store.add_schedule(
                        conn,
                        account_id,
                        source,
                        params,
                        int(body.get("interval_minutes") or 60),
                    )
                finally:
                    conn.close()
                write_json(self, 200, {"id": schedule_id})
                return
            match = re.match(r"^/api/schedules/(\d+)/toggle$", path)
            if match:
                conn = store.connect()
                store.init_db(conn)
                try:
                    store.set_schedule_enabled(conn, int(match.group(1)), bool(body.get("enabled", True)))
                finally:
                    conn.close()
                write_json(self, 200, {"ok": True})
                return
            if path == "/api/settings/ntfy":
                conn = store.connect()
                store.init_db(conn)
                try:
                    store.set_setting(conn, "ntfy", body)
                finally:
                    conn.close()
                write_json(self, 200, {"ok": True})
                return
            if path == "/api/ntfy/test":
                ok, message = jobs.send_ntfy("INYOURHEART 测试", "如果你看到这条，说明 ntfy 已接通。")
                write_json(self, 200, {"ok": ok, "message": message})
                return
            write_json(self, 404, {"error": "未知接口"})
        except weibo_api.ApiError as exc:
            write_json(self, 400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            write_json(self, 400, {"error": str(exc)})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        match = re.match(r"^/api/accounts/(\d+)$", path)
        if match:
            conn = store.connect()
            store.init_db(conn)
            try:
                store.delete_account(conn, int(match.group(1)))
            finally:
                conn.close()
            write_json(self, 200, {"ok": True})
            return
        match = re.match(r"^/api/schedules/(\d+)$", path)
        if match:
            conn = store.connect()
            store.init_db(conn)
            try:
                store.delete_schedule(conn, int(match.group(1)))
            finally:
                conn.close()
            write_json(self, 200, {"ok": True})
            return
        write_json(self, 404, {"error": "未知接口"})

    # -- helpers -------------------------------------------------------------

    def _add_account(self, body: dict[str, Any]) -> dict[str, Any]:
        mode = str(body.get("mode") or "direct")
        name = str(body.get("name") or "").strip()
        conn = store.connect()
        store.init_db(conn)
        try:
            if mode == "cli":
                if not weibo_api.cli_available():
                    raise ValueError("没有找到高级工具，请先安装并登录")
                try:
                    me = weibo_api.cli_me(timeout=20)
                except weibo_api.ApiError as exc:
                    raise ValueError("高级工具还没登录，请先点「一键登录高级工具」") from exc
                account_id = store.add_account(
                    conn, name or me["screen_name"] or me["uid"], "cli",
                    me["uid"], me["screen_name"], "", str(me.get("balance") or ""),
                )
            else:
                token = str(body.get("token") or "").strip()
                if not token:
                    raise ValueError("请填写微博授权码")
                me = weibo_api.verify(token)
                account_id = store.add_account(
                    conn, name or f"UID {me['uid']}", "direct",
                    me["uid"], me["screen_name"], token,
                )
        finally:
            conn.close()
        return {"id": account_id, "uid": me["uid"], "screen_name": me["screen_name"]}

    def _export_csv(self, params: dict[str, list[str]]) -> None:
        account_id = int((params.get("account_id") or ["0"])[0])
        conn = store.connect()
        store.init_db(conn)
        try:
            all_items: list[dict[str, Any]] = []
            offset = 0
            while True:
                payload = store.list_comments(conn, account_id, "", 500, offset)
                if not payload["items"]:
                    break
                all_items.extend(payload["items"])
                offset += len(payload["items"])
                if offset >= payload["total"]:
                    break
        finally:
            conn.close()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["评论ID", "时间", "评论者", "评论者UID", "被评论者", "内容", "原微博ID", "原微博内容"])
        for item in all_items:
            flat = weibo_api.flatten(item)
            writer.writerow([
                flat["id"], flat["time"], flat["author"], flat["author_uid"],
                flat["target"], flat["text"], flat["post_id"], flat["post_text"],
            ])
        body = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="inyourheart-{account_id}.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        if path in ("", "/"):
            target = WEB / "index.html"
        else:
            target = (WEB / path.lstrip("/")).resolve()
            if not str(target).startswith(str(WEB.resolve())):
                write_json(self, 403, {"error": "forbidden"})
                return
        if not target.exists() or not target.is_file():
            write_json(self, 404, {"error": "not found"})
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)


def _pause() -> None:
    try:
        input("按回车键退出...")
    except Exception:
        pass


def main() -> None:
    try:
        run_server()
    except OSError as exc:
        print()
        print("启动失败：", exc)
        print("常见原因：8790 端口被别的程序占用。")
        print("可以编辑 config.json，把 port 改成 8791 再试。")
        _pause()
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        print()
        print("启动失败：", exc)
        print("请把上面的报错截图反馈。")
        _pause()


def run_server() -> None:
    config = load_config()
    DATA.mkdir(parents=True, exist_ok=True)
    conn = store.connect()
    store.init_db(conn)
    if store.get_setting(conn, "ntfy") is None:
        store.set_setting(conn, "ntfy", config.get("ntfy", {}))
    conn.close()
    threading.Thread(target=jobs.scheduler_loop, daemon=True, name="scheduler").start()
    Handler.config = config
    host = config.get("host", "127.0.0.1")
    port = int(config.get("port", 8790))
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print("=" * 56)
    print(f"OTA Monitor {VERSION} is running")
    print(f"Open in browser: {url}")
    print("不能关掉这个窗口，关掉就没有了喵（但是打开就又有了喵）")
    print("=" * 56)
    print(flush=True)
    if host in ("127.0.0.1", "localhost"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
