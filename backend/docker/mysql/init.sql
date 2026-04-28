CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    login VARCHAR(32) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS balances (
    user_id INT NOT NULL,
    currency_code CHAR(3) NOT NULL,
    amount DECIMAL(12, 2) NOT NULL DEFAULT 0.00,
    PRIMARY KEY (user_id, currency_code),
    CONSTRAINT fk_balances_user
        FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS currency_rates_history (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    currency_code CHAR(3) NOT NULL,
    uah_per_1 DECIMAL(18, 6) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_rates_history_code_time (currency_code, created_at)
);
