from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from seekdb import __version__
from seekdb.auth import DeviceAuth
from seekdb.cache import secure_mkdir
from seekdb.channel import ChannelReadService
from seekdb.channel_index import ChannelIndex
from seekdb.config import ConfigError, Settings
from seekdb.index.sqlite import SQLiteIndex
from seekdb.pairing import InviteManager
from seekdb.pool import PoolService
from seekdb.remote import RemotePoolClient
from seekdb.store.fs import FileSystemObjectStore
from seekdb.store.telegram_bot import TelegramBotStore, TelegramObjectStore
from seekdb.telegram_mt import TelegramMTProtoStore
from seekdb.tls import ensure_certificate, spki_pin_from_cert_pem


def _settings() -> Settings:
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    return Settings.from_env()


def _runtime(settings: Settings):
    secure_mkdir(settings.data_dir)
    index = SQLiteIndex(settings.data_dir / "seekdb.db")
    cert, key, pin = ensure_certificate(settings.data_dir)
    auth = DeviceAuth(index)
    invites = InviteManager(index, pin, auth.register)
    if settings.effective_pool_mode == "channel":
        # Channel history always uses the owner's user session. The bot token,
        # when configured, belongs to the separate librarian client.
        user_settings = replace(settings, telegram_bot_token="", librarian_enabled=False)
        store = TelegramMTProtoStore(user_settings)
        pool = None
    elif settings.effective_pool_mode == "host" and settings.store == "telegram":
        store = TelegramObjectStore(TelegramBotStore(settings.telegram_bot_token, settings.telegram_channel_id), index)
        remote = (RemotePoolClient(settings.pool_url, settings.pool_read_key, settings.pool_write_key,
                                   settings.pool_timeout_s) if settings.pool_url else None)
        pool = PoolService(index, store, settings.pool_read_key, settings.pool_write_key,
                           settings.max_storage_bytes, remote, settings.pool_share)
    else:
        store = FileSystemObjectStore(settings.data_dir)
        remote = (RemotePoolClient(settings.pool_url, settings.pool_read_key, settings.pool_write_key,
                                   settings.pool_timeout_s) if settings.pool_url else None)
        pool = PoolService(index, store, settings.pool_read_key, settings.pool_write_key,
                           settings.max_storage_bytes, remote, settings.pool_share)
    return index, cert, key, pin, auth, invites, store, pool


def _print_json(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _command_serve(settings: Settings) -> int:
    import uvicorn

    from seekdb.api.app import create_app

    index, cert, key, pin, auth, invites, store, _pool = _runtime(settings)
    if settings.effective_pool_mode in ("host", "channel") and settings.bootstrap_invite and not index.q("SELECT 1 FROM invites LIMIT 1"):
        role = "reader" if settings.effective_pool_mode == "channel" else "contributor"
        invitation = invites.create(label="bootstrap", role=role, ttl_s=settings.bootstrap_invite_ttl_s,
                                    max_uses=settings.bootstrap_invite_uses, contribute=role == "contributor")
        print(f"Initial {role} invite (shown once): {invitation['code']}")
    if settings.public_url:
        print(f"SeekDB URL: {settings.public_url}")
    print(f"SeekDB TLS pin: {pin}")
    print(f"SeekDB mode: {settings.effective_pool_mode}; store: {settings.store}")
    app = create_app(settings, index=index, store=store)
    uvicorn.run(app, host=settings.host, port=settings.port, ssl_certfile=str(cert), ssl_keyfile=str(key),
                access_log=False, server_header=False, date_header=False)
    return 0


def _command_invite(args, settings: Settings) -> int:
    index, _cert, _key, _pin, _auth, invites, _store, _pool = _runtime(settings)
    try:
        if args.invite_action == "create":
            result = invites.create(label=args.label, role=args.role, ttl_s=args.ttl_s,
                                    max_uses=args.uses, contribute=args.role in ("guest", "contributor"))
            _print_json(result)
        elif args.invite_action == "list":
            _print_json({"invites": invites.listing()})
        elif args.invite_action == "revoke":
            _print_json({"revoked": invites.revoke(args.invite_id)})
        else:
            raise ConfigError("choose invite create, list, or revoke")
    finally:
        index.close()
    return 0


def _command_init(settings: Settings) -> int:
    index, cert, key, pin, _auth, _invites, _store, _pool = _runtime(settings)
    index.close()
    print(f"Initialized data directory: {settings.data_dir}")
    print(f"TLS certificate: {cert}")
    print(f"TLS key: {key}")
    print(f"TLS pin: {pin}")
    return 0


def _command_backup(destination: Path, settings: Settings) -> int:
    index, _cert, _key, _pin, _auth, _invites, _store, _pool = _runtime(settings)
    try:
        destination = destination.expanduser().resolve()
        if destination.exists() and any(destination.iterdir()):
            raise ConfigError("backup destination must be empty")
        secure_mkdir(destination)
        if destination.stat().st_mode & 0o077:
            destination.chmod(0o700)
        target_db = destination / "seekdb.db"
        with SQLiteIndex(target_db) as as_backup:
            index.conn.backup(as_backup.conn)
        object_source = settings.data_dir / "objects"
        if object_source.exists():
            shutil.copytree(object_source, destination / "objects", dirs_exist_ok=True)
        tls_source = settings.data_dir / "tls"
        if tls_source.exists():
            shutil.copytree(tls_source, destination / "tls", dirs_exist_ok=True)
        for private_name in ("admin.token", "secret.key"):
            private_source = settings.data_dir / private_name
            if private_source.is_file():
                shutil.copy2(private_source, destination / private_name)
                private_source_copy = destination / private_name
                if private_source_copy.stat().st_mode & 0o077:
                    private_source_copy.chmod(0o600)
        telegram_source = settings.data_dir / "telegram"
        if telegram_source.exists():
            shutil.copytree(telegram_source, destination / "telegram", dirs_exist_ok=True)
        print(f"Backup written to {destination}")
    finally:
        index.close()
    return 0


def _command_restore(source: Path, settings: Settings) -> int:
    source = source.expanduser().resolve()
    if not (source / "seekdb.db").is_file():
        raise ConfigError("backup does not contain seekdb.db")
    if settings.data_dir.exists() and any(settings.data_dir.iterdir()):
        raise ConfigError("SEEKDB_DATA must be empty before restore")
    secure_mkdir(settings.data_dir)
    shutil.copy2(source / "seekdb.db", settings.data_dir / "seekdb.db")
    if (source / "objects").is_dir():
        shutil.copytree(source / "objects", settings.data_dir / "objects", dirs_exist_ok=True)
    if (source / "tls").is_dir():
        shutil.copytree(source / "tls", settings.data_dir / "tls", dirs_exist_ok=True)
    for private_name in ("admin.token", "secret.key"):
        private_source = source / private_name
        if private_source.is_file():
            shutil.copy2(private_source, settings.data_dir / private_name)
            (settings.data_dir / private_name).chmod(0o600)
    if (source / "telegram").is_dir():
        shutil.copytree(source / "telegram", settings.data_dir / "telegram", dirs_exist_ok=True)
    print(f"Restored backup from {source}")
    return 0


def _command_gc(args, settings: Settings) -> int:
    index, _cert, _key, _pin, _auth, _invites, _store, pool = _runtime(settings)
    try:
        if pool is None:
            raise ConfigError("garbage collection is not available in channel mode")
        _print_json(pool.gc(dry_run=not args.execute))
    finally:
        index.close()
    return 0


def _command_pool(args, settings: Settings) -> int:
    if settings.effective_pool_mode != "channel":
        raise ConfigError("pool scan and rebuild are channel-mode operations")
    index, _cert, _key, _pin, _auth, _invites, store, _pool = _runtime(settings)
    channel_index = ChannelIndex(index, store, channel_id=settings.telegram_channel_id,
                                 scan_limit=settings.pool_channel_scan_limit,
                                 refresh_s=settings.pool_index_refresh_s)
    try:
        store.start()
        if args.pool_action == "scan":
            _print_json(channel_index.refresh(force=True))
        elif args.pool_action == "rebuild":
            _print_json(channel_index.rebuild())
        else:
            _print_json(channel_index.status())
    finally:
        channel_index.stop()
        store.close()
        index.close()
    return 0


def _command_telegram(args, settings: Settings) -> int:
    if settings.effective_pool_mode != "channel":
        raise ConfigError("Telegram user login is used by channel mode")
    store = TelegramMTProtoStore(replace(settings, telegram_bot_token=""))
    try:
        store.login()
        print("Telegram session authorized and stored with owner-only permissions.")
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seekdb", description="Standalone Nuvio-compatible thumbnail pool")
    parser.add_argument("--version", action="version", version=f"seekdb {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create the data directory and TLS identity")
    commands.add_parser("serve", help="run the HTTPS pool API")
    commands.add_parser("status", help="show local pool counts")
    commands.add_parser("pin", help="print the TLS SPKI pin")
    invite = commands.add_parser("invite", help="manage pairing invites")
    invite_sub = invite.add_subparsers(dest="invite_action", required=True)
    create = invite_sub.add_parser("create")
    create.add_argument("--label", default="")
    create.add_argument("--role", choices=("reader", "contributor", "guest"), default="contributor")
    create.add_argument("--ttl-s", type=int, default=86400)
    create.add_argument("--uses", type=int, default=1)
    invite_sub.add_parser("list")
    revoke = invite_sub.add_parser("revoke")
    revoke.add_argument("invite_id")
    gc = commands.add_parser("gc", help="remove unreferenced content objects")
    gc.add_argument("--execute", action="store_true", help="delete the objects reported by the dry run")
    backup = commands.add_parser("backup", help="create a metadata, object, and TLS backup")
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="restore a backup into an empty data directory")
    restore.add_argument("source", type=Path)
    pool_command = commands.add_parser("pool", help="inspect the channel Telegram index")
    pool_sub = pool_command.add_subparsers(dest="pool_action", required=True)
    pool_sub.add_parser("scan")
    pool_sub.add_parser("rebuild")
    pool_sub.add_parser("status")
    telegram = commands.add_parser("telegram", help="authorize the channel-mode Telegram user session")
    telegram.add_subparsers(dest="telegram_action", required=True).add_parser("login")
    commands.add_parser("doctor", help="check configuration and writable data paths")
    args = parser.parse_args(argv)

    try:
        settings = _settings()
        if args.command == "serve":
            return _command_serve(settings)
        if args.command == "init":
            return _command_init(settings)
        if args.command == "invite":
            return _command_invite(args, settings)
        if args.command == "gc":
            return _command_gc(args, settings)
        if args.command == "backup":
            return _command_backup(args.destination, settings)
        if args.command == "restore":
            return _command_restore(args.source, settings)
        if args.command == "pool":
            return _command_pool(args, settings)
        if args.command == "telegram":
            return _command_telegram(args, settings)
        if args.command == "status":
            index, _cert, _key, pin, _auth, _invites, _store, _pool = _runtime(settings)
            try:
                _print_json({**index.pool_status(), "mode": settings.effective_pool_mode,
                             "store": settings.store, "spki_pin": pin})
            finally:
                index.close()
            return 0
        if args.command == "pin":
            _index, _cert, _key, pin, _auth, _invites, _store, _pool = _runtime(settings)
            print(pin)
            _index.close()
            return 0
        if args.command == "doctor":
            index, cert, key, pin, _auth, _invites, _store, _pool = _runtime(settings)
            index.close()
            _print_json({"ok": True, "data_dir": str(settings.data_dir), "database": str(settings.data_dir / "seekdb.db"),
                         "tls_cert": str(cert), "tls_key": str(key), "spki_pin": pin,
                         "mode": settings.effective_pool_mode, "store": settings.store})
            return 0
    except (ConfigError, ValueError, OSError) as exc:
        print(f"seekdb: {exc}", file=sys.stderr)
        return 2
    return 0
