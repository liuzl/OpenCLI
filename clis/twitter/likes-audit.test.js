import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { describe, expect, it } from 'vitest';
import { auditLikesPage, prepareAuditDirectory, writeAuditPage } from './likes-audit.js';
import { __test__ } from './likes.js';

const item = result => ({ itemContent: { itemType: 'TimelineTweet', tweet_results: { result } } });
const tweet = id => ({ __typename: 'Tweet', rest_id: id, legacy: { full_text: 'fixture' } });
function page(instructions) { return { data: { user: { result: { timeline_v2: { timeline: { instructions } } } } } }; }

describe('independent Likes page accounting', () => {
    it('balances valid, duplicate, unavailable, missing and unknown results without counting quotes', () => {
        const quoted = tweet('1'); quoted.quoted_status_result = { result: tweet('quoted-only') };
        const content = [item(quoted), item(tweet('1')), item(tweet('old')),
            item({ __typename: 'TweetTombstone' }), item(null), item({ __typename: 'NewShape' })];
        const data = page([{ type: 'TimelineAddEntries', entries: [
            ...content.map((c, i) => ({ entryId: `tweet-${i}`, content: c })),
            { entryId: 'cursor-bottom-1', content: { entryType: 'TimelineTimelineCursor', cursorType: 'Bottom', value: 'NEXT' } },
        ] }]);
        const seen = new Set(['old']); const parsed = __test__.parseLikes(data, new Set(seen));
        const audit = auditLikesPage(data, seen, parsed.tweets);
        expect(audit).toMatchObject({ entries: 7, cursorEntries: 1, itemSlots: 6, resultsWithId: 3,
            unavailableResults: 1, missingResults: 1, unrecognizedResults: 1, duplicateOccurrences: 2,
            newRawUniqueIds: ['1'], parsedRows: 1, unparsedNewIds: [] });
        expect(audit.rawUniqueIds).not.toContain('quoted-only');
    });
    it('detects unsupported instruction shapes instead of trusting the main parser', () => {
        const data = page([{ type: 'TimelineAddToModule', moduleItems: [{ item: item(tweet('missed')) }] }]);
        const parsed = __test__.parseLikes(data, new Set());
        expect(parsed.tweets).toHaveLength(0);
        expect(auditLikesPage(data, new Set(), parsed.tweets).unparsedNewIds).toEqual(['missed']);
    });
    it('finds tweet objects in unknown containers independently of itemContent paths', () => {
        const data = page([{ type: 'FutureShape', content: { changedContainer: { result: tweet('hidden-shape') } } }]);
        const parsed = __test__.parseLikes(data, new Set());
        expect(auditLikesPage(data, new Set(), parsed.tweets)).toMatchObject({
            itemSlots: 0, unparsedNewIds: [], unparsedStructuralIds: ['hidden-shape'],
        });
    });
    it('reconciles raw pages and both archives with aggregate-only CLI output', () => {
        const root = fs.mkdtempSync(path.join(os.tmpdir(), 'likes-audit-report-'));
        try {
            const dir = path.join(root, 'pages'); fs.mkdirSync(dir);
            const data = page([{ type: 'TimelineAddEntries', entries: [
                ...['1', '2'].map(id => ({ entryId: `tweet-${id}`, content: item(tweet(id)) })),
                { entryId: 'cursor-bottom-1', content: { cursorType: 'Bottom', value: 'NEXT' } },
            ] }]);
            fs.writeFileSync(path.join(dir, 'page.json'), JSON.stringify({
                capturedAt: '2026-01-01T00:00:00Z', request: { cursor: null }, response: data,
            }));
            const archive = path.join(root, 'archive.jsonl'), baseline = path.join(root, 'baseline.jsonl');
            fs.writeFileSync(archive, '{"id":"1"}\n{"id":"2"}\n');
            fs.writeFileSync(baseline, '{"id":"1"}\n{"id":"3"}\n');
            const result = spawnSync(process.execPath, ['scripts/twitter-likes-audit-report.mjs',
                '--audit-dir', dir, '--archive', archive, '--baseline', baseline], { encoding: 'utf8' });
            expect(result.status).toBe(0);
            expect(result.stderr).toBe('');
            expect(JSON.parse(result.stdout)).toMatchObject({ rawUniqueIds: 2, archiveCount: 2,
                unparsedUniqueIds: 0, rawIdsAbsentFromArchive: 0, chainGaps: 0,
                newVsBaseline: 1, missingVsBaseline: 1, identicalIdSets: false });
            expect(result.stdout).not.toContain('fixture');
        } finally { fs.rmSync(root, { recursive: true, force: true }); }
    });
    it('keeps raw response capture private and explicit', () => {
        const root = fs.mkdtempSync(path.join(os.tmpdir(), 'likes-audit-'));
        try {
            const dir = path.join(root, 'private'); prepareAuditDirectory(dir);
            writeAuditPage(dir, { response: { fixture: true }, analysis: { entries: 0 } });
            const file = path.join(dir, fs.readdirSync(dir)[0]);
            expect(JSON.parse(fs.readFileSync(file)).response.fixture).toBe(true);
            expect(fs.statSync(file).mode & 0o077).toBe(0);
            fs.chmodSync(dir, 0o755);
            expect(() => prepareAuditDirectory(dir)).toThrow(/private/);
        } finally { fs.rmSync(root, { recursive: true, force: true }); }
    });
});
