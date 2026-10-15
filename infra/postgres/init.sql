-- Local development only. One database, two schemas with separate migration owners:
--   shopify_app -> Prisma (frontend-ui), gateway -> Alembic (backend-engine).
CREATE SCHEMA IF NOT EXISTS shopify_app;
CREATE SCHEMA IF NOT EXISTS gateway;

-- Separate database for the backend test suite (`make test-backend`).
CREATE DATABASE gateway_test OWNER gateway;
