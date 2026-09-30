-- Integration-test schema + seed for CI (`python -m tests.ci.bootstrap`).
--
-- This is TEST INFRASTRUCTURE ONLY. It mirrors a minimal, production-like shape
-- of the external read-only database so `pytest -m integration` runs against
-- real MySQL in CI. It is intentionally NOT a second production schema: it
-- holds only the tables/columns the application queries (verified against
-- REQUIRED_SCHEMA and the repository SQL), and it is never used to build or
-- migrate the production schema.
--
-- Dates are relative to NOW() so the fixture never expires as time passes.

-- ------------------------------------------------------------------------ --
-- Tables (columns deliberately match app usage; NO bookings.supplier_id --  --
-- the dead get_agent_supplier_l2b_snapshot test relies on that absence)     --
-- ------------------------------------------------------------------------ --

CREATE TABLE users (
    id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    is_active TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE roles (
    id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    name VARCHAR(64) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE model_has_roles (
    id BIGINT UNSIGNED NOT NULL PRIMARY KEY AUTO_INCREMENT,
    role_id BIGINT UNSIGNED NOT NULL,
    model_id BIGINT UNSIGNED NOT NULL,
    model_type VARCHAR(255) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE agents (
    id BIGINT UNSIGNED NOT NULL PRIMARY KEY AUTO_INCREMENT,
    establishment_name VARCHAR(255) NOT NULL,
    user_id BIGINT UNSIGNED NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    approval_status VARCHAR(32) NOT NULL DEFAULT 'approved',
    created_at DATETIME NOT NULL,
    email VARCHAR(255) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE suppliers (
    id BIGINT UNSIGNED NOT NULL PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(50) NOT NULL,
    name VARCHAR(150) NOT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    health_status VARCHAR(32) NULL,
    search_limit INT NULL,
    minimum_booking INT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE bookings (
    id BIGINT UNSIGNED NOT NULL PRIMARY KEY AUTO_INCREMENT,
    agent_id BIGINT UNSIGNED NOT NULL,
    provider VARCHAR(50) NULL,
    status VARCHAR(32) NOT NULL,
    total_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE booking_processes (
    id BIGINT UNSIGNED NOT NULL PRIMARY KEY AUTO_INCREMENT,
    user_id BIGINT UNSIGNED NOT NULL,
    current_step VARCHAR(50) NOT NULL,
    state VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE search_sessions (
    id BIGINT UNSIGNED NOT NULL PRIMARY KEY AUTO_INCREMENT,
    user_id BIGINT UNSIGNED NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE search_supplier_runs (
    id BIGINT UNSIGNED NOT NULL PRIMARY KEY AUTO_INCREMENT,
    search_session_id BIGINT UNSIGNED NOT NULL,
    supplier_id BIGINT UNSIGNED NOT NULL,
    supplier_code VARCHAR(50) NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE search_session_accesses (
    id BIGINT UNSIGNED NOT NULL PRIMARY KEY AUTO_INCREMENT,
    search_session_id BIGINT UNSIGNED NOT NULL,
    agent_id BIGINT UNSIGNED NOT NULL,
    first_access_type VARCHAR(16) NOT NULL,
    first_accessed_at DATETIME NOT NULL,
    access_count INT NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE credit_transactions (
    id BIGINT UNSIGNED NOT NULL PRIMARY KEY AUTO_INCREMENT,
    agent_id BIGINT UNSIGNED NOT NULL,
    principal_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
    total_payable DECIMAL(12,2) NOT NULL DEFAULT 0,
    service_fee DECIMAL(12,2) NOT NULL DEFAULT 0,
    paid_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
    credit_days INT NOT NULL DEFAULT 0,
    due_date DATE NULL,
    payment_date DATE NULL,
    status VARCHAR(32) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------------------------ --
-- Seed data (deterministic; every table used by the integration suite is    --
-- populated so nothing silently skips in CI)                                --
-- ------------------------------------------------------------------------ --

INSERT INTO users (id, is_active) VALUES
    (2001, 1);

INSERT INTO roles (id, name) VALUES
    (1, 'admin'),
    (2, 'agent'),
    (3, 'internal-service');

INSERT INTO model_has_roles (role_id, model_id, model_type) VALUES
    (2, 2001, 'App\\Models\\User'),
    (3, 2001, 'App\\Models\\User');

INSERT INTO agents
    (id, establishment_name, user_id, is_active, approval_status, created_at, email)
VALUES
    (1001, 'Trust Test Travel', 2001, 1, 'approved',
     NOW() - INTERVAL 900 DAY, 'travel@example.com');

INSERT INTO suppliers (id, code, name, is_active, health_status, search_limit, minimum_booking)
VALUES
    (1, 'AEGEAN', 'Aegean Airlines', 1, 'healthy',   1000, 50),
    (2, 'SABRE',  'Sabre GDS',      1, 'healthy',   2000, 100),
    (3, 'GF',     'Gulf Air',       1, 'healthy',    500, 10),
    (4, 'AMADEUS','Amadeus GDS',    0, 'degraded',  NULL, NULL);

INSERT INTO bookings (id, agent_id, provider, status, total_amount, created_at) VALUES
    (1, 1001, 'GF',      'confirmed', 1500.00, NOW() - INTERVAL 2 DAY),
    (2, 1001, 'SABRE',   'ticketed',   800.00, NOW() - INTERVAL 20 DAY),
    (3, 1001, 'AMADEUS', 'cancelled', 1200.00, NOW() - INTERVAL 30 DAY),
    (4, 1001, 'GF',      'confirmed',  900.00, NOW() - INTERVAL 200 DAY),
    (5, 1001, 'SABRE',   'pending',    400.00, NOW() - INTERVAL 5 DAY);

INSERT INTO booking_processes (id, user_id, current_step, state, created_at) VALUES
    (1, 2001, 'BookStep',    'FAILED',  NOW() - INTERVAL 300 DAY),
    (2, 2001, 'BookStep',    'FAILED',  NOW() - INTERVAL 15 DAY),
    (3, 2001, 'PaymentStep', 'FAILED',  NOW() - INTERVAL 10 DAY),
    (4, 2001, 'BookStep',    'SUCCESS', NOW() - INTERVAL 2 DAY);

INSERT INTO search_sessions (id, user_id, status, created_at) VALUES
    (10, 2001, 'completed', NOW() - INTERVAL 5 DAY),
    (11, 2001, 'completed', NOW() - INTERVAL 30 DAY),
    (12, 2001, 'completed', NOW() - INTERVAL 200 DAY);

INSERT INTO search_supplier_runs
    (id, search_session_id, supplier_id, supplier_code, status, created_at)
VALUES
    (101, 10, 1, 'AEGEAN', 'success', NOW() - INTERVAL 5 DAY),
    (102, 10, 2, 'SABRE',  'success', NOW() - INTERVAL 5 DAY),
    (103, 11, 1, 'AEGEAN', 'success', NOW() - INTERVAL 30 DAY),
    (104, 12, 3, 'GF',     'success', NOW() - INTERVAL 200 DAY);

INSERT INTO search_session_accesses
    (id, search_session_id, agent_id, first_access_type, first_accessed_at, access_count)
VALUES
    (201, 10, 1001, 'created', NOW() - INTERVAL 5 DAY,   12),
    (202, 11, 1001, 'reused',  NOW() - INTERVAL 30 DAY,   5),
    (203, 12, 1001, 'created', NOW() - INTERVAL 200 DAY,  8);

INSERT INTO credit_transactions
    (id, agent_id, principal_amount, total_payable, service_fee, paid_amount,
     credit_days, due_date, payment_date, status)
VALUES
    (301, 1001, 100.00, 105.00, 5.00,  105.00, 30, NOW() - INTERVAL 40 DAY, NOW() - INTERVAL 38 DAY, 'paid'),
    (302, 1001, 250.00, 260.00, 10.00, 260.00, 30, NOW() - INTERVAL 10 DAY, NOW() - INTERVAL 9 DAY,  'paid'),
    (303, 1001, 500.00, 520.00, 20.00,   0.00, 30, NOW() - INTERVAL 20 DAY, NULL,                    'unpaid'),
    (304, 1001, 300.00, 310.00, 10.00,   0.00, 30, NOW() + INTERVAL 5 DAY,  NULL,                    'unpaid');