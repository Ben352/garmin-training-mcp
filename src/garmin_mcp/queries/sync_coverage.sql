-- How far a fetch command's syncs reach, for coverage notes.
-- Params: command
SELECT max(range_end) AS covered_until, max(ran_at_utc) AS last_sync_utc
FROM sync_log
WHERE command = ?
