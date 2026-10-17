"""Money engine: activation, level income, 10% cut, Super 3 boards.

Rules:
- Every function here gets `conn` and must run inside `cpo.transaction()`.
  If anything fails, the whole activation is rolled back. No half credits.
- Every credit has a fixed `ref` (UNIQUE in the ledger), so the same income
  can never be paid twice even if a request is retried.
"""

import json
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from base import cpo, fail, get_settings, level_amount, percent_of

WALLETS = ("income", "fund", "product")


# ================================================================ ledgers
async def wallet_move(conn, user_id: int, wallet: str, amount: int, type_: str, ref: str,
                      meta: Optional[Dict] = None, visible: bool = True, cuts: Optional[Dict[str, int]] = None,
                      count_as_earned: bool = False) -> int:
    """Adds (amount > 0) or takes (amount < 0) money. Returns the new balance.
    Not enough balance -> CHECK (>= 0) fails -> handled in main.py as a 400."""
    if wallet not in WALLETS:
        raise ValueError(wallet)
    if amount == 0:
        return 0
    cuts = cuts or {}
    earned = amount if count_as_earned and amount > 0 else 0
    balance = await cpo.fetch_val(
        f"""
        WITH w AS (
            UPDATE wallets SET {wallet} = {wallet} + $2,
                   total_earned = total_earned + $3,
                   updated_at = now()
            WHERE user_id = $1
            RETURNING {wallet} AS bal
        )
        INSERT INTO wallet_ledger (user_id, wallet, type, amount, balance_after, gross, tds, service,
                                   single_leg, other_cut, ref, meta, visible_to_user)
        SELECT $1, $4, $5, $2, w.bal, $6, $7, $8, $9, $10, $11, $12, $13 FROM w
        RETURNING balance_after
        """,
        user_id, amount, earned, wallet, type_,
        cuts.get("gross", 0), cuts.get("tds", 0), cuts.get("service", 0),
        cuts.get("single_leg", 0), cuts.get("other_cut", 0), ref, meta or {}, visible,
        conn=conn,
    )
    if balance is None:
        raise RuntimeError(f"Wallet missing for user {user_id}")
    return balance


async def company_add(conn, type_: str, amount: int, ref: str, user_id: Optional[int] = None,
                      meta: Optional[Dict] = None):
    if amount == 0:
        return
    await cpo.insertion("company_ledger", {"type": type_, "amount": amount, "ref": ref, "user_id": user_id,
                                           "meta": meta or {}}, conn=conn, returning=None)


async def can_earn(conn, user_id: Optional[int]) -> bool:
    if not user_id:
        return False
    row = await cpo.fetch_one("SELECT role, status FROM users WHERE id = $1", user_id, conn=conn)
    return bool(row) and row["role"] == "USER" and row["status"] == "active"


async def credit_income(conn, user_id: Optional[int], gross: int, type_: str, ref: str,
                        meta: Optional[Dict] = None, other_cut: int = 0, eligible: Optional[bool] = None,
                        s: Optional[Dict] = None) -> int:
    """Pays income after the 10% cut (2% TDS, 3% service, 5% Single Leg) and any other cut.
    If the person cannot earn (missing / blocked / company), the full amount goes to the company.
    Pass `s` (settings) so one activation never mixes old and new plan numbers."""
    if gross <= 0:
        return 0
    if eligible is None:
        eligible = await can_earn(conn, user_id)
    if not eligible:
        await company_add(conn, f"lapse_{type_}", gross, f"lapse:{ref}", None, {"for_user": user_id, **(meta or {})})
        return 0

    s = s or await get_settings(conn)
    pct = s["cut_percent"]
    tds = percent_of(gross, pct["tds"])
    service = percent_of(gross, pct["service"])
    single_leg = percent_of(gross, pct["single_leg"])
    net = gross - tds - service - single_leg - other_cut
    if net < 0:
        raise ValueError(f"Net income below zero for {ref}")

    await wallet_move(conn, user_id, "income", net, type_, ref, meta,
                      cuts={"gross": gross, "tds": tds, "service": service, "single_leg": single_leg,
                            "other_cut": other_cut},
                      count_as_earned=True)
    await company_add(conn, "tds", tds, f"tds:{ref}", user_id)
    await company_add(conn, "service", service, f"svc:{ref}", user_id)
    if single_leg:
        await cpo.insertion("single_leg_pool", {"earner_id": user_id, "amount": single_leg, "ref": f"sl:{ref}"},
                            conn=conn, returning=None)
    return net


async def bulk_credit_income(conn, type_: str, items: List[Tuple[int, int, str, Dict]], s: Dict) -> Dict[str, int]:
    """credit_income for many people at once (nightly jobs): items = (user_id, gross, ref, meta), one per user.
    Same rules: 10% cut, blocked / not earning -> the full gross to the company. Refs already used are skipped,
    so running it again never pays twice. Wallets are locked in user_id order (no deadlocks with P2P)."""
    if not items:
        return {"paid": 0, "lapsed": 0}
    refs = [r for _, _, r, _ in items]
    done = {r["ref"] for r in await cpo.fetch_all(
        "SELECT ref FROM wallet_ledger WHERE ref = ANY($1::text[]) UNION ALL "
        "SELECT substr(ref, 7) FROM company_ledger WHERE ref = ANY($2::text[])",
        refs, [f"lapse:{r}" for r in refs], conn=conn)}
    items = [i for i in items if i[2] not in done]
    ids = sorted(i[0] for i in items)
    ok = {r["id"] for r in await cpo.fetch_all(
        "SELECT id FROM users WHERE id = ANY($1::bigint[]) AND role = 'USER' AND status = 'active'", ids, conn=conn)}
    await cpo.execute("SELECT 1 FROM wallets WHERE user_id = ANY($1::bigint[]) ORDER BY user_id FOR UPDATE", ids, conn=conn)

    pct = s["cut_percent"]
    pay, lapse = [], []
    for uid, gross, ref, meta in items:
        if uid not in ok:
            lapse.append((uid, gross, ref, meta))
            continue
        tds, svc, sl = (percent_of(gross, pct["tds"]), percent_of(gross, pct["service"]),
                        percent_of(gross, pct["single_leg"]))
        pay.append((uid, gross, tds, svc, sl, gross - tds - svc - sl, ref, meta))

    if pay:
        cols = list(zip(*pay))
        await cpo.execute(
            """
            WITH d AS (SELECT * FROM unnest($1::bigint[], $2::bigint[], $3::bigint[], $4::bigint[], $5::bigint[],
                                            $6::bigint[], $7::text[], $8::text[])
                       AS d(uid, gross, tds, svc, sl, net, ref, meta)),
                 w AS (UPDATE wallets SET income = income + d.net, total_earned = total_earned + d.net, updated_at = now()
                       FROM d WHERE wallets.user_id = d.uid RETURNING wallets.user_id, wallets.income)
            INSERT INTO wallet_ledger (user_id, wallet, type, amount, balance_after, gross, tds, service, single_leg, ref, meta)
            SELECT d.uid, 'income', $9, d.net, w.income, d.gross, d.tds, d.svc, d.sl, d.ref, d.meta::jsonb
            FROM d JOIN w ON w.user_id = d.uid
            """,
            list(cols[0]), list(cols[1]), list(cols[2]), list(cols[3]), list(cols[4]), list(cols[5]), list(cols[6]),
            [json.dumps(m) for m in cols[7]], type_, conn=conn)
        company = [(t, amt, f"{p}:{ref}", uid) for uid, _, tds, svc, _, _, ref, _ in pay
                   for t, amt, p in (("tds", tds, "tds"), ("service", svc, "svc")) if amt]
        pool = [(uid, sl, f"sl:{ref}") for uid, _, _, _, sl, _, ref, _ in pay if sl]
    else:
        company, pool = [], []
    company += [(f"lapse_{type_}", gross, f"lapse:{ref}", None) for uid, gross, ref, meta in lapse]
    if company:
        c = list(zip(*company))
        lapse_meta = {f"lapse:{ref}": json.dumps({"for_user": uid, **meta}) for uid, _, ref, meta in lapse}
        await cpo.execute(
            "INSERT INTO company_ledger (type, amount, ref, user_id, meta) "
            "SELECT t, a, r, u, m::jsonb FROM unnest($1::text[], $2::bigint[], $3::text[], $4::bigint[], $5::text[]) "
            "AS x(t, a, r, u, m)",
            list(c[0]), list(c[1]), list(c[2]), list(c[3]), [lapse_meta.get(r, "{}") for r in c[2]], conn=conn)
    if pool:
        p = list(zip(*pool))
        await cpo.execute("INSERT INTO single_leg_pool (earner_id, amount, ref) "
                          "SELECT * FROM unnest($1::bigint[], $2::bigint[], $3::text[])",
                          list(p[0]), list(p[1]), list(p[2]), conn=conn)
    return {"paid": len(pay), "lapsed": len(lapse), "lapsed_ids": [i[0] for i in lapse]}


# ================================================================ activation
async def activate_user(conn, user_id: int, method: str, source_ref: str, by_user: Optional[int] = None) -> Dict:
    """Turns an inactive ID active and pays everyone. method = 'payment' | 'pin' | 'admin'.
    One activation at a time: it touches many wallets and the Super 3 line, and running
    them one by one removes any chance of deadlocks (still ~20+ activations a second)."""
    await cpo.advisory_lock(conn, "super3")
    user = await cpo.fetch_one("SELECT id, user_code, full_name, sponsor_id, status FROM users WHERE id = $1 FOR UPDATE",
                               user_id, conn=conn)
    if not user:
        fail(404, "Member not found.")
    if user["status"] == "active":
        fail(409, "This ID is already active.")
    if user["status"] != "inactive":
        fail(400, "This ID cannot be activated. Please contact support.")

    s = await get_settings(conn)
    await cpo.execute(
        "UPDATE users SET status = 'active', activated_at = now(), single_leg_no = nextval('single_leg_seq') "
        "WHERE id = $1",
        user_id, conn=conn,
    )
    meta = {"from": user["user_code"], "from_name": user["full_name"], "method": method, "source": source_ref}

    # 1. ₹987 to the Product Wallet (YFI) + company turnover.
    await wallet_move(conn, user_id, "product", s["product_wallet_on_activation"], "activation_product",
                      f"yfi:{user_id}", meta)
    await company_add(conn, "turnover", s["activation_amount"], f"turnover:{user_id}", user_id, meta)

    # 2. Level income to 25 uplines. Missing / blocked levels -> company.
    uplines = await cpo.fetch_all(
        "SELECT u.level, u.ancestor_id, (m.role = 'USER' AND m.status = 'active') AS ok "
        "FROM user_uplines u JOIN users m ON m.id = u.ancestor_id WHERE u.user_id = $1",
        user_id, conn=conn,
    )
    by_level = {r["level"]: r for r in uplines}
    for level in range(1, 26):
        amount = level_amount(s["level_income"], level)
        up = by_level.get(level)
        await credit_income(conn, up["ancestor_id"] if up else None, amount, "level_income",
                            f"lvl:{user_id}:{level}", {**meta, "level": level},
                            eligible=bool(up and up["ok"]), s=s)

    # 3. Super 3 share (₹300) by the sponsor's real deposited shares.
    await _super3_share(conn, user, s, meta)
    return {"user_id": user_id, "user_code": user["user_code"]}


async def _super3_share(conn, user: Dict, s: Dict, meta: Dict):
    s3 = s["super3"]
    share = s3["share_per_join"]
    sponsor = await cpo.fetch_one(
        "UPDATE users SET direct_count = direct_count + 1 WHERE id = $1 "
        "RETURNING id, direct_count, s3_shares, (role = 'USER' AND status = 'active') AS ok",
        user["sponsor_id"], conn=conn,
    ) if user["sponsor_id"] else None

    ref = f"s3share:{user['id']}"
    if not sponsor or not sponsor["ok"]:
        # Sponsor blocked (or company): this share goes to the company and is not counted.
        # An earlier hold stays; after unblock the next share completes Board 1.
        await company_add(conn, "lapse_super3", share, ref, None,
                          {**meta, "for_user": sponsor["id"] if sponsor else None})
        return

    # Decided by real deposited shares (s3_shares), never by direct_count.
    sid, shares = sponsor["id"], sponsor["s3_shares"]
    if shares == 0:
        # First real share: company holds ₹300 until the next one comes.
        await company_add(conn, "s3_hold", share, ref, sid, meta)
        await cpo.execute("UPDATE users SET s3_shares = 1 WHERE id = $1", sid, conn=conn)
    elif shares == 1:
        # Held ₹300 + this ₹300 = ₹600 -> Board 1.
        await company_add(conn, "s3_hold", -share, f"{ref}:release", sid, meta)
        await cpo.execute("UPDATE users SET s3_shares = 2 WHERE id = $1", sid, conn=conn)
        await place_in_super3(conn, sid, "main", s)
    else:
        # Already on Board 1: ₹200 direct bonus + ₹100 Self Help Fund.
        await credit_income(conn, sid, s3["direct_bonus"], "direct_bonus", f"dbonus:{user['id']}", meta,
                            eligible=True, s=s)
        await company_add(conn, "self_help", s3["self_help"], f"selfhelp:{user['id']}", sid, meta)


# ================================================================ Super 3
def board_entry(s3: Dict, board: int) -> int:
    return s3["board1_entry"] * (2 ** (board - 1))


def parent_position(position: int) -> int:
    return (position + 1) // 3


def child_positions(position: int) -> List[int]:
    return [3 * position - 1, 3 * position, 3 * position + 1]


async def place_in_super3(conn, user_id: Optional[int], kind: str, s: Optional[Dict] = None) -> int:
    """Starts a new journey on Board 1 and runs every upgrade / recycle it causes.
    One placement at a time (advisory lock), so two joins never take the same spot."""
    await cpo.advisory_lock(conn, "super3")
    s = s or await get_settings(conn)
    s3 = s["super3"]
    journey_id = await cpo.insertion("super3_journeys", {"user_id": user_id, "kind": kind}, conn=conn)

    queue = deque([(journey_id, user_id, 1)])
    while queue:
        jid, uid, board = queue.popleft()
        position = await cpo.fetch_val(
            "UPDATE super3_counters SET next_position = next_position + 1 WHERE board = $1 "
            "RETURNING next_position - 1", board, conn=conn,
        )
        if position is None:
            raise RuntimeError(f"Super 3 board {board} is not set up. Run seed.py")
        await cpo.insertion("super3_nodes", {"board": board, "position": position, "journey_id": jid,
                                             "user_id": uid}, conn=conn, returning=None)
        await cpo.execute("UPDATE super3_journeys SET current_board = $2 WHERE id = $1", jid, board, conn=conn)

        parent = await cpo.fetch_one(
            "UPDATE super3_nodes SET children = children + 1, "
            "status = CASE WHEN children + 1 = 3 THEN 'full' ELSE status END, "
            "full_at = CASE WHEN children + 1 = 3 THEN now() ELSE full_at END "
            "WHERE board = $1 AND position = $2 RETURNING id, journey_id, user_id, children",
            board, parent_position(position), conn=conn,
        )
        if parent and parent["children"] == 3:
            queue.extend(await _board_full(conn, parent, board, s))
    return journey_id


async def _board_full(conn, node: Dict, board: int, s: Dict) -> List[tuple]:
    """A spot got its 3. Pays the member and returns the next placements."""
    s3 = s["super3"]
    entry = board_entry(s3, board)
    income = entry * 3
    ref = f"s3:{node['id']}"
    meta = {"board": board, "node_id": node["id"]}
    last = board == s3["boards"]

    if node["user_id"] is None:  # company spot #1
        await company_add(conn, "s3_company", income, ref, None, meta)
        return []

    nxt: List[tuple] = []
    wallet_part = income if last else entry       # the rest (2x entry) is the next board's entry
    recycle = s3["recycle"] if board >= 2 else 0
    s3_level = s3["s3_level_cut"] if board >= s3["s3_level_from_board"] else 0

    eligible = await can_earn(conn, node["user_id"])
    await credit_income(conn, node["user_id"], wallet_part, "super3_income", ref, meta,
                        other_cut=recycle + s3_level, eligible=eligible, s=s)
    if eligible and s3_level:
        await company_add(conn, "s3_level_pool", s3_level, f"s3lvl:{node['id']}", node["user_id"], meta)

    if last:
        await cpo.execute("UPDATE super3_journeys SET finished = TRUE WHERE id = $1", node["journey_id"], conn=conn)
    else:
        nxt.append((node["journey_id"], node["user_id"], board + 1))
    if recycle:
        # The recycle ID is a fresh journey. If the member was blocked, the ₹600 was
        # already taken by the company inside credit_income, so the spot is the company's.
        uid = node["user_id"] if eligible else None
        rj = await cpo.insertion("super3_journeys", {"user_id": uid, "kind": "recycle" if uid else "company"},
                                 conn=conn)
        nxt.append((rj, uid, 1))
    return nxt


def super3_member_net(s3: Dict, cut_total_pct: int = 10) -> int:
    """Total a member gets over all boards of one journey (shown in the app)."""
    total = 0
    for b in range(1, s3["boards"] + 1):
        e = board_entry(s3, b)
        part = e * 3 if b == s3["boards"] else e
        other = (s3["recycle"] if b >= 2 else 0) + (s3["s3_level_cut"] if b >= s3["s3_level_from_board"] else 0)
        total += part - percent_of(part, cut_total_pct) - other
    return total


# ================================================================ PINs
async def issue_pins(conn, owner_id: int, qty: int, source: str) -> List[str]:
    """Creates `qty` ready PINs for the owner. Codes are random and unique."""
    from base import new_pin_code

    codes: List[str] = []
    while len(codes) < qty:
        batch = list({new_pin_code() for _ in range(qty - len(codes))})
        rows = await cpo.fetch_all(
            "INSERT INTO pins (code, owner_id, bought_by, source) "
            "SELECT c, $2, $2, $3 FROM unnest($1::text[]) AS c ON CONFLICT (code) DO NOTHING RETURNING code",
            batch, owner_id, source, conn=conn,
        )
        codes += [r["code"] for r in rows]
    return codes
