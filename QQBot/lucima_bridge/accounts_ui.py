"""Keep a conversation's account selection separate from the existing vault owners."""
from __future__ import annotations

import re


class AccountSlots:
    LIMIT = 5

    def __init__(self, vault):
        self.vault = vault
        self.db = vault.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS lucima_account_slot (
                scope TEXT NOT NULL,
                position INTEGER NOT NULL,
                owner TEXT UNIQUE NOT NULL,
                alias TEXT NOT NULL DEFAULT '',
                signed_out INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (scope, position)
            );
            CREATE TABLE IF NOT EXISTS lucima_ui_preferences (
                scope TEXT PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1,
                theme TEXT NOT NULL DEFAULT 'default',
                page TEXT NOT NULL DEFAULT 'home'
            );
            CREATE TABLE IF NOT EXISTS lucima_log_display (
                owner TEXT PRIMARY KEY,
                cutoff INTEGER NOT NULL
            );
        """)

    def scope_for(self, owner: str) -> str:
        row = self.db.execute("SELECT scope FROM lucima_account_slot WHERE owner = ?", (owner,)).fetchone()
        return row[0] if row else owner

    def preferences(self, scope: str) -> tuple[int, str, str]:
        row = self.db.execute(
            "SELECT active, theme, page FROM lucima_ui_preferences WHERE scope = ?", (scope,)
        ).fetchone()
        return row or (1, "default", "home")

    def select(self, scope: str, position: int) -> str | None:
        if position == 1:
            return scope
        row = self.db.execute(
            "SELECT owner FROM lucima_account_slot WHERE scope = ? AND position = ?", (scope, position),
        ).fetchone()
        return row[0] if row else None

    def active(self, scope: str) -> str:
        return self.select(scope, self.preferences(scope)[0]) or scope

    def activate(self, scope: str, position: int) -> None:
        with self.db:
            self.db.execute("""INSERT INTO lucima_ui_preferences (scope, active) VALUES (?, ?)
                ON CONFLICT(scope) DO UPDATE SET active = excluded.active""", (scope, position))

    def set_theme(self, scope: str, theme: str) -> None:
        with self.db:
            self.db.execute("""INSERT INTO lucima_ui_preferences (scope, theme) VALUES (?, ?)
                ON CONFLICT(scope) DO UPDATE SET theme = excluded.theme""", (scope, theme))

    def set_page(self, scope: str, page: str) -> None:
        with self.db:
            self.db.execute("""INSERT INTO lucima_ui_preferences (scope, page) VALUES (?, ?)
                ON CONFLICT(scope) DO UPDATE SET page = excluded.page""", (scope, page))

    def owners(self, scope: str) -> tuple[str, ...]:
        return (scope, *(row[0] for row in self.db.execute(
            "SELECT owner FROM lucima_account_slot WHERE scope = ? AND position > 1 ORDER BY position", (scope,),
        )))

    def position_for(self, owner: str) -> int:
        row = self.db.execute("SELECT position FROM lucima_account_slot WHERE owner = ?", (owner,)).fetchone()
        return row[0] if row else 1

    def label(self, owner: str) -> str:
        row = self.db.execute("SELECT alias, position FROM lucima_account_slot WHERE owner = ?", (owner,)).fetchone()
        return (row[0] or f"账号 {row[1]}") if row else "账号 1"

    def reserve(self, scope: str) -> str | None:
        # Reuse an unbound slot. Failed or abandoned QR flows do not consume new slots.
        for position in range(1, self.LIMIT + 1):
            owner = self.select(scope, position)
            if owner and self.vault.record(owner):
                continue
            return owner or self._create(scope, position)
        return None

    def _create(self, scope: str, position: int) -> str:
        owner = self.vault.index(f"slot:{scope}:{position}")
        with self.db:
            self.db.execute("INSERT INTO lucima_account_slot (scope, position, owner) VALUES (?, ?, ?)",
                            (scope, position, owner))
        return owner

    def is_signed_out(self, owner: str) -> bool:
        row = self.db.execute("SELECT signed_out FROM lucima_account_slot WHERE owner = ?", (owner,)).fetchone()
        return bool(row and row[0])

    def set_signed_out(self, owner: str, signed_out: bool) -> None:
        with self.db:
            self.db.execute("""INSERT INTO lucima_account_slot (scope, position, owner, signed_out) VALUES (?, ?, ?, ?)
                ON CONFLICT(owner) DO UPDATE SET signed_out = excluded.signed_out""",
                (self.scope_for(owner), self.position_for(owner), owner, int(signed_out)))

    def rename(self, owner: str, label: str) -> bool:
        if not re.fullmatch(r"[\w\-]{1,12}", label) or not self.vault.record(owner):
            return False
        with self.db:
            self.db.execute("""INSERT INTO lucima_account_slot (scope, position, owner, alias) VALUES (?, ?, ?, ?)
                ON CONFLICT(owner) DO UPDATE SET alias = excluded.alias""",
                (self.scope_for(owner), self.position_for(owner), owner, label))
        return True

    def history_cutoff(self, owner: str) -> int:
        row = self.db.execute("SELECT cutoff FROM lucima_log_display WHERE owner = ?", (owner,)).fetchone()
        return row[0] if row else 0

    def hide_history(self, owner: str, cutoff: int) -> None:
        with self.db:
            self.db.execute("""INSERT INTO lucima_log_display VALUES (?, ?)
                ON CONFLICT(owner) DO UPDATE SET cutoff = excluded.cutoff""", (owner, cutoff))
