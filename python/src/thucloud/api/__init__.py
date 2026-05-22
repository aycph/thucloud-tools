import re
from collections.abc import Callable, Iterator
from concurrent.futures import Executor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, dataclass_transform
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

from .utils import DEFAULT_TIMEOUT, CachedProperty, parse_js_obj, traverse

__all__ = [
    'UrlGetter',
    'File',
    'Folder',
    'parse',
]


type UrlGetter = Callable[[str], requests.Response]


# root 只有 Folder 或 Folder 的子项可以获得
# path 一定以 / 打头，并且不含 root
# name 为短名，均不携带 '/'
# 目录的根目录无 name ，设置为 '.'
# 如果解析自文件链接，则 path == '/' + name

@dataclass_transform(eq_default=False, kw_only_default=True, frozen_default=True)
def _entry_dataclass[T](cls: type[T]) -> type[T]:
    return dataclass(cls, eq=False, frozen=True, match_args=False, kw_only=True, slots=True)

@_entry_dataclass
class _Entry:
    token: str
    can_download: bool | None
    root: str | None

    name: str
    path: str
    size: int
    last_modified: datetime | None

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        if type(other) is not type(self):
            return NotImplemented
        return self.token == other.token and self.path == other.path

    def __hash__(self) -> int:
        return hash((self.token, self.path))

@_entry_dataclass
class File(_Entry):
    _raw_path: str | None = field(init=False, repr=False)

    @CachedProperty(slotname='_raw_path', readonly=True)
    def raw_path(self) -> str | None:
        # 只有从目录直接产生的 File 才没有 raw_path
        if self.can_download:
            return f'https://cloud.tsinghua.edu.cn/d/{self.token}/files/?p={quote(self.path)}&dl=1'
        return None

    def get_raw_path(self, /, *, get: UrlGetter) -> str:
        file = _parse_file(f'https://cloud.tsinghua.edu.cn/d/{self.token}/files/?p={quote(self.path)}', get=get)
        raw_path = file.raw_path
        assert raw_path is not None, '_parse_file should always create a File with raw_path'
        object.__setattr__(self, '_raw_path', raw_path)
        return raw_path

@_entry_dataclass
class Folder(_Entry):
    can_download: bool
    root: str

    dirents: tuple[File | Folder, ...] = field(repr=False, compare=False)

    def __iter__(self) -> Iterator[File | Folder]:
        return iter(self.dirents)

    def iter_files(self) -> Iterator[File]:
        for f in self:
            if isinstance(f, Folder):
                yield from f.iter_files()
            else:
                yield f


def _extract_page_options(html: str) -> dict[str, Any] | None:
    m = re.search(r'<script type="text/javascript">\s*window\.shared = ((?s:.)*?);\s*</script>', html)
    if m is None:
        return None
    text = m.group(1)
    # 去除首位空格
    lines = [line.strip() for line in text.split('\n')]
    # 删除可能的函数键值对
    try:
        start = lines.index('zipped: (function() {')
    except ValueError:
        pass
    else:
        end = lines.index('})(),')
        del lines[start:end+1]
    # '||' -> 'or'
    # 删除可能的注释行
    lines = [
        line.replace('||', ' or ')
        for line in lines
        if not line.startswith('//')
    ]
    return parse_js_obj('\n'.join(lines))['pageOptions']


def _parse_wopi_file(html: str, /, token: str, *, get: UrlGetter) -> File:
    m_action = re.search(r'<form id="office_form" name="office_form" target="office_frame" action="(.*?)" method="post">', html)
    if m_action is None:
        raise ValueError('Unexpected html: office_form not found')
    action = m_action.group(1)
    wopis = parse_qs(urlparse(unquote(action).replace('&amp;', '&')).query).get('WOPISrc')
    if wopis is None:
        raise ValueError('Unexpected html: WOPISrc not found')
    wopi, = wopis

    m_token = re.search(r'<input name="access_token" value="([0-9a-f]{32})" type="hidden"/>', html)
    if m_token is None:
        raise ValueError('Unexpected html: access_token not found')
    access_token = m_token.group(1)

    info_url = f'{wopi}?access_token={access_token}'
    raw_path = f'{wopi}/contents?access_token={access_token}'
    info = get(info_url).json()
    file = File(
        token=token,
        can_download=None,
        root=None,
        name=info['BaseFileName'],
        path='/' + info['BaseFileName'],
        size=info['Size'],
        last_modified=datetime.fromisoformat(info['LastModifiedTime']),
    )
    object.__setattr__(file, '_raw_path', raw_path)
    return file

def _parse_file(url: str, /, *, get: UrlGetter) -> File:
    html = get(url).text
    info = _extract_page_options(html)
    if info is None:
        m_token = re.search(r'/([0-9a-f]{20})/', url)
        if m_token is None:
            raise ValueError(f'Unrecognized url: {url}')
        token = m_token.group(1)
        return _parse_wopi_file(html, token, get=get)
    file = File(
        token=info['sharedToken'],
        can_download=info['canDownload'],
        root=None,
        name=info['fileName'],
        path='/' + info['fileName'],
        size=info['fileSize'],
        last_modified=None,
    )
    object.__setattr__(file, '_raw_path', info['rawPath'])
    return file


def _fetch_dirent_list(path: str, /, token: str, *, get: UrlGetter) -> list[dict[str, Any]]:
    url = f'https://cloud.tsinghua.edu.cn/api/v2.1/share-links/{token}/dirents/?path={quote(path)}'
    return get(url).json()['dirent_list']

def _get_dirents(
    path: str,
    /, token: str, can_download: bool, root: str,
    *, get: UrlGetter, executor: Executor | None,
) -> tuple[File | Folder, ...]:
    if executor is None:
        def get_dirent_list(path: str):
            return _fetch_dirent_list(path, token, get=get)
    else:
        def visit(path: str):
            return _fetch_dirent_list(path, token, get=get)
        def expand(path: str, data: list[dict[str, Any]]) -> list[str]:
            return [item['folder_path'] for item in data if item['is_dir']]
        path2direntlist = traverse(executor, visit, [path], expand)
        def get_dirent_list(path: str):
            return path2direntlist[path]

    def get_dirents(path: str) -> tuple[File | Folder, ...]:
        return tuple(parse_item(item) for item in get_dirent_list(path))
    def parse_item(item: dict[str, Any]) -> File | Folder:
        if item['is_dir']:
            path = item['folder_path']
            dirents = get_dirents(path)
            size = sum(f.size for f in dirents)
            return Folder(
                token=token,
                can_download=can_download,
                root=root,
                name=item['folder_name'],
                path=path,
                size=size,
                last_modified=datetime.fromisoformat(item['last_modified']),
                dirents=dirents,
            )
        else:
            return File(
                token=token,
                can_download=can_download,
                root=root,
                name=item['file_name'],
                path=item['file_path'],
                size=item['size'],
                last_modified=datetime.fromisoformat(item['last_modified']),
            )

    return get_dirents(path)

def _parse_folder(url: str, /, *, get: UrlGetter, executor: Executor | None) -> Folder:
    html = get(url).text
    info = _extract_page_options(html)
    if info is None:
        raise ValueError(f'Unrecognized HTML: {url}')
    token = info['token']
    can_download = info['canDownload']
    root = info['dirName']
    path = info['dirPath']
    name = path.rstrip('/').rsplit('/', 1)[-1] or '.'
    dirents = _get_dirents(path, token, can_download, root, get=get, executor=executor)
    size = sum(f.size for f in dirents)
    return Folder(
        token=token,
        can_download=can_download,
        root=root,
        name=name,
        path=path,
        size=size,
        last_modified=None,
        dirents=dirents,
    )

def _default_get(url: str) -> requests.Response:
    r = requests.get(url, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r

def parse(
    url: str,
    /,
    *,
    get: UrlGetter = _default_get,
    executor: Executor | None = None,
) -> File | Folder:
    parsed = urlparse(url)
    if parsed.netloc != 'cloud.tsinghua.edu.cn':
        raise ValueError(f'Invalid netloc: {parsed.netloc}')
    paths = parsed.path.strip('/').split('/')
    if len(paths) < 2 or re.match('[0-9a-f]{20}$', paths[1]) is None:
        raise ValueError(f'Unrecognized url: {url}')
    if paths[0] == 'd':
        if len(paths) >= 3:
            if len(paths) > 3 or paths[2] != 'files':
                raise ValueError(f'Unrecognized url: {url}')
            return _parse_file(url, get=get)
        else:
            return _parse_folder(url, get=get, executor=executor)
    elif paths[0] == 'f':
        return _parse_file(url, get=get)
    else:
        raise ValueError(f'Unrecognized url: {url}')
