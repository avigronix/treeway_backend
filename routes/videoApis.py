"""YFI daily videos (Phase B).

Rules (SRS 4.1 + Vinay, 14 Oct 2026):
- 4 videos a day (setting video.per_day), each watched for 30 seconds (video.seconds), checked by the server.
- Videos go in order; starting a video again (app closed) restarts its timer.
- The moment the last video is done, ₹20 (video.reward) goes to the Product Wallet in the same request,
  once per day (ref yfi:{user}:{date}), no 10% cut.
- Only active IDs, from the activation date (Day 1) to Day 150 (video.days). A missed day is lost.
- All 4 videos are needed for the ₹20. If the admin did not set all links for a day, the links of the last
  day that had all of them (normally yesterday) are used automatically; the admin warning stays.
- The day is the IST date. A video started before midnight must be started again after midnight.
"""

import math

from base import *
from models import responses as R
from mlmEngine import wallet_move
from models.model import DayVideosModel

videoApi = APIRouter()

_videos_cache: Dict[date, tuple] = {}
VIDEO_CACHE_SECONDS = 30
PLATFORM_HOSTS = {
    "youtube": ("youtube.com", "youtu.be"),
    "facebook": ("facebook.com", "fb.watch"),
    "instagram": ("instagram.com",),
}


FALLBACK_DAYS = 30


async def day_videos(d: date, per_day: int) -> tuple[List[Dict], Optional[date]]:
    """The links members watch on day `d`, and the date they came from if not `d` itself.
    A day without all links uses the last complete day before it (Vinay, 14 Oct). Cached for 30 seconds,
    because every member opens this right after login."""
    hit = _videos_cache.get(d)
    if hit and time.monotonic() - hit[0] < VIDEO_CACHE_SECONDS:
        return hit[1]
    source = await cpo.fetch_val(
        "SELECT video_date FROM daily_videos WHERE video_date BETWEEN $1 AND $2 AND slot <= $3 "
        "GROUP BY video_date HAVING count(*) >= $3 ORDER BY video_date DESC LIMIT 1",
        d - timedelta(days=FALLBACK_DAYS), d, per_day)
    rows = await cpo.fetch_all("SELECT slot, platform, url, title FROM daily_videos WHERE video_date = $1 AND slot <= $2 "
                               "ORDER BY slot", source, per_day) if source else []
    result = (rows, source if source and source != d else None)
    _videos_cache[d] = (time.monotonic(), result)
    return result


def day_no(activated_at: Optional[datetime], today: date) -> Optional[int]:
    """Activation date (IST) is Day 1."""
    if not activated_at:
        return None
    return (today - activated_at.astimezone(IST).date()).days + 1


def _ready(videos: List[Dict], per_day: int) -> bool:
    return {v["slot"] for v in videos} >= set(range(1, per_day + 1))


async def _member_day(user_id: int) -> Dict:
    """Everything needed to decide what the member can do today."""
    s = (await get_settings())["video"]
    today = today_ist()
    u = await cpo.fetch_one("SELECT status, role, activated_at FROM users WHERE id = $1", user_id)
    n = day_no(u["activated_at"], today)
    videos, from_day = await day_videos(today, s["per_day"])
    return {
        "s": s, "today": today, "day_no": n, "videos": videos, "ready": _ready(videos, s["per_day"]),
        "links_from": from_day,
        "eligible": u["role"] == "USER" and u["status"] == "active" and n is not None and 1 <= n <= s["days"],
    }


async def _check_can_watch(md: Dict):
    if not md["eligible"]:
        if md["day_no"] and md["day_no"] > md["s"]["days"]:
            fail(400, f"Your {md['s']['days']} days of daily videos are complete.")
        fail(400, "Daily videos are not open for your ID.")
    if not md["ready"]:
        fail(400, "Today's videos are not ready yet. The app is open for you today.")  # no complete day in 30 days


# ================================================================ member
@videoApi.get("/videos/today", response_model=R.Envelope[R.VideosToday])
async def videos_today(user=Depends(tokenRequired())):
    """Login window: today's videos and whether the app must stay locked until they are done."""
    md = await _member_day(user["id"])
    s = md["s"]
    row = await cpo.fetch_one(
        "SELECT done_slots, current_slot, current_started, rewarded_at, "
        "extract(epoch FROM now() - current_started) AS elapsed FROM video_days WHERE user_id = $1 AND watch_date = $2",
        user["id"], md["today"]) or {"done_slots": 0, "current_slot": None, "rewarded_at": None, "elapsed": None}
    earned = await cpo.fetch_one(
        "SELECT count(*) AS days, coalesce(sum(amount), 0) AS total FROM wallet_ledger WHERE user_id = $1 AND type = 'yfi_daily'",
        user["id"])

    videos = []
    if md["eligible"]:
        for v in md["videos"]:
            if v["slot"] <= row["done_slots"]:
                status, left = "done", 0
            elif v["slot"] == row["current_slot"]:
                status, left = "running", max(0, math.ceil(s["seconds"] - float(row["elapsed"])))
            else:
                status, left = ("next" if v["slot"] == row["done_slots"] + 1 else "waiting"), s["seconds"]
            videos.append({**v, "status": status, "seconds_left": left})
    all_done = row["rewarded_at"] is not None
    return respond({"msg": "", "status": 200, "data": {
        "date": md["today"], "day_no": md["day_no"], "days_total": s["days"], "eligible": md["eligible"],
        "ready": md["ready"], "links_from": md["links_from"], "locked": md["eligible"] and md["ready"] and not all_done,
        "all_done": all_done, "done_count": row["done_slots"], "per_day": s["per_day"],
        "seconds": s["seconds"], "reward": rupees(s["reward"]), "videos": videos,
        "days_earned": earned["days"], "earned_total": rupees(earned["total"]),
        "max_total": rupees(s["reward"] * s["days"]),
    }})


@videoApi.post("/videos/{slot}/start", response_model=R.Envelope[R.VideoStarted])
async def start_video(slot: int, user=Depends(tokenRequired(activeOnly=True))):
    """Starts (or restarts) the timer of the next video."""
    md = await _member_day(user["id"])
    await _check_can_watch(md)
    async with cpo.transaction() as conn:
        await cpo.execute("INSERT INTO video_days (user_id, watch_date) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                          user["id"], md["today"], conn=conn)
        row = await cpo.fetch_one("SELECT * FROM video_days WHERE user_id = $1 AND watch_date = $2 FOR UPDATE",
                                  user["id"], md["today"], conn=conn)
        if row["rewarded_at"]:
            fail(409, "Today's videos are done.")
        if slot <= row["done_slots"]:
            fail(409, f"Video {slot} is already watched.")
        if slot != row["done_slots"] + 1:
            fail(400, f"Watch video {row['done_slots'] + 1} first.")
        started = await cpo.fetch_val(
            "UPDATE video_days SET current_slot = $3, current_started = now(), "
            "log = jsonb_set(log, ARRAY[$4], jsonb_build_object('started', now(), "
            "'starts', coalesce((log->$4->>'starts')::int, 0) + 1)) "
            "WHERE user_id = $1 AND watch_date = $2 RETURNING current_started",
            user["id"], md["today"], slot, str(slot), conn=conn)
    video = next(v for v in md["videos"] if v["slot"] == slot)
    return respond({"msg": "", "status": 200, "data": {**video, "started_at": started, "seconds": md["s"]["seconds"]}})


@videoApi.post("/videos/{slot}/complete", response_model=R.Envelope[R.VideoDone])
async def complete_video(slot: int, user=Depends(tokenRequired(activeOnly=True))):
    """Marks the running video as watched if 30 seconds passed (server time). The last one pays ₹20 right away."""
    md = await _member_day(user["id"])
    await _check_can_watch(md)
    s = md["s"]
    credited = 0
    async with cpo.transaction() as conn:
        row = await cpo.fetch_one(
            "SELECT *, extract(epoch FROM now() - current_started) AS elapsed FROM video_days "
            "WHERE user_id = $1 AND watch_date = $2 FOR UPDATE", user["id"], md["today"], conn=conn)
        if not row or row["current_slot"] != slot or row["current_started"] is None:
            if row and slot <= row["done_slots"]:
                fail(409, f"Video {slot} is already watched.")
            fail(400, "Start the video first.")
        left = math.ceil(s["seconds"] - float(row["elapsed"]))
        if left > 0:
            fail(400, f"Watch for {s['seconds']} seconds. {left} seconds left.")
        done = await cpo.fetch_val(
            "UPDATE video_days SET done_slots = done_slots + 1, current_slot = NULL, current_started = NULL, "
            "log = jsonb_set(log, ARRAY[$3], (log->$3) || jsonb_build_object('done', now())) "
            "WHERE user_id = $1 AND watch_date = $2 RETURNING done_slots", user["id"], md["today"], str(slot), conn=conn)
        if done >= s["per_day"]:
            await wallet_move(conn, user["id"], "product", s["reward"], "yfi_daily", f"yfi:{user['id']}:{md['today']}",
                              {"date": str(md["today"]), "day_no": md["day_no"]})
            await cpo.execute("UPDATE video_days SET rewarded_at = now() WHERE user_id = $1 AND watch_date = $2",
                              user["id"], md["today"], conn=conn)
            credited = s["reward"]
    return respond({"msg": f"All done! ₹{rupees(credited):g} added to your Product Wallet." if credited else "Video done.",
                    "status": 200, "data": {"done_count": done, "all_done": bool(credited), "credited": rupees(credited),
                                            "day_no": md["day_no"]}})


@videoApi.get("/videos/history", response_model=R.Envelope[R.VideoHistory])
async def videos_history(days: int = Query(14, ge=1, le=150), user=Depends(tokenRequired())):
    """'Last 14 days: 12 done, 2 missed' (only days since activation)."""
    md = await _member_day(user["id"])
    if md["day_no"] is None:
        return respond({"msg": "", "status": 200, "data": {"days": [], "done": 0, "missed": 0}})
    first = md["today"] - timedelta(days=min(days, md["day_no"], md["s"]["days"]) - 1)
    done = {r["d"] for r in await cpo.fetch_all(
        "SELECT (meta->>'date')::date AS d FROM wallet_ledger WHERE user_id = $1 AND type = 'yfi_daily' "
        "AND created_at >= $2", user["id"], datetime.combine(first, datetime.min.time(), IST))}
    out = [{"date": first + timedelta(days=i), "done": first + timedelta(days=i) in done}
           for i in range((md["today"] - first).days + 1)]
    # Today is not "missed" yet.
    missed = sum(1 for d in out if not d["done"] and d["date"] != md["today"])
    return respond({"msg": "", "status": 200, "data": {"days": out, "done": sum(d["done"] for d in out), "missed": missed}})


# ================================================================ admin
async def missing_video_days(n: int = 7) -> List[Dict]:
    """Today and the next days that do not have all links yet (admin warning)."""
    per_day = (await get_settings())["video"]["per_day"]
    today = today_ist()
    rows = await cpo.fetch_all(
        "SELECT d::date AS date, (SELECT count(*) FROM daily_videos v WHERE v.video_date = d::date AND v.slot <= $3) AS set "
        "FROM generate_series($1::date, $2::date, interval '1 day') AS d", today, today + timedelta(days=n - 1), per_day)
    return [{"date": r["date"], "missing": per_day - r["set"]} for r in rows if r["set"] < per_day]


@videoApi.get("/admin/videos", response_model=R.Envelope[R.AdminVideos])
async def admin_videos(day: Optional[date] = None, admin=Depends(tokenRequired(["ADMIN"]))):
    day = day or today_ist() + timedelta(days=1)
    rows = await cpo.fetch_all(
        "SELECT v.slot, v.platform, v.url, v.title, v.updated_at, u.full_name AS updated_by_name "
        "FROM daily_videos v LEFT JOIN users u ON u.id = v.updated_by WHERE v.video_date = $1 ORDER BY v.slot", day)
    return respond({"msg": "", "status": 200, "data": {
        "date": day, "videos": rows, "per_day": (await get_settings())["video"]["per_day"],
        "missing_days": await missing_video_days()}})


def _check_link(platform: str, url: str):
    from urllib.parse import urlparse

    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not any(host == h or host.endswith("." + h) for h in PLATFORM_HOSTS[platform]):
        fail(400, f"Use an https {platform.title()} link for this video.")


@videoApi.put("/admin/videos/{day}", response_model=R.Envelope[R.MissingDays])
async def set_day_videos(day: date, body: DayVideosModel, admin=Depends(tokenRequired(["ADMIN"]))):
    """Sets (or replaces) the given slots for a day. Past days cannot be changed."""
    if day < today_ist():
        fail(400, "Past days cannot be changed.")
    per_day = (await get_settings())["video"]["per_day"]
    slots = [v.slot for v in body.videos]
    if len(set(slots)) != len(slots) or max(slots) > per_day:
        fail(400, f"Use slots 1 to {per_day}, each once.")
    for v in body.videos:
        _check_link(v.platform, v.url)
    async with cpo.transaction() as conn:
        old = await cpo.fetch_all("SELECT slot, platform, url, title FROM daily_videos WHERE video_date = $1", day, conn=conn)
        for v in body.videos:
            await cpo.execute(
                "INSERT INTO daily_videos (video_date, slot, platform, url, title, updated_by) VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (video_date, slot) DO UPDATE SET platform = EXCLUDED.platform, url = EXCLUDED.url, "
                "title = EXCLUDED.title, updated_by = EXCLUDED.updated_by, updated_at = now()",
                day, v.slot, v.platform, v.url, v.title, admin["id"], conn=conn)
        await audit(conn, admin["id"], "videos_set", str(day),
                    {"old": old, "new": [v.model_dump() for v in body.videos]})
    _videos_cache.clear()  # a fix today can change what later days fall back to
    return respond({"msg": "Videos saved.", "status": 200, "data": {"missing_days": await missing_video_days()}})
