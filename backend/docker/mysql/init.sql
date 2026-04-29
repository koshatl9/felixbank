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

CREATE TABLE IF NOT EXISTS virtual_cards (
    user_id INT NOT NULL PRIMARY KEY,
    card_number CHAR(19) NULL,
    expiry_month TINYINT UNSIGNED NULL,
    expiry_year SMALLINT UNSIGNED NULL,
    cvv CHAR(3) NULL,
    is_blocked TINYINT(1) NOT NULL DEFAULT 0,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_virtual_cards_user
        FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE CASCADE
);
