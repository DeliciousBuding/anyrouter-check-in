from datetime import datetime, timedelta, timezone

from utils.state import CheckinStateStore


def test_state_roundtrip_keeps_only_non_sensitive_values(tmp_path):
	path = tmp_path / 'checkin_state.json'
	store = CheckinStateStore.load(path)
	store.mark_success('agentrouter:abc123', quota=12.5, used=1.25, bonus=0.5)
	store.save()

	loaded = CheckinStateStore.load(path)
	entry = loaded.account('agentrouter:abc123')

	assert entry['last_quota'] == 12.5
	assert entry['last_used'] == 1.25
	assert entry['last_bonus'] == 0.5
	assert 'email' not in entry
	assert 'password' not in entry
	assert 'cookies' not in entry


def test_daily_success_gate_and_force(tmp_path):
	now = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)
	store = CheckinStateStore.load(tmp_path / 'state.json')
	store.mark_success('agentrouter:abc', now=now)

	assert store.skip_reason('agentrouter:abc', daily_success_cooldown_hours=20, now=now + timedelta(hours=6))
	assert (
		store.skip_reason('agentrouter:abc', daily_success_cooldown_hours=20, now=now + timedelta(hours=6), force=True)
		is None
	)
	assert store.skip_reason('agentrouter:abc', daily_success_cooldown_hours=20, now=now + timedelta(hours=21)) is None


def test_failure_backoff_blocks_immediate_retry(tmp_path):
	now = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)
	store = CheckinStateStore.load(tmp_path / 'state.json')
	store.mark_failure('agentrouter:abc', 'rate_limited', retry_after_hours=6, now=now)

	assert store.skip_reason('agentrouter:abc', now=now + timedelta(hours=1))
	assert store.skip_reason('agentrouter:abc', now=now + timedelta(hours=7)) is None


def test_last_balance_is_available_for_skipped_accounts(tmp_path):
	store = CheckinStateStore.load(tmp_path / 'state.json')
	store.mark_success('anyrouter:abc', quota=9.0, used=2.0, bonus=1.0)

	assert store.last_balance('anyrouter:abc') == {'quota': 9.0, 'used': 2.0, 'bonus': 1.0}


def test_last_balance_keeps_bonus_when_quota_fields_are_absent(tmp_path):
	store = CheckinStateStore.load(tmp_path / 'state.json')
	store.mark_success('anyrouter:abc', bonus=1.5)

	assert store.last_balance('anyrouter:abc') == {'quota': 0.0, 'used': 0.0, 'bonus': 1.5}


def test_daily_success_gate_uses_site_calendar_day(tmp_path):
	last_success = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)  # 23:00 HKT
	store = CheckinStateStore.load(tmp_path / 'state.json')
	store.mark_success('agentrouter:abc', now=last_success)

	assert store.successful_today(
		'agentrouter:abc',
		timezone_name='UTC',
		now=last_success + timedelta(hours=7),
	)
	assert not store.successful_today(
		'agentrouter:abc',
		timezone_name='UTC',
		now=last_success + timedelta(hours=13),
	)


def test_daily_success_allows_next_site_day_but_keeps_min_interval(tmp_path):
	last_success = datetime(2026, 9, 8, 23, 30, tzinfo=timezone.utc)
	store = CheckinStateStore.load(tmp_path / 'state.json')
	store.mark_success('agentrouter:abc', now=last_success)

	assert store.skip_reason(
		'agentrouter:abc',
		daily_success_cooldown_hours=6,
		daily_success_timezone='UTC',
		now=last_success + timedelta(hours=1.5),
	)
	assert (
		store.skip_reason(
			'agentrouter:abc',
			daily_success_cooldown_hours=6,
			daily_success_timezone='UTC',
			now=last_success + timedelta(hours=6),
		)
		is None
	)


def test_zero_daily_cooldown_does_not_block_same_day_status_checks(tmp_path):
	now = datetime(2026, 9, 9, 1, 0, tzinfo=timezone.utc)
	store = CheckinStateStore.load(tmp_path / 'state.json')
	store.mark_success('anyrouter:abc', now=now)

	assert (
		store.skip_reason(
			'anyrouter:abc',
			daily_success_cooldown_hours=0,
			daily_success_timezone='UTC',
			now=now + timedelta(hours=1),
		)
		is None
	)
