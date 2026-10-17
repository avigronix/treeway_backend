import math

from base import *
from models import responses as R
from mlmEngine import board_entry, child_positions, parent_position, super3_member_net

matrixApi = APIRouter()


async def _speed(board: int) -> float:
    """New spots a day on this board (last 7 days)."""
    n = await cpo.fetch_val("SELECT count(*) FROM super3_nodes WHERE board = $1 AND created_at > now() - interval '7 days'",
                            board)
    return round(n / 7, 1)


def _board_net(s3: Dict, cut: int, board: int) -> int:
    e = board_entry(s3, board)
    part = e * 3 if board == s3["boards"] else e
    other = (s3["recycle"] if board >= 2 else 0) + (s3["s3_level_cut"] if board >= s3["s3_level_from_board"] else 0)
    return part - percent_of(part, cut) - other


@matrixApi.get("/super3/status", response_model=R.Envelope[R.Super3Status])
async def super3_status(user=Depends(tokenRequired())):
    """'When will my 3 come?' for every running journey of the member."""
    return respond({"msg": "", "status": 200, "data": await super3_status_data(user["id"])})


async def super3_status_data(uid: int) -> Dict:
    """Also used by the admin's member page (Super 3 tab)."""
    s = await get_settings()
    s3, cut = s["super3"], sum(s["cut_percent"].values())
    nodes = await cpo.fetch_all(
        "SELECT j.id AS journey_id, j.kind, j.current_board, j.finished, n.position, n.children "
        "FROM super3_journeys j LEFT JOIN super3_nodes n ON n.journey_id = j.id AND n.board = j.current_board "
        "WHERE j.user_id = $1 ORDER BY j.id", uid)
    counters = {r["board"]: r["next_position"] for r in await cpo.fetch_all("SELECT * FROM super3_counters")}
    speeds: Dict[int, float] = {}
    spots_of: Dict[int, List[Dict]] = {}
    for r in await cpo.fetch_all(
            "SELECT n.journey_id, n.board, n.position FROM super3_nodes n JOIN super3_journeys j ON j.id = n.journey_id "
            "WHERE j.user_id = $1 ORDER BY n.board", uid):
        spots_of.setdefault(r["journey_id"], []).append({"board": r["board"], "position": r["position"]})

    journeys = []
    for n in nodes:
        item = {"journey_id": n["journey_id"], "kind": n["kind"], "board": n["current_board"], "finished": n["finished"],
                "position": n["position"], "children": n["children"], "positions": spots_of.get(n["journey_id"], [])}
        if not n["finished"] and n["position"]:
            board = n["current_board"]
            line_at = counters[board] - 1
            spots = [{"spot": p, "to_go": max(0, p - line_at)} for p in child_positions(n["position"])]
            if board not in speeds:
                speeds[board] = await _speed(board)
            left = max(sp["to_go"] for sp in spots)
            item.update({
                "line_at": line_at, "spots": spots, "speed_per_day": speeds[board],
                "eta_days": math.ceil(left / speeds[board]) if left and speeds[board] else (0 if not left else None),
                "you_get_when_full": rupees(_board_net(s3, cut, board)),
            })
        journeys.append(item)

    held = await cpo.fetch_val("SELECT coalesce(sum(amount), 0) FROM company_ledger WHERE type = 's3_hold' AND user_id = $1",
                               uid)
    me = await cpo.fetch_one("SELECT direct_count, s3_shares FROM users WHERE id = $1", uid)
    return {
        "direct_count": me["direct_count"],
        # in_super3 comes from real deposited shares, not the direct count (a share lapses while blocked).
        "in_super3": me["s3_shares"] == 2, "s3_shares": me["s3_shares"],
        "on_hold": rupees(held),
        "journeys": journeys,
        "boards": [{"board": b, "entry": rupees(board_entry(s3, b)), "you_get": rupees(_board_net(s3, cut, b))}
                   for b in range(1, s3["boards"] + 1)],
        "journey_total": rupees(super3_member_net(s3, cut)),
        "recycle_ids": sum(1 for j in journeys if j["kind"] == "recycle"),
    }


@matrixApi.get("/super3/tree", response_model=R.Envelope[R.Super3Tree])
async def super3_tree(board: int = Query(..., ge=1, le=8), position: int = Query(..., ge=1),
                      user=Depends(tokenRequired())):
    """A spot with 2 rows below it. Members can open only trees under their own spots."""
    if user["role"] != "ADMIN":
        allowed = await cpo.fetch_val(
            "WITH RECURSIVE up AS (SELECT $2::bigint AS pos UNION ALL SELECT (pos + 1) / 3 FROM up WHERE pos > 1) "
            "SELECT EXISTS (SELECT 1 FROM super3_nodes WHERE board = $1 AND user_id = $3 AND position IN (SELECT pos FROM up))",
            board, position, user["id"])
        if not allowed:
            fail(403, "You can see only your own tree.")

    row1 = child_positions(position)
    row2 = [c for p in row1 for c in child_positions(p)]
    parent = parent_position(position) if position > 1 else None
    rows = await cpo.fetch_all(
        "SELECT n.position, n.children, n.status, n.created_at, u.user_code, split_part(u.full_name, ' ', 1) AS name, "
        "split_part(s.full_name, ' ', 1) AS sponsor_name "
        "FROM super3_nodes n LEFT JOIN users u ON u.id = n.user_id LEFT JOIN users s ON s.id = u.sponsor_id "
        "WHERE n.board = $1 AND n.position = ANY($2::bigint[])",
        board, [position] + row1 + row2 + ([parent] if parent else []))
    by_pos = {r["position"]: r for r in rows}

    def spot(p):
        r = by_pos.get(p)
        if not r:
            return {"position": p, "empty": True}
        return {**r, "user_code": r["user_code"] or "COMPANY", "name": r["name"] or "Treeway", "empty": False}

    return respond({"msg": "", "status": 200, "data": {
        "board": board, "root": spot(position), "above": spot(parent) if parent else None,
        "children": [{**spot(p), "children": [spot(c) for c in child_positions(p)]} for p in row1],
    }})


@matrixApi.get("/admin/super3/stats", response_model=R.Envelope[R.Super3Stats])
async def super3_stats(user=Depends(tokenRequired(["ADMIN"]))):
    """Reads every board and the company ledger: cached for 60 seconds."""
    data, _ = await cached("admin_super3_stats", 60, _super3_stats)
    return respond({"msg": "", "status": 200, "data": data})


async def _super3_stats() -> Dict:
    rows = await cpo.fetch_all(
        "SELECT c.board, c.next_position - 1 AS last_spot, "
        "(SELECT count(*) FROM super3_nodes n WHERE n.board = c.board AND n.status = 'full') AS full, "
        "(SELECT count(*) FROM super3_nodes n WHERE n.board = c.board AND n.created_at > now() - interval '1 day') AS today "
        "FROM super3_counters c ORDER BY c.board")
    for r in rows:
        r["filling"] = r["last_spot"] - r["full"]
        r["speed_7d"] = await _speed(r["board"])
        r["next_spot"] = r["last_spot"] + 1
    totals = await cpo.fetch_one(
        "SELECT count(*) FILTER (WHERE kind = 'main') AS main, count(*) FILTER (WHERE kind = 'recycle') AS recycle, "
        "count(*) FILTER (WHERE finished) AS finished FROM super3_journeys")
    pool = await cpo.fetch_one(
        "SELECT coalesce(sum(amount) FILTER (WHERE type = 's3_hold'), 0) AS on_hold, "
        "coalesce(sum(amount) FILTER (WHERE type = 's3_level_pool'), 0) AS s3_level_pool, "
        "coalesce(sum(amount) FILTER (WHERE type = 's3_company'), 0) AS company_spots FROM company_ledger")
    extra = await cpo.fetch_one(
        "SELECT (SELECT coalesce(sum(gross), 0)::bigint FROM wallet_ledger WHERE type = 'super3_income') AS wallet_part_paid, "
        "(SELECT coalesce(sum(amount), 0)::bigint FROM company_ledger WHERE type = 'self_help') AS self_help, "
        "(SELECT count(*) FROM users WHERE s3_shares = 1 AND role = 'USER') AS waiting_second_share, "
        "(SELECT count(*) FROM super3_nodes WHERE user_id IS NOT NULL) AS ids_on_boards")
    return {"boards": rows, "journeys": totals,
            "money": {**{k: rupees(v) for k, v in pool.items()}, "wallet_part_paid": rupees(extra["wallet_part_paid"]),
                      "self_help": rupees(extra["self_help"])},
            "waiting_second_share": extra["waiting_second_share"], "ids_on_boards": extra["ids_on_boards"]}
