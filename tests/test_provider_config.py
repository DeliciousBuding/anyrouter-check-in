import json

from utils.config import AccountConfig, AppConfig, ProviderConfig


def test_builtin_provider_profile_persistence_defaults(monkeypatch):
	monkeypatch.delenv('PROVIDERS', raising=False)

	config = AppConfig.load_from_env()

	assert config.providers['anyrouter'].persist_profile is True
	assert config.providers['agentrouter'].persist_profile is False


def test_unnamed_account_fallback_includes_provider():
	account = AccountConfig(cookies={'session': 'abc'}, provider='anyrouter')

	identity = account.get_identity(2)

	assert identity['name'] == 'Account 3 (anyrouter)'
	assert identity['label'] == 'Account 3 (anyrouter)'


def test_named_account_identity_prefers_name_and_email():
	account = AccountConfig(cookies=None, provider='anyrouter', name='delicious233', email='d@example.com')

	identity = account.get_identity(0)

	assert identity['name'] == 'delicious233'
	assert identity['email'] == 'd@example.com'
	assert identity['label'] == 'delicious233（d@example.com）'


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
