"""Import a canonical or legacy UTF-8 CSV using the application's shared database."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import load_settings
from db import MAX_CSV_BYTES, Repository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_file', nargs='?', type=Path, default=Path('clients.csv'))
    args = parser.parse_args(argv)
    repository = None
    try:
        if args.csv_file.stat().st_size > MAX_CSV_BYTES:
            raise ValueError('CSV must be at most 10 MiB.')
        content = args.csv_file.read_bytes()
        settings = load_settings(require_token=False)
        repository = Repository(settings.database_url, backup_dir=settings.backup_dir)
        report = repository.initialize()
        if report.quarantined:
            print(f'Migration quarantined {report.quarantined} legacy rows; inspect migration_quarantine.')
        result = repository.import_clients(content)
        print(f'Imported: {result.inserted}; duplicates: {result.duplicates}; input rows: {result.total}.')
        return 0
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print('Unable to read CSV or write the database backup.', file=sys.stderr)
        return 1
    except Exception:
        print('Import failed; no imported rows were committed.', file=sys.stderr)
        return 1
    finally:
        if repository:
            repository.close()


if __name__ == '__main__':
    raise SystemExit(main())
