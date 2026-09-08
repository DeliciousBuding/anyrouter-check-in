from checkin import _failure_retry_hours, get_check_in_status
from utils.retry import FailureKind


class FakeResponse:
	def __init__(self, status_code=200, payload=None):
		self.status_code = status_code
		self._payload = payload or {}

	def json(self):
		return self._payload


class FakeClient:
	def __init__(self, response):
		self.response = response

	def get(self, url, headers, timeout):
		return self.response


def test_check_in_status_reads_checked_in_today():
	client = FakeClient(FakeResponse(payload={'success': True, 'data': {'stats': {'checked_in_today': True}}}))

	assert get_check_in_status(client, {}, 'https://example.com/api/user/checkin?month=2026-09') is True


def test_check_in_status_returns_none_for_invalid_payload():
	client = FakeClient(FakeResponse(payload={'success': True, 'data': {'stats': {}}}))

	assert get_check_in_status(client, {}, 'https://example.com/api/user/checkin?month=2026-09') is None


def test_check_in_status_does_not_break_on_http_error():
	client = FakeClient(FakeResponse(status_code=503))

	assert get_check_in_status(client, {}, 'https://example.com/api/user/checkin?month=2026-09') is None


def test_failure_backoff_is_capped_for_daily_recovery(monkeypatch):
	monkeypatch.delenv('CHECKIN_MAX_BACKOFF_HOURS', raising=False)
	assert _failure_retry_hours(FailureKind.AUTH_INVALID) == 6.0
	assert _failure_retry_hours(FailureKind.MANUAL_REQUIRED) == 6.0

	monkeypatch.setenv('CHECKIN_MAX_BACKOFF_HOURS', '2')
	assert _failure_retry_hours(FailureKind.AUTH_INVALID) == 2.0
