from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import secrets
import socket
import sys
import urllib.request
from dataclasses import replace
from pathlib import Path

from telethon import TelegramClient, errors, utils
from telethon.sessions import StringSession

from . import __version__
from .config import AppSecrets, ConfigurationError, SecretVault, resolve_data_dir
from .database import Database
from .film_bot import FilmBotClient
from .indexer import MediaCatalog
from .security import hash_password, new_token


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _prompt_api_id() -> int:
    while True:
        try:
            value = int(_prompt("Telegram API ID"))
            if value > 0:
                return value
        except ValueError:
            pass
        print("  Enter the numeric API ID from my.telegram.org.")


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _prompt_port() -> int:
    while True:
        try:
            port = int(_prompt("Website port", "8765"))
        except ValueError:
            print("  Enter a number between 1024 and 65535.")
            continue
        if not 1024 <= port <= 65535:
            print("  Choose a port between 1024 and 65535.")
        elif not _port_available(port):
            print(f"  Port {port} is already in use. Choose another port.")
        else:
            return port


def _prompt_username() -> str:
    while True:
        username = _prompt("Website username")
        if USERNAME_PATTERN.fullmatch(username):
            return username
        print("  Use 3-32 letters, numbers, dots, underscores, or hyphens.")


def _prompt_password() -> str:
    while True:
        password = getpass.getpass("Website password: ")
        if len(password) < 10:
            print("  Use at least 10 characters.")
            continue
        confirmation = getpass.getpass("Confirm website password: ")
        if not secrets.compare_digest(password, confirmation):
            print("  Passwords did not match.")
            continue
        return password


def _public_ip() -> str:
    try:
        request = urllib.request.Request(
            "https://api.ipify.org", headers={"User-Agent": f"CineVault/{__version__}"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            value = response.read(64).decode("ascii").strip()
            if value:
                return value
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "SERVER-IP"


async def _telegram_login(api_id: int, api_hash: str, phone: str):
    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        connection_retries=5,
        request_retries=5,
        retry_delay=2,
        flood_sleep_threshold=60,
    )
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        for attempt in range(3):
            code = _prompt("Telegram login code")
            try:
                await client.sign_in(
                    phone=phone,
                    code=code,
                    phone_code_hash=sent.phone_code_hash,
                )
                break
            except errors.SessionPasswordNeededError:
                await client.sign_in(password=getpass.getpass("Telegram 2FA password: "))
                break
            except errors.PhoneCodeInvalidError:
                if attempt == 2:
                    raise
                print("  That code was invalid. Try again.")
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram login was not completed.")
        return client
    except Exception:
        await client.disconnect()
        raise


async def interactive_setup(data_dir: Path) -> int:
    print()
    print("  CineVault interactive VPS setup")
    print("  Telegram stores every original; this VPS keeps metadata and thumbnails only.")
    print("  Use only media you are authorised to access and stream.")
    print()
    api_id = _prompt_api_id()
    api_hash = getpass.getpass("Telegram API hash: ").strip()
    if len(api_hash) < 16:
        raise RuntimeError("The Telegram API hash does not look valid.")
    phone = _prompt("Telegram phone number with country code")

    print("\n  Requesting a Telegram login code...")
    login_client = await _telegram_login(api_id, api_hash, phone)
    try:
        me = await login_client.get_me()
        account_name = utils.get_display_name(me) or getattr(me, "username", "") or "Telegram account"
        print(f"  Logged in successfully as {account_name}.")
        while True:
            bot_reference = _prompt("Film bot username", "@FilterSECBot")
            try:
                entity = await login_client.get_entity(bot_reference)
                if not bool(getattr(entity, "bot", False)):
                    raise RuntimeError("That Telegram account is not a bot.")
                break
            except Exception as exc:
                print(f"  Could not open that bot: {exc}")
        bot_id = int(utils.get_peer_id(entity))
        bot_title = utils.get_display_name(entity) or bot_reference
        string_session = login_client.session.save()
        print(f"  Connected to: {bot_title}")
    finally:
        await login_client.disconnect()

    print()
    port = _prompt_port()
    web_username = _prompt_username()
    password = _prompt_password()
    config = AppSecrets(
        version=2,
        api_id=api_id,
        api_hash=api_hash,
        string_session=string_session,
        bot_reference=bot_reference,
        bot_id=bot_id,
        bot_title=bot_title,
        account_name=account_name,
        web_username=web_username,
        password_hash=hash_password(password),
        cookie_secret=new_token(48),
        port=port,
        prune_interval_seconds=900,
        access_mode="private",
        stream_link_secret=new_token(48),
        poster_lookup=True,
    )
    vault = SecretVault(data_dir)
    vault.initialise_directory()
    vault.save(config)
    Database(data_dir / "cinevault.db").initialise()
    runtime_path = data_dir / "runtime.env"
    runtime_path.write_text(
        f"CINEVAULT_DATA_DIR={data_dir}\nCINEVAULT_PORT={port}\n",
        encoding="utf-8",
    )
    os.chmod(runtime_path, 0o600)
    print()
    print("  Configuration saved securely.")
    print(f"  CINEVAULT_READY_URL=http://{_public_ip()}:{port}")
    print(f"  CINEVAULT_PORT={port}")
    return port


async def prune_existing(data_dir: Path, limit: int = 500) -> None:
    config = SecretVault(data_dir).load()
    database = Database(data_dir / "cinevault.db")
    database.initialise()
    telegram = FilmBotClient(config)
    await telegram.connect()
    try:
        result = await MediaCatalog(database, telegram, data_dir / "thumbnails").prune(limit)
    finally:
        await telegram.disconnect()
    print(f"Checked {result.checked} saved files; removed {result.removed} missing item(s).")


async def search_test(data_dir: Path, query: str) -> None:
    config = SecretVault(data_dir).load()
    telegram = FilmBotClient(config)
    await telegram.connect()
    try:
        page = await telegram.search(query)
    finally:
        await telegram.disconnect()
    print(page.text.strip() or "Bot response")
    for action in page.actions:
        print(f"  [{action.role}] {action.text}")


async def doctor(data_dir: Path) -> None:
    try:
        config = SecretVault(data_dir).load()
    except ConfigurationError as exc:
        print(f"Configuration: FAILED - {exc}")
        raise SystemExit(1) from exc
    database = Database(data_dir / "cinevault.db")
    database.initialise()
    print("Configuration: OK")
    print(f"Database: OK ({database.stats()['total']} saved media)")
    telegram = FilmBotClient(config)
    try:
        await telegram.connect()
        print(f"Telegram: OK ({config.bot_title})")
    except Exception as exc:
        print(f"Telegram: FAILED - {exc}")
        raise SystemExit(1) from exc
    finally:
        await telegram.disconnect()


def set_access_mode(data_dir: Path, mode: str) -> None:
    vault = SecretVault(data_dir)
    config = vault.load()
    selected = mode.casefold()
    if selected not in {"private", "public"}:
        raise RuntimeError("Access mode must be private or public.")
    vault.save(replace(config, version=max(2, config.version), access_mode=selected))
    if selected == "public":
        print("Access mode: PUBLIC (the website opens without a login)")
        print("Signed VLC links still use unguessable expiring tokens.")
    else:
        print("Access mode: PRIVATE (website username and password required)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cinevault", description="Private Telegram FilmBot streaming")
    parser.add_argument("--version", action="version", version=f"CineVault {__version__}")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Persistent data directory (default: CINEVAULT_DATA_DIR or ./data)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("setup", help="Run Telegram and website setup")
    prune_parser = subcommands.add_parser("prune", help="Remove saved files deleted from Telegram")
    prune_parser.add_argument("--limit", type=int, default=500)
    test_parser = subcommands.add_parser("search-test", help="Send a harmless test search to the bot")
    test_parser.add_argument("query")
    subcommands.add_parser("doctor", help="Check configuration, database, and Telegram access")
    mode_parser = subcommands.add_parser("access-mode", help="Set public or private website access")
    mode_parser.add_argument("mode", choices=("public", "private"))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    data_dir = resolve_data_dir(args.data_dir)
    try:
        if args.command == "setup":
            asyncio.run(interactive_setup(data_dir))
        elif args.command == "prune":
            asyncio.run(prune_existing(data_dir, max(1, min(args.limit, 500))))
        elif args.command == "search-test":
            asyncio.run(search_test(data_dir, args.query))
        elif args.command == "doctor":
            asyncio.run(doctor(data_dir))
        elif args.command == "access-mode":
            set_access_mode(data_dir, args.mode)
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)
    except (ConfigurationError, RuntimeError, errors.RPCError) as exc:
        print(f"\nCineVault error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
