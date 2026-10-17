"""Response schemas for /openapi.json (frontend types are generated from them).

Routes still return `respond(...)` (a JSONResponse), so FastAPI does not validate at runtime and big lists stay
fast. The test suite validates every response it sees against these models (tests/helpers.py), with
extra="forbid", so a field added, renamed or removed without updating the model fails the tests.

Money is rupees (float, 2 decimals) unless a field says paise. Dates are ISO strings.
"""

from datetime import date, datetime
from typing import Any, Dict, Generic, List, Literal, Optional, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Envelope(Out, Generic[T]):
    msg: str
    status: int
    data: T


class Page(Out, Generic[T]):
    rows: List[T]
    total: int = Field(description="Rows in the whole list")
    page: int
    limit: int
    next_cursor: Optional[str] = Field(description="Send as ?cursor= for the next page; null = no more rows")


NoData = List[Any]   # message-only answers: "data": []


class ApprovalWaiting(Out):
    """202: the action waits for a second admin (₹10,000 and above)."""
    approval_id: int
    waiting_for_second_admin: bool


WAITING = {202: {"model": Envelope[ApprovalWaiting], "description": "Waiting for a second admin"}}


class MaskedEmail(Out):
    email: str


# ================================================================ users
class SponsorCheck(Out):
    sponsor_code: str
    is_valid: bool
    sponsor_name: str


class ResetLinkSent(Out):
    """Admin 'Send password reset': where the link went (masked). The admin never sees the link or a password."""
    email: str
    expires_in: int = Field(description="Seconds the link works (24 hours)")


class SignupOtp(Out):
    """Sign-up step 1 of 2: the OTP went to the email. Send signup_ticket + the OTP to POST /users/register/verify."""
    signup_ticket: str
    email: str = Field(description="Masked, e.g. s****@gmail.com")
    resend_in: int = Field(description="Seconds before POST /users/register/resend works")
    expires_in: int = Field(description="Seconds the sign-up is kept; then the form must be filled again")


class PublicUser(Out):
    id: int
    user_code: str
    full_name: str
    mobile: str
    email: str
    role: Literal["USER", "ADMIN", "DOCTOR"]
    status: Literal["inactive", "active", "blocked", "removed"]
    activated_at: Optional[datetime]
    direct_count: int
    state: Optional[str]
    district: Optional[str]
    created_at: datetime


class LoginToken(Out):
    token: str
    user: PublicUser


class SignupDone(LoginToken):
    """Sign-up step 2 of 2: the ID is made and the member is logged in (the app opens "Activate your ID")."""
    user_code: str
    full_name: str


class LoginOtpNeeded(Out):
    """Admins: step 1 of 2. Send login_ticket + the emailed OTP to POST /users/login/otp."""
    otp_required: bool
    login_ticket: str
    email: str


class TokenOnly(Out):
    token: str


class Me(PublicUser):
    sponsor_code: Optional[str]
    sponsor_name: Optional[str]
    pan: Optional[str]
    bank_holder: Optional[str]
    bank_account: Optional[str] = Field(description="Masked: ********4521")
    bank_ifsc: Optional[str]
    bank_branch: Optional[str]
    bank_updated_at: Optional[datetime]
    referral_link: Optional[str] = Field(description="Only for active IDs")
    remove_on: Optional[date] = Field(description="Inactive IDs: removed after this date if not activated")


class MobileCheck(Out):
    available: bool


class TeamLevel(Out):
    level: int
    total: int
    active: int


class TeamSummary(Out):
    directs: int
    active_directs: int
    team: int
    active_team: int
    inactive_team: int
    payment_waiting: int
    levels: List[TeamLevel]


class TeamMember(Out):
    user_code: str
    full_name: str
    status: str
    direct_count: int
    created_at: datetime
    activated_at: Optional[datetime]
    level: int
    sponsor_code: Optional[str]
    team_size: int = Field(description="Active team at full depth, as of last night (rank job)")
    payment_pending: bool


class MemberLookup(Out):
    user_code: str
    full_name: str
    status: str
    created_at: datetime
    my_level: Optional[int] = Field(description="Level in my team, null if not in my team")
    is_my_sponsor: bool


# ================================================================ wallet
class WalletSummary(Out):
    income: float
    fund: float
    product: float
    total_earned: float
    total_withdrawn: float
    today_income: float
    ready_pins: int


class LedgerRow(Out):
    id: int
    wallet: Literal["income", "fund", "product"]
    type: str
    amount: float
    balance_after: float
    gross: float
    tds: float
    service: float
    single_leg: float
    other_cut: float
    meta: Dict[str, Any]
    created_at: datetime
    withdrawal_status: Optional[str]
    withdrawal_utr: Optional[str]


class IncomeTypeTotal(Out):
    count: int
    net: float
    gross: float
    tds: float


class IncomeTotal(Out):
    count: int
    net: float
    gross: float
    tds: float
    service: float
    single_leg: float
    other_cut: float


class IncomeSummary(Out):
    period: Literal["today", "week", "month", "all"]
    from_: datetime = Field(alias="from")
    by_type: Dict[str, IncomeTypeTotal]
    total: IncomeTotal


class LevelIncomeRow(Out):
    amount: float
    gross: float
    from_code: Optional[str]
    from_name: Optional[str]
    level: Optional[int]
    created_at: datetime


class LevelTotal(Out):
    level: int
    count: int
    net: float = Field(description="All-time net level income from this level (the EARNED column)")
    this_month: float


class LevelIncomePage(Page[LevelIncomeRow]):
    by_level: List[LevelTotal]
    this_month: float
    total_amount: float


class Created(Out):
    id: int
    amount: float


class MyPaymentRequest(Out):
    id: int
    purpose: Literal["activation", "fund", "pins"]
    amount: float
    pin_qty: int
    utr: str
    receipt_path: str
    status: Literal["pending", "approved", "rejected"]
    admin_remark: Optional[str]
    created_at: datetime
    reviewed_at: Optional[datetime]


class AdminPaymentRequest(Out):
    id: int
    user_id: int
    purpose: Literal["activation", "fund", "pins"]
    amount: float
    pin_qty: int
    utr: str
    receipt_path: str
    note: Optional[str]
    status: Literal["pending", "approved", "rejected"]
    admin_remark: Optional[str]
    reviewed_by: Optional[int]
    reviewed_at: Optional[datetime]
    created_at: datetime
    user_code: str
    full_name: str
    mobile: str
    user_status: str
    sponsor_code: Optional[str]
    sponsor_name: Optional[str]
    reviewed_by_name: Optional[str]


class MyWithdrawal(Out):
    id: int
    amount: float
    status: Literal["pending", "paid", "rejected"]
    utr: Optional[str]
    admin_remark: Optional[str]
    account: Optional[str] = Field(description="Masked account number")
    created_at: datetime
    reviewed_at: Optional[datetime]


class AdminWithdrawal(Out):
    id: int
    user_id: int
    amount: float
    bank: Dict[str, Any] = Field(description="holder, account, ifsc, branch, pan at request time")
    status: Literal["pending", "paid", "rejected"]
    utr: Optional[str]
    admin_remark: Optional[str]
    reviewed_by: Optional[int]
    reviewed_at: Optional[datetime]
    created_at: datetime
    user_code: str
    full_name: str
    mobile: str
    reviewed_by_name: Optional[str] = Field(description="Admin who marked it paid or rejected")


class P2PLimits(Out):
    fee_percent: float
    min: float
    max: float
    daily_max: float
    sent_today: float
    left_today: float


class P2PSent(Out):
    sent: float
    fee: float
    received: float


class BalanceAfter(Out):
    balance_after: float


# ================================================================ PINs
class PinReady(Out):
    code: str
    source: str
    created_at: datetime


class PinReceived(Out):
    code: str
    status: str
    from_code: str
    from_name: str
    created_at: datetime


class PinSent(Out):
    code: str
    status: str
    to_code: str
    to_name: str
    created_at: datetime


class PinUsed(Out):
    code: str
    activated_code: str
    activated_name: str
    used_at: datetime


class PinCounts(Out):
    ready: int
    received: int
    sent: int
    used: int


class MyPinsPage(Page[Union[PinReady, PinReceived, PinSent, PinUsed]]):
    """Row shape depends on ?tab=: ready / received / sent / used."""
    counts: PinCounts


class PinActivated(Out):
    pin: str


class PinCodes(Out):
    codes: List[str]


class PinsBought(Out):
    codes: List[str]
    paid: float


class AdminPin(Out):
    bought_by_code: str
    bought_by_name: str
    code: str
    status: Literal["ready", "used"]
    source: str
    created_at: datetime
    used_at: Optional[datetime]
    owner_code: str
    owner_name: str
    used_for_code: Optional[str]
    last_sent_at: Optional[datetime]   # last time a member sent it to another member


class AdminPinCounts(Out):
    ready: int
    used: int
    total: int
    sold: int
    sold_amount: float
    members_holding: int
    sent_between_members: int


class AdminPinsPage(Page[AdminPin]):
    counts: AdminPinCounts


# ================================================================ Super 3
class SpotToGo(Out):
    spot: int
    to_go: int


class BoardPosition(Out):
    board: int
    position: int


class Journey(Out):
    journey_id: int
    kind: Literal["main", "recycle", "company"]
    board: int
    finished: bool
    position: Optional[int]
    children: Optional[int]
    line_at: Optional[int] = None
    spots: Optional[List[SpotToGo]] = None
    speed_per_day: Optional[float] = None
    eta_days: Optional[int] = Field(None, description="null = no joins in the last 7 days")
    you_get_when_full: Optional[float] = None
    positions: List[BoardPosition] = Field(description="This ID's spot on every board it reached (for the tree)")


class BoardInfo(Out):
    board: int
    entry: float
    you_get: float


class Super3Status(Out):
    direct_count: int
    in_super3: bool
    s3_shares: int = Field(description="0 = nothing yet, 1 = ₹300 on hold, 2 = on Board 1")
    on_hold: float
    journeys: List[Journey]
    boards: List[BoardInfo]
    journey_total: float
    recycle_ids: int


class Spot(Out):
    position: int
    empty: bool
    children: Optional[int] = None
    status: Optional[str] = None
    created_at: Optional[datetime] = None
    user_code: Optional[str] = None
    name: Optional[str] = None
    sponsor_name: Optional[str] = None


class SpotRow(Spot):
    children: Optional[List[Spot]] = None   # type: ignore[assignment]


class Super3Tree(Out):
    board: int
    root: Spot
    above: Optional[Spot]
    children: List[SpotRow]


class BoardStat(Out):
    board: int
    last_spot: int
    full: int
    today: int
    filling: int
    speed_7d: float
    next_spot: int


class JourneyTotals(Out):
    main: int
    recycle: int
    finished: int


class Super3Money(Out):
    on_hold: float
    s3_level_pool: float
    company_spots: float
    wallet_part_paid: float
    self_help: float


class Super3Stats(Out):
    boards: List[BoardStat]
    journeys: JourneyTotals
    money: Super3Money
    waiting_second_share: int = Field(description="Members with ₹300 on hold, waiting for the next share")
    ids_on_boards: int


# ================================================================ complaints
class Category(Out):
    id: int
    name: str
    is_active: bool


class IdOnly(Out):
    id: int


class ComplaintCreated(Out):
    id: int
    complaint_no: str
    status: str


class PathOnly(Out):
    path: str


class ComplaintListRow(Out):
    id: int
    complaint_no: str
    status: str
    created_at: datetime
    updated_at: datetime
    category: str
    user_code: str
    full_name: str
    happened_on: Optional[str]   # incident date the member gave (YYYY-MM-DD), if any
    unread: int


class ComplaintDetail(Out):
    id: int
    complaint_no: str
    user_id: int
    category_id: int
    status: str
    incident: Dict[str, Any]
    suspect: Dict[str, Any]
    complainant: Dict[str, Any]
    evidence: List[str]
    share_with_agencies: bool
    remarks: Optional[str]
    created_at: datetime
    updated_at: datetime
    member_last_read: int
    admin_last_read: int
    category: str
    user_code: str
    full_name: str
    mobile: str


class ChatMessage(Out):
    id: int
    message: Optional[str]
    file_path: Optional[str]
    created_at: datetime
    mine: bool
    sender: str


# ================================================================ dashboards
class WalletBalances(Out):
    income: float
    fund: float
    product: float
    total_earned: float
    total_withdrawn: float


class DashTeam(Out):
    directs: int
    team: int
    active_team: int


class MemberDashboard(Out):
    status: str
    rank: int
    rank_name: Optional[str]
    wallet: WalletBalances
    team: DashTeam
    income: Dict[str, float] = Field(description="All-time net per income type")
    income_this_month: float
    activation_request: Optional[str]
    pending_withdrawal: Optional[float]


class AdminMembersCount(Out):
    active: int
    inactive: int
    blocked: int
    activated_today: int
    joined_today: int


class MissingVideoDay(Out):
    date: date
    missing: int


class AdminQueue(Out):
    payment_requests: int
    withdrawals: int
    withdrawals_amount: float
    open_complaints: int
    complaints_new_messages: int = Field(description="Complaints with a member message no admin has read yet")
    orders_to_pack: int
    order_payments_to_check: int
    doctor_requests_waiting: int
    doctor_requests_overdue: int
    contact_messages_new: int
    oldest_withdrawal: Optional[datetime]
    payment_requests_by_purpose: Dict[str, int] = Field(description="activation, fund, pins")
    video_days_missing: List[MissingVideoDay]


class DashJoins(Out):
    joined_yesterday: int
    activated_yesterday: int
    turnover_this_month: float
    joins_this_month: int
    ids_in_super3: int
    boards_full: int


class DayCount(Out):
    day: date
    activated: int


class AdminDashboard(Out):
    members: AdminMembersCount
    queue: AdminQueue
    joins: DashJoins
    last_30_days: List[DayCount]
    money: Dict[str, float]
    totals_as_of: datetime = Field(description="members / joins / chart / money are cached up to 60 s; queue is live")


class PnlIn(Out):
    turnover: float


class PnlCosts(Out):
    level_income_pool: float
    super3_share: float
    product_cost: float
    royalty_pools: float
    gullak_interest: float


class PnlCompanyIncome(Out):
    service_charge: float
    unpaid_levels_and_lapses: float
    company_spots_super3: float
    p2p_fee: float
    admin_debits: float
    reversals: float
    single_leg_leftover: float
    royalty_leftover: float
    s3_level_leftover: float
    gullak_charges: float


class PnlKeptOut(Out):
    tds: float
    self_help: float
    s3_hold: float
    s3_level_pool: float
    gullak_held: float


class PnlShop(Out):
    mrp_sold: float
    from_product_wallet: float
    from_fund_wallet: float
    from_income_wallet: float
    sales: float = Field(description="Paid from Fund + Income wallets (the Product Wallet part is not a sale)")
    in_profit: bool = Field(description="Setting shop_sales_in_profit: sales are added to the profit")


class Pnl(Out):
    month: str
    joins: int
    money_in: PnlIn
    costs: PnlCosts
    company_income: PnlCompanyIncome
    kept_out: PnlKeptOut
    profit: float
    shop: PnlShop


class AdminMemberRow(Out):
    id: int
    user_code: str
    full_name: str
    mobile: str
    email: str
    status: str
    direct_count: int
    created_at: datetime
    activated_at: Optional[datetime]
    sponsor_code: Optional[str]
    income: float
    fund: float
    product: float
    state: Optional[str]
    district: Optional[str]
    rank: int
    rank_name: Optional[str]
    active_team: int = Field(description="Full depth, active only (last night's count)")
    days_inactive: Optional[int] = Field(description="'Not active · day 3'")


class MemberCounts(Out):
    total: int
    active: int
    inactive: int
    blocked: int
    removed: int


class AdminMembersPage(Page[AdminMemberRow]):
    counts: MemberCounts


class AdminMemberUser(Out):
    id: int
    user_code: str
    sponsor_id: Optional[int]
    full_name: str
    mobile: str
    email: str
    state: Optional[str]
    district: Optional[str]
    role: str
    status: str
    activated_at: Optional[datetime]
    single_leg_no: Optional[int]
    direct_count: int
    pan: Optional[str]
    bank_holder: Optional[str]
    bank_account: Optional[str]
    bank_ifsc: Optional[str]
    bank_branch: Optional[str]
    bank_updated_at: Optional[datetime]
    token_version: int
    created_at: datetime
    s3_shares: int
    s3l_reached_on: Optional[date]
    sponsor_code: Optional[str]
    sponsor_name: Optional[str]


class AdminWallet(Out):
    user_id: int
    income: float
    fund: float
    product: float
    total_earned: float
    total_withdrawn: float
    updated_at: datetime


class AdminLedgerRow(Out):
    """Includes hidden admin debits (visible_to_user = false)."""
    id: int
    user_id: int
    wallet: str
    type: str
    amount: float
    balance_after: float
    gross: float
    tds: float
    service: float
    single_leg: float
    other_cut: float
    ref: str
    meta: Dict[str, Any]
    visible_to_user: bool
    created_at: datetime
    reversed: bool = Field(description="A reverse entry was already made for this row")


class MemberVideos(Out):
    day_no: Optional[int]
    days_total: int
    days_done: int


class LevelRow(Out):
    level: int
    people: int
    active: int
    level_income: float


class Team25(Out):
    people: int
    active: int
    directs: int


class MemberRank(Out):
    rank: int
    rank_name: Optional[str]
    active_team_full: int
    active_directs: int
    as_of: datetime


class MemberSuper3(Out):
    board: Optional[int]
    spot: Optional[int]
    recycle_ids: int


class ComplaintCounts(Out):
    open: int
    solved: int


class LastDeduction(Out):
    amount: float
    created_at: datetime
    comment: Optional[str]
    by_name: Optional[str]


class MemberSummary(Out):
    pending_withdrawal: Optional[float]
    people_above: int
    team_by_level: List[LevelRow]
    team_25_levels: Team25
    rank: Optional[MemberRank]
    super3: MemberSuper3
    complaints: ComplaintCounts
    last_deduction: Optional[LastDeduction]


class AdminMemberDetail(Out):
    member: AdminMemberUser
    wallet: AdminWallet
    ledger: List[AdminLedgerRow]
    videos: MemberVideos
    summary: MemberSummary


class SingleLegRun(Out):
    id: int
    run_date: date
    status: Literal["claimed", "computed", "done"]
    pool_rows: int
    pool_total: float
    shared: float
    company: float
    receivers: int
    started_at: datetime
    finished_at: Optional[datetime]


class AuditRow(Out):
    id: int
    admin_id: int
    action: str
    target: Optional[str]
    detail: Dict[str, Any]
    created_at: datetime
    admin_name: str


# ================================================================ company + settings
class CompanyDetail(Out):
    name: str
    upi_id: str
    account_holder: str
    account_no: str
    ifsc: str
    branch: str
    qr_path: str
    email: str
    phone: str
    whatsapp_group: str


class PublicP2P(Out):
    fee_percent: float
    min: float
    max: float
    daily_max: float


class PublicVideo(Out):
    reward: float
    days: int
    per_day: int
    seconds: int


class PublicShop(Out):
    order_max: float


class PublicConfig(Out):
    activation_amount: float
    pin_price: float
    product_wallet_on_activation: float
    min_withdraw: float
    inactive_remove_days: int
    cut_percent: Dict[str, float]
    p2p: PublicP2P
    video: PublicVideo
    shop: PublicShop
    level_income: Dict[str, float] = Field(description="Level range -> ₹, e.g. '4-10': 10")
    demo: bool = Field(description="True on the demo server: show a DEMO banner (every answer also has X-Treeway-Demo: 1)")


class SettingChange(Out):
    by_name: Optional[str]
    at: datetime


class AdminSettings(Out):
    settings: Dict[str, Any] = Field(description="Raw settings; money here is in PAISE")
    editable: List[str]
    locked: Dict[str, str] = Field(description="Setting (or super3.field) -> why it is locked")
    changed: Dict[str, SettingChange] = Field(description="Setting -> the admin who last changed it, and when")


# ================================================================ admin
class Approval(Out):
    id: int
    action: Literal["payment_approve", "wallet_debit", "pins_issue", "reverse_entry"]
    target: str
    amount: float
    payload: Dict[str, Any]
    status: Literal["pending", "approved", "rejected", "cancelled"]
    requested_by: int
    decided_by: Optional[int]
    remark: Optional[str]
    created_at: datetime
    decided_at: Optional[datetime]
    requested_by_name: str
    decided_by_name: Optional[str]
    i_can_decide: bool


class PowChallenge(Out):
    challenge: str = Field(description="Send back as pow_challenge with the sign-up")
    bits: int = Field(description="sha256('<salt>:<nonce>') must start with this many zero bits")
    expires_in: int = Field(description="Seconds the challenge works (once)")


class BankHoldSkipped(Out):
    updated: int = Field(description="IDs with this email whose 24 h bank hold was skipped (dev / e2e only)")


class AdminAccount(Out):
    user_code: str
    full_name: str
    mobile: str
    email: str
    status: str
    created_at: datetime


class UserCodeOnly(Out):
    user_code: str


class Reversed(Out):
    ledger_id: int
    reversed: float
    balance_after: float


# ================================================================ videos
class TodayVideo(Out):
    slot: int
    platform: Literal["youtube", "facebook", "instagram"]
    url: str
    title: str
    status: Literal["done", "running", "next", "waiting"]
    seconds_left: int


class VideosToday(Out):
    date: date
    day_no: Optional[int] = Field(description="Activation date is Day 1")
    days_total: int
    eligible: bool
    ready: bool = Field(description="False = no complete set of links in the last 30 days: no lock, no ₹20")
    links_from: Optional[date] = Field(description="Date of the links used when today has no full set")
    locked: bool = Field(description="True = keep the app locked until today's videos are done")
    all_done: bool
    done_count: int
    per_day: int
    seconds: int
    reward: float
    videos: List[TodayVideo]
    days_earned: int
    earned_total: float
    max_total: float


class VideoStarted(Out):
    slot: int
    platform: str
    url: str
    title: str
    started_at: datetime
    seconds: int


class VideoDone(Out):
    done_count: int
    all_done: bool
    credited: float
    day_no: Optional[int]


class VideoDay(Out):
    date: date
    done: bool


class VideoHistory(Out):
    days: List[VideoDay]
    done: int
    missed: int


class AdminVideo(Out):
    slot: int
    platform: str
    url: str
    title: str
    updated_at: datetime
    updated_by_name: Optional[str]


class AdminVideos(Out):
    date: date
    videos: List[AdminVideo]
    per_day: int
    missing_days: List[MissingVideoDay]


class MissingDays(Out):
    missing_days: List[MissingVideoDay]


# ================================================================ ranks + monthly payouts
class RankStep(Out):
    rank: int
    name: str
    need: int
    counts: Literal["active directs", "active team"]
    status: Literal["done", "next", "locked"]


class BiggestLine(Out):
    name: Optional[str]
    user_code: str
    active: int
    counted: int


class NextRank(Out):
    name: str
    need: int
    counted: int = Field(description="Toward this rank after the 50:50 rule")
    one_line_max: Optional[int]
    biggest_line: Optional[BiggestLine]


class LastRoyalty(Out):
    amount: float
    month: Optional[str]
    paid_at: datetime


class S3LevelProgress(Out):
    directs_needed: int
    active_directs: int
    reached_on: Optional[date]
    first_payout: Optional[date]
    payouts: int


class MyRank(Out):
    as_of: Optional[datetime] = Field(description="Counted every night; numbers are as of this time")
    rank: int
    rank_name: Optional[str]
    active_directs: int
    active_team: int
    ranks: List[RankStep]
    next: Optional[NextRank]
    leg_cap_percent: int
    pay_day: int
    last_royalty: Optional[LastRoyalty]
    s3_level: S3LevelProgress


class RankPool(Out):
    rank: int
    name: str
    pool: float
    members: int
    each: float
    to_company: float


class RoyaltyPlan(Out):
    turnover: float
    pool_each_rank: float
    pool_total: float
    ranks: List[RankPool]
    to_company: float
    snapshot_as_of: Optional[datetime]


class S3LevelPlan(Out):
    pool_total: float
    members: int
    each: float
    to_company: float


class RunState(Out):
    kind: str
    status: str
    finished_at: Optional[datetime]
    paid_by: Optional[int]


class PayoutPreview(Out):
    month: str
    pay_from: date
    can_pay: bool
    royalty: RoyaltyPlan
    s3_level: S3LevelPlan
    runs: Dict[str, RunState]
    note: Optional[str]


class MonthlyRun(Out):
    id: int
    month: date
    kind: Literal["royalty", "s3_level"]
    status: Literal["computed", "done"]
    pool_total: float
    shared: float
    company: float
    receivers: int
    detail: Dict[str, Any]
    paid_by: Optional[int] = Field(description="null = paid by the automatic backup")
    started_at: datetime
    finished_at: Optional[datetime]


class MonthlyRunRow(MonthlyRun):
    paid_by_name: Optional[str]


# ================================================================ dev / e2e only
class DevOtp(Out):
    otp: str


class GapsCleared(Out):
    cleared: int



# ================================================================ shop
class Product(Out):
    id: int
    name: str
    description: str
    category: str
    mrp: float
    in_stock: bool
    images: List[str] = Field(description="Public paths: GET /uploads/<path>")


class AdminProduct(Product):
    stock: int
    is_active: bool
    low_stock: bool
    updated_at: datetime


class CourierQuote(Out):
    pincode: str
    state: Optional[str] = Field(description="null = not known (default rate)")
    state_source: Literal["india_post", "pin_prefix", "unknown", "other_state"]
    rate: float
    rate_from: Literal["pincode", "state"]
    reviewed: bool


class WalletSplit(Out):
    product: float
    fund: float
    income: float


class OrderPlaced(Out):
    id: int
    order_no: str
    items_total: float
    paid: WalletSplit
    courier_charge: float


class OrderItem(Out):
    product_id: int
    name: str
    mrp: float
    qty: int


class Order(Out):
    id: int
    order_no: str
    status: Literal["placed", "packed", "shipped", "delivered", "cancelled"]
    items_total: float
    paid: WalletSplit
    courier_charge: float
    courier_state: Optional[str]
    courier_utr: str
    courier_status: Literal["pending", "approved", "rejected"]
    courier_remark: Optional[str]
    courier_refund_due: bool
    courier_refunded_at: Optional[datetime]
    address: Dict[str, Any]
    courier_name: Optional[str]
    tracking_no: Optional[str]
    cancel_reason: Optional[str]
    created_at: datetime
    packed_at: Optional[datetime]
    shipped_at: Optional[datetime]
    delivered_at: Optional[datetime]
    cancelled_at: Optional[datetime]
    prescription_id: Optional[int]
    items: List[OrderItem]


class AdminOrder(Order):
    user_code: str
    full_name: str
    courier_receipt: str
    courier_refund_utr: Optional[str]


class OrderCounts(Out):
    to_pack: int
    payment_to_check: int
    on_the_way: int
    delivered_this_month: int
    courier_refunds_due: int
    low_stock: int


class AdminOrdersPage(Page[AdminOrder]):
    counts: OrderCounts


class PincodeRate(Out):
    pincode: str
    rate: Optional[float]
    updated_at: datetime
    updated_by_name: Optional[str]


class RateNotSet(Out):
    pincode: str
    orders: int
    last_order: datetime
    state: Optional[str]


class CourierRates(Out):
    state_rates: Dict[str, float]
    pincode_rates: List[PincodeRate]
    rate_not_set: List[RateNotSet]



# ================================================================ doctor
class Medicine(Out):
    product_id: Optional[int]
    name: str
    dosage: str
    days: int
    in_shop: bool
    mrp: Optional[float]
    in_stock: bool


class Prescription(Out):
    id: int
    doctor_name: str
    bams_reg_no: str
    advice: str
    created_at: datetime
    medicines: List[Medicine]


class DoctorRequestCreated(Out):
    id: int
    reply_note: str


class MemberDoctorRequest(Out):
    id: int
    problem: str
    files: List[str]
    status: Literal["waiting", "answered"]
    created_at: datetime
    answered_at: Optional[datetime]
    prescriptions: List[Prescription]


class MemberDoctorPage(Page[MemberDoctorRequest]):
    reply_note: str
    open: int
    max_open: int


class CartItem(Out):
    product_id: int
    name: str
    mrp: float
    qty: int
    in_stock: bool


class PrescriptionCart(Out):
    prescription_id: int
    items: List[CartItem]
    not_in_shop: List[str]


class DoctorInboxRow(Out):
    """Only what a doctor may see: no mobile, wallet, team or bank."""
    id: int
    member_name: str
    age: Optional[int]
    state: Optional[str]
    problem: str
    files: List[str]
    status: Literal["waiting", "answered"]
    created_at: datetime
    waiting_hours: Optional[int]


class DoctorInboxCounts(Out):
    waiting: int
    answered_this_month: int


class DoctorInboxPage(Page[DoctorInboxRow]):
    counts: DoctorInboxCounts


class DoctorRequestView(DoctorInboxRow):
    prescriptions: List[Prescription]


class AdminDoctor(Out):
    user_code: str
    full_name: str
    mobile: str
    email: str
    status: str
    bams_reg_no: str
    created_at: datetime
    waiting: int
    answered_this_month: int


class Moved(Out):
    moved: int


class AdminDoctorRequest(Out):
    id: int
    problem: str
    files: List[str]
    status: Literal["waiting", "answered"]
    created_at: datetime
    answered_at: Optional[datetime]
    assigned_at: Optional[datetime]
    user_code: str
    full_name: str
    doctor_code: Optional[str]
    doctor_name: Optional[str]
    prescriptions: List[Prescription]


class AdminDoctorCounts(Out):
    waiting: int
    overdue: int = Field(description="Waiting more than 24 hours")
    no_doctor: int
    answered: int


class AdminDoctorPage(Page[AdminDoctorRequest]):
    counts: AdminDoctorCounts



# ================================================================ reports
class TdsRow(Out):
    pan: str
    name: str
    ids: str = Field(description="All member IDs with this PAN, comma separated")
    id_count: int
    gross: float
    tds: float
    net: float


class TdsTotals(Out):
    gross: float
    tds: float
    net: float
    pans: int
    tds_without_pan: float
    members_without_pan: int


class TdsPeriod(Out):
    fy: str
    quarter: Optional[str]
    from_: date = Field(alias="from")
    to: date


class TdsReport(Page[TdsRow]):
    totals: TdsTotals
    period: TdsPeriod


class TdsNoPan(Out):
    user_code: str
    full_name: str
    mobile: str
    gross: float
    tds: float
    net: float


class IncomeTypeRow(Out):
    type: str
    entries: int
    members: int
    gross: float
    tds: float
    service: float
    single_leg: float
    other_cut: float
    net: float


class LapseRow(Out):
    type: str
    amount: float


class IncomeReport(Out):
    from_: date = Field(alias="from")
    to: date
    by_type: List[IncomeTypeRow]
    lapsed_to_company: List[LapseRow]


class JoiningRow(Out):
    period: date
    registered: int
    activated: int
    turnover: float


class JoiningTotals(Out):
    registered: int
    activated: int
    turnover: float


class JoiningsReport(Out):
    from_: date = Field(alias="from")
    to: date
    group: Literal["day", "month"]
    rows: List[JoiningRow]
    totals: JoiningTotals


class WithdrawalDay(Out):
    day: date
    requested: int
    paid: int
    paid_amount: float
    rejected: int


class PendingWithdrawals(Out):
    n: int
    amount: float
    oldest: Optional[datetime]


class WithdrawalsReport(Out):
    from_: date = Field(alias="from")
    to: date
    rows: List[WithdrawalDay]
    pending: PendingWithdrawals


class TurnoverMonth(Out):
    month: date
    joins: int
    turnover: float


class TurnoverTotals(Out):
    joins: int
    turnover: float


class TurnoverReport(Out):
    fy: str
    rows: List[TurnoverMonth]
    totals: TurnoverTotals
    gst_threshold: float
    over_gst_threshold: bool



# ================================================================ content
class Banner(Out):
    id: int
    title: str
    image: str = Field(description="Public path: GET /uploads/<image>")
    link_url: Optional[str]
    audience: Literal["everyone", "login"]


class AdminBanner(Banner):
    starts_on: Optional[date]
    ends_on: Optional[date]
    is_active: bool
    sort_order: int
    updated_at: datetime
    live: bool = Field(description="On and inside its dates today")


class ContactRow(Out):
    id: int
    name: str
    mobile: str
    message: str
    status: Literal["new", "replied"]
    reply_note: Optional[str]
    replied_at: Optional[datetime]
    created_at: datetime
    replied_by_name: Optional[str]


class ContactCounts(Out):
    new: int
    replied: int


class ContactPage(Page[ContactRow]):
    counts: ContactCounts



# ================================================================ admin list counts (Phase G)
class PaymentCounts(Out):
    pending: int
    approved: int
    rejected: int
    pending_activation: int
    pending_funds_pins: int


class AdminPaymentPage(Page[AdminPaymentRequest]):
    counts: PaymentCounts


class WithdrawalCounts(Out):
    pending: int
    pending_amount: float
    paid_today: int
    paid_today_amount: float
    paid_this_month: int
    paid_this_month_amount: float
    rejected_this_month: int


class AdminWithdrawalsPage(Page[AdminWithdrawal]):
    counts: WithdrawalCounts


class TopSponsor(Out):
    user_code: str
    full_name: str
    joins: int = Field(description="IDs they got activated this month")



# ================================================================ Gullak
class GullakDeposit(Out):
    id: int
    amount: float = Field(description="In the Gullak (after the 3% charge)")
    taken: float
    charge: float
    interest_percent: float
    interest_from: date = Field(description="Interest only if withdrawn on or after this day")
    status: Literal["active", "withdrawn"]
    created_at: datetime
    withdrawn_at: Optional[datetime]
    interest_if_withdrawn_today: Optional[float]
    days: Optional[int]
    interest_paid: Optional[float]
    interest_tds: Optional[float]


class GullakView(Out):
    enabled: bool = Field(description="False = 'Coming soon' (opening and deposits closed; withdrawals still work)")
    opened_at: Optional[datetime]
    opening_charge: float
    deposit_charge_percent: float
    interest_percent: float
    min_months: int
    balance: float
    interest_if_all_withdrawn_today: float
    deposits: List[GullakDeposit]


class GullakDeposited(Out):
    id: int
    taken: float
    charge: float
    amount: float
    interest_from: date


class GullakWithdrawn(Out):
    amount: float
    interest: float
    tds: float
    to_income_wallet: float
    days: int


class AdminGullak(Out):
    enabled: bool
    accounts: int
    active_deposits: int
    held: float
    interest_paid: float
    charges: float
    interest_if_all_withdrawn_today: float
