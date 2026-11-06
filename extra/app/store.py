import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, path TEXT NOT NULL,
                    description TEXT NOT NULL, format TEXT NOT NULL, virtual_size INTEGER NOT NULL
                );
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('owner', ?)", (str(uuid4()),))
            self.owner = db.execute("SELECT value FROM settings WHERE key='owner'").fetchone()[0]

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def images(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM images ORDER BY name")]

    def image(self, image_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM images WHERE id=?", (str(image_id),)).fetchone()
            return dict(row) if row else None

    def add_image(self, name, path, description, image_format, virtual_size):
        image_id = str(uuid4())
        with self.connect() as db:
            db.execute("INSERT INTO images VALUES (?, ?, ?, ?, ?, ?)",
                       (image_id, name, str(path), description, image_format, virtual_size))
        return self.image(image_id)
