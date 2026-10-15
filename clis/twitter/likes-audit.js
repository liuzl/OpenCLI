import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

const bump = (counts, key) => { counts[key || 'unknown'] = (counts[key || 'unknown'] || 0) + 1; };

// Independent inventory of itemContent slots, including instruction shapes the
// current parser does not support. Do not recurse into tweet bodies: quoted and
// retweeted posts are context, not separate liked timeline memberships.
export function auditLikesPage(data, seenBefore, parsedRows) {
    const instructions = data?.data?.user?.result?.timeline_v2?.timeline?.instructions
        || data?.data?.user?.result?.timeline?.timeline?.instructions || [];
    const summary = { instructions: instructions.length, instructionTypes: {}, entries: 0,
        cursorEntries: 0, itemSlots: 0, itemTypes: {}, resultTypes: {}, resultsWithId: 0,
        unavailableResults: 0, missingResults: 0, unrecognizedResults: 0,
        duplicateOccurrences: 0, rawUniqueIds: [], newRawUniqueIds: [],
        parsedRows: parsedRows.length, unparsedNewIds: [], structuralTweetIds: [], unparsedStructuralIds: [] };
    const ids = new Set();
    function item(slot) {
        summary.itemSlots++;
        bump(summary.itemTypes, slot?.itemType || slot?.__typename);
        const result = slot?.tweet_results?.result;
        if (!result) { summary.missingResults++; return; }
        const tweet = result.tweet || result;
        bump(summary.resultTypes, tweet.__typename || result.__typename);
        if (!tweet.rest_id) {
            if (['TweetTombstone', 'TweetUnavailable', 'TweetDeleted'].includes(tweet.__typename))
                summary.unavailableResults++;
            else summary.unrecognizedResults++;
            return;
        }
        const id = String(tweet.rest_id);
        summary.resultsWithId++;
        if (seenBefore.has(id) || ids.has(id)) summary.duplicateOccurrences++;
        ids.add(id);
    }
    function visit(value) {
        if (!value || typeof value !== 'object') return;
        if (Array.isArray(value)) { value.forEach(visit); return; }
        for (const [key, child] of Object.entries(value)) {
            if (key === 'itemContent') item(child);
            else if (!['tweet_results', 'quoted_status_result', 'retweeted_status_result'].includes(key)) visit(child);
        }
    }
    for (const instruction of instructions) {
        bump(summary.instructionTypes, instruction.type || instruction.__typename);
        for (const entry of instruction.entries || []) {
            summary.entries++;
            const content = entry.content;
            if (content?.entryType === 'TimelineTimelineCursor' || content?.__typename === 'TimelineTimelineCursor'
                || entry.entryId?.startsWith('cursor-')) summary.cursorEntries++;
        }
        visit(instruction);
    }
    const structuralIds = new Set();
    function structural(value) {
        if (!value || typeof value !== 'object') return;
        if (Array.isArray(value)) { value.forEach(structural); return; }
        if (value.rest_id && (value.__typename === 'Tweet' || typeof value.legacy?.full_text === 'string')) {
            structuralIds.add(String(value.rest_id));
            return;
        }
        for (const [key, child] of Object.entries(value)) {
            if (!['quoted_status_result', 'retweeted_status_result', 'quoted_status', 'retweeted_status'].includes(key)) structural(child);
        }
    }
    structural(instructions);
    summary.structuralTweetIds = [...structuralIds];
    summary.rawUniqueIds = [...ids];
    summary.newRawUniqueIds = [...ids].filter(id => !seenBefore.has(id));
    const parsed = new Set(parsedRows.map(row => String(row.id)));
    summary.unparsedNewIds = summary.newRawUniqueIds.filter(id => !parsed.has(id));
    summary.unparsedStructuralIds = [...structuralIds].filter(id => !seenBefore.has(id) && !parsed.has(id));
    return summary;
}

export function prepareAuditDirectory(directory) {
    if (fs.existsSync(directory) && fs.lstatSync(directory).isSymbolicLink())
        throw new Error('Audit directory must not be a symlink');
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const stat = fs.statSync(directory);
    if (!stat.isDirectory() || (process.platform !== 'win32' && (stat.mode & 0o077)))
        throw new Error('Audit directory must be private (mode 700)');
}

export function writeAuditPage(directory, value) {
    const file = path.join(directory, `${Date.now()}-${randomUUID()}.json`);
    const fd = fs.openSync(file, 'wx', 0o600);
    try { fs.writeFileSync(fd, JSON.stringify(value) + '\n'); fs.fsyncSync(fd); }
    finally { fs.closeSync(fd); }
}
