from checkin import get_check_in_status


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
