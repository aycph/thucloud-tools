export interface Executor {
    submit<Args extends unknown[], T>(fn: (...args: Args) => Promise<T>, ...args: Args): Promise<T>;
}

type Task<Args extends unknown[] = any[], T = any> = {
    fn: (...args: Args) => Promise<T>,
    args: Args,
    resolve: (value: T) => void,
    reject: (reason?: any) => void,
};

export class CancelledError extends Error {}

export class PromisePoolExecutor implements Executor {
    private readonly queue = new Array<Task>();
    private readonly workers = new Map<number, Promise<void>>();
    private id = 0;
    private closed = false;

    constructor(private readonly max_workers: number) {
        if (!Number.isInteger(max_workers) || max_workers <= 0)
            throw new RangeError('max_workers must be a positive integer');
    }

    private async schedule(id: number) {
        do {
            const {fn, args, resolve, reject} = this.queue.shift()!;
            try {
                resolve(await fn(...args));
            } catch (reason) {
                reject(reason);
            }
        } while (this.queue.length);
        this.workers.delete(id);
    }

    submit<Args extends unknown[], T>(fn: (...args: Args) => Promise<T>, ...args: Args): Promise<T> {
        if (this.closed)
            throw new Error('cannot submit tasks after shutdown');
        return new Promise<T>((resolve, reject) => {
            this.queue.push({fn, args, resolve, reject});
            if (this.workers.size >= this.max_workers)
                return;
            const id = this.id++;
            const worker = this.schedule(id);
            this.workers.set(id, worker);
        });
    }

    shutdown(cancelPending: boolean = false): Promise<void> {
        this.closed = true;
        if (cancelPending) {
            for (const { reject } of this.queue)
                reject(new CancelledError('executor shutdown'));
            this.queue.length = 0;
        }
        return Promise.all(this.workers.values()).then(() => {});
    }
}

export const inlineExecutor = {
    submit<Args extends unknown[], T>(fn: (...args: Args) => Promise<T>, ...args: Args): Promise<T> {
        return fn(...args);
    }
} satisfies Executor;
