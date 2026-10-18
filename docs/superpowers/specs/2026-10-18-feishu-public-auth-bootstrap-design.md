# 飞书授权引导公开接口设计

日期：2026-10-18
状态：已实现

## 1. 意图与背景

飞书 OAuth 登录由前端发起授权跳转，跳转链接需要 `app_id` 与 `redirect_uri`。此前这两个公开值只能从后台管理接口 `GET /api/v1/integrations/feishu` 读取，而该接口需要 `integration_feishu:read` 权限；未登录用户在登录页无法获取，只能硬编码或依赖部署配置。本设计新增未登录可读的引导接口，让登录页在渲染前获得当前生效的飞书应用标识与回调地址，同时保持 App Secret 永不出现。

## 2. 契约

`GET /api/v1/integrations/feishu/public`

- 无任何鉴权依赖：不要求登录、不要求 permission code，不写审计。
- 成功响应沿用统一包装 `{code: 0, message: "ok", data: ...}`，`data` 恰好三个字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `configured` | `bool` | 飞书登录是否已配置 |
| `app_id` | `string \| null` | 当前生效的飞书 App ID |
| `redirect_uri` | `string \| null` | 当前生效的回调地址，未配置时为 `null` |

- 数据来源复用 `app/services/feishu_integration.py::resolve_feishu_config`：
  - `source=NONE` → `{configured: false, app_id: null, redirect_uri: null}`
  - `source=ENVIRONMENT / DATABASE` → `{configured: true, app_id: 配置值, redirect_uri: 配置值或 null}`
- 数据库配置损坏（`8010 / 503`，含密文不可解密与载荷校验失败）时与登录、管理接口一致地抛出，绝不静默回退环境配置。
- 环境配置的回调地址非空但非法时，鉴权接口与登录接口同样返回 `422 / 1006`；公开接口保持一致，不做特殊吞并。

## 3. 安全边界

- 只暴露 App ID 与回调地址。二者本就是飞书开放平台上的公开信息，前端发起授权跳转也必须持有，不构成泄密面。
- 永不返回 `app_secret`、`source`、`updated_at`、`updated_by` 等管理端字段。公开 schema 只声明三个字段，service 也不从密文载荷投影其余字段。
- 响应与日志中不出现密文或 App Secret；实现不新增配置读取路径，复用既有加解密与损坏检测。
- 公开接口是只读 `GET`：不写审计、不发上游飞书请求（不验证凭证有效性），因此不能被用作探测凭证的接口。
- 不新增功能权限与后台菜单；管理端接口的权限与审计行为不变。

## 4. 实现

- `app/schemas/integration.py`：新增 `FeishuIntegrationPublicStatus`（`configured` / `app_id` / `redirect_uri`），加入 `__all__`。
- `app/services/feishu_integration.py`：新增 `get_public_feishu_status(session)`，调用 `resolve_feishu_config` 并只投影三个公开字段。
- `app/api/integrations.py`：新增 `GET /feishu/public` 路由（tag 集成），不挂 `require_permission` 与 `get_current_context` 依赖。
- 无数据库迁移、无新增权限、无审计路由注册。

## 5. 测试与验收

新增 `tests/test_feishu_public_api.py`：

1. 不注入 `AuthContext`、不带 `Authorization` 的匿名客户端可拿到 `200`（证明免登录）。
2. 环境未配置 → `configured=false`，`app_id` / `redirect_uri` 为 `null`。
3. monkeypatch 环境配置（HTTPS 回调）→ `configured=true`，返回值正确，响应 JSON 不含 `app_secret` 与 secret 值。
4. 数据库配置优先于环境配置，返回数据库 `app_id` / `redirect_uri`，响应不含 secret。
5. `data` 的键集合恰好 `{configured, app_id, redirect_uri}`（未配置与已配置两种状态）。
6. 数据库配置损坏 → `503` + `8010`，与登录、管理接口一致。

验收命令：

```bash
uv run pytest tests/test_feishu_public_api.py tests/test_feishu_integration_api.py tests/test_app_boot.py -q
uv run ruff check app tests
uv run mypy app
```

验收标准：上述命令全绿；`git status --short` 只包含本设计列出的文件。

## 6. 非目标

- 不暴露 `source`、`updated_at`、`updated_by` 等管理字段。
- 不返回 App Secret，也不返回飞书 access token。
- 不做配置缓存，不新增配置表或迁移。
- 不改变管理端接口、权限与审计行为。
- 不实现前端授权跳转流程本身。
