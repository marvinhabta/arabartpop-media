#!/usr/bin/env python3
"""Arab ARTPOP free scheduler.

Runs on GitHub Actions every 15 minutes. Publishes every post in
scheduler/schedule.json whose time has come and that isn't yet recorded
in scheduler/posted.json.

Networks:
  facebook  -> Facebook Page Reel (Meta Graph API, Reels Publishing API)

Secrets (GitHub repo -> Settings -> Secrets and variables -> Actions):
  FB_PAGE_TOKEN   long-lived Facebook Page access token
"""
import json, os, sys, time, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
import urllib.request, urllib.parse, urllib.error

ROOT = Path(__file__).resolve().parent.parent
SCHED = ROOT / "scheduler" / "schedule.json"
POSTED = ROOT / "scheduler" / "posted.json"
GRAPH = "https://graph.facebook.com/v21.0"
DRY = "--dry-run" in sys.argv
LATE_LIMIT_H = 24  # skip posts more than this many hours overdue


def http(method, url, data=None, headers=None, body=None):
    headers = dict(headers or {})
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url.split('?')[0]} -> {e.code}: {e.read().decode()[:500]}")


def multipart(fields, file_field, file_path):
    boundary = "----arabartpop" + str(int(time.time() * 1000))
    out = b""
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{Path(file_path).name}\"\r\nContent-Type: image/jpeg\r\n\r\n").encode()
    out += Path(file_path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def post_facebook_reel(p, page_id, token):
    video = ROOT / p["video"]
    # 1. start session
    s = http("POST", f"{GRAPH}/{page_id}/video_reels",
             data={"upload_phase": "start", "access_token": token})
    vid = s["video_id"]
    # 2. upload bytes from the repo checkout
    size = video.stat().st_size
    http("POST", f"https://rupload.facebook.com/video-upload/v21.0/{vid}",
         headers={"Authorization": f"OAuth {token}", "offset": "0",
                  "file_size": str(size), "Content-Type": "application/octet-stream"},
         body=video.read_bytes())
    # 3. cover (best effort)
    if p.get("cover"):
        try:
            body, ctype = multipart({"is_preferred": "true", "access_token": token},
                                    "source", ROOT / p["cover"])
            http("POST", f"{GRAPH}/{vid}/thumbnails", headers={"Content-Type": ctype}, body=body)
        except Exception as e:
            print(f"  cover not set: {e}")
    # 4. publish
    http("POST", f"{GRAPH}/{page_id}/video_reels",
         data={"upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
               "description": p["text"], "access_token": token})
    # 5. wait for status
    # (published at this point: never raise for polling problems, so it can't double-post)
    for _ in range(40):
        try:
            st = http("GET", f"{GRAPH}/{vid}?fields=status&access_token={urllib.parse.quote(token)}")
        except Exception as e:
            print(f"  status check failed (post already sent): {e}")
            return vid
        status = st.get("status", {})
        vs = status.get("video_status")
        if vs in ("ready", "published") or status.get("publishing_phase", {}).get("status") == "complete":
            return vid
        if vs == "error":
            print(f"  Facebook processing error: {status}")
            return vid
        time.sleep(15)
    return vid  # still processing; Facebook will finish publishing on its own


def main():
    sched = json.loads(SCHED.read_text())
    posted = json.loads(POSTED.read_text()) if POSTED.exists() else {}
    tz = ZoneInfo(sched.get("timezone", "America/Edmonton"))
    now = dt.datetime.now(dt.timezone.utc)
    cfg = sched["accounts"]
    changed = False
    failures = 0

    for p in sched["posts"]:
        if p["id"] in posted:
            continue
        when = dt.datetime.fromisoformat(p["when"]).replace(tzinfo=tz)
        if when > now:
            continue
        if (now - when).total_seconds() > LATE_LIMIT_H * 3600:
            print(f"SKIP {p['id']}: more than {LATE_LIMIT_H}h overdue")
            posted[p["id"]] = {"status": "skipped_overdue", "at": now.isoformat()}
            changed = True
            continue
        for f in ("video", "cover"):
            if p.get(f) and not (ROOT / p[f]).exists():
                raise SystemExit(f"Missing file for {p['id']}: {p[f]}")
        print(f"POST {p['id']} ({p['network']}) due {when.isoformat()}")
        if DRY:
            continue
        try:
            if p["network"] == "facebook":
                token = os.environ.get("FB_PAGE_TOKEN")
                if not token:
                    raise RuntimeError("FB_PAGE_TOKEN secret is not set")
                rid = post_facebook_reel(p, cfg["facebook_page_id"], token)
            else:
                raise RuntimeError(f"unsupported network {p['network']}")
            posted[p["id"]] = {"status": "published", "remote_id": rid, "at": now.isoformat()}
            print(f"  done: {rid}")
        except Exception as e:
            failures += 1
            print(f"  FAILED: {e}")
            # don't record: a failure before upload retries next run;
            # mark after 3 tries to avoid duplicates
            tries = posted.get("_tries", {}).get(p["id"], 0) + 1
            posted.setdefault("_tries", {})[p["id"]] = tries
            if tries >= 3:
                posted[p["id"]] = {"status": "failed", "error": str(e)[:300], "at": now.isoformat()}
        changed = True

    if changed and not DRY:
        POSTED.write_text(json.dumps(posted, indent=2, ensure_ascii=False) + "\n")
    if DRY:
        upcoming = [p for p in sched["posts"] if p["id"] not in posted]
        print(f"\n{len(upcoming)} upcoming posts:")
        for p in upcoming:
            print(f"  {p['when']}  {p['network']:9} {p['id']}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
