from __future__ import annotations

import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from secrets import randbelow

from flask import current_app
from werkzeug.security import check_password_hash, generate_password_hash

from .config import (
    LOGIN_2FA_CODE_RE,
    LOGIN_2FA_CODE_TTL_SECONDS,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USE_SSL,
    SMTP_USE_TLS,
    SMTP_USER,
)
from .db import clear_login_2fa_codes, create_login_2fa_code, get_active_login_2fa_code, mark_login_2fa_code_used


class TwoFactorError(RuntimeError):
    pass


class TwoFactorSetupError(TwoFactorError):
    pass


class TwoFactorDeliveryError(TwoFactorError):
    pass


def _utc_now_naive() -> datetime:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def _smtp_available() -> bool:
    return bool(SMTP_HOST.strip() and SMTP_FROM.strip())


def _generate_email_code() -> str:
    return f"{randbelow(1000000):06d}"


def mask_email(email: str) -> str:
    normalized = str(email or "").strip()
    if "@" not in normalized:
        return normalized
    local_part, domain = normalized.split("@", 1)
    if len(local_part) <= 2:
        masked_local = local_part[:1] + "*"
    else:
        masked_local = local_part[:1] + "*" * (len(local_part) - 2) + local_part[-1:]
    return f"{masked_local}@{domain}"


def _send_email_code(to_email: str, login_value: str, code: str) -> None:
    message = EmailMessage()
    message["Subject"] = "FelixBank: код подтверждения входа"
    message["From"] = SMTP_FROM
    message["To"] = to_email
    message.set_content(
        "\n".join(
            [
                f"Здравствуйте, {login_value}!",
                "",
                f"Ваш код подтверждения входа: {code}",
                f"Код действует {max(1, LOGIN_2FA_CODE_TTL_SECONDS // 60)} мин.",
                "",
                "Если это были не вы, просто проигнорируйте это письмо.",
            ]
        )
    )

    smtp_class = smtplib.SMTP_SSL if SMTP_USE_SSL else smtplib.SMTP
    with smtp_class(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
        if not SMTP_USE_SSL and SMTP_USE_TLS:
            smtp.starttls()
        if SMTP_USER.strip():
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(message)


def issue_login_2fa_code(user_id: int, login_value: str, email: str) -> dict[str, str]:
    normalized_email = str(email or "").strip()
    if not normalized_email:
        raise TwoFactorSetupError("Для входа нужен email. Укажите его в профиле пользователя.")
    if not _smtp_available():
        raise TwoFactorSetupError("На сервере не настроена отправка email-кодов. Заполните SMTP_HOST и SMTP_FROM.")

    code = _generate_email_code()
    try:
        _send_email_code(normalized_email, login_value, code)
    except Exception as exc:
        current_app.logger.exception("Failed to send 2FA email to %s", normalized_email)
        raise TwoFactorDeliveryError("Не удалось отправить код на email. Проверьте SMTP-настройки.") from exc

    clear_login_2fa_codes(user_id)
    create_login_2fa_code(
        user_id=user_id,
        code_hash=generate_password_hash(code),
        channel="email",
        target=normalized_email,
        expires_at=_utc_now_naive() + timedelta(seconds=LOGIN_2FA_CODE_TTL_SECONDS),
    )
    return {
        "channel": "email",
        "target": normalized_email,
        "target_masked": mask_email(normalized_email),
        "demo_code": "",
    }


def verify_login_2fa_code(user_id: int, submitted_code: str) -> bool:
    normalized_code = str(submitted_code or "").strip()
    if not LOGIN_2FA_CODE_RE.fullmatch(normalized_code):
        return False

    row = get_active_login_2fa_code(user_id)
    if row is None:
        return False

    if not check_password_hash(str(row.get("code_hash") or ""), normalized_code):
        return False

    mark_login_2fa_code_used(int(row["id"]))
    return True
