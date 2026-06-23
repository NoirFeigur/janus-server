from enum import StrEnum


class ActiveStatus(StrEnum):
    active = "active"  # Enabled configuration record.
    disabled = "disabled"  # Manually disabled configuration record.


class UsageStatus(StrEnum):
    success = "success"  # LLM call completed successfully.
    error = "error"  # LLM call failed with gateway or upstream error.
    timeout = "timeout"  # LLM call timed out.


class AuditOutcome(StrEnum):
    success = "success"  # Operation/login succeeded.
    failure = "failure"  # Operation/login failed.


class LoginFailureReason(StrEnum):
    bad_credentials = "bad_credentials"  # Username/password mismatch.
    user_disabled = "user_disabled"  # Account is disabled.
    user_not_found = "user_not_found"  # No such username.
    account_locked = "account_locked"  # Login refused: too many recent failures (lockout).


class UserStatus(StrEnum):
    active = "active"  # Active employee user.
    disabled = "disabled"  # Disabled or departed employee user.


class OAuthSource(StrEnum):
    wecom = "wecom"  # WeCom identity source.


class ApiKeyStatus(StrEnum):
    active = "active"  # API key may authenticate requests.
    disabled = "disabled"  # API key has been revoked.


class MenuType(StrEnum):
    catalog = "catalog"  # Grouping node without a routable page.
    menu = "menu"  # Routable page node.
    button = "button"  # Fine-grained operation permission.


class DataScope(StrEnum):
    all_data = "all"  # All department data.
    custom = "custom"  # Explicit department set from role-department grants.
    dept_only = "dept"  # Current user's own department only.
    dept_and_child = "dept_and_child"  # Current department and descendants.
    self_only = "self"  # Records created by the current user only.
    dept_and_child_or_self = "dept_and_child_or_self"  # Department subtree plus own records.


class ChannelStatus(StrEnum):
    active = "active"  # Channel participates in Router construction.
    disabled = "disabled"  # Channel is excluded from routing.


class ChannelKeyStatus(StrEnum):
    active = "active"  # Upstream key participates in channel pool routing.
    disabled = "disabled"  # Upstream key is manually disabled.


class GrantScope(StrEnum):
    user = "user"  # Grant applies to a single user.
    department = "department"  # Grant applies to a department and its members.


class QuotaScope(StrEnum):
    user = "user"  # Personal quota.
    department = "department"  # Department aggregate quota.
    global_ = "global"  # Platform-wide fallback quota.


class QuotaPeriod(StrEnum):
    daily = "daily"  # Resets every day.
    monthly = "monthly"  # Resets every month.
    total = "total"  # Never resets automatically.


class QuotaMetric(StrEnum):
    tokens = "tokens"  # Token-based quota.
    requests = "requests"  # Request-count quota.
    cost = "cost"  # Internal cost-point quota.


class RateLimitScope(StrEnum):
    user = "user"  # Per-user rate limit.
    department = "department"  # Per-department rate limit.
    global_ = "global"  # Platform-wide rate limit.
    api_key = "api_key"  # Per-API-key rate limit.


class ConfigValueType(StrEnum):
    string = "string"  # Raw string value, used as-is.
    int = "int"  # Parsed as a base-10 integer.
    bool = "bool"  # Parsed as boolean (true/1/yes/on → True).
    json = "json"  # Parsed as a JSON document (object/array/scalar).


class AttachBizType(StrEnum):
    avatar = "avatar"  # User profile avatar image (object key under avatar/).
    attachment = "attachment"  # Generic business attachment (export/upload).


class ErrorCode(StrEnum):
    auth_invalid_token = "auth.invalid_token"  # JWT or sk-key is invalid or expired.
    auth_token_revoked = "auth.token_revoked"  # Token's session was revoked (logout/kick/reuse).
    auth_refresh_invalid = "auth.refresh_invalid"  # Refresh token unknown/expired/already rotated.
    auth_account_locked = "auth.account_locked"  # Login refused: too many recent failures.
    auth_password_too_weak = "auth.password_too_weak"  # New password fails strength policy.
    auth_user_disabled = "auth.user_disabled"  # Authenticated user is disabled.
    auth_forbidden = "auth.forbidden"  # Authenticated principal lacks permission.
    attach_not_found = "attach.not_found"  # Referenced attachment does not exist.
    attach_invalid_image = "attach.invalid_image"  # Uploaded file is not a decodable image.
    attach_too_large = "attach.too_large"  # Uploaded file exceeds the size limit.
    model_not_granted = "model.not_granted"  # Principal is not granted the logical model.
    model_not_found = "model.not_found"  # Logical model does not exist or is disabled.
    model_unavailable = "model.unavailable"  # Logical model has no active deployments.
    model_no_channel = "model.no_available_channel"  # Logical model has no usable channel.
    quota_exceeded = "quota.exceeded"  # Quota limit has been reached.
    rate_limit_exceeded = "rate_limit.exceeded"  # Hard rate limit (RPM/TPM/concurrent) breached.
    upstream_error = "upstream.error"  # Upstream provider returned an error.
    upstream_timeout = "upstream.timeout"  # Upstream provider timed out.
    upstream_rate_limited = "upstream.rate_limited"  # Upstream pool is rate-limited.
    request_invalid = "request.invalid"  # Request payload or parameters are invalid.
    request_conflict = "request.conflict"  # Write violates a uniqueness/FK constraint.
    internal_error = "internal.error"  # Unexpected platform error.
