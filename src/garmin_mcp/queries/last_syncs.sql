-- Latest run of each fetch command, for data_status.
-- Params: none
SELECT command,
       max(ran_at_utc)                  AS ran_at_utc,
       arg_max(synthetic, ran_at_utc)   AS synthetic
FROM sync_log
GROUP BY command
