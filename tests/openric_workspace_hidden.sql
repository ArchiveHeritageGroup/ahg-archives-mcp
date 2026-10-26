-- OpenRiC Workspace: records the public may not read, for tests/no_leak.py (HIDDEN_FILE).
-- Hidden = deleted, or with no published authorised name (Workspace's PublicCatalogue rule).
-- Not covered: records hidden only by the Public role's permissions.
--   psql ... -AtF $'\t' -f tests/openric_workspace_hidden.sql > hidden.tsv
SELECT r.id, COALESCE((SELECT n.value FROM names n WHERE n.subject_table = 'record_resources' AND n.subject_id = r.id
         ORDER BY n.updated_at DESC LIMIT 1), ''),
       COALESCE(r.parent_id::text, ''),
       CASE WHEN r.deleted_at IS NOT NULL THEN 'deleted' ELSE 'unpublished' END
FROM record_resources r
WHERE r.deleted_at IS NOT NULL OR r.id NOT IN (
  SELECT n.subject_id FROM names n
  JOIN concepts s ON s.id = n.state_id JOIN concepts t ON t.id = n.name_type_id
  WHERE n.subject_table = 'record_resources' AND s.iri LIKE '%value-state/published' AND t.iri LIKE '%name-type/authorised');
