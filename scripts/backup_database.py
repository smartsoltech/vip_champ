"""Create a native SQLite snapshot or PostgreSQL custom-format backup."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from db import Repository


def main() -> int:
    repository = None
    try:
        settings = load_settings(require_token=False)
        repository = Repository(settings.database_url, backup_dir=settings.backup_dir)
        print(repository.backup_database())
        return 0
    except Exception:
        print('Database backup failed; check configuration, storage, and native backup tools.', file=sys.stderr)
        return 1
    finally:
        if repository:
            repository.close()


if __name__ == '__main__':
    raise SystemExit(main())
