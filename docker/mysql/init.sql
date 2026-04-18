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
