import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { describe, expect, it } from 'vitest';
import { recoverLikesArchive, writeDurableCheckpoint, validateArchivePaths } from './likes-recovery.js';
import { getRegistry } from '@jackwener/opencli/registry';
import { __test__ as likesTest } from './likes.js';

function payload(ids, cursor) {
    const entries = ids.map(id => ({ entryId: `tweet-${id}`, content: { itemContent: { tweet_results: { result: {
        rest_id: id, legacy: { full_text: `中文 ${id}` },
    } } } } }));
    if (cursor) entries.push({ entryId: 'cursor-bottom-1', content: { cursorType: 'Bottom', value: cursor } });
    return { data: { user: { result: { timeline_v2: { timeline: { instructions: [{ entries }] } } } } } };
}
function pageFor() {
    return {
        getCookies: async () => [{ name: 'ct0', value: 'fixture' }],
        evaluate: async script => {
            if (script.includes('operationName')) return null;
            if (script.includes('/UserByScreenName')) return '42';
            if (script.includes('/Likes')) {
                return decodeURIComponent(script).includes('"cursor":"NEXT"')
                    ? payload(['2', '3'], null) : payload(['1', '2'], 'NEXT');
            }
            throw new Error('Unexpected browser call');
        },
    };
}
function fixture() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'likes-recovery-'));
    return { dir, output: path.join(dir, 'out.jsonl'), resume: path.join(dir, 'resume.json') };
}

describe('likes crash recovery', () => {
    for (const phase of ['before-append', 'partial-append', 'after-append', 'after-checkpoint', 'after-completion', 'after-recovery-truncate']) {
        it(`survives SIGKILL ${phase} with no missing or duplicated IDs`, async () => {
            const f = fixture();
            const args = { username: 'viewer', all: true, 'max-pages': 2, 'output-file': f.output, 'resume-file': f.resume };
            try {
                const script = `
import fs from 'node:fs';
import { getRegistry } from '@jackwener/opencli/registry';
import ${JSON.stringify(new URL('./likes.js', import.meta.url).href)};
const payload = ${payload.toString()};
const pageFor = ${pageFor.toString()};
const phase = ${JSON.stringify(phase)};
const output = ${JSON.stringify(f.output)}, resume = ${JSON.stringify(f.resume)};
const kill = () => process.kill(process.pid, 'SIGKILL');
const open = fs.openSync, write = fs.writeFileSync, rename = fs.renameSync, truncate = fs.ftruncateSync;
fs.ftruncateSync = function(fd, size) {
    truncate.call(this, fd, size);
    if (phase === 'after-recovery-truncate') kill();
};
const outputFds = new Set();
fs.openSync = function(file, flags, ...rest) {
    const fd = open.call(this, file, flags, ...rest);
    if (file === output && flags === 'a') {
        if (phase === 'before-append') kill();
        outputFds.add(fd);
    }
    return fd;
};
fs.writeFileSync = function(file, data, ...rest) {
    if (outputFds.has(file)) {
        outputFds.delete(file);
        if (phase === 'partial-append') {
            const bytes = Buffer.from(data);
            write.call(this, file, bytes.subarray(0, bytes.indexOf(10) + 12));
            kill();
        }
        write.call(this, file, data, ...rest);
        if (phase === 'after-append') kill();
        return;
    }
    return write.call(this, file, data, ...rest);
};
fs.renameSync = function(src, dst) {
    rename.call(this, src, dst);
    if (dst === resume) {
        const state = JSON.parse(fs.readFileSync(dst));
        if ((phase === 'after-checkpoint' && state.count === 2)
            || (phase === 'after-completion' && state.complete)) kill();
    }
};
await getRegistry().get('twitter/likes').func(pageFor(), ${JSON.stringify(args)});
`;
                if (phase === 'after-recovery-truncate') {
                    const first = spawnSync(process.execPath, ['--input-type=module', '-e',
                        script.replace('const phase = "after-recovery-truncate"', 'const phase = "partial-append"')],
                        { encoding: 'utf8', timeout: 15000 });
                    expect(first.stderr).toBe('');
                    expect(first.signal).toBe('SIGKILL');
                }
                const killed = spawnSync(process.execPath, ['--input-type=module', '-e', script], { encoding: 'utf8', timeout: 15000 });
                expect(killed.stderr).toBe('');
                expect(killed.signal).toBe('SIGKILL');
                const result = await getRegistry().get('twitter/likes').func(pageFor(), args);
                expect(result).toMatchObject({ count: 3, complete: true });
                const rows = fs.readFileSync(f.output, 'utf8').trim().split('\n').map(JSON.parse);
                expect(rows.map(row => row.id)).toEqual(['1', '2', '3']);
                expect(rows.map(row => row.text)).toEqual(['中文 1', '中文 2', '中文 3']);
                expect(JSON.parse(fs.readFileSync(f.resume)).complete).toBe(true);
            } finally { fs.rmSync(f.dir, { recursive: true, force: true }); }
        });
    }

    it('rejects an aliased output/checkpoint argument before touching the browser', async () => {
        await expect(getRegistry().get('twitter/likes').func({}, {
            username: 'viewer', all: true, 'output-file': '/tmp/same', 'resume-file': '/tmp/same',
        })).rejects.toThrow(/must be different files/);
    });

    it('rejects symlink, hardlink, parent and case-only aliases before archive writes', () => {
        const f = fixture();
        try {
            fs.writeFileSync(f.output, 'unchanged');
            fs.symlinkSync(f.output, f.resume);
            expect(() => validateArchivePaths(f.output, f.resume)).toThrow(/aliases/);
            fs.unlinkSync(f.resume);
            fs.linkSync(f.output, f.resume);
            expect(() => validateArchivePaths(f.output, f.resume)).toThrow(/aliases/);
            fs.unlinkSync(f.resume);
            fs.symlinkSync(f.dir, f.dir + '-alias');
            expect(() => validateArchivePaths(path.join(f.dir, 'new'), path.join(f.dir + '-alias', 'new')))
                .toThrow(/different files/);
            expect(() => validateArchivePaths(path.join(f.dir, 'new'), path.join(f.dir, 'NEW')))
                .toThrow(/different files/);
            expect(fs.readFileSync(f.output, 'utf8')).toBe('unchanged');
        } finally {
            fs.rmSync(f.dir + '-alias', { force: true });
            fs.rmSync(f.dir, { recursive: true, force: true });
        }
    });

    it('trims a torn UTF-8 suffix only after an intact committed prefix', () => {
        const f = fixture();
        try {
            fs.writeFileSync(f.output, '{"id":"1"}\n');
            const checkpoint = { count: 1, archive: recoverLikesArchive(f.output, { count: 1 }).archive };
            const tail = Buffer.from('{"id":"2","text":"中');
            fs.appendFileSync(f.output, tail.subarray(0, tail.length - 1));
            const result = recoverLikesArchive(f.output, checkpoint);
            expect(result).toMatchObject({ count: 1, recoveredRows: 0, truncatedBytes: tail.length - 1 });
            expect(fs.readFileSync(f.output, 'utf8')).toBe('{"id":"1"}\n');
        } finally { fs.rmSync(f.dir, { recursive: true, force: true }); }
    });

    it('keeps the committed cursor on 429 and resumes without missing or duplicated IDs', async () => {
        const f = fixture();
        const args = { username: 'viewer', all: true, 'max-pages': 2, 'output-file': f.output, 'resume-file': f.resume };
        try {
            const page = pageFor();
            const evaluate = page.evaluate;
            page.evaluate = async script => {
                if (script.includes('/Likes') && decodeURIComponent(script).includes('"cursor":"NEXT"'))
                    return { error: 429, retryAfter: '3600' };
                return evaluate(script);
            };
            const blocked = await getRegistry().get('twitter/likes').func(page, args);
            expect(blocked).toMatchObject({ count: 2, complete: false, stopReason: 'rate_limited', retryAfterSeconds: 3600 });
            expect(JSON.parse(fs.readFileSync(f.resume)).cursor).toBe('NEXT');
            const recovered = await getRegistry().get('twitter/likes').func(pageFor(), args);
            expect(recovered).toMatchObject({ count: 3, complete: true, stopReason: 'api_exhausted' });
            expect(fs.readFileSync(f.output, 'utf8').trim().split('\n').map(JSON.parse).map(row => row.id))
                .toEqual(['1', '2', '3']);
        } finally { fs.rmSync(f.dir, { recursive: true, force: true }); }
    });

    it('returns a resumable rate-limit receipt even before the first row', async () => {
        const f = fixture();
        try {
            const page = pageFor();
            const evaluate = page.evaluate;
            page.evaluate = async script => script.includes('/Likes') ? { error: 429 } : evaluate(script);
            const result = await getRegistry().get('twitter/likes').func(page, {
                username: 'viewer', all: true, 'output-file': f.output, 'resume-file': f.resume,
            });
            expect(result).toMatchObject({ count: 0, complete: false, stopReason: 'rate_limited', retryAfterSeconds: 900 });
            expect(JSON.parse(fs.readFileSync(f.resume))).toMatchObject({ cursor: null, count: 0 });
        } finally { fs.rmSync(f.dir, { recursive: true, force: true }); }
    });

    it('honors both retry headers, HTTP dates, and a conservative fallback', () => {
        const now = Date.parse('2026-01-01T00:00:00Z');
        expect(likesTest.retryAfterSeconds({ retryAfter: '120', rateLimitReset: String(now / 1000 + 300) }, now)).toBe(300);
        expect(likesTest.retryAfterSeconds({ retryAfter: 'Thu, 01 Jan 2026 00:10:00 GMT' }, now)).toBe(600);
        expect(likesTest.retryAfterSeconds({ retryAfter: 'invalid', rateLimitReset: '' }, now)).toBe(900);
    });

    it('fails closed on committed corruption, missing rows, complete-line corruption and duplicates', () => {
        const f = fixture();
        const original = Buffer.from('{"id":"1","text":"中文"}\n');
        try {
            fs.writeFileSync(f.output, original);
            const state = recoverLikesArchive(f.output, { count: 1 });
            const checkpoint = { count: 1, archive: state.archive };
            for (const invalid of [
                Buffer.from(original.toString().replace('中文', '改变')),
                Buffer.alloc(0),
                Buffer.concat([original, Buffer.from('{broken}\n')]),
                Buffer.concat([original, original]),
            ]) {
                fs.writeFileSync(f.output, invalid);
                expect(() => recoverLikesArchive(f.output, checkpoint)).toThrow();
                expect(fs.readFileSync(f.output)).toEqual(invalid);
            }
        } finally { fs.rmSync(f.dir, { recursive: true, force: true }); }
    });

    it('does not discard a torn committed record in a legacy archive', () => {
        const f = fixture();
        try {
            fs.writeFileSync(f.output, '{"id":"1"}\n{"id":');
            const before = fs.readFileSync(f.output);
            expect(() => recoverLikesArchive(f.output, { count: 2 })).toThrow(/expected resume count/);
            expect(fs.readFileSync(f.output)).toEqual(before);
        } finally { fs.rmSync(f.dir, { recursive: true, force: true }); }
    });

    it('rejects extra bytes after completion and invalid integrity metadata', () => {
        const f = fixture();
        try {
            fs.writeFileSync(f.output, '{"id":"1"}\n');
            const state = recoverLikesArchive(f.output, { count: 1 });
            writeDurableCheckpoint(f.resume, { count: 1, complete: true, archive: state.archive });
            fs.appendFileSync(f.output, '{"id":"2"}\n');
            expect(() => recoverLikesArchive(f.output, JSON.parse(fs.readFileSync(f.resume)))).toThrow(/trailing/);
            expect(() => recoverLikesArchive(f.output, { count: 1, archive: { version: 9 } })).toThrow(/metadata/);
        } finally { fs.rmSync(f.dir, { recursive: true, force: true }); }
    });
});
