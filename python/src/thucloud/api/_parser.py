import re
from concurrent.futures import Executor
from datetime import datetime
from html import unescape
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from ._entries import File, Folder
from ._utils import UrlGetter, default_get, parse_js_obj, traverse

__all__ = ['parse']


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


def _parse_wopi_file(html: str, /, token: str, path: str | None, *, get: UrlGetter) -> File:
    m_action = re.search(r'<form id="office_form" name="office_form" target="office_frame" action="(.*?)" method="post">', html)
    if m_action is None:
        raise ValueError('Unexpected html: office_form not found')
    action = m_action.group(1)
    wopis = parse_qs(urlparse(unescape(action)).query).get('WOPISrc')
    if wopis is None:
        raise ValueError('Unexpected html: WOPISrc not found')
    wopi, = wopis
    wopi = unquote(wopi)

    m_token = re.search(r'<input name="access_token" value="([0-9a-f]{32})" type="hidden"/>', html)
    if m_token is None:
        raise ValueError('Unexpected html: access_token not found')
    access_token = m_token.group(1)

    info_url = f'{wopi}?access_token={access_token}'
    raw_path = f'{wopi}/contents?access_token={access_token}'
    info = get(info_url).json()

    file = File(
        token=token,
        path=path if path is not None else '/' + info['BaseFileName'],
        name=info['BaseFileName'],
        size=info['Size'],
        last_modified=datetime.fromisoformat(info['LastModifiedTime']),
        root=None,
        can_download=None,
    )
    object.__setattr__(file, '_raw_path', raw_path)
    return file

def _parse_file(url: str, /, *, get: UrlGetter) -> File:
    html = get(url).text
    info = _extract_page_options(html)
    if info is None:
        # 说明可能是 office 文件
        m_token = re.search(r'/([0-9a-f]{20})/', url)
        if m_token is None:
            raise ValueError(f'Unrecognized url: {url}')
        token = m_token.group(1)
        # 尝试获取路径，如果来自于目录共享链接将提供 path
        path = parse_qs(urlparse(url).query).get('p')
        if path is not None:
            path, = path
            path = unquote(path)
        return _parse_wopi_file(html, token, path, get=get)
    file = File(
        token=info['sharedToken'],
        path=info['filePath'],
        name=info['fileName'],
        size=info['fileSize'],
        last_modified=None,
        root=None,
        can_download=info['canDownload'],
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
) -> MappingProxyType[str, File | Folder]:
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

    def get_dirents(path: str) -> MappingProxyType[str, File | Folder]:
        return MappingProxyType({
            (f := parse_item(item)).name: f
            for item in get_dirent_list(path)
        })
    def parse_item(item: dict[str, Any]) -> File | Folder:
        if item['is_dir']:
            path = item['folder_path']
            dirents = get_dirents(path)
            size = sum(f.size for f in dirents.values())
            return Folder(
                token=token,
                path=path,
                name=item['folder_name'],
                size=size,
                last_modified=datetime.fromisoformat(item['last_modified']),
                root=root,
                can_download=can_download,
                dirents=dirents,
            )
        else:
            return File(
                token=token,
                path=item['file_path'],
                name=item['file_name'],
                size=item['size'],
                last_modified=datetime.fromisoformat(item['last_modified']),
                root=root,
                can_download=can_download,
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
    path = info['relativePath']
    name = path.rstrip('/').rsplit('/', 1)[-1] or root
    dirents = _get_dirents(path, token, can_download, root, get=get, executor=executor)
    size = sum(f.size for f in dirents.values())
    return Folder(
        token=token,
        path=path,
        name=name,
        size=size,
        last_modified=None,
        root=root,
        can_download=can_download,
        dirents=dirents,
    )


def parse(
    url: str,
    /,
    *,
    get: UrlGetter = default_get,
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
