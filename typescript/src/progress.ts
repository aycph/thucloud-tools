import cliProgress from 'cli-progress';
import type { Options, Preset } from 'cli-progress';

import { CloudFile, type ProgressCallback } from './index.js';

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
    format: '{percentage}%|{bar}|{value}/{total}[{duration_formatted}<{eta_formatted}] {target}',
    formatValue(value, options, type) {
        if (type === 'value')
            return formatBytes(value).padStart(9);
        if (type === 'total')
            return formatBytes(value).padEnd(9);
        return cliProgress.Format.ValueFormat(value, options, type);
    },
    fps: 1,
    barCompleteChar: '█',
    barIncompleteChar: ' ',
    forceRedraw: true,
    autopadding: true,
};

const DEFAULT_TOTAL_BAR_FORMAT = '{name}: {percentage}%|{bar}| {value}/{total} [{duration_formatted}<{eta_formatted}, files:{filesDone}/{filesTotal}]';

export function makeProgressBarCallback(
    opt: Options = {},
    preset?: Preset,
    totalBarFormat: string = DEFAULT_TOTAL_BAR_FORMAT,
): ProgressCallback & { close: () => void } {
    type BarType = ReturnType<cliProgress.MultiBar['create']>;
    opt = { ...DEFAULT_OPTIONS, ...opt };
    const multibar = new cliProgress.MultiBar(opt, preset);
    let totalBar: BarType | undefined = undefined;
    const freeBars: BarType[] = [];
    const file2bar = new Map<CloudFile, BarType>();
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
                    multibar.update();
                    break;
                case 'skip':
                    filesDone += 1;
                    totalBar.increment(file.size, { filesDone, filesTotal });
                    multibar.update();
                    break;
                default:
                    event satisfies never;
            }
            return;
        }

        if (event === 'skip') {
            filesDone += 1;
            totalBar.increment(file.size, { filesDone, filesTotal });
            multibar.update();
            return;
        }

        let bar: BarType;
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
                file2bar.delete(file);
                freeBars.push(bar);
                filesDone += 1;
                totalBar.increment(step, { filesDone, filesTotal });
                multibar.update();
                bar.stop(); // 更新完才能 stop()
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
