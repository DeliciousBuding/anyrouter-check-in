"""失败分类与有界重试策略。

公开仓日志纪律：这里只处理错误字符串和抽象动作，不接触节点名、订阅 URL
或账号凭据。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum


class FailureKind(str, Enum):
	"""登录流程的可操作失败类型。"""

	PROXY_UNAVAILABLE = 'proxy_unavailable'
	TRANSIENT_NETWORK = 'transient_network'
	WAF_CHALLENGE = 'waf_challenge'
	AUTH_INVALID = 'auth_invalid'
	RATE_LIMITED = 'rate_limited'
	SITE_ERROR = 'site_error'
	MANUAL_REQUIRED = 'manual_required'
	UNKNOWN = 'unknown'


class RetryAction(str, Enum):
	"""一次失败后应执行的动作。"""

	RETRY_SAME = 'retry_same'
	ROTATE_EGRESS = 'rotate_egress'
	STOP = 'stop'


@dataclass(frozen=True)
class LoginFlowError(Exception):
	"""带分类的登录流程异常。"""

	kind: FailureKind
	message: str

	def __str__(self) -> str:
		return f'{self.kind.value}: {self.message}'


@dataclass(frozen=True)
class RetryDecision:
	action: RetryAction
	delay_seconds: float
	reason: str


@dataclass(frozen=True)
class RetryPolicy:
	"""单账号重试预算。"""

	max_attempts: int = 4
	max_egress_rotations: int = 3
	transient_retry_delay_seconds: float = 5.0
	egress_retry_delay_seconds: float = 8.0

	@classmethod
	def from_env(cls) -> 'RetryPolicy':
		return cls(
			max_attempts=_env_int('CHECKIN_MAX_ATTEMPTS', cls.max_attempts),
			max_egress_rotations=_env_int('CHECKIN_MAX_EGRESS_ROTATIONS', cls.max_egress_rotations),
			transient_retry_delay_seconds=_env_float(
				'CHECKIN_TRANSIENT_RETRY_DELAY_SECONDS',
				cls.transient_retry_delay_seconds,
			),
			egress_retry_delay_seconds=_env_float(
				'CHECKIN_EGRESS_RETRY_DELAY_SECONDS',
				cls.egress_retry_delay_seconds,
			),
		)


_WAF_PATTERNS = (
	r'\bwaf\b',
	r'access verification',
	r'verify you are human',
	r'slide to complete',
	r'please slide',
	r'captcha',
	r'turnstile',
	r'challenge',
	r'acw_sc__v2',
	r'请进行验证',
	r'为了更好的访问体验',
	r'访问受限',
	r'access denied',
)
_RATE_LIMIT_PATTERNS = (
	r'\b429\b',
	r'too many',
	r'rate limit',
	r'ratelimit',
	r'请求.*频繁',
	r'操作.*频繁',
	r'尝试.*次数',
	r'请稍后',
	r'try again later',
	r'temporarily blocked',
)
_AUTH_PATTERNS = (
	r'\b401\b',
	r'unauthorized',
	r'invalid credentials',
	r'invalid password',
	r'password.*incorrect',
	r'login failed',
	r'not authenticated',
	r'session.*(expired|invalid)',
)
_PROXY_PATTERNS = (
	r'proxy.*(unavailable|not ready|refused|failed|error)',
	r'checkin_proxy_url',
	r'mihomo',
	r'tunnel',
	r'net::err_(proxy|tunnel)',
)
_SITE_PATTERNS = (
	r'\b5\d\d\b',
	r'bad gateway',
	r'service unavailable',
	r'gateway timeout',
	r'maintenance',
)
_TRANSIENT_PATTERNS = (
	r'timeout',
	r'timed out',
	r'connection reset',
	r'connection refused',
	r'net::err_',
	r'chrome-error',
	r'err_connection',
	r'econnreset',
	r'navigation.*interrupted',
	r'temporary',
)


def classify_failure(message: str) -> FailureKind:
	"""把异常/日志文本映射为稳定、可测试的失败类型。"""

	text = (message or '').strip()
	lower = text.lower()
	if _matches(lower, _WAF_PATTERNS):
		return FailureKind.WAF_CHALLENGE
	if _matches(lower, _RATE_LIMIT_PATTERNS):
		return FailureKind.RATE_LIMITED
	if _matches(lower, _AUTH_PATTERNS):
		return FailureKind.AUTH_INVALID
	if _matches(lower, _PROXY_PATTERNS):
		return FailureKind.PROXY_UNAVAILABLE
	if _matches(lower, _SITE_PATTERNS):
		return FailureKind.SITE_ERROR
	if _matches(lower, _TRANSIENT_PATTERNS):
		return FailureKind.TRANSIENT_NETWORK
	return FailureKind.UNKNOWN


def decide_retry(
	kind: FailureKind,
	*,
	attempt: int,
	same_node_retries: int,
	egress_rotations: int,
	policy: RetryPolicy,
) -> RetryDecision:
	"""根据失败类型和剩余预算决定是否同节点重试、换出口或停止。"""

	if attempt >= policy.max_attempts:
		return RetryDecision(RetryAction.STOP, 0.0, 'attempt budget exhausted')

	if kind in (
		FailureKind.AUTH_INVALID,
		FailureKind.RATE_LIMITED,
		FailureKind.MANUAL_REQUIRED,
		FailureKind.PROXY_UNAVAILABLE,
	):
		return RetryDecision(RetryAction.STOP, 0.0, f'{kind.value} is not retryable')

	if kind == FailureKind.WAF_CHALLENGE:
		if egress_rotations < policy.max_egress_rotations:
			return RetryDecision(RetryAction.ROTATE_EGRESS, policy.egress_retry_delay_seconds, 'rotate after WAF')
		return RetryDecision(RetryAction.STOP, 0.0, 'egress rotation budget exhausted')

	if kind == FailureKind.TRANSIENT_NETWORK:
		if same_node_retries < 1:
			return RetryDecision(
				RetryAction.RETRY_SAME,
				policy.transient_retry_delay_seconds,
				'retry transient failure once',
			)
		if egress_rotations < policy.max_egress_rotations:
			return RetryDecision(
				RetryAction.ROTATE_EGRESS, policy.egress_retry_delay_seconds, 'rotate after repeated transient failure'
			)
		return RetryDecision(RetryAction.STOP, 0.0, 'egress rotation budget exhausted')

	if kind == FailureKind.SITE_ERROR:
		if same_node_retries < 1:
			return RetryDecision(RetryAction.RETRY_SAME, policy.transient_retry_delay_seconds, 'retry site error once')
		return RetryDecision(RetryAction.STOP, 0.0, 'site error not retried again')

	return RetryDecision(RetryAction.STOP, 0.0, f'{kind.value} is not retryable')


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
	return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _env_int(name: str, default: int) -> int:
	try:
		value = int(os.getenv(name, str(default)).strip())
	except (TypeError, ValueError):
		return default
	return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
	try:
		value = float(os.getenv(name, str(default)).strip())
	except (TypeError, ValueError):
		return default
	return value if value >= 0 else default
