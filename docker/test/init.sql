-- Throwaway PostGIS for the compose profile `test` (design §13).
-- A non-superuser role with CREATEDB: Django's test runner creates
-- test_manager_test, the role owns it and so may create the registry schema
-- there. A superuser would hide permission bugs (CLAUDE.md).
CREATE ROLE manager_test LOGIN PASSWORD 'test' CREATEDB;
CREATE DATABASE manager_test OWNER manager_test;
