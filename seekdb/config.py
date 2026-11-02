from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


log = logging.getLogger("seekdb.config")


def _value(env: dict[str, str], name: str, *aliases: str, default: str = "") -> str:
    for key in (name, *aliases):
        value = env.get(key)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _bool(value: str, name: str) -> bool:
    val = value.strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off", ""):
        return False
    raise ConfigError(f"{name} must be a boolean")


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    host: str = "0.0.0.0"
    port: int = 8787
    public_url: str = ""
    pool_mode: str = ""
    pool_enabled: bool = False
    store: str = "fs"
    pool_read_key: str = ""
    pool_write_key: str = ""
    pool_url: str = ""
    pool_share: bool = False
    pool_timeout_s: float = 2.0
    public_read: bool = False
    admin_token: str = ""
    max_package_bytes: int = 16 * 1024 * 1024
    max_storage_bytes: int = 0
    device_daily_bytes: int = 2 * 1024 * 1024 * 1024
    device_daily_packages: int = 100
    upload_requests_per_min: int = 60
    telegram_bot_token: str = ""
    telegram_channel_id: str = ""
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session: str = ""
    telegram_session_file: Path | None = None
    pool_channel_scan_limit: int = 2000
    pool_index_refresh_s: int = 300
    contribution: bool = False
    librarian: str = ""
    librarian_key: str = ""
    librarian_enabled: bool = False
    bootstrap_invite: bool = True
    bootstrap_invite_uses: int = 100
    bootstrap_invite_ttl_s: int = 30 * 86400

    @property
    def effective_pool_mode(self) -> str:
        if self.pool_mode:
            return self.pool_mode
        return "host" if self.pool_enabled else "local"

    @property
    def pool_host_enabled(self) -> bool:
        return self.effective_pool_mode == "host"

    @property
    def pool_upload(self) -> bool:
        return self.contribution if self.effective_pool_mode == "channel" else bool(self.pool_write_key)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        mode = _value(env, "POOL_MODE", "OPENSEEK_POOL_MODE").lower()
        pool_enabled = _bool(_value(env, "POOL_ENABLED", "OPENSEEK_POOL_ENABLED", default="0"), "POOL_ENABLED")
        effective_mode = mode or ("host" if pool_enabled else "local")
        store = _value(env, "SEEKDB_STORE", "OPENSEEK_STORE", default="fs").lower()
        if mode == "channel":
            store = "telegram"
        try:
            port = int(_value(env, "SEEKDB_PORT", "OPENSEEK_PORT", default="8787"))
            max_mb = int(_value(env, "SEEKDB_MAX_PACKAGE_MB", default="16"))
            max_gb = int(_value(env, "SEEKDB_MAX_STORAGE_GB", default="0"))
            device_gb = int(_value(env, "SEEKDB_DEVICE_DAILY_GB", default="2"))
            device_packages = int(_value(env, "SEEKDB_DEVICE_DAILY_PACKAGES", default="100"))
            upload_rpm = int(_value(env, "SEEKDB_UPLOADS_PER_MIN", default="60"))
            api_id = int(_value(env, "TELEGRAM_API_ID", "OPENSEEK_TELEGRAM_API_ID", default="0"))
            scan_limit = int(_value(env, "POOL_CHANNEL_SCAN_LIMIT", "OPENSEEK_POOL_CHANNEL_SCAN_LIMIT", default="2000"))
            refresh = int(_value(env, "POOL_INDEX_REFRESH_S", "OPENSEEK_POOL_INDEX_REFRESH_S", default="300"))
            invite_uses = int(_value(env, "SEEKDB_BOOTSTRAP_INVITE_USES", default="100"))
            invite_ttl = int(_value(env, "SEEKDB_BOOTSTRAP_INVITE_TTL", default="30d").rstrip("d"))
            pool_timeout = float(_value(env, "POOL_TIMEOUT_S", "OPENSEEK_POOL_TIMEOUT_S", default="2.0"))
        except ValueError as exc:
            raise ConfigError("integer configuration value is invalid") from exc
        if mode not in ("", "host", "channel"):
            raise ConfigError("POOL_MODE must be host or channel")
        if store not in ("fs", "telegram"):
            raise ConfigError("SEEKDB_STORE must be fs or telegram")
        if not 1 <= port <= 65535:
            raise ConfigError("SEEKDB_PORT must be between 1 and 65535")
        if not 1 <= max_mb <= 16:
            raise ConfigError("SEEKDB_MAX_PACKAGE_MB must be between 1 and 16 for v1")
        if max_gb < 0:
            raise ConfigError("SEEKDB_MAX_STORAGE_GB cannot be negative")
        if device_gb < 0 or device_packages < 0 or upload_rpm < 1:
            raise ConfigError("daily upload quotas cannot be negative and SEEKDB_UPLOADS_PER_MIN must be positive")
        if mode == "channel" or (mode == "host" and store == "telegram"):
            if not _value(env, "TELEGRAM_CHANNEL_ID", "OPENSEEK_TELEGRAM_CHANNEL_ID"):
                raise ConfigError("Telegram storage requires TELEGRAM_CHANNEL_ID")
            if mode == "channel" and (api_id <= 0 or not _value(env, "TELEGRAM_API_HASH", "OPENSEEK_TELEGRAM_API_HASH")):
                raise ConfigError("channel mode requires TELEGRAM_API_ID and TELEGRAM_API_HASH")
            if mode == "channel" and not (_value(env, "TELEGRAM_SESSION", "OPENSEEK_TELEGRAM_SESSION") or
                                            _value(env, "TELEGRAM_SESSION_FILE", "OPENSEEK_TELEGRAM_SESSION_FILE")):
                raise ConfigError("channel mode requires a user TELEGRAM_SESSION or TELEGRAM_SESSION_FILE for history scans")
            if mode == "host" and not _value(env, "TELEGRAM_BOT_TOKEN", "OPENSEEK_TELEGRAM_BOT_TOKEN"):
                raise ConfigError("host Telegram storage requires TELEGRAM_BOT_TOKEN")
        pool_url = _value(env, "POOL_URL", "OPENSEEK_POOL_URL")
        pool_share = _bool(_value(env, "POOL_SHARE", "OPENSEEK_POOL_SHARE", default="0"), "POOL_SHARE")
        pool_read_key = _value(env, "POOL_READ_KEY", "OPENSEEK_POOL_READ_KEY")
        pool_write_key = _value(env, "POOL_WRITE_KEY", "OPENSEEK_POOL_WRITE_KEY")
        if pool_url and effective_mode == "host":
            parsed_pool = urlsplit(pool_url)
            local_http = parsed_pool.scheme == "http" and parsed_pool.hostname in ("127.0.0.1", "localhost", "::1")
            if (parsed_pool.scheme != "https" and not local_http) or not parsed_pool.hostname or parsed_pool.username or parsed_pool.password:
                raise ConfigError("POOL_URL must use HTTPS (HTTP is allowed only for loopback testing)")
            if parsed_pool.query or parsed_pool.fragment:
                raise ConfigError("POOL_URL must not include a query or fragment")
            if len(pool_read_key) < 24:
                raise ConfigError("POOL_URL requires a POOL_READ_KEY of at least 24 characters")
            if not 0.25 <= pool_timeout <= 15:
                raise ConfigError("POOL_TIMEOUT_S must be between 0.25 and 15")
        if pool_share and effective_mode == "host" and (not pool_url or len(pool_write_key) < 24):
            raise ConfigError("POOL_SHARE requires POOL_URL and a POOL_WRITE_KEY of at least 24 characters")
        if effective_mode != "host" and (pool_url or pool_share):
            log.warning("POOL_URL and POOL_SHARE are host-mode settings and are ignored in %s mode", effective_mode)
        if _bool(_value(env, "LIBRARIAN_ENABLED", "OPENSEEK_LIBRARIAN_ENABLED", default="0"), "LIBRARIAN_ENABLED") and not _value(
                env, "TELEGRAM_BOT_TOKEN", "OPENSEEK_TELEGRAM_BOT_TOKEN"):
            raise ConfigError("LIBRARIAN_ENABLED requires TELEGRAM_BOT_TOKEN")
        url = _value(env, "SEEKDB_PUBLIC_URL", default="")
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.query or parsed.fragment:
                raise ConfigError("SEEKDB_PUBLIC_URL must be an HTTP(S) origin without query or fragment")
        session_path = _value(env, "TELEGRAM_SESSION_FILE", "OPENSEEK_TELEGRAM_SESSION_FILE")
        return cls(
            data_dir=Path(_value(env, "SEEKDB_DATA", "OPENSEEK_DATA_DIR", default="data")).expanduser(),
            host=_value(env, "SEEKDB_HOST", "OPENSEEK_HOST", default="0.0.0.0"),
            port=port,
            public_url=url,
            pool_mode=mode,
            pool_enabled=pool_enabled,
            store=store,
            pool_read_key=pool_read_key,
            pool_write_key=pool_write_key,
            pool_url=pool_url,
            pool_share=pool_share,
            pool_timeout_s=pool_timeout,
            public_read=_bool(_value(env, "SEEKDB_PUBLIC_READ", default="0"), "SEEKDB_PUBLIC_READ"),
            admin_token=_value(env, "SEEKDB_ADMIN_TOKEN", "OPENSEEK_ADMIN_TOKEN"),
            max_package_bytes=max_mb * 1024 * 1024,
            max_storage_bytes=max_gb * 1024 * 1024 * 1024,
            device_daily_bytes=device_gb * 1024 * 1024 * 1024,
            device_daily_packages=device_packages,
            upload_requests_per_min=upload_rpm,
            telegram_bot_token=_value(env, "TELEGRAM_BOT_TOKEN", "OPENSEEK_TELEGRAM_BOT_TOKEN"),
            telegram_channel_id=_value(env, "TELEGRAM_CHANNEL_ID", "OPENSEEK_TELEGRAM_CHANNEL_ID"),
            telegram_api_id=api_id,
            telegram_api_hash=_value(env, "TELEGRAM_API_HASH", "OPENSEEK_TELEGRAM_API_HASH"),
            telegram_session=_value(env, "TELEGRAM_SESSION", "OPENSEEK_TELEGRAM_SESSION"),
            telegram_session_file=Path(session_path).expanduser() if session_path else None,
            pool_channel_scan_limit=scan_limit,
            pool_index_refresh_s=refresh,
            contribution=_bool(_value(env, "CONTRIBUTION", "OPENSEEK_CONTRIBUTION", default="0"), "CONTRIBUTION"),
            librarian=_value(env, "LIBRARIAN", "OPENSEEK_LIBRARIAN"),
            librarian_key=_value(env, "LIBRARIAN_KEY", "OPENSEEK_LIBRARIAN_KEY"),
            librarian_enabled=_bool(_value(env, "LIBRARIAN_ENABLED", "OPENSEEK_LIBRARIAN_ENABLED", default="0"), "LIBRARIAN_ENABLED"),
            bootstrap_invite=_bool(_value(env, "SEEKDB_BOOTSTRAP_INVITE", default="1"), "SEEKDB_BOOTSTRAP_INVITE"),
            bootstrap_invite_uses=invite_uses,
            bootstrap_invite_ttl_s=invite_ttl * 86400,
        )
