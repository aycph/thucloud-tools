import functools
import os
import tempfile
import warnings
from collections.abc import Callable, Hashable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Executor, Future, wait
from pathlib import Path
from typing import Any, Literal, Protocol, Self, overload

import requests

__all__ = [
    'CachedProperty',
    'parse_js_obj',
    'ProgressCallback',
    'download',
    'traverse',
]


################################################################################
### CachedProperty: cached descriptors
################################################################################
class CachedProperty[O, T]:
    __slots__ = 'fget', '__doc__', 'attrname', 'slotname', 'readonly'

    fget: Callable[[O], T]
    __doc__: str | None
    attrname: str | None
    slotname: str | None
    readonly: bool

    @overload
    def __new__(
        cls,
        fget: None = None,
        /,
        *,
        slotname: str | None = None,
        readonly: bool = False,
    ) -> Callable[[Callable[[O], T]], Self]:
        ...
    @overload
    def __new__(
        cls,
        fget: Callable[[O], T],
        /,
        *,
        slotname: str | None = None,
        readonly: bool = False,
    ) -> Self:
        ...
    def __new__(
        cls,
        fget=None,
        /,
        *,
        slotname=None,
        readonly=False,
    ) -> Callable[[Callable[[O], T]], Self] | Self:
        if fget is None:
            return functools.partial(cls, slotname=slotname, readonly=readonly)
        return super().__new__(cls)

    def __init__(
        self,
        fget: Callable[[O], T],
        /,
        *,
        slotname: str | None = None,
        readonly: bool = False,
    ):
        self.fget = fget
        self.__doc__ = getattr(fget, '__doc__', None)
        self.attrname = None
        self.slotname = slotname
        self.readonly = readonly

    def __set_name__(self, owner: type[O], name: str) -> None:
        if self.attrname is not None and self.attrname != name:
            raise TypeError(
                f'Cannot assign a CachedProperty to two different names: '
                f'{self.attrname!r} and {name!r}'
            )
        if self.slotname == name:
            raise TypeError(
                f'CachedProperty {name!r} cannot use itself as the cache slot'
            )
        self.attrname = name
        if self.slotname is None:
            self.slotname = '_' + name

    @overload
    def __get__(self, obj: None, objtype: type[O] | None = None) -> Self: ...
    @overload
    def __get__(self, obj: O, objtype: type[O] | None = None) -> T: ...
    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        if self.slotname is None:
            raise TypeError(
                'Cannot use CachedProperty without assigning a slot name'
            )
        try:
            return object.__getattribute__(obj, self.slotname)
        except AttributeError:
            value = self.fget(obj)
            try:
                object.__setattr__(obj, self.slotname, value)
            except AttributeError as exc:
                raise TypeError(
                    f'Cannot cache {self.attrname!r} on '
                    f'{type(obj).__name__!r} object: '
                    f'missing slot {self.slotname!r} and no writable __dict__'
                ) from exc
            return value

    def __set__(self, obj: O, value: T) -> None:
        if self.readonly:
            raise AttributeError(
                f'CachedProperty {self.attrname!r} of {type(obj).__name__!r} '
                'object is readonly'
            )
        if self.slotname is None:
            raise TypeError(
                'Cannot use CachedProperty without assigning a slot name'
            )
        object.__setattr__(obj, self.slotname, value)

    def __delete__(self, obj: O) -> None:
        if self.readonly:
            raise AttributeError(
                f'CachedProperty {self.attrname!r} of {type(obj).__name__!r} '
                'object is readonly'
            )
        if self.slotname is None:
            raise TypeError(
                'Cannot use CachedProperty without assigning a slot name'
            )
        object.__delattr__(obj, self.slotname)


################################################################################
### parse_js_obj: JavaScript-like object parsing
################################################################################

class _JSLocals(Mapping[str, Any]):
    MAP = {
        'false': False,
        'true': True,
        'null': None,
        'NaN': float('nan'),
        'Infinity': float('inf'),
    }
    def __getitem__(self, key: str) -> Any:
        return self.MAP.get(key, key)
    def __iter__(self):
        return iter(())
    def __len__(self):
        return 0

def parse_js_obj(s: str) -> Any:
    globals = { '__builtins__': {} }
    locals = _JSLocals()
    return eval(s, globals=globals, locals=locals)


################################################################################
### download: download utilities
################################################################################

class ProgressCallback(Protocol):
    def __call__(
        self,
        event: Literal['start', 'progress', 'end'],
        downloaded: int,
        total: int | None,
        /,
    ) -> None: ...

DEFAULT_TIMEOUT = (5, 10)
DEFAULT_CHUNK_SIZE = 256 * 1024

def _get_content_length(response: requests.Response) -> int | None:
    # 有 Encoding 时 Content-Length 不可信，不提供
    if response.headers.get('Transfer-Encoding'):
        return None
    encoding = response.headers.get('Content-Encoding')
    if encoding and encoding.lower().strip() != 'identity':
        return None

    value = response.headers.get('Content-Length')
    if value is None:
        return None
    try:
        length = int(value)
        if length < 0:
            raise ValueError
        return length
    except ValueError:
        warnings.warn(
            f'Invalid Content-Length from {response.url}: {value!r}',
            RuntimeWarning,
            stacklevel=2,
        )
        return None

def download(
    url: str,
    path: str | os.PathLike[str],
    *,
    headers: Mapping[str, str] | None = None,
    session: requests.Session | None = None,
    timeout: float | tuple[float, float] | None = DEFAULT_TIMEOUT,
    overwrite: bool = True,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    callback: ProgressCallback | None = None,
) -> Path:
    if chunk_size <= 0:
        raise ValueError(f'Invalid chunk_size: {chunk_size}')

    target = Path(path)
    if target.exists() and not overwrite:
        raise FileExistsError(f'File already exists: {target}')

    client = session or requests
    response = client.get(url, headers=headers, timeout=timeout, stream=True)
    with response:
        response.raise_for_status()

        total = _get_content_length(response)
        downloaded = 0
        if callback is not None:
            callback('start', 0, total)

        fd, tmp_path = tempfile.mkstemp(
            suffix=f'-{target.name}.tmp',
            prefix=f'~$',
            dir=target.parent,
        )
        with os.fdopen(fd, 'wb') as file:
            for chunk in response.iter_content(chunk_size):
                if not chunk:
                    continue
                file.write(chunk)
                downloaded += len(chunk)
                if callback is not None:
                    callback('progress', downloaded, total)
        if callback is not None:
            callback('end', downloaded, total)

        # 再次检查以避免不期待的文件覆盖
        if target.exists() and not overwrite:
            # 删除后缀表示这是一个完整文件
            os.replace(tmp_path, tmp_path.removesuffix('.tmp'))
            raise FileExistsError(f'File already exists: {target}')
        os.replace(tmp_path, target)

    return target


################################################################################
### traverse: Concurrent traversal
################################################################################

def traverse[Node: Hashable, Value](
    executor: Executor,
    visit: Callable[[Node], Value],
    roots: Iterable[Node],
    expand: Callable[[Node, Value], Iterable[Node]],
) -> dict[Node, Value]:
    node2value: dict[Node, Value] = {}
    pending2node: dict[Future[Value], Node] = {}
    seen: set[Node] = set()
    def submit(node: Node):
        if node not in seen:
            seen.add(node)
            pending2node[executor.submit(visit, node)] = node
    try:
        for node in roots:
            submit(node)
        while pending2node:
            done, _ = wait(pending2node, return_when=FIRST_COMPLETED)
            for fut in done:
                node = pending2node.pop(fut)
                value = fut.result()
                node2value[node] = value
                for node in expand(node, value):
                    submit(node)
    except BaseException:
        # 以捕获 KeyboardInterrupt/SystemExit
        for fut in pending2node:
            fut.cancel()
        raise
    return node2value
