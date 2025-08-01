from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple, Optional, Protocol
from zoneinfo import ZoneInfo

import fastapi
from sqlalchemy import (
    Column,
    Table,
    and_,
    between,
    exc,
    func,
    literal,
    or_,
    select,
    types,
)
from sqlalchemy.sql import text
from sqlalchemy.sql.elements import ColumnElement

from .cannon import canonify
from .common import (
    DbVisit,
    PathWithMtime,
    default_output_dir,
    get_system_tz,
    setup_logger,
)
from .database.load import DbStuff, get_db_stuff, row_to_db_visit

Json = dict[str, Any]

app = fastapi.FastAPI()


# meh. need this since I don't have hooks in hug to initialize logging properly..
@lru_cache(1)
def get_logger() -> logging.Logger:
    # NOTE: uncomment to log sql queries
    # logging.basicConfig()
    # logging.getLogger('sqlalchemy.engine').setLevel(logging.DEBUG)

    # todo lazy log?
    logger = logging.getLogger('promnesia.server')
    setup_logger(logger, level=logging.DEBUG)

    # from hug.middleware import LogMiddleware
    # api = hug.API(__name__)
    # api.http.add_middleware(LogMiddleware(logger=logger))
    return logger


def get_version() -> str:
    assert __package__ is not None  # make type checker happy
    return importlib.metadata.version(__package__)


class ServerConfig(NamedTuple):
    db: Path
    timezone: ZoneInfo

    def as_str(self) -> str:
        return json.dumps(
            {
                'timezone': self.timezone.key,
                'db': str(self.db),
            }
        )

    @classmethod
    def from_str(cls, cfgs: str) -> ServerConfig:
        d = json.loads(cfgs)
        return cls(db=Path(d['db']), timezone=ZoneInfo(d['timezone']))


class EnvConfig:
    KEY = 'PROMNESIA_CONFIG'

    # apparently the only way to communicate with hug...
    @staticmethod
    @lru_cache(1)
    def get() -> ServerConfig:
        cfgs = os.environ.get(EnvConfig.KEY)
        assert cfgs is not None
        return ServerConfig.from_str(cfgs)

    @staticmethod
    def set(cfg: ServerConfig) -> None:
        os.environ[EnvConfig.KEY] = cfg.as_str()


# todo how to return exception in error?


def as_json(v: DbVisit) -> Json:
    # yep, this is NOT %Y-%m-%d as is seems to be the only format with timezone that Date.parse in JS accepts. Just forget it.
    dts = v.dt.strftime('%d %b %Y %H:%M:%S %z')
    loc = v.locator
    # TODO is locator always present??
    return {
        # TODO do not display year if it's current year??
        'dt': dts,
        # TODO the frontend had some bug with handling empty string as src. fix that later
        'src': v.src or 'unnamed',
        'context': v.context,
        'duration': v.duration,
        'locator': {
            'title': loc.title,
            'href': loc.href,
        },
        'original_url': v.orig_url,
        'normalised_url': v.norm_url,
    }


def get_db_path(*, check: bool = True) -> Path:
    db = EnvConfig.get().db
    if check:
        assert db.exists(), db
    return db


@lru_cache(1)
# PathWithMtime aids lru_cache in reloading the sqlalchemy binder
def _get_stuff(db_path: PathWithMtime) -> DbStuff:
    get_logger().debug('Reloading DB: %s', db_path)
    return get_db_stuff(db_path=db_path.path)


def get_stuff(db_path: Path | None = None) -> DbStuff:  # TODO better name
    # ok, it will always load from the same db file; but intermediate would be kinda an optional dump.
    if db_path is None:
        db_path = get_db_path()
    return _get_stuff(PathWithMtime.make(db_path))


def db_stats(db_path: Path) -> Json:
    engine, table = get_stuff(db_path)
    query = select(func.count()).select_from(table)
    with engine.connect() as conn:
        [(total,)] = conn.execute(query)
    return {
        'total_visits': total,
    }


class Where(Protocol):
    def __call__(self, table: Table, url: str) -> ColumnElement[bool]: ...


@dataclass
class VisitsResponse:
    original_url: str
    normalised_url: str
    visits: Any


def search_common(url: str, where: Where) -> VisitsResponse:
    logger = get_logger()
    config = EnvConfig.get()

    logger.info('url: %s', url)
    original_url = url and url.strip()
    url = canonify(original_url)
    if not url:  # Don't eliminate a "#tag" query.
        url = original_url
    logger.info('normalised url: %s', url)

    engine, table = get_stuff()

    query = table.select().where(where(table=table, url=url))
    logger.debug('query: %s', query)

    with engine.connect() as conn:
        try:
            # TODO make more defensive here
            visits: list[DbVisit] = [row_to_db_visit(row) for row in conn.execute(query)]
        except exc.OperationalError as e:
            if getattr(e, 'msg', None) == 'no such table: visits':
                logger.warning('you may have to run indexer first!')
                # result['visits'] = [{an error with a msg}] # TODO
                # return result
            raise

    logger.debug('got %d visits from db', len(visits))

    vlist: list[DbVisit] = []
    for vis in visits:
        dt = vis.dt
        if dt.tzinfo is None:  # FIXME need this for /visits endpoint as well?
            dt = dt.replace(tzinfo=config.timezone)
            vis = vis._replace(dt=dt)
        vlist.append(vis)

    logger.debug('responding with %d visits', len(vlist))
    # TODO respond with normalised result, then frontent could choose how to present children/siblings/whatever?
    return VisitsResponse(
        original_url=original_url,
        normalised_url=url,
        visits=list(map(as_json, vlist)),
    )


# TODO hmm, seems that the extension is using post for all requests??
# perhasp should switch to get for most endpoint
@app.get ('/status', response_model=Json)  # fmt: skip
@app.post('/status', response_model=Json)  # fmt: skip
def status() -> Json:
    '''
    Ideally, status will always respond, regardless the internal state of the backend?
    '''
    logger = get_logger()

    db = get_db_path(check=False)
    try:
        assert db.exists(), db
        db_path = str(db)
    except Exception as e:
        logger.exception(e)
        db_path = f'ERROR: db not found/unreadable (expected path {db}). You probably forgot to run indexer first. See https://github.com/karlicoss/promnesia/blob/master/doc/TROUBLESHOOTING.org'

    stats: Json
    try:
        stats = db_stats(db)
    except Exception as e:
        logger.exception(e)
        stats = {'ERROR': str(e)}

    version: str | None
    try:
        version = get_version()
    except Exception as e:
        logger.exception(e)
        version = None

    return {
        'version': version,
        'db'     : db_path,
        'stats'  : stats,
    }  # fmt: skip


@dataclass
class VisitsRequest:
    url: str


@app.get ('/visits', response_model=VisitsResponse)  # fmt: skip
@app.post('/visits', response_model=VisitsResponse)  # fmt: skip
def visits(request: VisitsRequest) -> VisitsResponse:
    url = request.url
    get_logger().info('/visited %s', url)
    return search_common(
        url=url,
        # odd, doesn't work just with: x or (y and z)
        where=lambda table, url: or_(
            # exact match
            table.c.norm_url == url,
            # + child visits, but only 'interesting' ones
            and_(table.c.context != None, table.c.norm_url.startswith(url, autoescape=True)),  # noqa: E711
        ),
    )


@dataclass
class SearchRequest:
    url: str


@app.get ('/search', response_model=VisitsResponse)  # fmt: skip
@app.post('/search', response_model=VisitsResponse)  # fmt: skip
def search(request: SearchRequest) -> VisitsResponse:
    url = request.url
    get_logger().info('/search %s', url)
    # fmt: off
    return search_common(
        url=url,
        where=lambda table, url: or_(
            # todo hmm. think about it, not sure if I need proper indexer for fuzzy search etc?
            table.c.norm_url     .contains(url, autoescape=True),
            table.c.orig_url     .contains(url, autoescape=True),
            table.c.context      .contains(url, autoescape=True),
            table.c.locator_title.contains(url, autoescape=True),
        ),
    )
    # fmt: on


@dataclass
class SearchAroundRequest:
    timestamp: float


@app.get ('/search_around', response_model=VisitsResponse)  # fmt: skip
@app.post('/search_around', response_model=VisitsResponse)  # fmt: skip
def search_around(request: SearchAroundRequest) -> VisitsResponse:
    timestamp = request.timestamp
    get_logger().info('/search_around %s', timestamp)
    utc_timestamp = timestamp  # old 'timestamp' name is legacy

    # TODO meh. use count/pagination instead?
    delta_back = timedelta(hours=3).total_seconds()
    delta_front = timedelta(minutes=2).total_seconds()
    # TODO not sure about delta_front.. but it also serves as quick hack to accommodate for all the truncations etc

    return search_common(
        url='http://dummy.org',  # NOTE: not used in the where query (below).. perhaps need to get rid of this
        where=lambda table, url: between(  # noqa: ARG005
            func.strftime(
                '%s',  # NOTE: it's tz aware, e.g. would distinguish +05:00 vs -03:00
                # this is a bit fragile, relies on cachew internal timestamp format, e.g.
                # 2020-11-10T06:13:03.196376+00:00 Europe/London
                func.substr(
                    table.c.dt,
                    1,  # substr is 1-indexed
                    # instr finds the first match, but if not found it defaults to 0.. which we hack by concatting with ' '
                    func.instr(func.cast(table.c.dt, types.Unicode).op('||')(' '), ' ') - 1,
                    # for fucks sake.. seems that cast is necessary otherwise it tries to treat ' ' as datetime???
                ),
            )
            - literal(utc_timestamp),
            literal(-delta_back),
            literal(delta_front),
        ),
    )


# before 0.11.14 (including), extension didn't share the version
# so if it's not shared, assume that version
_NO_VERSION = (0, 11, 14)
_LATEST = (9999, 9999, 9999)


def as_version(version: str) -> tuple[int, int, int]:
    if version == '':
        return _NO_VERSION
    try:
        [v1, v2, v3] = map(int, version.split('.'))
    except Exception as e:
        logger = get_logger()
        logger.error('error while parsing version %s', version)
        logger.exception(e)
        return _LATEST
    else:
        return (v1, v2, v3)


@dataclass
class VisitedRequest:
    urls: list[str]
    client_version: str = ''


VisitedResponse = list[Optional[Json]]


@app.get ('/visited', response_model=VisitedResponse)  # fmt: skip
@app.post('/visited', response_model=VisitedResponse)  # fmt: skip
def visited(request: VisitedRequest) -> VisitedResponse:
    # TODO instead switch logging to fastapi
    urls = request.urls
    client_version = request.client_version

    logger = get_logger()
    logger.info('/visited %s %s', urls, client_version)

    _version = as_version(client_version)  # todo use it?

    nurls = [canonify(u) for u in urls]
    snurls = sorted(set(nurls))

    if len(snurls) == 0:
        return []

    engine, table = get_stuff()

    # sqlalchemy doesn't seem to support SELECT FROM (VALUES (...)) in its api
    # also doesn't support array binding...
    # https://stackoverflow.com/questions/13190392/how-can-i-bind-a-list-to-a-parameter-in-a-custom-query-in-sqlalchemy
    bstring = ','.join(f'(:b{i})'   for i, _ in enumerate(snurls))  # fmt: skip
    bdict = {            f'b{i}': v for i, v in enumerate(snurls)}  # fmt: skip
    # TODO hopefully, visits.* thing only returns one visit??
    query = (
        text(f"""
WITH cte(queried) AS (SELECT * FROM (values {bstring}))
SELECT queried, visits.*
    FROM cte JOIN visits
    ON queried = visits.norm_url
/*  order stuff without contexts last
    this actually doesn't make sense, locially it should be ASC??
    but somehow DESC is the one that actually works..
*/
    ORDER BY visits.context IS NULL DESC
    """)
        .bindparams(**bdict)
        .columns(
            Column('match', types.Unicode),
            *table.columns,
        )
    )
    # TODO might be very beneficial for performance to have an intermediate table
    # SELECT visits.* FROM visits GROUP BY visits.norm_url ORDER BY visits.context IS NULL DESC
    # + unique index in norm_url
    # brings down large queries to 50ms...
    with engine.connect() as conn:
        res = list(conn.execute(query))
        present: dict[str, Any] = {row[0]: row_to_db_visit(row[1:]) for row in res}
    results = []
    for nu in nurls:
        r = present.get(nu, None)
        results.append(None if r is None else as_json(r))

    # no need for it anymore, extension has been updated since
    # just keeping as an example
    # if version <= (0, 11, 14):
    #     # older extension versions expected boolean result here
    #     results = [r is not None for r in results] # type: ignore[misc]

    return results


def _run(*, host: str, port: str, quiet: bool, config: ServerConfig) -> None:
    logger = get_logger()

    logger.info('Running server with %s', config)

    EnvConfig.set(config)

    import uvicorn

    uvicorn.run('promnesia.server:app', host=host, port=int(port), log_level='debug')


def run(args: argparse.Namespace) -> None:
    _run(
        port=args.port,
        host=args.host,
        quiet=args.quiet,
        config=ServerConfig(
            db=args.db,
            timezone=args.timezone,
        ),
    )


def default_db_path() -> Path:
    return default_output_dir() / 'promnesia.sqlite'


def setup_parser(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        '--host',
        type=str,
        # TODO hmm it's a somewhat unfortunate default..
        # but for now at least keeping it for compatibility with old hug runner
        # otherwise Promnesia might stop working for people who upgrade it
        default='0.0.0.0',
        help='Local IP to listen on',
    )

    p.add_argument(
        '--port',
        type=str,
        default='13131',
        help='Port for communicating with extension',
    )

    p.add_argument(
        '--quiet',
        action='store_true',
        help='Pass to log less',
    )
    # TODO need to keep consistent with the backend...
    # todo use output_dir instead?
    p.add_argument(
        '--db',
        type=Path,
        default=default_db_path(),
        help='Path to the links database (optional, uses user data dir by default)',
    )

    p.add_argument(
        '--timezone',
        type=ZoneInfo,
        default=get_system_tz(),
        help='Fallback timezone, defaults to the system timezone if not specified',
    )
