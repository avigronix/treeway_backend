from html import escape

OTP_PURPOSE = {
    "reset": "reset your password",
    "withdraw": "confirm your withdrawal",
    "p2p": "confirm sending money",
    "signup": "create your Treeway ID",
    "admin_email": "confirm your new email (Treeway support is changing it for you)",
}

_BOX = ('<div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:24px;'
        'border:1px solid #e5e7eb;border-radius:12px">')


def otp_mail(name: str, code: str, purpose: str) -> tuple[str, str]:
    """(plain text, html). The name comes from the member, so it is escaped in the html."""
    action = OTP_PURPOSE.get(purpose, "continue")
    who = name or "Member"
    text = (f"Hello {who},\n\nYour OTP to {action} is: {code}\n\n"
            "This OTP works for 5 minutes. Do not share it with anyone, not even our team.\n\nTreeway")
    html = f"""
    {_BOX}
      <h2 style="color:#1E5E2A;margin:0 0 12px">Treeway</h2>
      <p>Hello {escape(who)},</p>
      <p>Your OTP to {action} is:</p>
      <p style="font-size:28px;font-weight:bold;letter-spacing:6px;color:#14361B">{code}</p>
      <p>This OTP works for 5 minutes. Do not share it with anyone, not even our team.</p>
    </div>
    """
    return text, html


def notice_mail(name: str, lines: list[str], link: str = "") -> tuple[str, str]:
    who = name or "Member"
    text = f"Hello {who},\n\n" + "\n".join(lines) + (f"\n\n{link}" if link else "") + "\n\nTreeway"
    body = "".join(f"<p>{escape(line)}</p>" for line in lines)
    button = (f'<p><a href="{escape(link)}" style="display:inline-block;background:#1E5E2A;color:#fff;'
              f'padding:10px 18px;border-radius:8px;text-decoration:none">Set a new password</a></p>'
              f'<p style="font-size:12px;color:#6b7280">{escape(link)}</p>') if link else ""
    html = f"""
    {_BOX}
      <h2 style="color:#1E5E2A;margin:0 0 12px">Treeway</h2>
      <p>Hello {escape(who)},</p>
      {body}{button}
    </div>
    """
    return text, html
