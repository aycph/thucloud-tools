export interface Executor {
    submit<Args extends unknown[], T>(fn: (...args: Args) => Promise<T>, ...args: Args): Promise<T>;
}

type Task<Args extends unknown[] = any[], T = any> = {
    fn: (...args: Args) => Promise<T>;
    args: Args;
    resolve: (value: T) => void;
    reject: (reason?: any) => void;
};

export class CancelledError extends Error {}

export class PromisePoolExecutor implements Executor {
    private readonly queue: Task[] = [];
    private numWorkers: number = 0;
    private closed: boolean = false;
    private idle: Promise<void> = Promise.resolve();
    private resolveIdle!: () => void;

    constructor(private readonly maxWorkers: number) {
        if (!Number.isInteger(maxWorkers) || maxWorkers <= 0)
            throw new RangeError('Invalid maxWorkers: expected a positive integer');
    }

    private async schedule() {
        if (this.numWorkers >= this.maxWorkers)
            return;
        if (this.numWorkers++ === 0)
            this.idle = new Promise(resolve => this.resolveIdle = resolve);
        do {
            const { fn, args, resolve, reject } = this.queue.shift()!;
            try {
                resolve(await fn(...args));
            } catch (reason) {
                reject(reason);
            }
        } while (this.queue.length);
        if (--this.numWorkers === 0)
            this.resolveIdle();
    }

    submit<Args extends unknown[], T>(fn: (...args: Args) => Promise<T>, ...args: Args): Promise<T> {
        if (this.closed)
            throw new Error('Cannot submit tasks after shutdown');
        return new Promise<T>((resolve, reject) => {
            this.queue.push({ fn, args, resolve, reject });
            void this.schedule();
        });
    }

    shutdown(cancelPending: boolean = false): Promise<void> {
        this.closed = true;
        if (cancelPending) {
            const reason = new CancelledError('Executor shut down');
            for (const { reject } of this.queue)
                reject(reason);
            this.queue.length = 0;
        }
        return this.idle;
    }
}

export const inlineExecutor = {
    submit<Args extends unknown[], T>(fn: (...args: Args) => Promise<T>, ...args: Args): Promise<T> {
        return fn(...args);
    }
} satisfies Executor;
