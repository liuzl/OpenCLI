# Local Likes archive recovery patch

This patch applies to `twitter likes --all --output-file ... --resume-file ...`.
It does not change bookmarks or in-memory archives.

- Write an initial checkpoint before creating/appending the archive.
- Sync JSONL data before atomically publishing a synced checkpoint.
- Store the committed byte boundary and SHA-256 in checkpoints.
- Validate the committed prefix before recovery. Retain valid complete rows
  ahead of the checkpoint, replay the old cursor, and deduplicate by tweet ID.
- Trim only an unterminated suffix after all committed rows are verified.
  Invalid complete lines, duplicate IDs, missing committed rows, or modified
  committed bytes fail closed without modifying the archive.
- Keep completed file checkpoints. A retry validates the archive and returns
  `complete: true, pages: 0`; it is not a new incremental sync.
- Legacy checkpoints retain their source/account/path/count checks. Recovery
  cannot prove historical byte integrity absent a stored hash; after successful
  validation they are upgraded to the new integrity metadata.
- The receipt exposes `recovery.recoveredRows` and `recovery.truncatedBytes`.

For the tested macOS workflow, use the guarded entry point:

```sh
python3 scripts/twitter-likes-safe.py USERNAME --max-pages 1 \
  --output-file /private/path/likes.jsonl --resume-file /private/path/resume.json
```

It only runs read-only Likes with tracing disabled and defaults to one page.
POSIX flock locks cover both canonical paths and are inherited across exec into
Node. A second cooperating launcher fails immediately; process death releases
locks in the kernel. Empty `.opencli-lock` files remain deliberately: never
unlink them while a command may be running. That suffix is reserved.
Leaf symlinks/hardlinks and output/checkpoint aliases are rejected. Parent
symlinks are canonicalized. There is no dependency or privileged installation.

Scope: the raw `opencli twitter likes` command still requires a single writer;
it does not acquire the launcher's locks. Use the guarded entry point for all
writers. External edits or renaming lock paths during a run are unsupported.
No network-file-system guarantee, Windows durability qualification, or physical
power-loss qualification is included. Replaying an
X cursor cannot guarantee a historical snapshot if X changes the timeline or
expires the cursor. The archive is reread/hashed per page; very large archives
need a separate scalability evaluation.

Tests: `vitest run --project adapter clis/twitter/likes.test.js
clis/twitter/likes-recovery.test.js clis/twitter/archive.test.js`.
The recovery tests SIGKILL real child processes against synthetic browser
responses; they do not access Chrome, credentials, or X.

Launcher tests: `python3 scripts/test_twitter_likes_safe.py`.
