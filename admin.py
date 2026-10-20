"""Trusted local administrator provisioning and recovery CLI."""
from __future__ import annotations

import argparse
import getpass
import sys

from config import load_settings
from db import Repository


def read_password(stdin: bool) -> str:
    if stdin:
        if sys.stdin.isatty():
            raise ValueError('--password-stdin requires redirected input.')
        return sys.stdin.readline().rstrip('\r\n')
    password = getpass.getpass('Password (at least 12 characters): ')
    if password != getpass.getpass('Confirm password: '):
        raise ValueError('Passwords do not match.')
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest='command', required=True)
    add = subcommands.add_parser('add', help='Create an administrator with a password and Telegram binding.')
    add.add_argument('username')
    add.add_argument('--telegram-id', required=True, type=int)
    add.add_argument('--superadmin', action='store_true')
    add.add_argument('--password-stdin', action='store_true')
    update = subcommands.add_parser('update', help='Reset a password, bind Telegram, or change a role.')
    update.add_argument('username')
    update.add_argument('--telegram-id', type=int)
    update.add_argument('--reset-password', action='store_true')
    update.add_argument('--password-stdin', action='store_true')
    role = update.add_mutually_exclusive_group()
    role.add_argument('--superadmin', action='store_true')
    role.add_argument('--regular-admin', action='store_true')
    subcommands.add_parser('list', help='List accounts without passwords or hashes.')
    args = parser.parse_args(argv)
    repository = None
    try:
        settings = load_settings(require_token=False)
        repository = Repository(settings.database_url, backup_dir=settings.backup_dir)
        report = repository.initialize()
        if report.quarantined:
            print(f'Migration quarantined {report.quarantined} legacy rows; inspect migration_quarantine.')
        if args.command == 'add':
            account = repository.add_admin(args.username, read_password(args.password_stdin), args.telegram_id, args.superadmin)
            print(f'Created {account.username}; Telegram ID {account.telegram_id}; superadmin={account.is_superadmin}.')
        elif args.command == 'update':
            if args.password_stdin and not args.reset_password:
                raise ValueError('--password-stdin requires --reset-password.')
            if not any((args.telegram_id is not None, args.reset_password, args.superadmin, args.regular_admin)):
                raise ValueError('Specify a Telegram binding, password reset, or role change.')
            repository.backup_database()
            account = repository.update_admin(args.username, password=read_password(args.password_stdin) if args.reset_password else None, telegram_id=args.telegram_id, is_superadmin=True if args.superadmin else False if args.regular_admin else None)
            print(f'Updated {account.username}; Telegram ID {account.telegram_id}; superadmin={account.is_superadmin}.')
        else:
            for account in repository.list_admins():
                print(f'{account.username}\ttelegram_id={account.telegram_id}\tsuperadmin={account.is_superadmin}')
        return 0
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        # Database exceptions can embed connection details or bound password hashes.
        print('Administrator operation failed; inspect database availability and configuration.', file=sys.stderr)
        return 1
    finally:
        if repository:
            repository.close()


if __name__ == '__main__':
    raise SystemExit(main())
