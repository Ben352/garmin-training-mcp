-- Every column of the main tables, in table order, for the schema://tables resource.
-- Params: none
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'main'
ORDER BY table_name, ordinal_position
