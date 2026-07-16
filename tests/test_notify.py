import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from utils import notify


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    state_file = tmp_path / "notify_state.json"
    monkeypatch.setattr(notify, "STATE_FILE", state_file)
    return state_file


def _result(*, success=True, balance=3.0, delta=0.0, name="anyrouter.top"):
    return {
        "name": name,
        "success": success,
        "balance": balance,
        "balance_delta": delta,
        "used": 1.0,
        "used_delta": 0.0,
        "reward": 0.1,
    }


def test_should_email_only_first_success_or_large_change():
    assert notify._should_email(False, 0.0) is True
    assert notify._should_email(True, 0.0) is False
    assert notify._should_email(True, notify.BALANCE_CHANGE_THRESHOLD) is True


def test_email_kind_mapping():
    assert notify._email_kind(False, 0.0, True) == "first_success"
    assert notify._email_kind(True, 0.0, True) == "none"
    assert notify._email_kind(True, 1.5, True) == "balance_change"
    assert notify._email_kind(True, 0.0, False) == "failure"


def test_post_feishu_requires_business_success(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK", "https://example.invalid/hook")
    response = MagicMock(status_code=200)
    response.json.return_value = {"StatusCode": 0, "code": 0}
    client = MagicMock()
    client.post.return_value = response
    client_context = MagicMock()
    client_context.__enter__.return_value = client
    monkeypatch.setattr(notify.httpx, "Client", MagicMock(return_value=client_context))

    assert notify._post_feishu("ok", "body", severity="success", footer_kind="daily") is True
    payload = json.loads(client.post.call_args.kwargs["content"].decode("utf-8"))
    assert "每日汇总" in payload["card"]["elements"][1]["elements"][0]["content"]

    response.json.return_value = {"code": 9499, "msg": "invalid param"}
    assert notify._post_feishu("bad", "body") is False


def test_first_success_email_is_persisted_and_not_repeated(monkeypatch, isolated_state):
    monkeypatch.setattr(notify, "_post_feishu", MagicMock(return_value=True))
    send_email = MagicMock(return_value=True)
    monkeypatch.setattr(notify, "_send_email", send_email)

    first = notify.smart_notify([_result()])
    second = notify.smart_notify([_result()])

    assert first == {"feishu": True, "email": True}
    assert second == {"feishu": False, "email": False}
    assert send_email.call_count == 1
    subject = send_email.call_args.args[0]
    assert "首次成功" in subject
    state = json.loads(isolated_state.read_text(encoding="utf-8"))
    assert state["success_email_sent"] is True
    assert state["last_feishu_day"] == notify._today()


def test_failed_first_success_email_is_retried(monkeypatch, isolated_state):
    monkeypatch.setattr(notify, "_post_feishu", MagicMock(return_value=True))
    send_email = MagicMock(return_value=False)
    monkeypatch.setattr(notify, "_send_email", send_email)

    notify.smart_notify([_result()])
    notify.smart_notify([_result()])

    assert send_email.call_count == 2
    state = json.loads(isolated_state.read_text(encoding="utf-8"))
    assert state.get("success_email_sent", False) is False


def test_failure_always_sends_email_and_feishu(monkeypatch, isolated_state):
    isolated_state.write_text(
        json.dumps({"success_email_sent": True, "last_balance": 3.0, "last_feishu_day": notify._today()}),
        encoding="utf-8",
    )
    post_feishu = MagicMock(return_value=True)
    send_email = MagicMock(return_value=True)
    monkeypatch.setattr(notify, "_post_feishu", post_feishu)
    monkeypatch.setattr(notify, "_send_email", send_email)

    result = notify.smart_notify([_result(success=False)])

    assert result == {"feishu": True, "email": True}
    assert send_email.call_count == 1
    assert "签到失败" in send_email.call_args.args[0]
    assert post_feishu.call_args.kwargs["severity"] == "critical"
    assert post_feishu.call_args.kwargs["footer_kind"] == "failure"


def test_all_ok_feishu_is_daily_summary_not_every_run(monkeypatch, isolated_state):
    isolated_state.write_text(
        json.dumps(
            {
                "success_email_sent": True,
                "last_balance": 3.0,
                "last_feishu_day": notify._today(),
            }
        ),
        encoding="utf-8",
    )
    post_feishu = MagicMock(return_value=True)
    monkeypatch.setattr(notify, "_post_feishu", post_feishu)
    monkeypatch.setattr(notify, "_send_email", MagicMock(return_value=True))

    result = notify.smart_notify([_result(balance=3.0, delta=0.0)])

    assert result == {"feishu": False, "email": False}
    post_feishu.assert_not_called()


def test_all_ok_feishu_sends_on_new_day_or_large_balance_change(monkeypatch, isolated_state):
    isolated_state.write_text(
        json.dumps(
            {
                "success_email_sent": True,
                "last_balance": 3.0,
                "last_feishu_day": "2000-01-01",
            }
        ),
        encoding="utf-8",
    )
    post_feishu = MagicMock(return_value=True)
    send_email = MagicMock(return_value=True)
    monkeypatch.setattr(notify, "_post_feishu", post_feishu)
    monkeypatch.setattr(notify, "_send_email", send_email)

    daily = notify.smart_notify([_result(balance=3.0, delta=0.0)])
    assert daily == {"feishu": True, "email": False}
    args = post_feishu.call_args.args
    kwargs = post_feishu.call_args.kwargs
    title, body = args[0], args[1]
    assert "签到正常" in title
    assert "总余额" in body
    assert kwargs.get("severity") == "success"
    assert kwargs.get("footer_kind") == "daily"

    post_feishu.reset_mock()
    send_email.reset_mock()
    isolated_state.write_text(
        json.dumps(
            {
                "success_email_sent": True,
                "last_balance": 3.0,
                "last_feishu_day": notify._today(),
            }
        ),
        encoding="utf-8",
    )
    change = notify.smart_notify(
        [_result(balance=4.5, delta=notify.BALANCE_CHANGE_THRESHOLD)]
    )
    assert change == {"feishu": True, "email": True}
    assert post_feishu.call_args.kwargs.get("footer_kind") == "balance_change"
    assert "余额变化" in send_email.call_args.args[0]


def test_email_templates_are_operator_friendly():
    subject = notify._email_subject("failure", "2026-07-16", 10.0)
    body = notify._email_body(
        kind="failure",
        results=[_result(success=False, name="a1")],
        ok_count=0,
        total_balance=10.0,
        total_delta=0.0,
    )
    assert subject.startswith("[AnyRouter] 签到失败")
    assert "<table" in body
    assert "失败" in body
