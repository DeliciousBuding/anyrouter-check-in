#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, cast

if hasattr(sys.stdout, 'reconfigure'):
	sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
	sys.stderr.reconfigure(line_buffering=True)

from urllib.parse import urlparse

import httpx
from cloakbrowser import launch_async
from dotenv import load_dotenv

from utils.browser import (
	BrowserLoginResult,
	has_session_cookie,
	is_logged_in,
	is_waf_challenge,
	launch_login_context,
	load_browser_login_settings,
	login_with_email_form,
	navigate_login_page,
	prepare_browser_page,
	sanitize_login_message,
	save_login_screenshot,
	verify_browser_login,
	wait_for_waf_ready,
)
from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils.debug import debug_print, is_debug_enabled
from utils.egress import EgressController, EgressRotator
from utils.notify import smart_notify
from utils.proxy import get_playwright_proxy, get_proxy_server
from utils.retry import (
	FailureKind,
	LoginFlowError,
	RetryAction,
	RetryPolicy,
	classify_failure,
	decide_retry,
)
from utils.state import DEFAULT_STATE_FILE, CheckinStateStore

load_dotenv()

BALANCE_SNAPSHOT_FILE = 'balance_snapshot.json'
SITE_TIMEZONE = timezone(timedelta(hours=8))


def _env_bool(name: str, default: bool = False) -> bool:
	raw = os.getenv(name)
	if raw is None:
		return default
	return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _failure_retry_hours(kind: FailureKind) -> float:
	"""失败后多久允许该账号再次尝试。认证失败不参与当轮重试。"""

	return {
		FailureKind.AUTH_INVALID: 24.0,
		FailureKind.RATE_LIMITED: 6.0,
		FailureKind.WAF_CHALLENGE: 2.0,
		FailureKind.TRANSIENT_NETWORK: 1.0,
		FailureKind.SITE_ERROR: 2.0,
		FailureKind.PROXY_UNAVAILABLE: 1.0,
		FailureKind.MANUAL_REQUIRED: 24.0,
	}.get(kind, 6.0)


def _quota_to_usd(value: object) -> float:
	"""把 NewAPI 的 quota 整数换算成美元；异常值按 0 处理。"""

	if not isinstance(value, (int, float, str)):
		return 0.0
	try:
		return round(float(value) / 500000, 2)
	except (TypeError, ValueError):
		return 0.0


def _user_info_from_data(user_data: dict) -> dict[str, Any]:
	"""把 NewAPI /api/user/self 的 data 转成统一用户信息。"""

	quota = _quota_to_usd(user_data.get('quota'))
	used = _quota_to_usd(user_data.get('used_quota'))
	bonus = _quota_to_usd(user_data.get('bonus_quota'))
	display = f':money: Current balance: ${quota}, Used: ${used}'
	if bonus:
		display += f', Bonus: ${bonus}'
	return {
		'success': True,
		'quota': quota,
		'used_quota': used,
		'bonus_quota': bonus,
		'display': display,
	}


def _user_info_from_state(balance: dict[str, float]) -> dict[str, Any]:
	return _user_info_from_data(
		{
			'quota': float(balance.get('quota') or 0.0) * 500000,
			'used_quota': float(balance.get('used') or 0.0) * 500000,
			'bonus_quota': float(balance.get('bonus') or 0.0) * 500000,
		}
	)


def load_balance_snapshot():
	"""加载余额快照 {hash, total_quota}"""
	try:
		if os.path.exists(BALANCE_SNAPSHOT_FILE):
			with open(BALANCE_SNAPSHOT_FILE, 'r', encoding='utf-8') as f:
				data = json.loads(f.read())
				if isinstance(data, dict) and 'hash' in data:
					return {'hash': data['hash'], 'total_quota': float(data.get('total_quota', 0))}
	except Exception:  # nosec B110
		pass
	return None


def save_balance_snapshot(snapshot):
	"""保存余额快照"""
	try:
		with open(BALANCE_SNAPSHOT_FILE, 'w', encoding='utf-8') as f:
			json.dump(snapshot, f)
	except Exception as e:
		print(f'Warning: Failed to save balance snapshot: {e}')


def generate_balance_hash(balances):
	"""生成余额数据的 hash；bonus 为 0 时保持旧快照兼容。"""
	simple_balances = {}
	for key, value in (balances or {}).items():
		item = {'quota': value.get('quota'), 'used': value.get('used')}
		bonus = value.get('bonus')
		if bonus:
			item['bonus'] = bonus
		simple_balances[key] = item
	balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
	return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]


def parse_cookies(cookies_data):
	"""解析 cookies 数据"""
	if isinstance(cookies_data, dict):
		return cookies_data

	if isinstance(cookies_data, str):
		cookies_dict = {}
		for cookie in cookies_data.split(';'):
			if '=' in cookie:
				key, value = cookie.strip().split('=', 1)
				cookies_dict[key] = value
		return cookies_dict
	return {}


async def get_waf_cookies_with_browser(
	account_name: str,
	login_url: str,
	required_cookies: list[str],
	*,
	use_proxy: bool = False,
):
	"""使用浏览器获取 WAF cookies"""
	print(f'[PROCESSING] {account_name}: Starting browser to get WAF cookies...')

	launch_kwargs: dict = {'headless': True}
	proxy = get_playwright_proxy(use_proxy=use_proxy)
	if proxy:
		launch_kwargs['proxy'] = proxy
	browser = await launch_async(**launch_kwargs)

	try:
		page = await browser.new_page()
		await prepare_browser_page(page)
		print(f'[PROCESSING] {account_name}: Access login page to get initial cookies...')

		await page.goto(login_url, wait_until='domcontentloaded')
		await wait_for_waf_ready(page)

		cookies = await page.context.cookies()

		waf_cookies = {}
		for cookie in cookies:
			cookie_name = cookie.get('name')
			cookie_value = cookie.get('value')
			if cookie_name in required_cookies and cookie_value is not None:
				waf_cookies[cookie_name] = cookie_value

		print(f'[INFO] {account_name}: Got {len(waf_cookies)} WAF cookies')

		missing_cookies = [c for c in required_cookies if c not in waf_cookies]

		if missing_cookies:
			print(f'[FAILED] {account_name}: Missing WAF cookies: {missing_cookies}')
			await browser.close()
			return None

		print(f'[SUCCESS] {account_name}: Successfully got all WAF cookies')
		await browser.close()
		return waf_cookies

	except Exception as e:
		print(f'[FAILED] {account_name}: Error occurred while getting WAF cookies: {sanitize_login_message(e)}')
		await browser.close()
		return None


async def login_with_credentials(
	account_name: str,
	provider_config,
	provider_name: str,
	email: str,
	password: str,
) -> BrowserLoginResult:
	"""使用邮箱密码通过浏览器登录，失败时抛出带分类的 LoginFlowError。"""

	print(f'[PROCESSING] {account_name}: Logging in with email/password...')

	login_url = f'{provider_config.domain}{provider_config.login_path}'
	settings = load_browser_login_settings(
		account_name,
		provider_name,
		persist_profile=provider_config.persist_profile,
	)
	timeout_ms = settings.wait_timeout_ms

	debug_print(
		f'[INFO] {account_name}: Browser profile={settings.profile_dir}, '
		f'persist={settings.persist_profile}, headless={settings.headless}, '
		f'humanize={settings.humanize}, timeout={timeout_ms}ms'
	)

	print(
		f'[INFO] {account_name}: Provider proxy={"enabled" if provider_config.use_proxy else "disabled"} '
		f'({provider_name})'
	)

	context = None
	page = None
	try:
		try:
			context = await launch_login_context(settings, use_proxy=provider_config.use_proxy)
		except Exception as exc:
			kind = classify_failure(str(exc))
			if kind == FailureKind.UNKNOWN and provider_config.use_proxy:
				kind = FailureKind.PROXY_UNAVAILABLE
			raise LoginFlowError(kind, f'Browser launch failed: {str(exc)[:120]}') from exc

		page = await context.new_page()
		await prepare_browser_page(page)
		await navigate_login_page(
			page,
			login_url,
			timeout_ms,
			provider=provider_name,
			account_name=account_name,
		)

		if not await is_logged_in(page):
			if await has_session_cookie(page):
				print(f'[WARN] {account_name}: Stale session cookie on login page, forcing email login')
			await save_login_screenshot(page, provider_name, account_name, 'before-email-login')
			form_result = await login_with_email_form(
				page,
				email,
				password,
				timeout_ms,
				provider=provider_name,
				account_name=account_name,
			)
			if form_result.api_status == 429:
				raise LoginFlowError(FailureKind.RATE_LIMITED, 'login API rate limited')
			if form_result.api_status in (401, 403):
				raise LoginFlowError(FailureKind.AUTH_INVALID, f'login API HTTP {form_result.api_status}')
			if form_result.api_status is not None and form_result.api_status >= 500:
				raise LoginFlowError(FailureKind.SITE_ERROR, f'login API HTTP {form_result.api_status}')
			if form_result.api_success is False:
				message = form_result.api_message or 'login API returned success=false'
				kind = classify_failure(message)
				if kind == FailureKind.UNKNOWN:
					kind = FailureKind.AUTH_INVALID
				raise LoginFlowError(kind, message)
		else:
			print(f'[INFO] {account_name}: Browser profile already logged in')

		console_url = f'{provider_config.domain}/console'
		user_profile = await verify_browser_login(page, console_url, timeout_ms)
		if not user_profile:
			if await is_waf_challenge(page):
				raise LoginFlowError(FailureKind.WAF_CHALLENGE, 'login verification blocked by WAF')
			raise LoginFlowError(FailureKind.AUTH_INVALID, '/api/user/self did not return a user profile')

		cookies = await context.cookies()
		all_cookies: dict[str, str] = {}
		for cookie in cookies:
			name = cookie.get('name')
			value = cookie.get('value')
			if isinstance(name, str) and isinstance(value, str):
				all_cookies[name] = value
		api_user = str(user_profile['id']) if user_profile.get('id') is not None else None

		success_msg = f'[SUCCESS] {account_name}: Login successful, got {len(all_cookies)} cookies'
		if is_debug_enabled() and api_user:
			success_msg += f', api_user={api_user}'
		print(success_msg)
		return BrowserLoginResult(cookies=all_cookies, api_user=api_user, profile=user_profile)
	except LoginFlowError:
		raise
	except Exception as exc:
		waf_challenge = await is_waf_challenge(page) if page is not None else False
		if waf_challenge:
			failure_kind = FailureKind.WAF_CHALLENGE
		else:
			failure_kind = classify_failure(str(exc))
			if failure_kind == FailureKind.UNKNOWN:
				failure_kind = FailureKind.TRANSIENT_NETWORK
		if page is not None:
			await save_login_screenshot(page, provider_name, account_name, 'login-error')
		raise LoginFlowError(failure_kind, str(exc)[:160]) from exc
	finally:
		if context is not None:
			try:
				await context.close()
			except Exception:  # nosec B110
				pass


def get_check_in_status(client, headers, status_url: str) -> bool | None:
	"""查询 NewAPI 今日签到状态；查询失败返回 None，不阻断签到。"""

	try:
		response = client.get(status_url, headers=headers, timeout=30)
		if response.status_code != 200:
			return None
		data = response.json()
		if not isinstance(data, dict) or not data.get('success'):
			return None
		status_data = data.get('data') or {}
		stats = status_data.get('stats') if isinstance(status_data, dict) else {}
		checked = stats.get('checked_in_today') if isinstance(stats, dict) else None
		return checked if isinstance(checked, bool) else None
	except Exception:  # nosec B110
		return None


def get_user_info(client, headers, user_info_url: str):
	"""获取用户信息"""
	try:
		response = client.get(user_info_url, headers=headers, timeout=30)

		if response.status_code == 200:
			data = response.json()
			if data.get('success'):
				user_data = data.get('data', {})
				if not isinstance(user_data, dict):
					return {'success': False, 'error': 'Failed to get user info: invalid data'}
				return _user_info_from_data(user_data)
		return {'success': False, 'error': f'Failed to get user info: HTTP {response.status_code}'}
	except Exception as e:
		message = sanitize_login_message(f'Failed to get user info: {str(e)[:50]}...')
		return {'success': False, 'error': message or 'Failed to get user info'}


async def prepare_cookies(account_name: str, provider_config, user_cookies: dict) -> dict | None:
	"""准备请求所需的 cookies（可能包含 WAF cookies）"""
	waf_cookies = {}

	if provider_config.needs_waf_cookies():
		login_url = f'{provider_config.domain}{provider_config.login_path}'
		waf_cookies = await get_waf_cookies_with_browser(
			account_name,
			login_url,
			provider_config.waf_cookie_names,
			use_proxy=provider_config.use_proxy,
		)
		if not waf_cookies:
			print(f'[FAILED] {account_name}: Unable to get WAF cookies')
			return None
	else:
		print(f'[INFO] {account_name}: Bypass WAF not required, using user cookies directly')

	return {**waf_cookies, **user_cookies}


def execute_check_in(client, account_name: str, provider_config, headers: dict):
	"""执行签到请求，返回 (success, error_msg)"""
	print(f'[NETWORK] {account_name}: Executing check-in')

	checkin_headers = headers.copy()
	checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})

	sign_in_url = f'{provider_config.domain}{provider_config.sign_in_path}'
	response = client.post(sign_in_url, headers=checkin_headers, timeout=30)

	print(f'[RESPONSE] {account_name}: Response status code {response.status_code}')

	if response.status_code == 200:
		try:
			result = response.json()
			if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
				print(f'[SUCCESS] {account_name}: Check-in successful!')
				return True, None
			else:
				error_msg = str(result.get('msg', result.get('message', 'Unknown error')) or 'Unknown error')
				already_checked_keywords = ['已经签到', '已签到', '重复签到', 'already checked', 'already signed']
				if any(keyword in error_msg.lower() for keyword in already_checked_keywords):
					print(f'[SUCCESS] {account_name}: Already checked in today')
					return True, None
				error_msg = sanitize_login_message(error_msg) or 'Unknown error'
				print(f'[FAILED] {account_name}: Check-in failed - {error_msg}')
				return False, error_msg
		except json.JSONDecodeError:
			if 'success' in response.text.lower():
				print(f'[SUCCESS] {account_name}: Check-in successful!')
				return True, None
			else:
				message = 'Invalid response format'
				print(f'[FAILED] {account_name}: Check-in failed - {message}')
				return False, message
	else:
		message = f'HTTP {response.status_code}'
		print(f'[FAILED] {account_name}: Check-in failed - {message}')
		return False, message


def format_check_in_notification(detail: dict) -> str:
	"""格式化签到通知消息"""
	before_bonus = float(detail.get('before_bonus', 0) or 0)
	after_bonus = float(detail.get('after_bonus', 0) or 0)
	before_bonus_text = f'  |  Bonus: ${before_bonus:.2f}' if before_bonus else ''
	after_bonus_text = f'  |  Bonus: ${after_bonus:.2f}' if after_bonus else ''
	lines = [
		f'[CHECK-IN] {detail["name"]}',
		'  ━━━━━━━━━━━━━━━━━━━━',
		'  签到前',
		(
			f'     余额: ${detail["before_quota"] + before_bonus:.2f}'
			f'  |  累计消耗: ${detail["before_used"]:.2f}{before_bonus_text}'
		),
		'  签到后',
		(
			f'     余额: ${detail["after_quota"] + after_bonus:.2f}'
			f'  |  累计消耗: ${detail["after_used"]:.2f}{after_bonus_text}'
		),
	]

	has_reward = detail['check_in_reward'] != 0
	has_usage = detail['usage_increase'] != 0

	if has_reward or has_usage:
		lines.append('  ━━━━━━━━━━━━━━━━━━━━')

		if not has_reward and has_usage:
			lines.append('  今日已签到（期间有使用）')

		if has_reward:
			lines.append(f'  签到获得: +${detail["check_in_reward"]:.2f}')

		if has_usage:
			lines.append(f'  期间消耗: ${detail["usage_increase"]:.2f}')

		if detail['balance_change'] != 0:
			change_symbol = '+' if detail['balance_change'] > 0 else ''
			lines.append(f'  余额变化: {change_symbol}${detail["balance_change"]:.2f}')
	else:
		lines.extend(['  ━━━━━━━━━━━━━━━━━━━━', '  今日已签到，无变化'])

	return '\n'.join(lines)


async def login_with_retry(
	account_name: str,
	provider_config,
	provider_name: str,
	email: str,
	password: str,
	egress_rotator: EgressRotator | None = None,
	retry_policy: RetryPolicy | None = None,
) -> BrowserLoginResult:
	"""执行邮箱密码登录，并按失败类型在同节点重试或轮换出口。"""

	policy = retry_policy or RetryPolicy.from_env()
	same_node_retries = 0
	last_error: LoginFlowError | None = None

	for attempt in range(1, policy.max_attempts + 1):
		try:
			return await login_with_credentials(
				account_name,
				provider_config,
				provider_name,
				email,
				password,
			)
		except LoginFlowError as exc:
			last_error = exc
			rotations = egress_rotator.rotations if egress_rotator else 0
			decision = decide_retry(
				exc.kind,
				attempt=attempt,
				same_node_retries=same_node_retries,
				egress_rotations=rotations,
				policy=policy,
			)
			print(f'[WARN] {account_name}: Login attempt {attempt}/{policy.max_attempts} failed [{exc.kind.value}]')
			print(f'[INFO] {account_name}: Retry action={decision.action.value} reason={decision.reason}')

			if decision.action == RetryAction.STOP:
				break

			if decision.action == RetryAction.ROTATE_EGRESS:
				if egress_rotator is None:
					print(f'[WARN] {account_name}: No egress controller available for rotation')
					break
				node, label = await egress_rotator.rotate()
				if not node:
					print(f'[WARN] {account_name}: No alternate egress node available')
					break
				same_node_retries = 0
				print(
					f'[INFO] {account_name}: Rotated egress node_sha={label} '
					f'({egress_rotator.rotations}/{policy.max_egress_rotations})'
				)
			elif decision.action == RetryAction.RETRY_SAME:
				same_node_retries += 1

			if decision.delay_seconds:
				await asyncio.sleep(decision.delay_seconds)

	raise last_error or LoginFlowError(FailureKind.UNKNOWN, 'login failed without a classified error')


async def check_in_account(
	account: AccountConfig,
	account_index: int,
	app_config: AppConfig,
	egress_controller: EgressController | None = None,
	retry_policy: RetryPolicy | None = None,
):
	"""为单个账号执行签到操作，返回 (success, before, after, error_msg)"""
	account_name = account.get_log_label(account_index)
	print(f'\n[PROCESSING] Starting to process {account_name}')

	provider_config = app_config.get_provider(account.provider)
	if not provider_config:
		message = f'Provider "{account.provider}" not found in configuration'
		print(f'[FAILED] {account_name}: {message}')
		return False, None, None, message

	print(f'[INFO] {account_name}: Using provider "{account.provider}" ({provider_config.domain})')

	egress_rotator: EgressRotator | None = None
	policy = retry_policy or RetryPolicy.from_env()
	if provider_config.use_proxy and egress_controller is not None:
		egress_rotator = EgressRotator(
			egress_controller,
			account.get_state_key(account_index),
			policy.max_egress_rotations,
		)
		await egress_rotator.ensure_selected()

	if provider_config.use_proxy and not get_proxy_server(use_proxy=True):
		# 该 provider 必须走代理：数据中心 IP 会被下发滑块人机验证，登录页根本不渲染。
		# 代理没起来时直连只会烧掉多轮登录重试，还会把失败原因误报成「站点拦截」
		# 而不是「代理未就绪」——两者处置方式完全不同，必须区分开。
		message = '代理未就绪（CHECKIN_PROXY_URL 为空），已跳过直连尝试'
		print(f'[FAILED] {account_name}: {message}')
		return False, None, None, message

	# 邮箱密码优先
	all_cookies = None
	resolved_api_user: str | None = None
	auth_method = None
	if account.has_login_credentials():
		print(f'[INFO] {account_name}: Attempting email/password login (priority)...')
		assert account.email is not None and account.password is not None
		try:
			login_result = await login_with_retry(
				account_name,
				provider_config,
				account.provider,
				account.email,
				account.password,
				egress_rotator=egress_rotator if provider_config.use_proxy else None,
				retry_policy=policy,
			)
		except LoginFlowError as exc:
			message = sanitize_login_message(exc) or 'login failed'
			print(f'[FAILED] {account_name}: {message}')
			return False, None, None, message
		all_cookies = login_result.cookies
		resolved_api_user = login_result.api_user
		auth_method = 'email/password'
		if not provider_config.needs_manual_check_in() and login_result.profile:
			# 无签到端点的站点（agentrouter）：真实登录本身就是签到动作，余额已在
			# 登录校验的同一会话里拿到。再开一个浏览器上下文重新过 WAF，只会在数据
			# 中心 IP 上多一次失败机会（08-21 实测那条路连续 8 次拿不到 JSON）。
			print(f'[AUTH] {account_name}: Using auth method -> email/password (login-triggered check-in)')
			print(f'[SUCCESS] {account_name}: Check-in completed by fresh login (user info verified)')
			return True, None, format_user_info_from_profile(login_result.profile), None
	else:
		user_cookies = parse_cookies(account.cookies)
		if not user_cookies:
			message = '账号配置缺少 cookies'
			print(f'[FAILED] {account_name}: {message}')
			return False, None, None, message
		if provider_config.sign_in_path is None and provider_config.needs_waf_cookies():
			# 浏览器路由（agentrouter）：WAF 挑战由浏览器上下文自己执行解决，
			# 无需也不该预取 WAF cookie（该站 /login 不下发 acw_sc__v2 等标记）。
			all_cookies = user_cookies
			auth_method = 'session cookies (browser)'
		else:
			all_cookies = await prepare_cookies(account_name, provider_config, user_cookies)
			auth_method = 'session cookies'

	if not all_cookies:
		return False, None, None, '无法获取有效 cookies（WAF 或 session 异常）'

	print(f'[AUTH] {account_name}: Using auth method -> {auth_method}')

	# WAF 保护且无签到端点（agentrouter）的站点：签到由浏览器内 /api/user/self
	# 查询触发。数据中心 IP 的 httpx 会被 WAF 硬拦，必须走真实浏览器上下文。
	if provider_config.sign_in_path is None and provider_config.needs_waf_cookies():
		print(f'[AUTH] {account_name}: Routing check-in through browser context')
		return await check_in_via_browser(
			account, account_name, provider_config, all_cookies, api_user_override=resolved_api_user
		)

	return run_check_in_requests(
		all_cookies,
		account,
		account_name,
		provider_config,
		api_user_override=resolved_api_user,
		use_proxy=provider_config.use_proxy,
	)


async def check_in_via_browser(
	account: AccountConfig,
	account_name: str,
	provider_config,
	all_cookies: dict,
	*,
	api_user_override: str | None = None,
) -> tuple[bool, dict | None, dict | None, str | None]:
	"""浏览器上下文内触发签到。

	流程：注入 session cookie → 浏览器执行 JS 挑战过 WAF → 带 New-Api-User 头
	做一次「文档导航」级 /api/user/self 请求（该请求即完成签到；agentrouter 的
	签到 = 登录查询）。fetch() 不会执行 WAF 挑战页 JS，因此改用文档导航：挑战页
	JS 自解后 reload，最终页面正文即 JSON 用户信息。数据中心 IP 的 httpx 会被
	WAF 硬拦，必须走真实浏览器。

	api_user_override 用于邮箱密码登录的账号：配置里没有 api_user，登录时才拦截到，
	缺失会让多用户 NewAPI 返回「未提供 New-Api-User」。
	"""
	settings = load_browser_login_settings(
		account_name,
		account.provider,
		persist_profile=provider_config.persist_profile,
	)
	effective_api_user = api_user_override or account.api_user
	context = None
	page = None
	try:
		context = await launch_login_context(settings, use_proxy=provider_config.use_proxy)
		page = await context.new_page()
		await prepare_browser_page(page)

		domain = urlparse(provider_config.domain).hostname
		session_cookies = [
			{'name': name, 'value': value, 'domain': domain, 'path': '/'}
			for name, value in all_cookies.items()
			if name == 'session' and value
		]
		if not session_cookies:
			message = 'session invalid: 未提供 session cookie，无法浏览器内签到'
			print(f'[FAILED] {account_name}: {message}')
			return False, None, None, message
		await context.add_cookies(cast(Any, session_cookies))

		# 先访问登录页：让浏览器执行 WAF JS 挑战并落 WAF cookie
		login_url = f'{provider_config.domain}{provider_config.login_path}'
		await page.goto(login_url, wait_until='domcontentloaded', timeout=60_000)
		await wait_for_waf_ready(page)

		profile = await request_user_self_via_document_navigation(
			page, account, account_name, provider_config, api_user_override=effective_api_user
		)

		# 兜底：页面内 fetch（正常情况下文档导航已拿到 profile）
		if not profile:
			for attempt in range(3):
				profile = await fetch_user_self_in_browser(page, effective_api_user, account_name)
				if profile is not None:
					break
				# WAF 可能对 API 路径再触发一次挑战：等待解决后重试
				await wait_for_waf_ready(page)

		if not profile:
			if await is_waf_challenge(page):
				message = 'browser check-in blocked by WAF'
			else:
				message = 'session expired: /api/user/self did not return a user profile'
			print(f'[FAILED] {account_name}: {message}')
			return False, None, None, message

		user_info_after = format_user_info_from_profile(profile)
		print(f'[SUCCESS] {account_name}: Check-in completed via browser (user info queried)')
		return True, None, user_info_after, None
	except Exception as exc:
		if page is not None and await is_waf_challenge(page):
			message = 'browser check-in blocked by WAF'
		else:
			message = sanitize_login_message(exc) or 'browser check-in failed'
		print(f'[FAILED] {account_name}: Browser check-in error - {message}')
		return False, None, None, message
	finally:
		# finally 里不得 return：会覆盖 try/except 的返回值，把成功判成失败。
		if context is not None:
			await context.close()


async def request_user_self_via_document_navigation(
	page,
	account: AccountConfig,
	account_name: str,
	provider_config,
	*,
	api_user_override: str | None = None,
) -> dict | None:
	"""文档导航到 /api/user/self 并解析正文 JSON。

	通过 page.route 给该导航注入 New-Api-User 头。WAF 挑战页会在文档上下文中
	执行自解 JS 并 reload（挑战自解时 route 拦截依然生效，头不丢），最终页面
	正文就是 /api/user/self 的 JSON 响应。
	"""
	api_user = (api_user_override or account.api_user or '').strip()
	api_url = f'{provider_config.domain}{provider_config.user_info_path}'

	async def attach_user_header(route):
		headers = {**route.request.headers}
		if api_user:
			headers['new-api-user'] = api_user
		await route.continue_(headers=headers)

	try:
		await page.route('**/api/user/self', attach_user_header)
		await page.goto(api_url, wait_until='domcontentloaded', timeout=60_000)
	except Exception as exc:  # nosec B110
		print(
			f'[WARN] {account_name}: document navigation unavailable ({sanitize_login_message(exc) or "unknown error"})'
		)
		return None

	for _ in range(8):
		await wait_for_waf_ready(page)
		body_text = await page.evaluate('() => document.body ? (document.body.innerText || "") : ""')
		if not body_text.strip():
			await asyncio.sleep(3)
			continue
		parsed = None
		try:
			parsed = json.loads(body_text)
		except Exception:  # nosec B110
			parsed = None
		if parsed and isinstance(parsed, dict) and parsed.get('success') is True:
			data = parsed.get('data')
			if isinstance(data, dict) and data.get('id'):
				print(f'[INFO] {account_name}: document navigation returned user profile')
				return data
		if parsed and isinstance(parsed, dict):
			message = parsed.get('message') or parsed.get('msg')
			safe_message = sanitize_login_message(message) or 'no message'
			print(f'[INFO] {account_name}: API returned success=false: {safe_message}')
		else:
			print(f'[INFO] {account_name}: page body not JSON yet (WAF challenge solving)')
		await asyncio.sleep(3)
	return None


async def fetch_user_self_in_browser(page, api_user: str | None, account_name: str) -> dict | None:
	"""在页面上下文内请求 /api/user/self（带 New-Api-User），返回用户 profile 或 None。

	站点是多用户 NewAPI：仅凭 session cookie 会返回「未提供 New-Api-User」，
	必须显式带头；api_user 在账号配置中提供。
	"""
	api_user = (api_user or '').strip()
	script = """
	async (apiUser) => {
		const headers = {};
		if (apiUser) { headers['New-Api-User'] = apiUser; }
		const response = await fetch('/api/user/self', { credentials: 'include', headers });
		return { status: response.status, text: await response.text() };
	}
	"""
	result = await page.evaluate(script, api_user)
	if not result:
		print(f'[INFO] {account_name}: browser fetch returned nothing')
		return None
	if result.get('status') != 200:
		print(f'[INFO] {account_name}: browser fetch HTTP {result.get("status")}')
		return None
	try:
		payload = json.loads(result['text'])
	except Exception:  # nosec B110
		print(f'[INFO] {account_name}: browser fetch returned non-JSON (WAF challenge)')
		return None
	if payload.get('success') is True:
		data = payload.get('data')
		if isinstance(data, dict) and data.get('id'):
			return data
	message = payload.get('message') or payload.get('msg')
	print(f'[INFO] {account_name}: browser fetch success=false: {sanitize_login_message(message) or "no message"}')
	return None


def format_user_info_from_profile(profile: dict) -> dict:
	"""把 /api/user/self 的 data 对象转成 get_user_info 同构结果。"""

	return _user_info_from_data(profile)


def run_check_in_requests(
	all_cookies: dict,
	account: AccountConfig,
	account_name: str,
	provider_config,
	*,
	api_user_override: str | None = None,
	use_proxy: bool = False,
) -> tuple[bool, dict | None, dict | None, str | None]:
	"""执行 HTTP 签到请求，返回 (success, before, after, error_msg)。同步执行。"""
	try:
		client_kwargs: dict = {'http2': True, 'timeout': 30.0}
		proxy_url = get_proxy_server(use_proxy=use_proxy)
		if proxy_url:
			client_kwargs['proxy'] = proxy_url
			if is_debug_enabled():
				print(f'[INFO] {account_name}: HTTP client proxy enabled (url withheld)')
			else:
				print(f'[INFO] {account_name}: HTTP client proxy enabled')
		elif use_proxy:
			print(f'[WARN] {account_name}: Provider requires proxy but CHECKIN_PROXY_URL is not set')

		with httpx.Client(**client_kwargs) as client:
			client.cookies.update(all_cookies)

			headers = {
				'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
				'Accept': 'application/json, text/plain, */*',
				'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
				'Accept-Encoding': 'gzip, deflate, br, zstd',
				'Referer': provider_config.domain,
				'Origin': provider_config.domain,
				'Connection': 'keep-alive',
				'Sec-Fetch-Dest': 'empty',
				'Sec-Fetch-Mode': 'cors',
				'Sec-Fetch-Site': 'same-origin',
			}

			api_user = api_user_override or account.api_user
			if api_user:
				headers[provider_config.api_user_key] = api_user

			user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
			user_info_before = get_user_info(client, headers, user_info_url)
			if user_info_before and user_info_before.get('success'):
				# 金额不进日志：公开仓的 run 日志任何登录用户可读，余额等于把账号资产
				# 公开。数值只走 smart_notify 的飞书/邮件私有通道。
				print(f'[INFO] {account_name}: Pre check-in balance read ok')
			elif user_info_before:
				print(user_info_before.get('error', 'Unknown error'))

			if provider_config.needs_manual_check_in():
				if provider_config.check_in_status_path:
					month = datetime.now(SITE_TIMEZONE).strftime('%Y-%m')
					status_url = f'{provider_config.domain}{provider_config.check_in_status_path}?month={month}'
					if get_check_in_status(client, headers, status_url) is True:
						print(f'[SUCCESS] {account_name}: Already checked in today')
						user_info_after = get_user_info(client, headers, user_info_url)
						return True, user_info_before, user_info_after, None
				success, checkin_error = execute_check_in(client, account_name, provider_config, headers)
				user_info_after = get_user_info(client, headers, user_info_url)
				return success, user_info_before, user_info_after, checkin_error

			user_info_after = get_user_info(client, headers, user_info_url)
			if user_info_after and user_info_after.get('success'):
				print(f'[INFO] {account_name}: Check-in completed automatically (triggered by user info request)')
				return True, user_info_before, user_info_after, None
			error = user_info_after.get('error', 'Unknown error') if user_info_after else 'Unknown error'
			print(f'[FAILED] {account_name}: Auto check-in failed - {error}')
			return False, user_info_before, user_info_after, error

	except Exception as e:
		message = sanitize_login_message(f'{str(e)[:80]}...') or 'check-in request failed'
		print(f'[FAILED] {account_name}: Error occurred during check-in process - {message}')
		return False, None, None, message


async def main():
	"""主函数"""
	if is_debug_enabled():
		print('[INFO] DEBUG_MODE enabled')
		proxy_server = os.getenv('CHECKIN_PROXY_URL', '').strip()
		if proxy_server:
			print(f'[INFO] Proxy endpoint available: {proxy_server} (enabled per provider use_proxy)')
		else:
			print('[INFO] CHECKIN_PROXY_URL not set; providers with use_proxy=true will run without proxy')
	else:
		print('[INFO] Debug mode disabled (set DEBUG_MODE=true to enable screenshots and verbose logs)')

	print('[SYSTEM] AnyRouter.top multi-account auto check-in script started')
	print(f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

	app_config = AppConfig.load_from_env()
	print(f'[INFO] Loaded {len(app_config.providers)} provider configuration(s)')
	if is_debug_enabled():
		for provider_name, provider in sorted(app_config.providers.items()):
			print(f'[INFO] Provider "{provider_name}": use_proxy={provider.use_proxy}')

	accounts = load_accounts_config()
	if not accounts:
		print('[FAILED] 无法加载账号配置')
		smart_notify(
			[
				{
					'name': 'config',
					'success': False,
					'balance': 0,
					'balance_delta': 0,
					'used': 0,
					'used_delta': 0,
					'reward': 0,
				}
			]
		)
		sys.exit(1)

	print(f'[INFO] Found {len(accounts)} account configurations')

	retry_policy = RetryPolicy.from_env()
	egress_controller = EgressController.from_env()
	if egress_controller:
		print('[INFO] Egress controller enabled; selection is per account')
	else:
		print('[INFO] Egress controller unavailable; WAF retries will stop after one attempt')
	state_store = CheckinStateStore.load(os.getenv('CHECKIN_STATE_FILE', DEFAULT_STATE_FILE))
	force = _env_bool('CHECKIN_FORCE', False)

	last_snapshot = load_balance_snapshot()
	last_balance_hash = last_snapshot['hash'] if last_snapshot else None
	last_total_quota = last_snapshot['total_quota'] if last_snapshot else 0.0

	success_count = 0
	total_count = len(accounts)
	notification_content: list[str] = []
	current_balances: dict[str, dict[str, float]] = {}
	account_check_in_details: dict[str, dict[str, Any]] = {}
	need_notify = False
	balance_changed = False

	for i, account in enumerate(accounts):
		account_key = f'account_{i + 1}'
		state_key = account.get_state_key(i)
		try:
			provider_config = app_config.get_provider(account.provider)
			skip_reason = None
			if provider_config:
				skip_reason = state_store.skip_reason(
					state_key,
					daily_success_cooldown_hours=provider_config.daily_success_cooldown_hours,
					force=force,
				)

			if skip_reason:
				recent_success = state_store.is_recent_success(
					state_key,
					daily_success_cooldown_hours=provider_config.daily_success_cooldown_hours
					if provider_config
					else 0.0,
				)
				last_balance = state_store.last_balance(state_key)
				user_info_before = None
				if recent_success:
					success = True
					error_msg = None
					user_info_after = _user_info_from_state(last_balance or {})
					print(f'[SKIP] {account.get_log_label(i)}: {skip_reason}')
				else:
					success = False
					user_info_after = None
					error_msg = f'skipped: {skip_reason}'
					print(f'[SKIP] {account.get_log_label(i)}: {error_msg}')
			else:
				state_store.mark_attempt(state_key)
				success, user_info_before, user_info_after, error_msg = await check_in_account(
					account,
					i,
					app_config,
					egress_controller=egress_controller,
					retry_policy=retry_policy,
				)
				if success:
					state_store.mark_success(
						state_key,
						quota=(user_info_after or {}).get('quota'),
						used=(user_info_after or {}).get('used_quota'),
						bonus=(user_info_after or {}).get('bonus_quota'),
					)
				else:
					kind = classify_failure(error_msg or '')
					state_store.mark_failure(
						state_key,
						kind.value,
						retry_after_hours=_failure_retry_hours(kind),
					)

			if success:
				success_count += 1

			should_notify_this_account = False

			identity = account.get_identity(i)
			# Always record identity + success so failure notifications can name the account.
			account_check_in_details[account_key] = {
				'name': identity['name'],
				'email': identity['email'],
				'label': identity['label'],
				'success': success,
				'error': error_msg,
				'before_quota': 0.0,
				'before_used': 0.0,
				'before_bonus': 0.0,
				'after_quota': 0.0,
				'after_used': 0.0,
				'after_bonus': 0.0,
				'check_in_reward': 0.0,
				'usage_increase': 0.0,
				'balance_change': 0.0,
			}

			if not success:
				should_notify_this_account = True
				need_notify = True
				print(f'[NOTIFY] {account.get_log_label(i)} failed, will send notification')

			if user_info_after and user_info_after.get('success'):
				current_quota = float(user_info_after['quota'])
				current_used = float(user_info_after['used_quota'])
				current_bonus = float(user_info_after.get('bonus_quota', 0.0) or 0.0)
				current_balances[account_key] = {'quota': current_quota, 'used': current_used, 'bonus': current_bonus}
				account_check_in_details[account_key]['after_quota'] = current_quota
				account_check_in_details[account_key]['after_used'] = current_used
				account_check_in_details[account_key]['after_bonus'] = current_bonus

				if user_info_before and user_info_before.get('success'):
					before_quota = float(user_info_before['quota'])
					before_used = float(user_info_before['used_quota'])
					before_bonus = float(user_info_before.get('bonus_quota', 0.0) or 0.0)
					after_quota = current_quota
					after_used = current_used
					after_bonus = current_bonus

					total_before = before_quota + before_bonus + before_used
					total_after = after_quota + after_bonus + after_used

					check_in_reward = total_after - total_before
					usage_increase = after_used - before_used
					balance_change = (after_quota + after_bonus) - (before_quota + before_bonus)

					account_check_in_details[account_key].update(
						{
							'before_quota': before_quota,
							'before_used': before_used,
							'before_bonus': before_bonus,
							'after_quota': after_quota,
							'after_used': after_used,
							'after_bonus': after_bonus,
							'check_in_reward': check_in_reward,
							'usage_increase': usage_increase,
							'balance_change': balance_change,
						}
					)

			if should_notify_this_account:
				account_name = account.get_log_label(i)
				status = '[SUCCESS]' if success else '[FAIL]'
				account_result = f'{status} {account_name}'
				if error_msg:
					account_result += f'\n原因: {error_msg}'
				if user_info_after and user_info_after.get('success'):
					account_result += f'\n{user_info_after["display"]}'
				elif user_info_after:
					account_result += f'\n{user_info_after.get("error", "Unknown error")}'
				notification_content.append(account_result)

		except Exception as e:
			identity = account.get_identity(i)
			account_check_in_details[account_key] = {
				'name': identity['name'],
				'email': identity['email'],
				'label': identity['label'],
				'success': False,
				'error': str(e)[:80],
				'before_quota': 0.0,
				'before_used': 0.0,
				'before_bonus': 0.0,
				'after_quota': 0.0,
				'after_used': 0.0,
				'after_bonus': 0.0,
				'check_in_reward': 0.0,
				'usage_increase': 0.0,
				'balance_change': 0.0,
			}
			kind = classify_failure(str(e))
			state_store.mark_failure(
				state_key,
				kind.value,
				retry_after_hours=_failure_retry_hours(kind),
			)
			print(
				f'[FAILED] {account.get_log_label(i)} processing exception: {sanitize_login_message(e) or "unknown error"}'
			)
			need_notify = True
			notification_content.append(f'[FAIL] {account.get_log_label(i)} exception: {str(e)[:50]}...')

		current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
		current_total_quota = (
			sum(v['quota'] + float(v.get('bonus', 0.0) or 0.0) for v in current_balances.values())
			if current_balances
			else 0.0
		)
		if current_balance_hash:
			if last_balance_hash is None:
				balance_changed = True
				need_notify = True
				print('[NOTIFY] First run detected, will send notification with current balances')
			elif current_total_quota > last_total_quota:
				balance_changed = True
				need_notify = True
				print('[NOTIFY] Balance increased (sign-in reward), will send notification')
			elif current_balance_hash != last_balance_hash:
				print('[INFO] Balance decreased (consumption only), notification skipped')
			else:
				print('[INFO] No balance changes detected')

	state_store.save()

	if balance_changed:
		for i, account in enumerate(accounts):
			account_key = f'account_{i + 1}'
			if account_key in account_check_in_details:
				detail = account_check_in_details[account_key]
				account_name = str(detail['name'])
				account_result = format_check_in_notification(detail)
				if not any(account_name in item for item in notification_content):
					notification_content.append(account_result)

	if current_balance_hash:
		save_balance_snapshot({'hash': current_balance_hash, 'total_quota': current_total_quota})

	# 收集结构化结果 → 智能通知（飞书卡片 每次 / 邮件 按状态机）
	structured_results = []
	for i, account in enumerate(accounts):
		detail = account_check_in_details.get(f'account_{i + 1}', {})
		identity = account.get_identity(i)
		structured_results.append(
			{
				'name': detail.get('name', identity['name']),
				'email': detail.get('email', identity['email']),
				'label': detail.get('label', identity['label']),
				'success': detail.get('success', False),
				'error': detail.get('error'),
				'balance': float(detail.get('after_quota', 0) or 0) + float(detail.get('after_bonus', 0) or 0),
				'bonus': float(detail.get('after_bonus', 0) or 0),
				'balance_delta': float(detail.get('balance_change', 0) or 0),
				'used': float(detail.get('after_used', 0) or 0),
				'used_delta': float(detail.get('usage_increase', 0) or 0),
				'reward': float(detail.get('check_in_reward', 0) or 0),
			}
		)

	print(f'[NOTIFY] smart notify: {success_count}/{total_count} ok')
	sent = smart_notify(structured_results)
	print(f'[NOTIFY] feishu={"ok" if sent["feishu"] else "skip"}  email={"ok" if sent["email"] else "skip"}')
	strict = os.getenv('CHECKIN_STRICT', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
	if success_count == total_count:
		sys.exit(0)
	sys.exit(1 if strict or success_count == 0 else 0)


def run_main():
	"""运行主函数的包装函数"""
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		print('\n[WARNING] Program interrupted by user')
		sys.exit(1)
	except Exception as e:
		print(f'\n[FAILED] Error occurred during program execution: {sanitize_login_message(e) or "unknown error"}')
		sys.exit(1)


if __name__ == '__main__':
	run_main()
