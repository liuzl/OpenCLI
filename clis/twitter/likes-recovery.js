import fs from 'node:fs';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { CommandExecutionError } from '@jackwener/opencli/errors';
import { ensureParentDir } from './archive.js';

const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
const fail = (message) => { throw new CommandExecutionError(`Twitter likes recovery: ${message}`); };
function syncDirectory(file) {
    const fd = fs.openSync(path.dirname(file), 'r');
    try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}

// A single writer owns an archive/checkpoint pair. Sync data before publishing
// its checkpoint; an interrupted append is replayed from the previous cursor.
export function writeDurableCheckpoint(file, payload) {
    ensureParentDir(file);
    const tmp = `${file}.tmp-${randomUUID()}`;
    let fd;
    try {
        fd = fs.openSync(tmp, 'wx', 0o600);
        fs.writeFileSync(fd, JSON.stringify(payload, null, 2) + '\n');
        fs.fsyncSync(fd);
        fs.closeSync(fd);
        fd = undefined;
        fs.renameSync(tmp, file);
        syncDirectory(file);
    } finally {
        if (fd !== undefined) fs.closeSync(fd);
        fs.rmSync(tmp, { force: true });
    }
}

export function appendDurableRows(file, rows) {
    ensureParentDir(file);
    const fd = fs.openSync(file, 'a', 0o600);
    try {
        const text = rows.map(row => JSON.stringify(row)
            .replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029')).join('\n');
        if (text) fs.writeFileSync(fd, text + '\n');
        fs.fsyncSync(fd);
    } finally { fs.closeSync(fd); }
    syncDirectory(file);
}

function parseRows(bytes) {
    const seen = new Set();
    const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    for (const [i, line] of text.split('\n').entries()) {
        if (!line.trim()) continue;
        let row;
        try { row = JSON.parse(line); } catch { fail(`invalid complete record at line ${i + 1}`); }
        if (!row?.id || seen.has(String(row.id))) fail(`missing or duplicate id at line ${i + 1}`);
        seen.add(String(row.id));
    }
    return seen;
}

export function recoverLikesArchive(file, checkpoint) {
    let bytes = fs.existsSync(file) ? fs.readFileSync(file) : Buffer.alloc(0);
    const expected = checkpoint?.count ?? 0;
    const meta = checkpoint?.archive;
    if (meta !== undefined) {
        if (meta?.version !== 1 || !Number.isSafeInteger(meta.bytes) || meta.bytes < 0
            || !/^[a-f0-9]{64}$/.test(meta.sha256 ?? '')) fail('invalid checkpoint integrity metadata');
        if (bytes.length < meta.bytes) fail('archive is shorter than committed bytes');
        const prefix = bytes.subarray(0, meta.bytes);
        if ((meta.bytes > 0 && prefix.at(-1) !== 10) || digest(prefix) !== meta.sha256)
            fail('committed prefix checksum mismatch');
        if (parseRows(prefix).size !== expected) fail('committed prefix count mismatch');
    }
    const boundary = bytes.lastIndexOf(10) + 1;
    const complete = bytes.subarray(0, boundary);
    const seen = parseRows(complete); // Validate everything before changing the file.
    if (seen.size < expected) fail(`archive has ${seen.size} record(s), expected resume count ${expected}`);
    if (checkpoint?.complete && (seen.size !== expected || boundary !== bytes.length
        || (meta && meta.bytes !== bytes.length)))
        fail('completed archive has unexpected trailing data');
    // A missing newline can be a torn JSON object or UTF-8 sequence. Only discard
    // it after proving that all committed records are present and valid.
    const truncatedBytes = bytes.length - boundary;
    if (truncatedBytes) {
        const fd = fs.openSync(file, 'r+');
        try { fs.ftruncateSync(fd, boundary); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
        bytes = complete;
    }
    return {
        seen, count: seen.size,
        recoveredRows: seen.size - expected, truncatedBytes,
        archive: { version: 1, bytes: bytes.length, sha256: digest(bytes) },
    };
}

// Reject leaf aliases rather than treating an atomic checkpoint rename as a
// write through a symlink. Canonicalize existing parents for not-yet-made files.
export function validateArchivePaths(output, resume) {
    const canonical = (file) => {
        let info;
        try { info = fs.lstatSync(file); } catch (error) {
            if (error.code !== 'ENOENT') throw error;
        }
        if (info && (!info.isFile() || info.nlink !== 1))
            fail('archive and checkpoint must be regular files without symlink/hardlink aliases');
        let parent = path.dirname(file);
        const parts = [path.basename(file)];
        while (!fs.existsSync(parent)) {
            parts.unshift(path.basename(parent));
            const next = path.dirname(parent);
            if (next === parent) fail('cannot resolve archive parent');
            parent = next;
        }
        return path.join(fs.realpathSync(parent), ...parts);
    };
    // Conservatively reject case-only aliases, including on case-sensitive FS.
    if (canonical(output).toLowerCase() === canonical(resume).toLowerCase())
        fail('archive and checkpoint must be different files');
}
