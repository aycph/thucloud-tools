import os
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any, ClassVar

from tqdm import tqdm

from ._download import ProgressCallback, ProgressEvent
from ._entries import File, Folder
from ._utils import Via

__all__ = ['TqdmProgressCallback']


def _char_width(ch: str) -> int:
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

class TqdmProgressCallback(ProgressCallback):
    TQDM_KW: ClassVar[dict[str, Any]] = {
        'leave': None,
        'mininterval': 1,
        'miniters': 0,
        'unit': 'B',
        'unit_scale': True,
        'unit_divisor': 1024,
        'dynamic_ncols': True,
        'smoothing': 1,
    }
    DESC_WIDTH: ClassVar[int] = 40

    def write(self, msg: str):
        tqdm.write(msg, file=self._tqdm_kw.get('file', sys.stderr))

    @classmethod
    def pad_desc(cls, path: Path) -> str:
        # 若长度已够短，直接填充空格
        path_str = str(path)
        path_str_width = [_char_width(ch) for ch in path_str]
        path_width = sum(path_str_width)
        if (path_width <= cls.DESC_WIDTH):
            return path_str + ' '*(cls.DESC_WIDTH - path_width)

        # 若文件名够短，返回 '{prefix}.../{name}'
        name_str = path.name
        name_str_width = [_char_width(ch) for ch in name_str]
        name_width = sum(name_str_width)
        if (name_width <= cls.DESC_WIDTH - 4):
            remaining = cls.DESC_WIDTH - name_width - 4
            index = 0
            while remaining >= (w := path_str_width[index]):
                remaining -= w
                index += 1
            return f'{path_str[:index]}...{os.sep}{name_str}' + ' '*remaining

        # 若文件名也不够短，返回 '.../{nameprefix}...{namepostfix}'
        remaining = cls.DESC_WIDTH - 7
        index = 0
        while remaining >= (w := (name_str_width[index] + name_str_width[-(index+1)])):
            remaining -= w
            index += 1
        if remaining >= (w := name_str_width[-(index+1)]):
            remaining -= w
            rindex = -(index+1)
        else:
            rindex = -index
        return f'...{os.sep}{name_str[:index]}...{name_str[rindex:]}' + ' '*remaining

    def __init__(self, tqdm_kw: dict[str, Any] | None = None):
        self._tqdm_kw = self.TQDM_KW | (tqdm_kw or {})
        self._total_bar = None
        self._bars: list[tqdm] = []
        self._next_position = 1

        self._lock = threading.Lock()
        self._local = threading.local()

        self._root = None
        self._files_done = 0

        self._closed = False

    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        self.close()
    def close(self):
        if not self._closed:
            self._closed = True
            for bar in self._bars:
                bar.close()
            if self._total_bar is not None:
                self._total_bar.close()

    @Via
    def _thread_local(self):
        return self._local

    _thread_bar = _thread_local.bar[tqdm]
    _thread_file = _thread_local.file[File]

    def __call__(
        self,
        root_entry: File | Folder,
        file: File,
        target: Path,
        event: ProgressEvent,
        downloaded: int,
        /,
    ):
        if self._closed:
            raise RuntimeError('progress callback has already been closed')

        if self._root is None:
            with self._lock:
                if self._root is None:
                    self._root = root_entry
        if root_entry != self._root:
            raise ValueError(
                '`root_entry` is inconsistent: '
                f'expected={self._root!r}, actual={root_entry!r}'
            )

        if self._total_bar is None:
            with self._lock:
                if self._total_bar is None:
                    total_bar = tqdm(
                        position=0,
                        total=root_entry.size,
                        desc=root_entry.name,
                        **self._tqdm_kw
                    )
                    self._total_bar = total_bar
        total_bar = self._total_bar

        if isinstance(self._root, File):
            if file != root_entry:
                raise ValueError(
                    '`file` must be `root_entry` when `root_entry` is `File`'
                    f'{file=}, {root_entry=}'
                )
            match event:
                case 'start':
                    pass
                case 'progress':
                    delta = downloaded - total_bar.n
                    total_bar.update(delta)
                case 'end':
                    if downloaded != file.size:
                        raise RuntimeError(
                            'downloaded size mismatch: '
                            f'expected={file.size}, actual={downloaded}'
                        )
                    delta = downloaded - total_bar.n
                    total_bar.update(delta)
                    total_bar.refresh()
                    self._files_done += 1
                    self._update_desc(total_bar, self._files_done, self._get_file_cnt(root_entry))
                case 'skip':
                    total_bar.update(file.size)
                    with self._lock:
                        self._files_done += 1
                        self._update_desc(total_bar, self._files_done, self._get_file_cnt(root_entry))
            return

        if event == 'skip': # 提前返回，避免 skip 时创建 bar
            total_bar.update(file.size)
            with self._lock:
                self._files_done += 1
                self._update_desc(total_bar, self._files_done, self._get_file_cnt(root_entry))
            return

        try:
            bar = self._thread_bar
        except AttributeError:
            with self._lock:
                pos = self._next_position
                self._next_position = pos + 1
            bar = tqdm(
                position=pos,
                desc=self.pad_desc(target),
                total=file.size,
                **self._tqdm_kw
            )
            self._thread_bar = bar
            self._bars.append(bar)

        match event:
            case 'start':
                self._thread_file = file
                bar.reset(file.size)
                bar.set_description(self.pad_desc(target))
            case 'progress':
                if file != self._thread_file:
                    raise RuntimeError(
                        "'progress' event received while another file is active: "
                        f'active={self._thread_file!r}, new={file!r}'
                    )
                delta = downloaded - bar.n
                bar.update(delta)
                total_bar.update(delta)
            case 'end':
                if file != self._thread_file:
                    raise RuntimeError(
                        "'end' event received while another file is active: "
                        f'active={self._thread_file!r}, new={file!r}'
                    )
                if downloaded != file.size:
                    raise RuntimeError(
                        'downloaded size mismatch: '
                        f'expected={file.size}, actual={downloaded}'
                    )
                delta = downloaded - bar.n
                bar.update(delta)
                bar.refresh()
                total_bar.update(delta)
                total_bar.refresh()
                with self._lock:
                    self._files_done += 1
                    self._update_desc(total_bar, self._files_done, self._get_file_cnt(root_entry))
                del self._thread_file

    @staticmethod
    def _get_file_cnt(entry: File | Folder):
        return 1 if isinstance(entry, File) else entry.file_count

    @staticmethod
    def _update_desc(total_bar: tqdm, files_done: int, files_cnt: int):
        total_bar.set_postfix_str(f'files: {files_done}/{files_cnt}')
