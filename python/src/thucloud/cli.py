import argparse
import signal
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Literal


def pos_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f'invalid int value: {value!r}') from None
    if n <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Download files from a Tsinghua Cloud shared link.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        'share_link',
        metavar='URL',
        help='shared link to download',
    )
    parser.add_argument(
        '-o',
        '--output-dir',
        type=Path,
        default=Path('.'),
        metavar='DIR',
        help='directory to save downloaded files',
    )
    parser.add_argument(
        '-j',
        '--workers',
        type=pos_int,
        default=4,
        metavar='N',
        help='number of concurrent download workers',
    )
    parser.add_argument(
        '--parse-workers',
        type=pos_int,
        default=None,
        metavar='N',
        help='number of concurrent workers for parsing; None means auto',
    )
    parser.add_argument(
        '--if-exists',
        choices=('error', 'overwrite', 'skip'),
        default='skip',
        help='strategy when the target file already exists',
    )
    parser.add_argument(
        '--mtime',
        choices=('off', 'reported', 'derived'),
        default='derived',
        help='how to set local file modification times',
    )
    parser.add_argument(
        '--progress',
        action=argparse.BooleanOptionalAction,
        default=True,
        help='show progress bars',
    )
    parser.add_argument(
        '-q',
        '--quiet',
        action='store_true',
        help='suppress progress bars and final summary',
    )
    return parser


def _format_size(size: int, /, exact: bool = False) -> str:
    if exact:
        return f'{size:,} B'
    if size < 1024:
        return f'{size} B'
    units = ('KiB', 'MiB', 'GiB', 'TiB')
    n = size
    for unit in units:
        n /= 1024
        if n < 1024:
            return f'{n:.2f} {unit}'
    return f'{n:.2f} {units[-1]}'

def _main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    url: str = args.share_link
    output_dir: Path = args.output_dir.expanduser()
    workers: int = args.workers
    parse_workers: int | None = args.parse_workers
    if_exists: Literal['error', 'overwrite', 'skip'] = args.if_exists
    mtime: Literal['off', 'reported', 'derived'] = args.mtime
    quiet = args.quiet
    use_progress: bool = args.progress and not quiet

    from .api import TqdmProgressCallback, download, parse
    from .api.utils import SessionThreadPoolExecutor

    progress_ctx = TqdmProgressCallback() if use_progress else nullcontext(None)
    with progress_ctx as callback:
        with callback.hack_parse() if callback is not None else nullcontext():
            with SessionThreadPoolExecutor(max_workers=parse_workers) as executor:
                entry = parse(url, get=executor.thread_session_get, executor=executor)
        summary = download(
            entry,
            output_dir=output_dir,
            workers=workers,
            if_exists=if_exists,
            mtime=mtime,
            callback=callback,
        )
    if not quiet:
        print(
            'Download complete.',
            f'Target: {summary.target}',
            (
                f'Files: {summary.files_downloaded}/{summary.files_total} downloaded, '
                f'{len(summary.skipped)} skipped, '
                f'{len(summary.renamed)} renamed, '
                f'{len(summary.overwritten)} overwritten'
            ),
            (
                f'Size: {_format_size(summary.bytes_downloaded)} / '
                f'{_format_size(summary.bytes_total)} downloaded '
                f'({_format_size(summary.bytes_downloaded, exact=True)} / '
                f'{_format_size(summary.bytes_total, exact=True)})'
            ),
            f'Elapsed: {summary.elapsed} ({summary.elapsed.total_seconds():.3f} s)',
            sep='\n',
        )

def main(argv: list[str] | None = None) -> int | None:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print('Interrupted.', file=sys.stderr)
        return 128 + signal.SIGINT
