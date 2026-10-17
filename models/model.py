import re
from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

MOBILE_RE = re.compile(r"^[6-9]\d{9}$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
UTR_RE = re.compile(r"^[A-Za-z0-9]{6,30}$")
CODE_RE = re.compile(r"^TW\d{7}$")


def _strip(v):
    return v.strip() if isinstance(v, str) else v


class Clean(BaseModel):
    """Trims every text field."""

    @model_validator(mode="before")
    @classmethod
    def _trim(cls, data):
        if isinstance(data, dict):
            return {k: _strip(v) for k, v in data.items()}
        return data


def _password(v: str) -> str:
    if len(v) < 8 or not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
        raise ValueError("Use at least 8 characters with letters and numbers.")
    if len(v) > 64:
        raise ValueError("Password is too long.")
    return v


def _member_code(v: str) -> str:
    v = v.upper()
    if not CODE_RE.match(v):
        raise ValueError("Enter a valid member ID like TW1234567.")
    return v


# ================================================================ users
class RegisterModel(Clean):
    sponsor_code: str
    full_name: str = Field(min_length=2, max_length=80)
    mobile: str
    email: EmailStr
    password: str
    confirm_password: str
    # No date of birth (bhaiya, 16 Oct): an old app that still sends `dob` is fine, it is ignored.
    # State and district are required (Q2, Vinay 16 Oct).
    state: str = Field(min_length=2, max_length=60)
    district: str = Field(min_length=2, max_length=60)
    # Bot protection (botGuard.py): the challenge from GET /users/register/challenge and the phone's answer.
    pow_challenge: Optional[str] = Field(None, max_length=120)
    pow_nonce: Optional[str] = Field(None, max_length=20)
    website: Optional[str] = Field(None, max_length=200, description="Must stay empty (spam trap)")

    _sp = field_validator("sponsor_code")(_member_code)
    _pw = field_validator("password")(_password)

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, v):
        if not MOBILE_RE.match(v):
            raise ValueError("Enter a valid 10 digit mobile number.")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        return v.lower()

    @model_validator(mode="after")
    def _match(self):
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match.")
        return self


class SignupTicketModel(Clean):
    signup_ticket: str = Field(min_length=20, max_length=100)


class SignupVerifyModel(SignupTicketModel):
    otp: str = Field(min_length=6, max_length=6)


class LoginModel(Clean):
    login_id: str = Field(min_length=3, max_length=20, description="Member ID or mobile")
    password: str = Field(min_length=1, max_length=64)


class LoginOtpModel(Clean):
    login_ticket: str = Field(min_length=20, max_length=100)
    otp: str = Field(min_length=6, max_length=6)


class ForgotOtpModel(Clean):
    user_code: str  # Member ID only, not mobile

    _c = field_validator("user_code")(_member_code)


class ResetPasswordModel(Clean):
    user_code: str
    otp: str = Field(min_length=6, max_length=6)
    new_password: str

    _c = field_validator("user_code")(_member_code)
    _pw = field_validator("new_password")(_password)


class ChangePasswordModel(Clean):
    old_password: str
    new_password: str

    _pw = field_validator("new_password")(_password)


class BankModel(Clean):
    bank_holder: str = Field(min_length=2, max_length=80)
    bank_account: str
    bank_ifsc: str
    bank_branch: Optional[str] = Field(None, max_length=80)
    pan: str
    otp: str = Field(min_length=6, max_length=6)

    @field_validator("bank_account")
    @classmethod
    def _acc(cls, v):
        if not re.fullmatch(r"\d{9,18}", v):
            raise ValueError("Enter a valid account number.")
        return v

    @field_validator("bank_ifsc")
    @classmethod
    def _ifsc(cls, v):
        v = v.upper()
        if not IFSC_RE.match(v):
            raise ValueError("Enter a valid IFSC code.")
        return v

    @field_validator("pan")
    @classmethod
    def _pan(cls, v):
        v = v.upper()
        if not PAN_RE.match(v):
            raise ValueError("Enter a valid PAN like ABCDE1234F.")
        return v


class ProfileModel(Clean):
    full_name: Optional[str] = Field(None, min_length=2, max_length=80)
    email: Optional[EmailStr] = None
    state: Optional[str] = Field(None, max_length=60)
    district: Optional[str] = Field(None, max_length=60)
    otp: Optional[str] = Field(None, min_length=6, max_length=6)  # needed only to change the email


# ================================================================ money in
class PaymentRequestModel(Clean):
    """Form fields of a UTR request (receipt comes as a file)."""
    purpose: Literal["activation", "fund", "pins"]
    amount: Optional[str] = None        # only for 'fund' (₹)
    pin_qty: int = Field(0, ge=0, le=500)
    utr: str
    note: Optional[str] = Field(None, max_length=300)

    @field_validator("utr")
    @classmethod
    def _utr(cls, v):
        v = v.upper()
        if not UTR_RE.match(v):
            raise ValueError("Enter a valid UTR / transaction ID.")
        return v

    @model_validator(mode="after")
    def _purpose(self):
        if self.purpose == "pins" and self.pin_qty < 1:
            raise ValueError("Choose how many PINs to buy.")
        if self.purpose == "fund" and not self.amount:
            raise ValueError("Enter the amount you paid.")
        return self


class ReviewModel(Clean):
    action: Literal["approve", "reject"]
    remark: Optional[str] = Field(None, max_length=300)

    @model_validator(mode="after")
    def _remark(self):
        if self.action == "reject" and not self.remark:
            raise ValueError("Write the reason for rejecting.")
        return self


# ================================================================ money out
class WithdrawModel(Clean):
    amount: float = Field(gt=0)
    otp: str = Field(min_length=6, max_length=6)


class WithdrawReviewModel(Clean):
    action: Literal["paid", "reject"]
    utr: Optional[str] = None
    remark: Optional[str] = Field(None, max_length=300)

    @model_validator(mode="after")
    def _check(self):
        if self.action == "paid":
            if not self.utr or not UTR_RE.match(self.utr.upper()):
                raise ValueError("Enter the bank UTR of the payment.")
            self.utr = self.utr.upper()
        if self.action == "reject" and not self.remark:
            raise ValueError("Write the reason for rejecting.")
        return self


class P2PModel(Clean):
    to_code: str
    amount: float = Field(gt=0)
    otp: str = Field(min_length=6, max_length=6)

    _c = field_validator("to_code")(_member_code)


class AdminDebitModel(Clean):
    user_code: str
    wallet: Literal["income", "fund", "product"]
    amount: float = Field(gt=0)
    comment: str = Field(min_length=3, max_length=300)

    _c = field_validator("user_code")(_member_code)


# ================================================================ PINs
class PinTransferModel(Clean):
    to_code: str
    qty: int = Field(ge=1, le=500)

    _c = field_validator("to_code")(_member_code)


class PinActivateModel(Clean):
    user_code: str

    _c = field_validator("user_code")(_member_code)


class PinBuyModel(Clean):
    qty: int = Field(ge=1, le=500)


class AdminPinIssueModel(Clean):
    user_code: str
    qty: int = Field(ge=1, le=500)
    remark: str = Field(min_length=3, max_length=300)

    _c = field_validator("user_code")(_member_code)


# ================================================================ complaints
class ComplaintModel(Clean):
    category_id: int
    incident: Dict[str, Any] = {}
    suspect: Dict[str, Any] = {}
    complainant: Dict[str, Any] = {}
    share_with_agencies: bool = False
    submit: bool = False

    @field_validator("incident", "suspect", "complainant")
    @classmethod
    def _small_flat(cls, v):
        """Form answers only: at most 30 fields of short text / number / yes-no (no huge or nested JSON)."""
        if len(v) > 30:
            raise ValueError("Too many fields.")
        for k, x in v.items():
            if len(str(k)) > 40 or not (x is None or isinstance(x, (str, int, float, bool))):
                raise ValueError("Each answer must be short text, a number or yes / no.")
            if isinstance(x, str) and len(x) > 2000:
                raise ValueError("An answer is too long (2,000 letters at most).")
        return v


class ComplaintStatusModel(Clean):
    status: Literal["Submitted", "In Process", "Resolved", "Closed", "Rejected"]
    remarks: Optional[str] = Field(None, max_length=1000)


class CategoryModel(Clean):
    name: str = Field(min_length=2, max_length=80)
    is_active: bool = True


# ================================================================ admin
class SettingModel(BaseModel):
    key: str = Field(min_length=2, max_length=60)
    value: Any


class VideoItemModel(Clean):
    slot: int = Field(ge=1, le=10)
    platform: Literal["youtube", "facebook", "instagram"]
    url: str = Field(min_length=10, max_length=500)
    title: str = Field(min_length=2, max_length=120)


class DayVideosModel(BaseModel):
    videos: List[VideoItemModel] = Field(min_length=1, max_length=10)


class GullakDepositModel(Clean):
    amount: float = Field(gt=0, le=10_000_000)


class ReverseModel(Clean):
    ledger_id: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=300)


class AdminCreateModel(Clean):
    full_name: str = Field(min_length=2, max_length=80)
    mobile: str
    email: EmailStr
    password: str

    _pw = field_validator("password")(_password)

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, v):
        if not MOBILE_RE.match(v):
            raise ValueError("Enter a valid 10 digit mobile number.")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        return v.lower()


class MemberStatusModel(Clean):
    action: Literal["block", "unblock"]
    remark: str = Field(min_length=3, max_length=300)


class AdminRemarkModel(Clean):
    remark: str = Field(min_length=3, max_length=300)


class AdminMobileModel(AdminRemarkModel):
    mobile: str

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, v):
        if not MOBILE_RE.match(v):
            raise ValueError("Enter a valid 10 digit mobile number.")
        return v


class AdminEmailOtpModel(Clean):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        return v.lower()


class AdminEmailModel(AdminRemarkModel, AdminEmailOtpModel):
    otp: str = Field(min_length=6, max_length=6)


class LinkResetModel(Clean):
    token: str = Field(min_length=20, max_length=100)
    new_password: str

    _pw = field_validator("new_password")(_password)


class IdsModel(BaseModel):
    ids: List[int] = Field(min_length=1, max_length=200)


class DevOtpGapModel(Clean):
    # Plain text, not EmailStr: the e2e admin's own address (admin@e2e.local) is a "reserved" domain (FE #6).
    email: str = Field(min_length=3, max_length=120)
    purpose: str = Field(min_length=2, max_length=30)


class DevEmailModel(Clean):
    email: str = Field(min_length=3, max_length=120)


class DevCodeModel(Clean):
    user_code: str

    _c = field_validator("user_code")(_member_code)



# ================================================================ shop
class OrderItemModel(BaseModel):
    product_id: int = Field(gt=0)
    qty: int = Field(ge=1, le=50)


class OrderUtrModel(Clean):
    courier_utr: str

    @field_validator("courier_utr")
    @classmethod
    def _utr(cls, v):
        v = v.upper()
        if not UTR_RE.match(v):
            raise ValueError("Enter a valid UTR / transaction ID.")
        return v


class OrderFormModel(OrderUtrModel):
    items: List[OrderItemModel]
    prescription_id: Optional[int] = Field(None, gt=0, description="'Order this medicine': products must be on it")
    name: str = Field(min_length=2, max_length=80)
    mobile: str
    address: str = Field(min_length=10, max_length=300)
    pincode: str

    @field_validator("items", mode="before")
    @classmethod
    def _items(cls, v):
        import json
        items = json.loads(v) if isinstance(v, str) else v
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValueError("Add 1 to 20 products.")
        if len({i.get("product_id") for i in items}) != len(items):
            raise ValueError("Each product once (use qty).")
        return items

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, v):
        if not MOBILE_RE.match(v):
            raise ValueError("Enter a valid 10 digit mobile number.")
        return v

    @field_validator("pincode")
    @classmethod
    def _pin(cls, v):
        if not re.fullmatch(r"[1-9]\d{5}", v):
            raise ValueError("Enter a valid 6 digit PIN code.")
        return v


class OrderCancelModel(Clean):
    reason: Optional[str] = Field(None, max_length=300)


class OrderStatusModel(Clean):
    status: Literal["packed", "shipped", "delivered", "cancelled"]
    remark: Optional[str] = Field(None, max_length=300)
    courier_name: Optional[str] = Field(None, max_length=60)
    tracking_no: Optional[str] = Field(None, max_length=60)

    @model_validator(mode="after")
    def _remark(self):
        if self.status == "cancelled" and not self.remark:
            raise ValueError("Write the reason for cancelling.")
        return self


class CourierReviewModel(ReviewModel):
    pass


class CourierRateModel(BaseModel):
    rate: Optional[float] = Field(None, ge=0, le=5000, description="₹; null = use the state rate")


class ProductFormModel(Clean):
    name: str = Field(min_length=2, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    category: Optional[str] = Field(None, max_length=60)
    mrp: str
    stock: int = Field(0, ge=0, le=1_000_000)
    is_active: bool = True


class ProductPatchModel(Clean):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    category: Optional[str] = Field(None, max_length=60)
    mrp: Optional[str] = None
    is_active: Optional[bool] = None
    stock_add: Optional[int] = Field(None, ge=-1_000_000, le=1_000_000)
    keep_images: Optional[List[str]] = None

    @field_validator("keep_images", mode="before")
    @classmethod
    def _keep(cls, v):
        import json
        return json.loads(v) if isinstance(v, str) else v



# ================================================================ doctor
class DoctorCreateModel(AdminCreateModel):
    bams_reg_no: str = Field(min_length=3, max_length=40, description="Shown on every prescription")


class DoctorAssignModel(Clean):
    doctor_code: str
    remark: Optional[str] = Field(None, max_length=300)

    _c = field_validator("doctor_code")(_member_code)


class DoctorReassignModel(Clean):
    to_doctor: Optional[str] = Field(None, description="Doctor ID; empty = spread over active doctors")
    remark: str = Field(min_length=3, max_length=300)

    @field_validator("to_doctor")
    @classmethod
    def _to(cls, v):
        return _member_code(v) if v else None


class MedicineModel(Clean):
    product_id: Optional[int] = Field(None, gt=0, description="A shop product; empty = free text name")
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    dosage: str = Field(min_length=2, max_length=200, description="e.g. morning and evening, after food")
    days: int = Field(ge=1, le=365)

    @model_validator(mode="after")
    def _one(self):
        if not self.product_id and not self.name:
            raise ValueError("Choose a shop medicine or write its name.")
        return self


class PrescriptionModel(Clean):
    advice: Optional[str] = Field(None, max_length=2000)
    medicines: List[MedicineModel] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _some(self):
        if not self.medicines and not self.advice:
            raise ValueError("Write a medicine or some advice.")
        return self



# ================================================================ content
class BannerFormModel(Clean):
    title: str = Field(min_length=2, max_length=120)
    audience: Literal["everyone", "login"] = "everyone"
    starts_on: Optional[date] = None
    ends_on: Optional[date] = None
    is_active: bool = True
    sort_order: int = Field(0, ge=0, le=1000)
    link_url: Optional[str] = Field(None, max_length=300)

    @field_validator("starts_on", "ends_on", mode="before")
    @classmethod
    def _no_date(cls, v):
        return v or None              # an empty form field = no date (the edit form clears it this way)

    @field_validator("link_url")
    @classmethod
    def _url(cls, v):
        if v and not re.match(r"^(https://|/)", v):
            raise ValueError("Use an https:// link or an app path like /shop.")
        return v or None


class BannerPatchModel(BannerFormModel):
    title: Optional[str] = Field(None, min_length=2, max_length=120)
    audience: Optional[Literal["everyone", "login"]] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = Field(None, ge=0, le=1000)


class ContactModel(Clean):
    name: str = Field(min_length=2, max_length=80)
    mobile: str
    message: str = Field(min_length=5, max_length=1000)
    website: Optional[str] = Field(None, max_length=200, description="Leave empty (spam trap)")

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, v):
        if not MOBILE_RE.match(v):
            raise ValueError("Enter a valid 10 digit mobile number.")
        return v


class ContactRepliedModel(Clean):
    note: Optional[str] = Field(None, max_length=300)
