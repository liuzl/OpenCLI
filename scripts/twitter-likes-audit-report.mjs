#!/usr/bin/env node
// Offline, aggregate-only reconciliation. Raw IDs and content stay in input files.
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { parseArgs } from 'node:util';
import { auditLikesPage } from '../clis/twitter/likes-audit.js';
import { __test__ } from '../clis/twitter/likes.js';

const { values } = parseArgs({ options: {
    'audit-dir': { type: 'string' }, archive: { type: 'string' }, baseline: { type: 'string' },
} });
if (!values['audit-dir'] || !values.archive || !values.baseline) {
    throw new Error('Required: --audit-dir DIR --archive JSONL --baseline JSONL');
}
const readIds = file => {
    const bytes = fs.readFileSync(file);
    const rows = bytes.toString('utf8').split('\n').filter(line => line.trim()).map(JSON.parse);
    const ids = new Set(rows.map(row => String(row.id)));
    if (rows.some(row => !row.id) || rows.length !== ids.size) throw new Error('Invalid/duplicate archive IDs');
    return { ids, sha256: createHash('sha256').update(bytes).digest('hex') };
};
const archive = readIds(values.archive), baseline = readIds(values.baseline);
const pages = fs.readdirSync(values['audit-dir']).filter(file => file.endsWith('.json'))
    .map(file => JSON.parse(fs.readFileSync(path.join(values['audit-dir'], file))))
    .sort((a, b) => a.capturedAt.localeCompare(b.capturedAt));
const seen = new Set(), rawIds = new Set(), structuralIds = new Set(), missed = new Set();
const totals = {}, instructionTypes = {}, resultTypes = {}, itemTypes = {}, pageSizes = {};
const add = (target, source) => { for (const [key, value] of Object.entries(source)) target[key] = (target[key] || 0) + value; };
let expectedCursor = null, previousCursor, chainGaps = 0, requestReplays = 0, repeatedCursors = 0, errorPages = 0;
for (const page of pages) {
    const before = new Set(seen);
    const parsed = __test__.parseLikes(page.response, seen);
    const audit = auditLikesPage(page.response, before, parsed.tweets);
    if (page.request.cursor !== expectedCursor) {
        if (page.request.cursor === previousCursor) requestReplays++;
        else chainGaps++;
    }
    if (parsed.nextCursor && parsed.nextCursor === page.request.cursor) repeatedCursors++;
    if (page.response?.error) errorPages++;
    previousCursor = page.request.cursor;
    if (!page.response?.error) expectedCursor = parsed.nextCursor;
    for (const key of ['instructions', 'entries', 'cursorEntries', 'itemSlots', 'resultsWithId',
        'unavailableResults', 'missingResults', 'unrecognizedResults', 'duplicateOccurrences', 'parsedRows'])
        totals[key] = (totals[key] || 0) + audit[key];
    add(instructionTypes, audit.instructionTypes); add(resultTypes, audit.resultTypes); add(itemTypes, audit.itemTypes);
    pageSizes[audit.itemSlots] = (pageSizes[audit.itemSlots] || 0) + 1;
    audit.rawUniqueIds.forEach(id => rawIds.add(id));
    audit.structuralTweetIds.forEach(id => structuralIds.add(id));
    [...audit.unparsedNewIds, ...audit.unparsedStructuralIds].forEach(id => missed.add(id));
}
const difference = (a, b) => [...a].filter(id => !b.has(id)).length;
console.log(JSON.stringify({ pages: pages.length, totals, instructionTypes, resultTypes, itemTypes, pageSizes,
    rawUniqueIds: rawIds.size, structuralUniqueIds: structuralIds.size, replayedUniqueIds: seen.size,
    unparsedUniqueIds: missed.size, rawIdsAbsentFromArchive: difference(rawIds, archive.ids),
    structuralIdsAbsentFromArchive: difference(structuralIds, archive.ids),
    archiveIdsAbsentFromRaw: difference(archive.ids, rawIds), archiveCount: archive.ids.size,
    baselineCount: baseline.ids.size, newVsBaseline: difference(archive.ids, baseline.ids),
    missingVsBaseline: difference(baseline.ids, archive.ids), identicalIdSets: difference(archive.ids, baseline.ids) === 0 && difference(baseline.ids, archive.ids) === 0,
    archiveSha256: archive.sha256, baselineSha256: baseline.sha256,
    chainGaps, requestReplays, repeatedCursors, errorPages,
}, null, 2));
