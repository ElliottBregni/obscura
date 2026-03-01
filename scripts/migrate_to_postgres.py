#!/usr/bin/env python3
"""
Migrate Obscura from SQLite to PostgreSQL.

Usage:
    python scripts/migrate_to_postgres.py --dry-run    # Preview migration
    python scripts/migrate_to_postgres.py              # Run migration
    python scripts/migrate_to_postgres.py --verify     # Verify only
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    print("Error: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)


class PostgreSQLMigrator:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.obscura_dir = Path.home() / ".obscura"
        self.events_db = self.obscura_dir / "events.db"
        self.memory_dir = self.obscura_dir / "memory"
        
        # PostgreSQL connection
        self.pg_conn = psycopg2.connect(
            host=os.getenv("OBSCURA_DB_HOST", "localhost"),
            port=int(os.getenv("OBSCURA_DB_PORT", "5432")),
            database=os.getenv("OBSCURA_DB_NAME", "obscura"),
            user=os.getenv("OBSCURA_DB_USER", "obscura_user"),
            password=os.getenv("OBSCURA_DB_PASSWORD", ""),
            cursor_factory=RealDictCursor
        )

    def backup_sqlite(self):
        """Create backup of SQLite databases."""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = self.obscura_dir / "backups" / f"pre-postgres-{timestamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        if self.dry_run:
            print(f"[DRY RUN] Would backup to: {backup_dir}")
            return backup_dir
        
        print(f"📦 Backing up to: {backup_dir}")
        
        # Backup events.db
        if self.events_db.exists():
            shutil.copy2(self.events_db, backup_dir / "events.db")
            print(f"  ✓ Backed up events.db ({self.events_db.stat().st_size / 1024:.1f} KB)")
        
        # Backup memory directory
        if self.memory_dir.exists():
            shutil.copytree(self.memory_dir, backup_dir / "memory", dirs_exist_ok=True)
            db_count = len(list((backup_dir / "memory").glob("*.db")))
            print(f"  ✓ Backed up {db_count} memory databases")
        
        # Create archive
        archive_path = backup_dir.with_suffix(".tar.gz")
        shutil.make_archive(str(backup_dir), 'gztar', backup_dir)
        print(f"  ✓ Created archive: {archive_path.name}")
        
        return backup_dir

    def migrate_events(self):
        """Migrate events.db to PostgreSQL."""
        if not self.events_db.exists():
            print("⚠️  No events.db found, skipping")
            return
        
        print("\n📊 Migrating events.db...")
        
        sqlite_conn = sqlite3.connect(str(self.events_db))
        sqlite_conn.row_factory = sqlite3.Row
        
        # Migrate sessions
        sessions = sqlite_conn.execute("SELECT * FROM sessions").fetchall()
        print(f"  Found {len(sessions)} sessions")
        
        if not self.dry_run:
            with self.pg_conn.cursor() as cur:
                for row in sessions:
                    cur.execute("""
                        INSERT INTO events.sessions 
                        (id, status, active_agent, created_at, updated_at, backend, model, source, project, summary, message_count, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            status = EXCLUDED.status,
                            updated_at = EXCLUDED.updated_at
                    """, (
                        row["id"], row["status"], row.get("active_agent", ""),
                        row["created_at"], row["updated_at"],
                        row.get("backend", ""), row.get("model", ""),
                        row.get("source", "live"), row.get("project", ""),
                        row.get("summary", ""), row.get("message_count", 0),
                        row.get("metadata", "{}")
                    ))
                self.pg_conn.commit()
                print(f"  ✓ Migrated {len(sessions)} sessions")
        
        # Migrate events
        events = sqlite_conn.execute("SELECT * FROM events").fetchall()
        print(f"  Found {len(events)} events")
        
        if not self.dry_run:
            with self.pg_conn.cursor() as cur:
                for row in events:
                    # Parse payload as JSON if it's a string
                    payload = row["payload"]
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except json.JSONDecodeError:
                            payload = {"raw": payload}
                    
                    cur.execute("""
                        INSERT INTO events.events (session_id, seq, kind, payload, timestamp)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (session_id, seq) DO NOTHING
                    """, (
                        row["session_id"], row["seq"], row["kind"],
                        json.dumps(payload), row["timestamp"]
                    ))
                self.pg_conn.commit()
                print(f"  ✓ Migrated {len(events)} events")
        
        sqlite_conn.close()

    def migrate_memory(self):
        """Migrate memory/*.db files to consolidated PostgreSQL table."""
        if not self.memory_dir.exists():
            print("\n⚠️  No memory directory found, skipping")
            return
        
        print("\n💾 Migrating memory databases...")
        
        memory_dbs = list(self.memory_dir.glob("*.db"))
        print(f"  Found {len(memory_dbs)} memory databases")
        
        total_rows = 0
        for db_file in memory_dbs:
            user_hash = db_file.stem
            sqlite_conn = sqlite3.connect(str(db_file))
            sqlite_conn.row_factory = sqlite3.Row
            
            try:
                rows = sqlite_conn.execute("SELECT * FROM memory").fetchall()
                if self.dry_run:
                    print(f"  [DRY RUN] Would migrate {len(rows)} rows from {user_hash}")
                else:
                    with self.pg_conn.cursor() as cur:
                        for row in rows:
                            # Parse value as JSON if it's a string
                            value = row["value"]
                            if isinstance(value, str):
                                try:
                                    value = json.loads(value)
                                except json.JSONDecodeError:
                                    value = {"raw": value}
                            
                            cur.execute("""
                                INSERT INTO memory.kv_store 
                                (user_hash, namespace, key, value, created_at, updated_at, expires_at)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (user_hash, namespace, key) DO UPDATE SET
                                    value = EXCLUDED.value,
                                    updated_at = EXCLUDED.updated_at
                            """, (
                                user_hash, row["namespace"], row["key"],
                                json.dumps(value), row.get("created_at"),
                                row.get("updated_at"), row.get("expires_at")
                            ))
                    self.pg_conn.commit()
                    print(f"  ✓ Migrated {len(rows)} rows from {user_hash}")
                
                total_rows += len(rows)
            except sqlite3.OperationalError as e:
                print(f"  ⚠️  Skipping {user_hash}: {e}")
            finally:
                sqlite_conn.close()
        
        if not self.dry_run:
            print(f"  ✓ Total: {total_rows} memory entries migrated")

    def verify(self):
        """Verify migration success."""
        print("\n✅ Verifying migration...")
        
        # Check sessions count
        sqlite_conn = sqlite3.connect(str(self.events_db))
        sqlite_sessions = sqlite_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        sqlite_events = sqlite_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        sqlite_conn.close()
        
        with self.pg_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM events.sessions")
            pg_sessions = cur.fetchone()["count"]
            
            cur.execute("SELECT COUNT(*) FROM events.events")
            pg_events = cur.fetchone()["count"]
            
            cur.execute("SELECT COUNT(*) FROM memory.kv_store")
            pg_memory = cur.fetchone()["count"]
        
        print(f"  Sessions: SQLite={sqlite_sessions}, PostgreSQL={pg_sessions}", end="")
        if sqlite_sessions == pg_sessions:
            print(" ✓")
        else:
            print(" ⚠️  MISMATCH!")
        
        print(f"  Events:   SQLite={sqlite_events}, PostgreSQL={pg_events}", end="")
        if sqlite_events == pg_events:
            print(" ✓")
        else:
            print(" ⚠️  MISMATCH!")
        
        print(f"  Memory:   PostgreSQL={pg_memory} entries ✓")
        
        return sqlite_sessions == pg_sessions and sqlite_events == pg_events

    def run(self):
        """Run full migration."""
        print("🚀 Obscura SQLite → PostgreSQL Migration")
        print("=" * 50)
        
        if self.dry_run:
            print("\n⚠️  DRY RUN MODE - No changes will be made\n")
        
        # Backup
        self.backup_sqlite()
        
        # Migrate
        self.migrate_events()
        self.migrate_memory()
        
        # Verify
        if not self.dry_run:
            success = self.verify()
            
            if success:
                print("\n✅ Migration completed successfully!")
                print("\nNext steps:")
                print("1. Add to ~/.zshrc:")
                print("   export OBSCURA_DB_TYPE=postgresql")
                print("   export OBSCURA_DB_PASSWORD='your_password'")
                print("2. Restart your shell: source ~/.zshrc")
                print("3. Test Obscura: obscura --version")
            else:
                print("\n⚠️  Migration completed with warnings")
                print("   Review the counts above and check logs")
        else:
            print("\n✅ Dry run complete - no changes made")
            print("   Run without --dry-run to perform migration")

    def close(self):
        """Close connections."""
        self.pg_conn.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate Obscura from SQLite to PostgreSQL")
    parser.add_argument("--dry-run", action="store_true", help="Preview migration without making changes")
    parser.add_argument("--verify", action="store_true", help="Verify migration only")
    args = parser.parse_args()
    
    migrator = PostgreSQLMigrator(dry_run=args.dry_run)
    
    try:
        if args.verify:
            migrator.verify()
        else:
            migrator.run()
    finally:
        migrator.close()


if __name__ == "__main__":
    main()
