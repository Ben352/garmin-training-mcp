-- Latest VO2 max on or before a date (Garmin only stores the dates it changed).
-- Params: date
SELECT vo2max_precise, date
FROM vo2max
WHERE date <= ?
ORDER BY date DESC
LIMIT 1
