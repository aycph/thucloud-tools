import functools
import os
import sys
import tempfile
import threading
import unicodedata
import warnings
from collections.abc import Callable, Hashable, Iterable, Mapping
from concurrent.futures import (
    FIRST_COMPLETED, Executor, Future, ThreadPoolExecutor, wait
)
from pathlib import Path
from typing import Any, Literal, Protocol, Self, cast, overload, override

if sys.version_info < (3, 15):
    try:
        from typing_extensions import TypeForm
    except ImportError:
        from builtins import type as TypeForm
else:
    from typing import TypeForm

import requests

__all__ = [
    'CachedProperty',
    'parse_js_obj',
    'ProgressCallback',
    'download',
    'UrlGetter',
    'default_get',
    'SessionThreadPoolExecutor',
    'traverse',
    'sanitize_filename',
    'Via',
    'char_width',
]


################################################################################
### CachedProperty: cached descriptor
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
### parse_js_obj: JavaScript-like literal object parsing
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
    # eval 的关键字传参需要 3.13 起
    return eval(s, globals, locals)


################################################################################
### download: streaming download utilities
################################################################################

class ProgressCallback(Protocol):
    def __call__(
        self,
        event: Literal['start', 'progress', 'end'],
        downloaded: int,
        total: int | None,
        /,
    ) -> None: ...

def _get_content_length(response: requests.Response) -> int | None:
    # 有 Content-Encoding 时 Content-Length 不可信，不提供
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

DEFAULT_TIMEOUT = (5, 10)
DEFAULT_CHUNK_SIZE = 256 * 1024

def download(
    url: str,
    path: str | os.PathLike[str],
    /,
    *,
    headers: Mapping[str, str] | None = None,
    session: requests.Session | None = None,
    timeout: float | tuple[float, float] | None = DEFAULT_TIMEOUT,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overwrite: bool = True,
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
                file.write(chunk)
                downloaded += len(chunk)
                if callback is not None:
                    callback('progress', downloaded, total)

        # 再次检查，避免意外覆盖文件
        if target.exists() and not overwrite:
            # 移除临时后缀，表示文件已被完整下载
            os.replace(tmp_path, tmp_path.removesuffix('.tmp'))
            raise FileExistsError(f'File already exists: {target}')
        os.replace(tmp_path, target)

        # 汇报完成
        if callback is not None:
            callback('end', downloaded, total)

    return target


################################################################################
### default_get: default UrlGetter
################################################################################

type UrlGetter = Callable[[str], requests.Response]

def default_get(url: str, /, timeout=DEFAULT_TIMEOUT) -> requests.Response:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r


################################################################################
### SessionThreadPoolExecutor: ThreadPoolExecutor with thread-local sessions
################################################################################

class SessionThreadPoolExecutor(ThreadPoolExecutor):
    __slots__ = '_local', '_sessions'

    @overload
    def __init__(
        self,
        max_workers: int | None = None,
        thread_name_prefix: str = '',
        initializer: None = None,
        initargs: tuple[()] = (),
    ) -> None:
        ...
    @overload
    def __init__[*Ts](
        self,
        max_workers: int | None,
        thread_name_prefix: str,
        initializer: Callable[[*Ts], None],
        initargs: tuple[*Ts],
    ) -> None:
        ...
    @override
    def __init__(self, max_workers=None, thread_name_prefix='',
                 initializer=None, initargs=()):
        super().__init__(max_workers, thread_name_prefix, initializer, initargs)
        self._local = threading.local()
        self._sessions = []

    @override
    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False):
        super().shutdown(wait, cancel_futures=cancel_futures)
        if wait:
            for session in self._sessions:
                session.close()
            self._sessions.clear()

    @property
    def thread_session(self) -> requests.Session:
        if self._shutdown:
            raise RuntimeError('cannot use thread_session after shutdown')
        try:
            return self._local.session
        except AttributeError:
            session = requests.Session()
            self._local.session = session
            self._sessions.append(session)
            return session

    def thread_session_get(self, url: str, **kwargs) -> requests.Response:
        return self.thread_session.get(url, **kwargs)

################################################################################
### traverse: concurrent graph traversal
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
        wait(pending2node)
        raise
    return node2value


################################################################################
### sanitize_filename: filename sanitization
################################################################################

# Copied from `Lib/ntpath.py`
_reserved_chars = frozenset(
    {chr(i) for i in range(32)} |
    {'"', '*', ':', '<', '>', '?', '|', '/', '\\'}
)
_reserved_names = frozenset(
    {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} |
    {f'COM{c}' for c in '123456789\xb9\xb2\xb3'} |
    {f'LPT{c}' for c in '123456789\xb9\xb2\xb3'}
)

_reserved_char_table = {ord(c): '_' for c in _reserved_chars}

def sanitize_filename(name: str) -> str:
    name0 = name
    if not isinstance(name, str):
        raise TypeError(f'name must be str, not {type(name)!r}')
    if '/' in name or '\\' in name:
        raise ValueError(f'name cannot contain slash or backslash: {name0!r}')
    name = name.rstrip(' .')
    if name in {'', '.', '..'}:
        raise ValueError(f'unsanitized filename: {name0!r}')
    if name.partition('.')[0].rstrip(' ').upper() in _reserved_names:
        name = '_' + name
    return name.translate(_reserved_char_table)


################################################################################
### Via: attribute routing
################################################################################

class Via[O, T]:
    __slots__ = '_fget'
    def __init__(self, fget: Callable[[O], T]):
        self._fget = fget
    def __getattr__(self, name: str) -> 'Via.RoutedAttribute[Any, O, T]':
        if name.startswith('__') and name.endswith('__'):
            raise AttributeError(name)
        return self.RoutedAttribute(self._fget, name)
    @overload
    def __get__(self, obj: None, objtype: type[O] | None = None) -> Self: ...
    @overload
    def __get__(self, obj: O, objtype: type[O] | None = None) -> T: ...
    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return self._fget(obj)

    class RoutedAttribute[V_, O_, T_]:
        __slots__ = '_fget', '_attrname'
        def __init__(self, fget: Callable[[O_], T_], attrname: str):
            self._fget = fget
            self._attrname = attrname
        @overload
        def __get__(self, obj: None, objtype: type[O_] | None = None) -> Self: ...
        @overload
        def __get__(self, obj: O_, objtype: type[O_] | None = None) -> V_: ...
        def __get__(self, obj, objtype=None):
            if obj is None:
                return self
            return getattr(self._fget(obj), self._attrname)
        def __set__(self, obj: O_, value: V_) -> None:
            setattr(self._fget(obj), self._attrname, value)
        def __delete__(self, obj: O_) -> None:
            delattr(self._fget(obj), self._attrname)
        def __getitem__[V](self, _: TypeForm[V]) -> 'Via.RoutedAttribute[V, O_, T_]':
            return cast(Via.RoutedAttribute[V, O_, T_], self)


################################################################################
### char_width: character display width
################################################################################

def char_width(ch: str) -> int:
    if len(ch) != 1:
        raise TypeError(f'expected a character, but string of length {len(ch)} found')
    # 组合字符，比如重音符号
    if unicodedata.combining(ch):
        return 0
    # 控制字符
    if unicodedata.category(ch).startswith("C"):
        return 0
    # CJK/全角字符
    if unicodedata.east_asian_width(ch) in {'F', 'W'}:
        return 2
    return 1
