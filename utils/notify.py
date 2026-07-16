"""
AnyRouter 签到通知（最佳实践）

飞书：
  - 失败立刻发（红卡）
  - 全正常：每日最多 1 条绿卡摘要
  - 总余额绝对变化 ≥ $1：立刻绿卡

邮件：
  - 首次成功
  - 余额绝对变化 ≥ $1
  - 失败
  - 收件人仅 EMAIL_TO（默认 delicious233@qq.com）
"""
from __future__ import annotations

import html
import json
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import httpx

TZ_HKT = timezone(timedelta(hours=8))
BALANCE_CHANGE_THRESHOLD = 1.0
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


def _now_hkt() -> str:
    return datetime.now(TZ_HKT).strftime("%Y-%m-%d %H:%M HKT")


def _should_email(first_success_sent: bool, balance_delta: float) -> bool:
    return (not first_success_sent) or balance_delta >= BALANCE_CHANGE_THRESHOLD


def _should_feishu_all_ok(
    *,
    last_feishu_day: str,
    today: str,
    balance_delta: float,
) -> bool:
    if balance_delta >= BALANCE_CHANGE_THRESHOLD:
        return True
    return last_feishu_day != today


def _email_kind(first_success_sent: bool, balance_delta: float, all_ok: bool) -> str:
    if not all_ok:
        return "failure"
    if not first_success_sent:
        return "first_success"
    if balance_delta >= BALANCE_CHANGE_THRESHOLD:
        return "balance_change"
    return "none"


def _feishu_footer(kind: str) -> str:
    label = {
        "daily": "每日汇总",
        "balance_change": "余额变化",
        "failure": "失败告警",
        "first_success": "首次成功",
    }.get(kind, "通知")
    return f"MetAPI · {label}  ·  {_now_hkt()}"


def _post_feishu(title: str, content_md: str, *, severity: str = "info", footer_kind: str = "daily") -> bool:
    webhook = os.environ.get("FEISHU_WEBHOOK", "")
    if not webhook:
        return False
    colors = {
        "info": "blue",
        "warning": "yellow",
        "critical": "red",
        "success": "green",
        "resolved": "green",
    }
    template = colors.get(severity, "blue")
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": template,
                "title": {"content": title, "tag": "plain_text"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content_md}},
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": _feishu_footer(footer_kind)}],
                },
            ],
        },
    }
    try:
        body = json.dumps(card, ensure_ascii=False).encode("utf-8")
        with httpx.Client(timeout=10) as client:
            response = client.post(
                webhook,
                content=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
        if response.status_code >= 300:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        return payload.get("StatusCode", 0) in (0, None) and payload.get("code", 0) in (0, None)
    except Exception:
        return False


def _send_email(subject: str, content_html: str) -> bool:
    user = os.environ.get("EMAIL_USER", "")
    password = os.environ.get("EMAIL_PASS", "")
    to = os.environ.get("EMAIL_TO", "delicious233@qq.com")
    sender = os.environ.get("EMAIL_SENDER", "") or user
    smtp_server = os.environ.get("CUSTOM_SMTP_SERVER", "")
    if not user or not password:
        return False
    try:
        message = MIMEText(content_html, "html", "utf-8")
        message["From"] = f"AnyRouter Checkin <{sender}>"
        message["To"] = to
        message["Subject"] = subject
        server = smtp_server if smtp_server else f"smtp.{user.split('@')[1]}"
        with smtplib.SMTP_SSL(server, 465) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)
        return True
    except Exception:
        return False


def _email_subject(kind: str, today: str, total_balance: float) -> str:
    if kind == "failure":
        return f"[AnyRouter] 签到失败 · {today}"
    if kind == "first_success":
        return f"[AnyRouter] 首次成功 · {today} · 余额 ${total_balance:.2f}"
    if kind == "balance_change":
        return f"[AnyRouter] 余额变化 · {today} · ${total_balance:.2f}"
    return f"[AnyRouter] 签到 · {today}"


def _account_label(item: dict[str, Any]) -> str:
    """Prefer explicit label; otherwise name + email."""
    label = str(item.get("label") or "").strip()
    if label:
        return label
    name = str(item.get("name") or "").strip() or "?"
    email = str(item.get("email") or "").strip()
    if email and email not in name:
        return f"{name}（{email}）"
    return email or name


def _failed_accounts(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in results if not item.get("success")]


def _email_body(
    *,
    kind: str,
    results: list[dict[str, Any]],
    ok_count: int,
    total_balance: float,
    total_delta: float,
) -> str:
    rows = []
    for item in results:
        status = "成功" if item.get("success") else "失败"
        label = html.escape(_account_label(item))
        email = html.escape(str(item.get("email") or ""))
        bal = float(item.get("balance", 0) or 0)
        delta = float(item.get("balance_delta", 0) or 0)
        rows.append(
            f"<tr><td>{label}</td><td>{email or '-'}</td><td>{status}</td>"
            f"<td>${bal:.2f}</td><td>{delta:+.2f}</td></tr>"
        )
    table = (
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<tr><th>账号</th><th>邮箱</th><th>状态</th><th>余额</th><th>变化</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    failed = _failed_accounts(results)
    failed_block = ""
    if kind == "failure" and failed:
        items = "".join(
            f"<li><b>{html.escape(_account_label(item))}</b>"
            + (f" / {html.escape(str(item.get('email')))}" if item.get("email") and str(item.get("email")) not in _account_label(item) else "")
            + "</li>"
            for item in failed
        )
        failed_block = f"<p><b>失败账号（请优先处理）</b></p><ul>{items}</ul>"
        headline = f"签到失败 {len(failed)} 个账号，请立即检查。"
    elif kind == "first_success":
        headline = "首次成功签到邮件，用于确认通知链路可用。"
    else:
        headline = "账户总余额发生明显变化。"
    return (
        f"<p><b>{html.escape(headline)}</b></p>"
        f"{failed_block}"
        f"<p>成功：{ok_count}/{len(results)}　总余额：<b>${total_balance:.2f}</b>（{total_delta:+.2f}）</p>"
        f"<p>时间：{_now_hkt()}</p>"
        f"{table}"
    )


def smart_notify(results: list[dict[str, Any]]) -> dict[str, bool]:
    state = _read_state()
    today = _today()
    first_success_sent = bool(state.get("success_email_sent", False))
    last_feishu_day = str(state.get("last_feishu_day", "") or "")

    all_ok = all(bool(item.get("success")) for item in results)
    ok_count = sum(1 for item in results if item.get("success"))
    total_balance = sum(float(item.get("balance", 0) or 0) for item in results)
    total_delta = sum(float(item.get("balance_delta", 0) or 0) for item in results)
    # Only earned balance is material for an immediate notification. Normal API
    # consumption decreases quota frequently and must not turn a 6-hour check-in
    # into a 6-hour "balance change" alert.
    balance_increase = sum(
        max(float(item.get("balance_delta", 0) or 0), 0.0) for item in results
    )

    feishu_sent = False
    if not all_ok:
        failed = _failed_accounts(results)
        failed_lines = []
        for item in failed:
            label = _account_label(item)
            email = str(item.get("email") or "").strip()
            extra = f"\n   邮箱：{email}" if email and email not in label else ""
            failed_lines.append(f"❌ **{label}**{extra}")
        other_lines = []
        for item in results:
            if not item.get("success"):
                continue
            bal = float(item.get("balance", 0) or 0)
            other_lines.append(f"✅ {_account_label(item)}：${bal:.2f}")
        body = (
            f"**失败账号（{len(failed)}）**：\n"
            + "\n".join(failed_lines)
            + f"\n\n**成功**：{ok_count} / {len(results)}\n"
            f"**总余额**：${total_balance:.2f}"
        )
        if other_lines:
            body += "\n\n**其余账号**：\n" + "\n".join(other_lines)
        feishu_sent = _post_feishu(
            f"🔴 签到异常 · 失败 {len(failed)}/{len(results)}",
            body,
            severity="critical",
            footer_kind="failure",
        )
        if feishu_sent:
            state["last_feishu_day"] = today
    elif _should_feishu_all_ok(
        last_feishu_day=last_feishu_day,
        today=today,
        balance_delta=balance_increase,
    ):
        footer_kind = (
            "balance_change"
            if balance_increase >= BALANCE_CHANGE_THRESHOLD
            else "daily"
        )
        body = (
            f"**成功**：{ok_count} / {len(results)}\n"
            f"**总余额**：${total_balance:.2f}\n"
            f"**较上次**：{total_delta:+.2f}"
        )
        if balance_increase >= BALANCE_CHANGE_THRESHOLD:
            changed = []
            for item in results:
                delta = float(item.get("balance_delta", 0) or 0)
                if delta >= BALANCE_CHANGE_THRESHOLD:
                    bal = float(item.get("balance", 0) or 0)
                    changed.append(
                        f"• **{_account_label(item)}**：${bal:.2f}（{delta:+.2f}）"
                    )
            if changed:
                body += "\n\n" + "\n".join(changed)
        feishu_sent = _post_feishu(
            f"🟢 签到正常 · {ok_count}/{len(results)}",
            body,
            severity="success",
            footer_kind=footer_kind,
        )
        if feishu_sent:
            state["last_feishu_day"] = today

    email_sent = False
    kind = _email_kind(first_success_sent, balance_increase, all_ok)
    if kind != "none":
        email_sent = _send_email(
            _email_subject(kind, today, total_balance),
            _email_body(
                kind=kind,
                results=results,
                ok_count=ok_count,
                total_balance=total_balance,
                total_delta=balance_increase if kind == "balance_change" else total_delta,
            ),
        )

    if all_ok:
        # Only mark first-success delivered after SMTP succeeds; otherwise retry next run.
        if kind == "first_success" and email_sent:
            state["success_email_sent"] = True
            state["last_email_day"] = today
        elif kind == "balance_change" and email_sent:
            state["last_email_day"] = today
        state["last_balance"] = total_balance
        _write_state(state)
    elif feishu_sent or email_sent:
        _write_state(state)

    return {"feishu": feishu_sent, "email": email_sent}
