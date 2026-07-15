"""
AnyRouter 签到通知（简化版）
规则：
  - 每次签到 → 飞书 MetAPI 卡片（清爽，不占邮箱）
  - 邮件 → 仅首次成功/余额剧变/失败
  - 废弃渠道不配 secret
"""
import json
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import httpx

TZ_HKT = timezone(timedelta(hours=8))

# 余额变化 > $1 才发邮件提醒
BALANCE_CHANGE_EMAIL_THRESHOLD = 1.0

STATE_FILE = Path("notify_state.json")


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _today() -> str:
    return datetime.now(TZ_HKT).strftime("%Y-%m-%d")


def _should_email(first_success_sent: bool, balance_delta: float) -> bool:
    """仅首次成功或余额明显变化时发邮件。"""
    return (not first_success_sent) or abs(balance_delta) >= BALANCE_CHANGE_EMAIL_THRESHOLD


def _post_feishu(title: str, content_md: str, severity: str = "info") -> bool:
    """向 MetAPI bot 发卡片"""
    webhook = os.environ.get("FEISHU_WEBHOOK", "")
    if not webhook:
        return False
    colors = {
        "info": "blue",
        "warning": "yellow",
        "critical": "red",
        "success": "green",
    }
    template = colors.get(severity, "blue")
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {"template": template, "title": {"content": title, "tag": "plain_text"}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content_md}},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"MetAPI · GHA cron 每 6h  ·  {datetime.now(TZ_HKT).strftime('%Y-%m-%d %H:%M HKT')}",
                        }
                    ],
                },
            ],
        },
    }
    try:
        body = json.dumps(card, ensure_ascii=False).encode("utf-8")
        with httpx.Client(timeout=10) as c:
            r = c.post(webhook, content=body, headers={"Content-Type": "application/json; charset=utf-8"})
        if r.status_code >= 300:
            return False
        try:
            response = r.json()
        except ValueError:
            return False
        return response.get("StatusCode", 0) in (0, None) and response.get("code", 0) in (0, None)
    except Exception:
        return False


def _send_email(title: str, content_html: str) -> bool:
    user = os.environ.get("EMAIL_USER", "")
    pwd = os.environ.get("EMAIL_PASS", "")
    to = os.environ.get("EMAIL_TO", "delicious233@qq.com")
    sender = os.environ.get("EMAIL_SENDER", "") or user
    smtp_srv = os.environ.get("CUSTOM_SMTP_SERVER", "")
    if not user or not pwd:
        return False
    try:
        msg = MIMEText(content_html, "html", "utf-8")
        msg["From"] = f"AnyRouter Checkin <{sender}>"
        msg["To"] = to
        msg["Subject"] = title
        server = smtp_srv if smtp_srv else f"smtp.{user.split('@')[1]}"
        with smtplib.SMTP_SSL(server, 465) as s:
            s.login(user, pwd)
            s.send_message(msg)
        return True
    except Exception:
        return False


def smart_notify(results: list[dict[str, Any]]) -> dict[str, bool]:
    """
    results = [
      {
        "name": "anyrouter.top",
        "success": True,
        "balance": 3.14,
        "balance_delta": 0.23,
        "used": 1.86,
        "used_delta": 0.05,
        "reward": 0.14,
      },
      ...
    ]
    """
    state = _read_state()
    today = _today()
    first_success_sent = bool(state.get("success_email_sent", False))

    all_ok = all(r["success"] for r in results)
    total_balance = sum(r.get("balance", 0) for r in results)
    total_delta = sum(r.get("balance_delta", 0) for r in results)

    # ── 飞书卡片：每次发 ──────────────────────────────────────────────
    items_md = ""
    for r in results:
        status = "✅" if r["success"] else "❌"
        name = r["name"]
        bal = r.get("balance", 0)
        delta = r.get("balance_delta", 0)
        reward = r.get("reward", 0)
        delta_str = f"（{delta:+.2f}）" if delta != 0 else ""
        reward_str = f" +${reward:.2f}" if reward > 0 else ""
        items_md += f"{status} **{name}**：${bal:.2f}{delta_str}{reward_str}\n"

    sev = "success" if all_ok else "critical"
    title = f"签到完成 · {len([r for r in results if r['success']])}/{len(results)}"
    fs_ok = _post_feishu(title, items_md, severity=sev)

    # ── 邮件：按状态机 ────────────────────────────────────────────────
    email_sent = False
    if not all_ok:
        # 失败必须邮件通知
        email_sent = _send_email(
            f"[AnyRouter] 签到异常 · {today}",
            f"<h3>签到失败</h3><pre>{json.dumps(results, ensure_ascii=False, indent=2)}</pre>",
        )
    elif _should_email(first_success_sent, total_delta):
        email_sent = _send_email(
            f"[AnyRouter] 签到 · {today}  余额 ${total_balance:.2f}",
            f"<p>总余额：<b>${total_balance:.2f}</b>（{total_delta:+.2f}）</p>"
            + f"<p>时间：{datetime.now(TZ_HKT)}</p>",
        )

    # ── 更新状态（仅成功时）───────────────────────────────────────────
    if all_ok:
        # SMTP 失败时不能伪装成已投递；下次计划任务应继续重试首次成功邮件。
        if email_sent:
            state["success_email_sent"] = True
            state["last_email_day"] = today
        state["last_balance"] = total_balance
        _write_state(state)

    return {"feishu": fs_ok, "email": email_sent}
