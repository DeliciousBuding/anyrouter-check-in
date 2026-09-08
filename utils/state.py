"""每账号的非敏感运行状态。

状态只保存时间、余额数值、失败分类和稳定账号 key；账号、密码、cookie、token
不得进入该文件。公开仓的 Actions cache 仍按敏感运行态对待。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

STATE_VERSION = 1
DEFAULT_STATE_FILE = 'checkin_state.json'


def utc_now() -> datetime:
	return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
	return value.astimezone(timezone.utc).isoformat() if value else None


def _parse(value: object) -> datetime | None:
	if not isinstance(value, str) or not value:
		return None
	try:
		parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
	except ValueError:
		return None
	if parsed.tzinfo is None:
		return parsed.replace(tzinfo=timezone.utc)
	return parsed.astimezone(timezone.utc)


def _float_or_none(value: object) -> float | None:
	if isinstance(value, (int, float)):
		return float(value)
	return None


def _timezone(name: str) -> timezone | ZoneInfo:
	try:
		return ZoneInfo(name)
	except (ZoneInfoNotFoundError, ValueError):
		return timezone.utc


@dataclass
class CheckinStateStore:
	"""JSON 状态文件，按不透明账号 key 记录最近一次结果。"""

	path: Path
	data: dict[str, Any] = field(default_factory=lambda: {'version': STATE_VERSION, 'accounts': {}})

	@classmethod
	def load(cls, path: str | os.PathLike[str] = DEFAULT_STATE_FILE) -> 'CheckinStateStore':
		state_path = Path(path)
		data: dict[str, Any] = {'version': STATE_VERSION, 'accounts': {}}
		try:
			raw = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
			if isinstance(raw, dict):
				accounts = raw.get('accounts')
				if isinstance(accounts, dict):
					data['accounts'] = {str(key): value for key, value in accounts.items() if isinstance(value, dict)}
		except Exception:  # nosec B110
			pass
		return cls(path=state_path, data=data)

	def account(self, key: str) -> dict[str, Any]:
		accounts = self.data.setdefault('accounts', {})
		value = accounts.setdefault(key, {})
		return value if isinstance(value, dict) else {}

	def mark_attempt(self, key: str, *, now: datetime | None = None) -> None:
		self.account(key)['last_attempt_at'] = _iso(now or utc_now())

	def mark_success(
		self,
		key: str,
		*,
		quota: float | None = None,
		used: float | None = None,
		bonus: float | None = None,
		now: datetime | None = None,
	) -> None:
		current = now or utc_now()
		entry = self.account(key)
		entry.update(
			{
				'last_success_at': _iso(current),
				'last_success_date': current.date().isoformat(),
				'last_failure_at': None,
				'last_failure_kind': None,
				'next_eligible_at': None,
			}
		)
		for name, value in (('last_quota', quota), ('last_used', used), ('last_bonus', bonus)):
			if value is not None:
				entry[name] = float(value)

	def mark_failure(
		self,
		key: str,
		kind: str,
		*,
		retry_after_hours: float = 6.0,
		now: datetime | None = None,
	) -> None:
		current = now or utc_now()
		entry = self.account(key)
		entry.update(
			{
				'last_failure_at': _iso(current),
				'last_failure_kind': kind,
				'next_eligible_at': _iso(current + timedelta(hours=max(0.0, retry_after_hours))),
			}
		)

	def successful_today(
		self,
		key: str,
		*,
		timezone_name: str = 'UTC',
		now: datetime | None = None,
	) -> bool:
		"""按站点日判断今天是否已经成功，避免滚动 24h 漏掉下一个日历日。"""

		current = now or utc_now()
		last_success = _parse(self.account(key).get('last_success_at'))
		if last_success is None:
			return False
		tz = _timezone(timezone_name)
		return last_success.astimezone(tz).date() == current.astimezone(tz).date()

	def is_recent_success(
		self,
		key: str,
		*,
		daily_success_cooldown_hours: float = 0.0,
		now: datetime | None = None,
	) -> bool:
		current = now or utc_now()
		last_success = _parse(self.account(key).get('last_success_at'))
		return bool(
			last_success is not None
			and daily_success_cooldown_hours > 0
			and current - last_success < timedelta(hours=daily_success_cooldown_hours)
		)

	def skip_reason(
		self,
		key: str,
		*,
		daily_success_cooldown_hours: float = 0.0,
		daily_success_timezone: str = 'UTC',
		force: bool = False,
		now: datetime | None = None,
	) -> str | None:
		if force:
			return None
		current = now or utc_now()
		entry = self.account(key)
		last_success = _parse(entry.get('last_success_at'))
		if daily_success_cooldown_hours > 0 and self.successful_today(
			key, timezone_name=daily_success_timezone, now=current
		):
			assert last_success is not None
			return f'last successful login was {last_success.isoformat()} (same {daily_success_timezone} day)'
		if self.is_recent_success(key, daily_success_cooldown_hours=daily_success_cooldown_hours, now=current):
			assert last_success is not None
			return f'last successful login was {last_success.isoformat()} (within {daily_success_cooldown_hours:g}h)'
		next_eligible = _parse(entry.get('next_eligible_at'))
		if next_eligible is not None and current < next_eligible:
			return f'backoff until {next_eligible.isoformat()}'
		return None

	def last_balance(self, key: str) -> dict[str, float] | None:
		entry = self.account(key)
		quota = _float_or_none(entry.get('last_quota'))
		used = _float_or_none(entry.get('last_used'))
		bonus = _float_or_none(entry.get('last_bonus'))
		if quota is None and used is None and bonus is None:
			return None
		return {'quota': quota or 0.0, 'used': used or 0.0, 'bonus': bonus or 0.0}

	def save(self) -> None:
		"""原子写入状态文件；失败不影响主流程。"""

		try:
			self.path.parent.mkdir(parents=True, exist_ok=True)
			payload = json.dumps(self.data, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
			with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.path.parent, delete=False) as handle:
				temp_path = Path(handle.name)
				handle.write(payload)
			os.replace(temp_path, self.path)
		except Exception as exc:
			print(f'[WARN] Failed to save check-in state: {str(exc)[:120]}')
