import pytest

import checkin
from utils.egress import EgressController, EgressRotator
from utils.retry import FailureKind, LoginFlowError


class FakeProvider:
	use_proxy = True


class FakeController(EgressController):
	def __init__(self):
		super().__init__('http://127.0.0.1:1', 'secret')
		self.selected = []

	async def current_node(self):
		return 'node-a'

	async def select_stable_node(self, account_key, excluded=None):
		self.selected.append('node-b')
		return 'node-b'


@pytest.mark.asyncio
async def test_login_with_retry_rotates_egress_after_waf(monkeypatch):
	monkeypatch.setenv('CHECKIN_EGRESS_RETRY_DELAY_SECONDS', '0')
	calls = 0

	async def fake_login(*args, **kwargs):
		nonlocal calls
		calls += 1
		if calls == 1:
			raise LoginFlowError(FailureKind.WAF_CHALLENGE, 'blocked')
		return 'login-ok'

	monkeypatch.setattr(checkin, 'login_with_credentials', fake_login)
	rotator = EgressRotator(FakeController(), max_rotations=2)
	result = await checkin.login_with_retry('account', FakeProvider(), 'agentrouter', 'u', 'p', rotator)
	assert result is not None
	assert calls == 2
	assert rotator.rotations == 1


@pytest.mark.asyncio
async def test_login_with_retry_does_not_rotate_for_auth_failure(monkeypatch):
	async def fake_login(*args, **kwargs):
		raise LoginFlowError(FailureKind.AUTH_INVALID, 'bad password')

	monkeypatch.setattr(checkin, 'login_with_credentials', fake_login)
	rotator = EgressRotator(FakeController(), max_rotations=2)
	with pytest.raises(LoginFlowError):
		await checkin.login_with_retry('account', FakeProvider(), 'agentrouter', 'u', 'p', rotator)
	assert rotator.rotations == 0
