from collections.abc import Hashable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import dataclass_transform
from urllib.parse import quote

from .utils import CachedProperty, UrlGetter

__all__ = ['File', 'Folder']


# root 只有 Folder 或 Folder 的子项可以获得
# path 是相对于 root 的路径，一定以 / 打头，并且不含 root
# 特殊文件无法获得 path ，如果也无法从 url 获得，则会被设置为 '/' + name
# 这样 path 与单文件链接相符合
# name 为短名
# 目录的根目录无 name ，name 会被设置为 root
# name、path、root 均不保证为合法路径或文件名，非法字符需手动处理


@dataclass_transform(eq_default=False, kw_only_default=True, frozen_default=True)
def _entry_dataclass[T](cls: type[T]) -> type[T]:
    return dataclass(cls, eq=False, frozen=True, match_args=False, kw_only=True, slots=True)

@_entry_dataclass
class _Entry(Hashable):
    token: str
    path: str

    name: str
    size: int
    last_modified: datetime | None
    root: str | None
    can_download: bool | None

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
        from ._parser import _parse_file # 避免循环导入
        file = _parse_file(f'https://cloud.tsinghua.edu.cn/d/{self.token}/files/?p={quote(self.path)}', get=get)
        raw_path = file.raw_path
        assert raw_path is not None, '_parse_file should always create a File with raw_path'
        object.__setattr__(self, '_raw_path', raw_path)
        return raw_path

@_entry_dataclass
class Folder(_Entry): # 选择不继承 Mapping ，因为迭代的是 values()
    root: str
    can_download: bool

    dirents: MappingProxyType[str, File | Folder] = field(repr=False)
    file_count: int = field(init=False)
    folder_count: int = field(init=False)

    def __post_init__(self):
        file_count = 0
        folder_count = 0
        for f in self.dirents.values():
            if isinstance(f, Folder):
                file_count += f.file_count
                folder_count += f.folder_count + 1
            else:
                file_count += 1
        object.__setattr__(self, 'file_count', file_count)
        object.__setattr__(self, 'folder_count', folder_count)

    def __iter__(self) -> Iterator[File | Folder]:
        return iter(self.dirents.values())

    def __len__(self) -> int:
        return len(self.dirents)

    def iter_files(self) -> Iterator[File]:
        for f in self:
            if isinstance(f, Folder):
                yield from f.iter_files()
            else:
                yield f

    def iter_folders(self) -> Iterator[Folder]:
        for f in self:
            if isinstance(f, Folder):
                yield f
                yield from f.iter_folders()

    def __getitem__(self, key: str) -> File | Folder:
        return self.dirents[key]

    def get[T = None](self, key: str, default: T = None) -> File | Folder | T:
        return self.dirents.get(key, default)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            return key in self.dirents
        if isinstance(key, (File, Folder)):
            return self.dirents.get(key.name) == key
        return False
