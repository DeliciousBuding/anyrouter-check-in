# NewAPI Check-in

[![GitHub Actions](https://github.com/DeliciousBuding/newapi-check-in/workflows/PR%20Quality%20Checks/badge.svg)](https://github.com/DeliciousBuding/newapi-check-in/actions)
[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![License](https://img.shields.io/github/license/DeliciousBuding/newapi-check-in)](LICENSE)

面向 NewAPI / OneAPI 的多账号自动签到工具，内置 AnyRouter 与 AgentRouter 支持，可直接运行在 GitHub Actions，也支持本地运行。

项目重点不是“能点一次按钮”，而是长期稳定运行：

- 浏览器登录与 WAF 挑战处理
- 主订阅 + 备用订阅代理池
- 按账号稳定选路，WAF 失败后再换出口
- 失败分类、同节点重试、出口轮换和每日成功门禁
- 每账号独立状态持久化，进程中途失败也不丢前一个账号的结果
- 飞书、邮件等通知
- 公开仓默认不缓存浏览器登录态

> 本项目起步于 [`millylee/anyrouter-check-in`](https://github.com/millylee/anyrouter-check-in)，当前按独立项目维护，与任何中转站没有隶属关系。

## 支持情况

| Provider | 登录方式 | 签到方式 | 代理 | 浏览器 Profile |
| --- | --- | --- | --- | --- |
| `anyrouter` | 邮箱密码，或 session cookie | 调用 `/api/user/sign_in` | 可选 | 可显式开启 |
| `agentrouter` | 邮箱密码 | 真实登录事件触发奖励 | 必须 | 默认关闭 |
| 自定义 NewAPI / OneAPI | 按站点配置 | 自定义签到接口 | 可选 | 可选 |

AgentRouter 没有独立的签到接口，`access_token` 不能替代一次真实登录。因此本项目把 AgentRouter 的登录校验本身当作签到动作；不要用“只有 token 的 HTTP 请求”来判断它签到成功。

## 快速开始

### 1. 创建自己的仓库

点击 GitHub 页面右上角的 `Fork`，或克隆本仓库作为自己的起点。

### 2. 配置生产环境

在仓库中进入：

`Settings -> Environments -> New environment`

新建环境 `production`，然后在该环境中添加账号 Secret：

- `ANYROUTER_ACCOUNTS`：账号 JSON 数组，必填
- `PROVIDERS`：自定义 provider 配置，可选

代理相关 Secret：

- `PROXY_SUBSCRIPTION_URL`：主订阅，可选但 AgentRouter 必需
- `PROXY_SUBSCRIPTION_URL_FALLBACK`：备用订阅，可选；主订阅未配置时可单独使用
- `PROXY_NODE_FILTER`：节点名过滤正则，可选

通知相关 Secret：

- `FEISHU_WEBHOOK`：飞书机器人 Webhook，可选
- `EMAIL_USER`、`EMAIL_PASS`、`EMAIL_TO`：邮件通知，可选
- `CUSTOM_SMTP_SERVER`：自定义 SMTP，可选

不要把账号、密码、cookie、token、订阅链接写进仓库文件。

### 3. 启用 Actions

进入 `Actions`，启用 `NewAPI 自动签到` 和 `Keepalive`。首次运行建议手动触发一次：

`Actions -> NewAPI 自动签到 -> Run workflow`

## 账号配置

`ANYROUTER_ACCOUNTS` 使用 JSON 数组：

```json
[
  {
    "name": "AnyRouter 主账号",
    "provider": "anyrouter",
    "email": "account1@example.com",
    "password": "<your-password>"
  },
  {
    "name": "AgentRouter 账号",
    "provider": "agentrouter",
    "email": "account2@example.com",
    "password": "<your-password>"
  }
]
```

字段说明：

- `email` + `password`：推荐的浏览器登录方式，登录成功后自动获取 cookie 和用户标识
- `cookies`：兼容旧版 session cookie 登录
- `api_user`：cookie 模式下用于请求头的 `new-api-user`；邮箱密码模式通常不需要
- `provider`：可选，默认 `anyrouter`
- `name`：可选，用于日志和通知中的显示名称

### Cookie 模式

如果不使用邮箱密码，也可以在浏览器登录后获取 session：

1. 打开目标站点并登录
2. F12 -> Application / 存储 -> Cookies
3. 复制 `session` 的值
4. F12 -> Network -> Fetch/XHR，找到 `New-Api-User` 请求头
5. 把 `session` 和 `api_user` 填入账号 JSON

![获取 session](./assets/request-session.png)

![获取 api_user](./assets/request-api-user.png)

cookie 过期后通常返回 401，需要重新获取。
## Provider 说明

### AnyRouter

- 每轮先查询签到状态，已经签到则跳过写请求
- 邮箱密码登录优先，失败时才走其他认证路径
- 默认允许浏览器 profile 持久化，但公开仓必须显式设置仓库变量 `ENABLE_BROWSER_PROFILE_CACHE=true` 才会缓存

### AgentRouter

- 没有独立签到接口，真实登录本身触发奖励
- 必须通过代理运行；代理未就绪时 fail-closed，不会用数据中心 IP 直连硬撞 WAF
- 默认不持久化浏览器 profile，避免把登录态缓存到公开仓
- 同一 UTC 日历日成功一次后跳过重复登录，跨日重试至少间隔 6 小时

### 自定义 Provider

通过 `PROVIDERS` 添加其他 NewAPI / OneAPI 站点：

```json
{
  "customrouter": {
    "domain": "https://custom.example.com",
    "login_path": "/login",
    "sign_in_path": "/api/user/sign_in",
    "check_in_status_path": "/api/user/checkin",
    "user_info_path": "/api/user/self",
    "api_user_key": "new-api-user",
    "bypass_method": "waf_cookies",
    "waf_cookie_names": ["acw_tc", "cdn_sec_tc", "acw_sc__v2"],
    "use_proxy": false,
    "persist_profile": false
  }
}
```

`bypass_method` 为空时直接使用 cookie 请求；设置为 `waf_cookies` 时，先由 CloakBrowser 获取 WAF cookie，再执行签到。

## 代理与 WAF

代理方案使用 mihomo：

- 主订阅和备用订阅共同进入 `CHECKIN_AUTO` 健康检查池
- 正常情况下复用当前健康节点，避免每个账号把出口跳来跳去
- 只有 WAF 或人机验证才排除当前节点，并按账号 hash 选择稳定备用节点
- 节点名、订阅 URL、出口 IP 不进入公开日志
- AgentRouter 的代理是硬要求；代理不可用时跳过而不是直连

本地运行时可以直接使用已有代理：

```bash
CHECKIN_PROXY_URL=http://127.0.0.1:7890
```

## 重试与每日成功

生产 workflow 默认每 6 小时运行一次。每次运行按账号独立处理，失败不会阻断后续账号。

失败类型会先分类，再决定动作：

- WAF / 人机验证：换出口
- 网络瞬断：原节点重试一次，仍失败再换出口
- 认证失败：不换出口，直接告警，避免拿错误密码撞多个 IP
- 代理未就绪：AgentRouter 直接跳过，不直连
- 站点 5xx / 限流：按预算停止或重试

默认重试预算：

- `CHECKIN_MAX_ATTEMPTS=4`
- `CHECKIN_MAX_EGRESS_ROTATIONS=3`
- `CHECKIN_MAX_BACKOFF_HOURS=6`

本项目的生产 workflow 使用 `5` 次尝试 / `4` 次换节点，覆盖“WAF -> 断流 -> WAF”的混合失败。需要更强或更保守时可以调整这些环境变量。

AgentRouter 以 UTC 日历日判断成功，并在成功后的 6 小时内跳过重复登录；这既保证每天至少尝试一次，也避免短时间反复登录触发站点风控。

## 状态持久化与隐私

以下文件通过 GitHub Actions cache 保存，属于非敏感运行状态：

- `checkin_state.json`：每账号最近成功/失败时间、失败分类、余额
- `notify_state.json`：通知去重状态
- `balance_snapshot.json`：余额变化检测

`checkin_state.json` 在每个账号处理完后立即原子写入，后续账号异常时前面成功账号的状态仍会保存。它不包含邮箱、密码、cookie 或 token。

浏览器 profile 可能包含登录态，因此：

- 公开仓默认不缓存 `.browser_profiles`
- 只有私有仓库或自建 runner 才建议设置 `ENABLE_BROWSER_PROFILE_CACHE=true`
- `DEBUG_MODE=true` 可能生成截图和详细日志，公开仓不要开启

## 通知

当前 workflow 支持：

- 飞书 Webhook
- 邮件 SMTP

通知策略：

- 签到失败时发送
- 首次成功或余额增加达到阈值时发送
- 全部正常时每天最多发送一次

其他通知通道仍保留在脚本中，但生产 workflow 默认不注入对应 Secret。

## Keepalive

公开仓的 scheduled workflow 在 60 天无仓库活动后会被 GitHub 自动禁用。仓库内的 `Keepalive` workflow 每周通过 GitHub REST API 重新启用签到、Keepalive 和配置健康检查 workflow，从而重置不活动计时。

保活只能防止“定时任务被 GitHub 自动禁用”，不能替代签到逻辑本身的重试和代理容错。

## 故障排除

如果签到失败，按顺序检查：

1. `ANYROUTER_ACCOUNTS` 是否为合法 JSON
2. 邮箱密码是否正确，cookie 是否过期
3. cookie 模式下 `api_user` 是否正确
4. AgentRouter 是否配置了可用的 `PROXY_SUBSCRIPTION_URL` 或 `CHECKIN_PROXY_URL`
5. 代理出口是否被目标站下发人机验证
6. 目标站是否修改了登录页或签到接口
7. Actions 日志中的失败分类和重试动作

### 定向重试失败账号

如果某一次运行只有一个账号失败，不需要重跑所有账号：

1. 打开 `Actions -> NewAPI 自动签到 -> Run workflow`
2. `account` 填日志中的序号或标签，例如 `5` 或 `agentrouter-5`
3. 需要绕过当天成功门禁或失败 backoff 时，勾选 `force`
4. 运行一次

`account` 只接受序号或 `<provider>-<序号>`，不要填邮箱或账号名，避免把身份信息写进公开 Actions 日志。定向运行不会覆盖全局余额快照。

## 本地开发

```bash
uv sync --dev
uv run python -m cloakbrowser install
uv run checkin.py
```

常用检查：

```bash
uv run ruff check .
uv run ruff format .
uv run mypy .
uv run bandit -r . -c pyproject.toml
uv run pytest tests/
```

## 贡献

欢迎提交 Issue 和 Pull Request。涉及登录流程、代理选路、浏览器指纹或状态持久化的改动，请同时补充对应测试和边界说明。

## 来源与许可

本项目起步于 `millylee/anyrouter-check-in`，保留原项目许可和 Git 历史。后续针对 GHA、WAF、代理轮换、状态持久化、通知和 AgentRouter 的定制均在本仓库独立维护。

## 免责声明

本脚本仅用于学习和研究。使用前请确认遵守目标网站的服务条款，并自行承担账号、网络出口和自动化操作的风险。
