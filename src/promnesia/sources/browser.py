'''
Uses [[https://github.com/karlicoss/HPI][HPI]] for visits from web browsers.
'''

from __future__ import annotations

import re
import warnings
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from promnesia.common import Loc, PathIsh, Results, Second, Visit, is_sqlite_db, logger


def index(p: PathIsh | None = None) -> Results:
    from . import hpi  # noqa: F401,I001

    if p is None:
        from my.browser.all import history

        yield from _index_new(history())
        return

    warnings.warn(
        f'Passing paths to promnesia.sources.browser is deprecated, you should setup my.browser.export instead. '
        f'See https://github.com/purarue/browserexport#hpi .'
        f'Will try to hack path to browser databases {p} into HPI config.'
    )
    try:
        yield from _index_new_with_adhoc_config(path=p)
    except Exception as e:
        logger.exception(e)
        warnings.warn("Hacking my.config.browser.export didn't work. You probably need to update HPI.")
    else:
        return

    logger.warning("Falling back onto legacy promnesia.sources.browser_legacy module")
    yield from _index_old(path=p)


def _index_old(*, path: PathIsh) -> Results:
    from . import browser_legacy

    yield from browser_legacy.index(path)


def _index_new_with_adhoc_config(*, path: PathIsh) -> Results:
    from . import hpi  # noqa: F401,I001

    ## previously, it was possible to index be called with multiple different db search paths
    ## this would result in each subsequent call to my.browser.export.history to invalidate cache every time
    ## so we hack cachew path so it's different for each call
    from my.core.core_config import config as hpi_core_config

    hpi_cache_dir = hpi_core_config.get_cache_dir()
    sanitized_path = re.sub(r'\W', '_', str(path))
    cache_override = None if hpi_cache_dir is None else hpi_cache_dir / sanitized_path
    ##

    from my.core.common import Paths, classproperty, get_files

    class config:
        class core:
            cache_dir = cache_override

        class browser:
            class export:
                @classproperty
                def export_path(cls) -> Paths:
                    return tuple([f for f in get_files(path, glob='**/*') if is_sqlite_db(f)])

    from my.core.cfg import tmp_config

    with tmp_config(modules='my.browser.export|my.core.core_config', config=config):
        from my.browser.export import history

        yield from _index_new(history())


if TYPE_CHECKING:
    from browserexport.merge import Visit as BrowserMergeVisit
else:
    BrowserMergeVisit = Any


def _index_new(history: Iterator[BrowserMergeVisit]) -> Results:
    for v in history:
        desc: str | None = None
        duration: Second | None = None
        metadata = v.metadata
        if metadata is not None:
            desc = metadata.title
            duration = metadata.duration
        yield Visit(
            url=v.url,
            dt=v.dt,
            locator=Loc(title=desc or v.url, href=v.url),
            duration=duration,
        )
