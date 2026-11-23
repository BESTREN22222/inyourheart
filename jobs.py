"""Background pulls, scheduled polling and ntfy notifications."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import store
import weibo_api

JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
TZ = timezone.utc
CST = timezone(timedelta(hours=8))


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=CST)
    except ValueError:
        return None


def _job_update(job_id: str, **fields: Any) -> None:
    with _LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def _fetch(account: dict[str, Any], source: str, params: dict[str, Any]) -> dict[str, Any]:
    if account["mode"] == "cli":
        return weibo_api.cli_fetch(source, params)
    return weibo_api.fetch(source, account.get("token", ""), params)


def start_pull(account_id: int, source: str, params: dict[str, Any], pages: int = 1) -> str:
    job_id = uuid.uuid4().hex[:12]
    conn = store.connect()
    store.init_db(conn)
    try:
        query_id = store.add_query(conn, account_id, source, params, max(1, pages))
    finally:
        conn.close()
    with _LOCK:
        JOBS[job_id] = {
            "id": job_id, "account_id": account_id, "source": source,
            "status": "running", "pages": 0, "fetched": 0, "saved": 0,
            "error": None, "started_at": store.now(), "query_id": query_id,
        }
    threading.Thread(
        target=_run_pull, args=(job_id, account_id, source, params, max(1, pages), query_id),
        daemon=True, name=f"pull-{job_id}",
    ).start()
    return job_id


def _run_pull(job_id: str, account_id: int, source: str, params: dict[str, Any],
              pages: int, query_id: int) -> None:
    conn = store.connect()
    store.init_db(conn)
    try:
        account = store.get_account(conn, account_id)
        if not account:
            raise RuntimeError("账号不存在")

        mode = str(params.pop("mode", "latest"))
        if mode == "history":
            bounds = store.comment_bounds(conn, account_id, source)
            if bounds["oldest"]:
                params["max_id"] = bounds["oldest"] - 1
            else:
                mode = "latest"
        elif mode == "newer":
            bounds = store.comment_bounds(conn, account_id, source)
            if bounds["newest"]:
                params["since_id"] = bounds["newest"]
            else:
                mode = "latest"

        if mode == "range":
            start_raw = str(params.pop("start_date", "")).strip()
            end_raw = str(params.pop("end_date", "")).strip()
            if not start_raw or not end_raw:
                raise RuntimeError("指定历史时间需要开始和结束日期")
            start = datetime.fromisoformat(start_raw).replace(tzinfo=CST)
            end = datetime.fromisoformat(end_raw).replace(tzinfo=CST) + timedelta(days=1) - timedelta(seconds=1)
            if start > end:
                raise RuntimeError("开始日期不能晚于结束日期")
            for page in range(1, pages + 1):
                page_params = dict(params)
                page_params["page"] = page
                result = _fetch(account, source, page_params)
                raw_items = result.get("items") or []
                if not raw_items:
                    break
                keep = []
                for item in raw_items:
                    flat = weibo_api.flatten(item)
                    moment = _parse_time(flat.get("time", ""))
                    if moment and start <= moment <= end:
                        keep.append(item)
                saved = store.save_comments(conn, account_id, source, keep) if keep else 0
                _job_update(
                    job_id, pages=page,
                    fetched=JOBS[job_id]["fetched"] + len(raw_items),
                    saved=JOBS[job_id]["saved"] + saved,
                )
                oldest_raw = min(
                    (_parse_time(weibo_api.flatten(i).get("time", "")) for i in raw_items),
                    default=None,
                )
                if oldest_raw and oldest_raw < start:
                    break
                if len(raw_items) < int(page_params.get("count", 50)):
                    break
        else:
            for page in range(1, pages + 1):
                page_params = dict(params)
                page_params["page"] = page
                result = _fetch(account, source, page_params)
                items = result.get("items") or []
                if not items:
                    break
                saved = store.save_comments(conn, account_id, source, items)
                _job_update(
                    job_id, pages=page,
                    fetched=JOBS[job_id]["fetched"] + len(items),
                    saved=JOBS[job_id]["saved"] + saved,
                )
                _job_update(job_id, oldest=(weibo_api.flatten(items[-1]).get("time") if items else None))
                if len(items) < int(page_params.get("count", 50)):
                    break
        _job_update(job_id, status="done", finished_at=store.now())
    except Exception as exc:  # noqa: BLE001
        _job_update(job_id, status="error", error=str(exc)[:300], finished_at=store.now())
    finally:
        job = get_job(job_id) or {}
        if query_id:
            try:
                store.finish_query(
                    conn, query_id, str(job.get("status") or "error"),
                    int(job.get("pages") or 0), int(job.get("fetched") or 0),
                    int(job.get("saved") or 0), str(job.get("error") or ""),
                )
            except Exception:  # noqa: BLE001
                pass
        conn.close()


# ---- ntfy ------------------------------------------------------------------

def send_ntfy(title: str, message: str) -> tuple[bool, str]:
    conn = store.connect()
    store.init_db(conn)
    config = store.get_setting(conn, "ntfy", {}) or {}
    conn.close()
    if not config.get("enabled") or not config.get("topic"):
        return False, "未启用或未设置 topic"
    payload = {
        "topic": config["topic"],
        "title": title,
        "message": message,
        "priority": 4,
        "tags": ["speech_balloon"],
    }
    request = urllib.request.Request(
        config.get("server", "https://ntfy.sh").rstrip("/"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=15) as response:
            return True, f"HTTP {response.status}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


def notify_new(account: dict[str, Any], source: str, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    lines = []
    for item in items[:5]:
        flat = weibo_api.flatten(item)
        text = (flat["text"] or "").replace("\n", " ").strip()
        if len(text) > 40:
            text = text[:40] + "…"
        lines.append(f"{flat['author'] or '?'}：{text}")
    if len(items) > 5:
        lines.append(f"…还有 {len(items) - 5} 条")
    source_name = {
        "by_me": "发出评论", "to_me": "收到评论", "show": "微博评论",
        "mentions": "@提及", "user": "用户资料",
        "by_me_other": "其他OTA&小偶像发出的评论", "to_me_other": "其他OTA&小偶像收到的评论",
        "mentions_other": "其他OTA&小偶像被@的评论", "timeline_other": "其他OTA&小偶像发出+收到的评论",
    }.get(source, source)
    send_ntfy(f"{account.get('screen_name') or account.get('name')} · {source_name} +{len(items)}", "\n".join(lines))


# ---- scheduler -------------------------------------------------------------

def _due(schedule: dict[str, Any]) -> bool:
    if not schedule.get("enabled"):
        return False
    last = schedule.get("last_run_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now(TZ) - last_dt >= timedelta(minutes=int(schedule["interval_minutes"]))


def tick() -> None:
    conn = store.connect()
    store.init_db(conn)
    try:
        for schedule in store.list_schedules(conn):
            if not _due(schedule):
                continue
            account = store.get_account(conn, int(schedule["account_id"]))
            if not account:
                continue
            params = json.loads(schedule.get("params") or "{}")
            params["since_id"] = int(schedule.get("last_since_id") or 0)
            params.setdefault("count", 50)
            params.setdefault("uid", account.get("uid"))
            try:
                result = _fetch(account, schedule["source"], params)
                items = result.get("items") or []
                saved = store.save_comments(conn, int(account["id"]), schedule["source"], items)
                max_id = max((int(weibo_api.flatten(i).get("id") or 0) for i in items), default=0)
                store.update_schedule(
                    conn, int(schedule["id"]),
                    last_run_at=store.now(),
                    last_status="ok" if saved >= 0 else "error",
                    last_new=saved,
                    last_since_id=max(max_id, int(schedule.get("last_since_id") or 0)),
                )
                if saved:
                    notify_new(account, schedule["source"], items)
            except Exception as exc:  # noqa: BLE001
                store.update_schedule(
                    conn, int(schedule["id"]), last_run_at=store.now(),
                    last_status=f"error: {str(exc)[:160]}", last_new=0,
                )
    finally:
        conn.close()


def scheduler_loop() -> None:
    while True:
        try:
            tick()
        except Exception:
            pass
        time.sleep(15)
