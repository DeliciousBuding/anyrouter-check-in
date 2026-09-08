import pytest

from utils.retry import (
	FailureKind,
	RetryAction,
	RetryPolicy,
	classify_failure,
	decide_retry,
)


@pytest.mark.parametrize(
	('message', 'expected'),
	[
		('Access Verification slide to complete', FailureKind.WAF_CHALLENGE),
		('acw_sc__v2 challenge page', FailureKind.WAF_CHALLENGE),
		('net::ERR_CONNECTION_RESET', FailureKind.TRANSIENT_NETWORK),
		('navigation interrupted by chrome-error://chromewebdata/', FailureKind.TRANSIENT_NETWORK),
		('HTTP 401 unauthorized', FailureKind.AUTH_INVALID),
		('session expired', FailureKind.AUTH_INVALID),
		('mihomo tunnel failed', FailureKind.PROXY_UNAVAILABLE),
		('HTTP 503 service unavailable', FailureKind.SITE_ERROR),
		('unexpected response', FailureKind.UNKNOWN),
	],
)
def test_classify_failure(message, expected):
	assert classify_failure(message) == expected


def test_waf_rotates_egress_before_attempt_budget():
	decision = decide_retry(
		FailureKind.WAF_CHALLENGE,
		attempt=1,
		same_node_retries=0,
		egress_rotations=0,
		policy=RetryPolicy(),
	)
	assert decision.action == RetryAction.ROTATE_EGRESS


def test_transient_retries_same_node_once_then_rotates():
	policy = RetryPolicy()
	first = decide_retry(
		FailureKind.TRANSIENT_NETWORK,
		attempt=1,
		same_node_retries=0,
		egress_rotations=0,
		policy=policy,
	)
	second = decide_retry(
		FailureKind.TRANSIENT_NETWORK,
		attempt=2,
		same_node_retries=1,
		egress_rotations=0,
		policy=policy,
	)
	assert first.action == RetryAction.RETRY_SAME
	assert second.action == RetryAction.ROTATE_EGRESS


def test_auth_failure_stops_without_rotating():
	decision = decide_retry(
		FailureKind.AUTH_INVALID,
		attempt=1,
		same_node_retries=0,
		egress_rotations=0,
		policy=RetryPolicy(),
	)
	assert decision.action == RetryAction.STOP


def test_attempt_budget_stops_even_for_waf():
	decision = decide_retry(
		FailureKind.WAF_CHALLENGE,
		attempt=3,
		same_node_retries=0,
		egress_rotations=0,
		policy=RetryPolicy(max_attempts=3),
	)
	assert decision.action == RetryAction.STOP


def test_retry_policy_reads_env(monkeypatch):
	monkeypatch.setenv('CHECKIN_MAX_ATTEMPTS', '4')
	monkeypatch.setenv('CHECKIN_MAX_EGRESS_ROTATIONS', '3')
	monkeypatch.setenv('CHECKIN_TRANSIENT_RETRY_DELAY_SECONDS', '1.5')
	policy = RetryPolicy.from_env()
	assert policy.max_attempts == 4
	assert policy.max_egress_rotations == 3
	assert policy.transient_retry_delay_seconds == 1.5
