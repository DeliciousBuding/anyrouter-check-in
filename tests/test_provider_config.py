import json

from utils.config import AccountConfig, AppConfig, ProviderConfig


def test_builtin_provider_profile_persistence_defaults(monkeypatch):
	monkeypatch.delenv('PROVIDERS', raising=False)

	config = AppConfig.load_from_env()

	assert config.providers['anyrouter'].persist_profile is True
	assert config.providers['anyrouter'].check_in_status_path == '/api/user/checkin'
	assert config.providers['agentrouter'].persist_profile is False
	assert config.providers['agentrouter'].check_in_status_path is None
	assert config.providers['agentrouter'].daily_success_cooldown_hours == 6.0
	assert config.providers['agentrouter'].daily_success_timezone == 'UTC'


def test_unnamed_account_fallback_includes_provider():
	account = AccountConfig(cookies={'session': 'abc'}, provider='anyrouter')

	identity = account.get_identity(2)

	assert identity['name'] == 'Account 3 (anyrouter)'
	assert identity['label'] == 'Account 3 (anyrouter)'


def test_named_account_identity_prefers_name_and_email():
	account = AccountConfig(cookies=None, provider='anyrouter', name='sample-user', email='d@example.com')

	identity = account.get_identity(0)

	assert identity['name'] == 'sample-user'
	assert identity['email'] == 'd@example.com'
	assert identity['label'] == 'sample-user（d@example.com）'


def test_provider_profile_persistence_can_override_builtin(monkeypatch):
	monkeypatch.setenv(
		'PROVIDERS',
		json.dumps(
			{
				'anyrouter': {'domain': 'https://anyrouter.top', 'persist_profile': False},
				'agentrouter': {'domain': 'https://agentrouter.org', 'persist_profile': True},
			}
		),
	)

	config = AppConfig.load_from_env()

	assert config.providers['anyrouter'].persist_profile is False
	assert config.providers['agentrouter'].persist_profile is True


def test_custom_provider_profile_persistence_defaults_to_false(monkeypatch):
	monkeypatch.setenv('PROVIDERS', json.dumps({'custom': {'domain': 'https://custom.example.com'}}))

	config = AppConfig.load_from_env()

	assert config.providers['custom'].persist_profile is False


def test_provider_from_dict_inherits_profile_persistence_from_defaults():
	defaults = ProviderConfig(name='custom', domain='https://old.example.com', persist_profile=True)

	provider = ProviderConfig.from_dict(
		'custom',
		{'domain': 'https://new.example.com'},
		defaults=defaults,
	)

	assert provider.persist_profile is True


def test_state_key_is_stable_and_does_not_expose_email():
	account = AccountConfig(cookies=None, provider='agentrouter', email='sample@example.com', password='secret')

	key = account.get_state_key(0)

	assert key.startswith('agentrouter:')
	assert 'sample@example.com' not in key
	assert key == account.get_state_key(1)


def test_state_key_uses_name_when_api_user_is_absent():
	first = AccountConfig(cookies={'session': 'abc'}, provider='anyrouter', name='primary')
	second = AccountConfig(cookies={'session': 'abc'}, provider='anyrouter', name='primary')

	assert first.get_state_key(0) == second.get_state_key(7)
	assert 'primary' not in first.get_state_key(0)
