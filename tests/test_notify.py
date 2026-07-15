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


def _result(*, success=True, balance=3.0, delta=0.0):
    return {
        "name": "anyrouter.top",
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
    assert notify._should_email(True, notify.BALANCE_CHANGE_EMAIL_THRESHOLD) is True


def test_post_feishu_requires_business_success(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK", "https://example.invalid/hook")
    response = MagicMock(status_code=200)
    response.json.return_value = {"StatusCode": 0, "code": 0}
    client = MagicMock()
    client.post.return_value = response
    client_context = MagicMock()
    client_context.__enter__.return_value = client
    monkeypatch.setattr(notify.httpx, "Client", MagicMock(return_value=client_context))

    assert notify._post_feishu("ok", "body") is True

    response.json.return_value = {"code": 9499, "msg": "invalid param"}
    assert notify._post_feishu("bad", "body") is False


def test_first_success_email_is_persisted_and_not_repeated(monkeypatch, isolated_state):
    monkeypatch.setattr(notify, "_post_feishu", MagicMock(return_value=True))
    send_email = MagicMock(return_value=True)
    monkeypatch.setattr(notify, "_send_email", send_email)

    first = notify.smart_notify([_result()])
    second = notify.smart_notify([_result()])

    assert first == {"feishu": True, "email": True}
    assert second == {"feishu": True, "email": False}
    assert send_email.call_count == 1
    state = json.loads(isolated_state.read_text(encoding="utf-8"))
    assert state["success_email_sent"] is True


def test_failed_first_success_email_is_retried(monkeypatch, isolated_state):
    monkeypatch.setattr(notify, "_post_feishu", MagicMock(return_value=True))
    send_email = MagicMock(return_value=False)
    monkeypatch.setattr(notify, "_send_email", send_email)

    notify.smart_notify([_result()])
    notify.smart_notify([_result()])

    assert send_email.call_count == 2
    state = json.loads(isolated_state.read_text(encoding="utf-8"))
    assert state.get("success_email_sent", False) is False


def test_failure_always_sends_email(monkeypatch, isolated_state):
    isolated_state.write_text(
        json.dumps({"success_email_sent": True, "last_balance": 3.0}),
        encoding="utf-8",
    )
    monkeypatch.setattr(notify, "_post_feishu", MagicMock(return_value=True))
    send_email = MagicMock(return_value=True)
    monkeypatch.setattr(notify, "_send_email", send_email)

    result = notify.smart_notify([_result(success=False)])

    assert result == {"feishu": True, "email": True}
    assert send_email.call_count == 1


def test_large_balance_change_sends_email_after_first_success(monkeypatch, isolated_state):
    isolated_state.write_text(
        json.dumps({"success_email_sent": True, "last_balance": 3.0}),
        encoding="utf-8",
    )
    monkeypatch.setattr(notify, "_post_feishu", MagicMock(return_value=True))
    send_email = MagicMock(return_value=True)
    monkeypatch.setattr(notify, "_send_email", send_email)

    result = notify.smart_notify(
        [_result(balance=4.5, delta=notify.BALANCE_CHANGE_EMAIL_THRESHOLD)]
    )

    assert result == {"feishu": True, "email": True}
    assert send_email.call_count == 1
