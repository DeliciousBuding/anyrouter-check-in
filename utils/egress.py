"""mihomo 控制面：为 WAF 失败提供应用层出口轮换。

公开仓日志纪律：节点名只用于调用本地 API，不进入日志；调用方只记录 SHA-256
前缀。订阅 URL、节点地址和代理凭据都不属于本模块。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

_GROUP_SENTINELS = frozenset({'DIRECT', 'REJECT', 'GLOBAL'})


def node_label(name: str | None) -> str:
	"""返回适合公开日志的稳定节点标识。"""

	if not name:
		return 'unknown'
	return hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]


@dataclass
class EgressController:
	"""通过 mihomo external-controller 读取和切换代理组节点。"""

	api_url: str
	api_secret: str
	group: str = 'CHECKIN'
	auto_group: str = 'CHECKIN_AUTO'
	timeout_seconds: float = 10.0

	@classmethod
	def from_env(cls) -> 'EgressController | None':
		api_url = os.getenv('CHECKIN_PROXY_API_URL', '').strip()
		api_secret = os.getenv('CHECKIN_PROXY_API_SECRET', '').strip()
		if not api_url or not api_secret:
			return None
		return cls(
			api_url=api_url.rstrip('/'),
			api_secret=api_secret,
			group=os.getenv('CHECKIN_PROXY_GROUP', cls.group).strip() or cls.group,
			auto_group=os.getenv('CHECKIN_PROXY_AUTO_GROUP', cls.auto_group).strip() or cls.auto_group,
		)

	async def _request(self, method: str, path: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
		async with httpx.AsyncClient(trust_env=False, timeout=self.timeout_seconds) as client:
			response = await client.request(
				method,
				f'{self.api_url}{path}',
				headers={'Authorization': f'Bearer {self.api_secret}'},
				json=payload,
			)
			response.raise_for_status()
			if not response.content:
				return {}
			data = response.json()
			return data if isinstance(data, dict) else {}

	async def list_nodes(self) -> list[str]:
		"""返回控制组中可手动选择的节点，不含 AUTO 组和内置策略。"""

		try:
			data = await self._request('GET', f'/proxies/{self.group}')
		except Exception:  # nosec B110
			return []
		nodes = data.get('all') or []
		if not isinstance(nodes, list):
			return []
		return [
			str(name) for name in nodes if name and str(name) not in _GROUP_SENTINELS and str(name) != self.auto_group
		]

	async def current_node(self) -> str | None:
		"""返回控制组实际使用的节点；若当前是 AUTO 组则解析到内部节点。

		初始选路交给 mihomo url-test，避免账号 hash 直接覆盖健康检查结果。
		只有 WAF 等失败后，调用方才显式选择别的节点。
		"""

		try:
			data = await self._request('GET', f'/proxies/{self.group}')
			now = data.get('now')
			if now == self.auto_group:
				auto_data = await self._request('GET', f'/proxies/{self.auto_group}')
				auto_now = auto_data.get('now')
				if auto_now:
					return str(auto_now)
			return str(now) if now else None
		except Exception:  # nosec B110
			return None

	async def select_node(self, name: str) -> None:
		await self._request('PUT', f'/proxies/{self.group}', payload={'name': name})

	async def select_stable_node(self, account_key: str, excluded: set[str] | None = None) -> str | None:
		"""优先复用当前节点，否则按账号 hash 确定性选择备用节点。

		同一轮里 mihomo 控制组是全局单例。先复用已选中的当前节点，避免每个账号
		都把出口切来切去；只有当前节点不可用或被排除时，才按账号 hash 选一个稳定
		备用节点。节点名只用于本地 API，不进入日志。
		"""

		nodes = await self.list_nodes()
		excluded = excluded or set()
		available = [name for name in nodes if name not in excluded]
		if not available:
			return None
		current = await self.current_node()
		if current in available:
			return current
		ordered = sorted(
			available,
			key=lambda name: hashlib.sha256(f'{account_key}:{name}'.encode('utf-8')).hexdigest(),
		)
		for name in ordered:
			try:
				await self.select_node(name)
			except Exception:  # nosec B112
				continue
			return name
		return None

	async def rotate(self, excluded: set[str]) -> str | None:
		"""按控制组顺序选择下一个未排除节点。节点名不落日志。"""

		nodes = await self.list_nodes()
		if not nodes:
			return None
		current = await self.current_node()
		for name in nodes:
			if name == current or name in excluded:
				continue
			try:
				await self.select_node(name)
			except Exception:  # nosec B112
				continue
			return name
		return None


@dataclass
class EgressRotator:
	"""单账号轮换预算和已排除节点集合。"""

	controller: EgressController
	account_key: str = 'default'
	max_rotations: int = 2
	rotations: int = 0
	excluded: set[str] = field(default_factory=set)

	async def ensure_selected(self) -> str | None:
		"""确保该账号已经绑定到一个稳定节点。"""

		return await self.controller.select_stable_node(self.account_key, self.excluded)

	async def rotate(self) -> tuple[str | None, str | None]:
		if self.rotations >= self.max_rotations:
			return None, None
		current = await self.controller.current_node()
		if current and current != self.controller.auto_group:
			self.excluded.add(current)
		node = await self.controller.select_stable_node(self.account_key, self.excluded)
		if not node:
			return None, None
		self.excluded.add(node)
		self.rotations += 1
		return node, node_label(node)
