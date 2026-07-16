export interface Executor {
    submit<Args extends unknown[], T>(fn: (...args: Args) => Promise<T>, ...args: Args): Promise<T>;
}

type Task<Args extends unknown[] = any[], T = any> = {
    fn: (...args: Args) => Promise<T>,
    args: Args,
    resolve: (value: T) => void,
    reject: (reason?: any) => void,
};

export class PromisePoolExecutor implements Executor {
    private num_idle: number;
    private queue: Task[];

    constructor(max_workers: number) {
        if (!Number.isInteger(max_workers) || max_workers <= 0)
            throw new Error("max_workers must be a positive integer");
        this.num_idle = max_workers;
        this.queue = [];
    }

    private async schedule() {
        if (this.num_idle <= 0 || this.queue.length <= 0)
            return;
        --this.num_idle;
        do {
            const {fn, args, resolve, reject} = this.queue.shift()!;
            try {
                resolve(await fn(...args));
            } catch (reason) {
                reject(reason);
            }
        } while (this.queue.length);
        ++this.num_idle;
    }

    submit<Args extends unknown[], T>(fn: (...args: Args) => Promise<T>, ...args: Args): Promise<T> {
        return new Promise<T>((resolve, reject) => {
            this.queue.push({fn, args, resolve, reject});
            void this.schedule();
        });
    }
}

export const inlineExecutor = {
    submit<Args extends unknown[], T>(fn: (...args: Args) => Promise<T>, ...args: Args): Promise<T> {
        return fn(...args);
    }
} satisfies Executor;
