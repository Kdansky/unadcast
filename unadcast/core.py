import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import db
from .config import digest, excluded


@dataclass
class Ctx:
    cfg: dict
    con: object = None

    @classmethod
    def open(cls, cfg):
        data = Path(cfg["paths"]["data"])
        data.mkdir(parents=True, exist_ok=True)
        return cls(cfg, db.connect(data / "unadcast.db"))

    @property
    def data(self):
        return Path(self.cfg["paths"]["data"])

    @property
    def public(self):
        return Path(self.cfg["paths"]["public"])

    @property
    def models(self):
        return Path(self.cfg["paths"]["models"])

    def feed(self, slug):
        return next(f for f in self.cfg["feeds"] if f["slug"] == slug)

    def episode(self, id):
        row = self.con.execute("SELECT * FROM episodes WHERE id = ?", (id,)).fetchone()
        if not row:
            raise KeyError(f"no episode {id}")
        return Episode(**dict(row), ctx=self)

    def pending(self, slug):
        """The newest `keep` episodes of a feed, newest first: the one you'll want to hear next is done first."""
        feed = self.feed(slug)
        rows = self.con.execute("SELECT * FROM episodes WHERE feed = ? ORDER BY published DESC, position ASC", (slug,))
        return [Episode(**dict(r), ctx=self) for r in rows if not excluded(feed, r["title"])][: feed["keep"]]


@dataclass
class Episode:
    id: str
    feed: str
    guid: str
    title: str
    published: float
    url: str
    position: int
    ctx: Ctx = field(repr=False, default=None)

    @property
    def dir(self):
        return self.ctx.data / "work" / self.feed / self.id

    def path(self, name):
        return self.dir / name

    def load(self, name):
        return json.loads(self.path(name).read_text())

    def save(self, name, obj):
        write(self.path(name), json.dumps(obj, indent=1).encode())


def write(path, data):
    """Atomic, so a crash never leaves a half-written file that looks finished."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


@dataclass
class Stage:
    name: str
    run: Callable
    inputs: tuple = ()
    # globs whose matches count as inputs when present
    optional: tuple = ()
    outputs: tuple = ()
    config: Callable = lambda cfg, ep: None
    enabled: Callable = lambda cfg: True
    # called once after a pass ran the stage for any episode: frees models
    after: Callable = lambda cfg: None
    version: int = 1

    def state(self, ep):
        names = list(self.inputs) + sorted(str(p.relative_to(ep.dir)) for g in self.optional for p in ep.dir.glob(g))
        return {
            "version": self.version,
            "config": digest(self.config(ep.ctx.cfg, ep)),
            "inputs": {n: stat(ep.path(n)) for n in names},
        }

    def stamp(self, ep):
        return ep.path(f"stamps/{self.name}.json")

    def ready(self, ep):
        return all(ep.path(n).exists() for n in self.inputs)

    def stale(self, ep):
        s = self.stamp(ep)
        if not s.exists() or not all(ep.path(n).exists() for n in self.outputs):
            return True
        return json.loads(s.read_text()) != self.state(ep)

    def execute(self, ep):
        self.stamp(ep).unlink(missing_ok=True)
        ep.dir.mkdir(parents=True, exist_ok=True)
        self.run(ep)
        ep.save(f"stamps/{self.name}.json", self.state(ep))


def stat(path):
    s = path.stat()
    return f"{s.st_size}:{s.st_mtime_ns}"
