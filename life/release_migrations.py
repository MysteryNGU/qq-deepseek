"""Additive SQLite migrations, checked on a temporary copy and committed once."""
import hashlib
import re
import sqlite3
from pathlib import Path
import tempfile
import time


def validate(migrations):
    if not isinstance(migrations,list):
        raise ValueError('invalid_migrations')
    seen = set()
    for migration in migrations:
        if (not isinstance(migration,dict) or not migration.get('id') or migration['id'] in seen
                or not isinstance(migration.get('sql'),list) or not migration['sql']):
            raise ValueError('invalid_migration_identity_or_sql')
        seen.add(migration['id'])
        for sql in migration['sql']:
            if not isinstance(sql,str) or not re.match(
                    r'^\s*(?:CREATE\s+(?:TABLE|(?:UNIQUE\s+)?INDEX)\s+|ALTER\s+TABLE\s+\S+\s+ADD\s+(?:COLUMN\s+)?)',sql,re.I):
                raise ValueError('only_additive_compatible_migrations_supported')


def apply(db,migrations):
    validate(migrations)
    db.execute('CREATE TABLE IF NOT EXISTS release_migrations(id TEXT PRIMARY KEY,digest TEXT NOT NULL,applied REAL NOT NULL)')
    for migration in migrations:
        checksum = hashlib.sha256('\n'.join(migration['sql']).encode()).hexdigest()
        old = db.execute('SELECT digest FROM release_migrations WHERE id=?',(migration['id'],)).fetchone()
        if old:
            if old[0]!=checksum:
                raise ValueError('migration_identity_conflict')
            continue
        for sql in migration['sql']:
            db.execute(sql)
        db.execute('INSERT INTO release_migrations VALUES(?,?,?)',(migration['id'],checksum,time.time()))


def preflight(database,migrations):
    if not migrations:
        return dict(ok=True)
    validate(migrations)
    if not database:
        raise ValueError('migration_requires_bound_life_database')
    with tempfile.TemporaryDirectory() as folder:
        original = sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)
        clone = sqlite3.connect(str(Path(folder)/'migration.db'))
        try:
            original.backup(clone)
            tables = [r[0] for r in clone.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            def count(name):
                return clone.execute('SELECT COUNT(*) FROM "'+name.replace('"','""')+'"').fetchone()[0]
            counts = {name:count(name) for name in tables}
            with clone:
                apply(clone,migrations)
                if any(count(name)!=number for name,number in counts.items() if name!='release_migrations'):
                    raise ValueError('migration_changed_existing_rows')
                if clone.execute('PRAGMA quick_check').fetchone()[0]!='ok' or clone.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('migration_integrity_check_failed')
            return dict(ok=True,existing_tables_checked=len(tables),migrations=len(migrations))
        finally:
            original.close()
            clone.close()
