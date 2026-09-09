"""调试模式开关：控制敏感日志与调试产物（截图等）。"""

from __future__ import annotations

import os


def _env_enabled(name: str) -> bool:
	raw = os.getenv(name, '').strip().lower()
	return raw in {'1', 'true', 'yes', 'on'}


def is_debug_enabled() -> bool:
	"""是否开启完整调试模式（含截图），读取 DEBUG_MODE，默认 false。"""
	return _env_enabled('DEBUG_MODE')


def is_diagnostic_enabled() -> bool:
	"""是否开启脱敏诊断日志（不上传截图），读取 DIAGNOSTIC_MODE。"""
	return _env_enabled('DIAGNOSTIC_MODE')


def is_verbose_enabled() -> bool:
	"""调试或诊断模式下输出详细日志。"""
	return is_debug_enabled() or is_diagnostic_enabled()


def debug_print(message: str) -> None:
	"""在调试或脱敏诊断模式下输出日志。"""
	if is_verbose_enabled():
		print(message)


def diagnostic_print(message: str) -> None:
	"""仅输出脱敏诊断日志，不生成截图。"""
	if is_diagnostic_enabled():
		print(message)
