import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (slug TEXT PRIMARY KEY, url TEXT, etag TEXT, modified TEXT, polled REAL);
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY, feed TEXT, guid TEXT, title TEXT, published REAL, url TEXT, position INTEGER
);
CREATE INDEX IF NOT EXISTS episodes_feed ON episodes (feed, published);
CREATE TABLE IF NOT EXISTS runs (episode TEXT, stage TEXT, ok INTEGER, error TEXT, at REAL, PRIMARY KEY (episode, stage));
CREATE TABLE IF NOT EXISTS prints (episode TEXT PRIMARY KEY, feed TEXT, data BLOB);
CREATE TABLE IF NOT EXISTS hashes (hash INTEGER, episode TEXT, pos INTEGER);
CREATE INDEX IF NOT EXISTS hashes_hash ON hashes (hash);
CREATE INDEX IF NOT EXISTS hashes_episode ON hashes (episode);
"""


def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def record(con, episode, stage, error=None):
    con.execute(
        "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?)", (episode, stage, error is None, error, time.time())
    )
    con.commit()
