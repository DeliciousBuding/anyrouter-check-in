import pytest

import checkin


class FakeAccount:
	provider = 'anyrouter'

	def get_state_key(self, index):
		return f'anyrouter:{index + 1}'

	def get_log_label(self, index):
		return f'anyrouter-{index + 1}'

	def get_identity(self, index):
		return {'name': f'account-{index + 1}', 'email': '', 'label': f'account-{index + 1}'}


class FakeAppConfig:
	providers: dict[str, object] = {}

	def get_provider(self, name):
		return None


async def test_main_persists_first_account_before_later_account_crashes(monkeypatch, tmp_path):
	accounts = [FakeAccount(), FakeAccount()]
	calls = 0
	state_path = tmp_path / 'checkin_state.json'

	async def fake_check_in_account(*args, **kwargs):
		nonlocal calls
		calls += 1
		if calls == 1:
			return (
				True,
				None,
				{
					'success': True,
					'quota': 1.0,
					'used_quota': 0.0,
					'bonus_quota': 0.0,
					'display': 'ok',
				},
				None,
			)
		raise KeyboardInterrupt

	monkeypatch.setenv('CHECKIN_STATE_FILE', str(state_path))
	monkeypatch.setattr(checkin, 'is_debug_enabled', lambda: False)
	monkeypatch.setattr(checkin.AppConfig, 'load_from_env', classmethod(lambda cls: FakeAppConfig()))
	monkeypatch.setattr(checkin, 'load_accounts_config', lambda: accounts)
	monkeypatch.setattr(checkin, 'load_balance_snapshot', lambda: None)
	monkeypatch.setattr(checkin, 'check_in_account', fake_check_in_account)

	with pytest.raises(KeyboardInterrupt):
		await checkin.main()

	state = checkin.CheckinStateStore.load(state_path)
	assert state.account('anyrouter:1')['last_success_at'] is not None
