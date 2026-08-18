import { MultiBar } from 'cli-progress';
import type { Options, Preset, SingleBar } from 'cli-progress';

import { CloudFile, ProgressCallback } from './index.js';

function formatBytes(value: number): string {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (value >= 1024 && i < units.length - 1) {
        value /= 1024;
        ++i;
    }
    return `${i === 0 ? value : value.toFixed(2)}${units[i]}`;
}

const DEFAULT_OPTIONS: Options = {
    format: '{percentage}%|{bar}| {value}/{total} [{duration_formatted}<{eta_formatted}] {target}',
    formatValue(value, _options, type) {
        if (type === 'value' || type === 'total')
            return formatBytes(value);
        return String(value);
    },
    barCompleteChar: '█',
    barIncompleteChar: ' ',
    autopadding: true,
};

const DEFAULT_TOTAL_BAR_FORMAT = '{name}: {percentage}%|{bar}| {value}/{total} [{duration_formatted}<{eta_formatted}, files:{filesDone}/{filesTotal}]';

export function makeProgressBarCallback(
    opt: Options = {},
    preset?: Preset,
    totalBarFormat: string = DEFAULT_TOTAL_BAR_FORMAT,
): ProgressCallback & { close: () => void } {
    opt = { ...DEFAULT_OPTIONS, ...opt };
    const multibar = new MultiBar(opt, preset);
    let totalBar: SingleBar | undefined = undefined;
    const freeBars: SingleBar[] = [];
    const file2bar = new Map<CloudFile, SingleBar>();
    const file2downloaded = new Map<CloudFile, number>();
    let filesDone = 0;
    let filesTotal = NaN;

    let closed = false;

    const callback: ProgressCallback & { close: () => void } = (root, file, target, event, downloaded) => {
        if (closed)
            throw new Error('Progress callback has already been closed');

        if (totalBar === undefined) {
            filesTotal = root instanceof CloudFile ? 1 : root.file_count;
            totalBar = multibar.create(root.size, 0, { name: root.name, filesDone, filesTotal }, { format: totalBarFormat });
        }

        let bar: SingleBar;
        if (root instanceof CloudFile) {
            switch (event) {
                case 'start':
                    break;
                case 'progress':
                    totalBar.update(downloaded);
                    break;
                case 'end':
                    if (downloaded !== file.size)
                        throw new Error(`Downloaded size mismatch: expected=${file.size}, actual=${downloaded}`);
                    filesDone += 1;
                    totalBar.update(downloaded, { filesDone, filesTotal });
                    break;
                case 'skip':
                    filesDone += 1;
                    totalBar.increment(file.size, { filesDone, filesTotal });
                    break;
                default:
                    event satisfies never;
            }
            return;
        }

        if (event === 'skip') {
            filesDone += 1;
            totalBar.increment(file.size, { filesDone, filesTotal });
            return;
        }

        switch (event) {
            case 'start':
                if (freeBars.length) {
                    bar = freeBars.shift()!;
                    bar.start(file.size, 0, { target });
                } else {
                    bar = multibar.create(file.size, 0, { target });
                }
                file2bar.set(file, bar);
                file2downloaded.set(file, 0);
                break;
            case 'progress': {
                bar = file2bar.get(file)!;
                const step = downloaded - file2downloaded.get(file)!;
                file2downloaded.set(file, downloaded);
                bar.increment(step);
                totalBar.increment(step);
                break;
            }
            case 'end': {
                if (downloaded !== file.size)
                    throw new Error(`Downloaded size mismatch: expected=${file.size}, actual=${downloaded}`);
                bar = file2bar.get(file)!;
                const step = downloaded - file2downloaded.get(file)!;
                file2downloaded.delete(file);
                bar.increment(step);
                bar.stop();
                file2bar.delete(file);
                freeBars.push(bar);
                filesDone += 1;
                totalBar.increment(step, { filesDone, filesTotal });
                break;
            }
            default:
                event satisfies never;
        }
    };
    callback.log = multibar.log.bind(multibar);
    callback.close = () => {
        if (closed)
            return;
        closed = true;
        multibar.update(); // flush log 中的 buffer
        multibar.stop();
    };
    return callback;
}
