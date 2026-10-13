-- =============================================================================
-- 0006_seed_cabins.sql
-- The ten meeting cabins. Idempotent: re-running never overwrites details an
-- administrator has edited. Names, locations, capacity and equipment are
-- placeholders — edit them from Admin › Cabins.
-- =============================================================================

insert into cabins.cabins (code, name, location, capacity, equipment, sort_order) values
  ('C01', 'Cabin 01', 'Floor 1 · East wing',  4, '{"TV display"}',                                   1),
  ('C02', 'Cabin 02', 'Floor 1 · East wing',  4, '{"TV display"}',                                   2),
  ('C03', 'Cabin 03', 'Floor 1 · West wing',  6, '{"TV display","Whiteboard"}',                      3),
  ('C04', 'Cabin 04', 'Floor 1 · West wing',  6, '{"Video conferencing","Whiteboard"}',              4),
  ('C05', 'Cabin 05', 'Floor 2 · East wing',  8, '{"Projector","Whiteboard"}',                       5),
  ('C06', 'Cabin 06', 'Floor 2 · East wing',  8, '{"Video conferencing","TV display"}',              6),
  ('C07', 'Cabin 07', 'Floor 2 · West wing',  4, '{"TV display"}',                                   7),
  ('C08', 'Cabin 08', 'Floor 2 · West wing',  2, '{}',                                               8),
  ('C09', 'Cabin 09', 'Floor 3 · Board room', 12, '{"Video conferencing","Projector","Whiteboard"}', 9),
  ('C10', 'Cabin 10', 'Floor 3 · Focus room', 2, '{}',                                               10)
on conflict (code) do nothing;
