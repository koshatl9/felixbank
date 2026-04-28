from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from uuid import uuid4

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from ..auth import current_user, login_required
from ..config import TWOPLACES
from ..db import (
    get_all_user_logins,
    get_balances,
    get_user_profile,
    save_user_profile,
    update_balances,
)
from ..rates import rates_payload
from ..utils import decimal_input, decimal_to_str
from .common import ALLOWED_AVATAR_EXT, RATES_UAH_PER_1, avatar_url_for, build_virtual_card


def register_profile_routes(app: Flask) -> None:
    @app.route("/profile/", methods=["GET", "POST"])
    @login_required
    def profile():
        if request.args.get("logout"):
            session.clear()
            return redirect(url_for("login"))

        user = current_user()
        assert user is not None

        if request.method == "POST" and request.form.get("action") == "exchange":
            from_code = str(request.form.get("from") or "")
            to_code = str(request.form.get("to") or "")
            amount = decimal_input(request.form.get("amount") or "")
            balances = get_balances(user["id"])

            if from_code not in RATES_UAH_PER_1 or to_code not in RATES_UAH_PER_1:
                flash("Выберите валюты обмена.", "error")
            elif from_code == to_code:
                flash("Выберите разные валюты.", "error")
            elif amount is None or amount <= 0:
                flash("Введите сумму больше нуля.", "error")
            elif balances.get(from_code, Decimal("0")) < amount:
                flash("Недостаточно средств для обмена.", "error")
            else:
                uah_amount = amount * RATES_UAH_PER_1[from_code]
                to_amount = (uah_amount / RATES_UAH_PER_1[to_code]).quantize(
                    TWOPLACES,
                    rounding=ROUND_HALF_UP,
                )
                balances[from_code] = (balances.get(from_code, Decimal("0")) - amount).quantize(
                    TWOPLACES,
                    rounding=ROUND_HALF_UP,
                )
                balances[to_code] = (balances.get(to_code, Decimal("0")) + to_amount).quantize(
                    TWOPLACES,
                    rounding=ROUND_HALF_UP,
                )
                update_balances(user["id"], balances)
                flash(
                    f"Обмен выполнен: {decimal_to_str(amount)} {from_code} -> "
                    f"{decimal_to_str(to_amount)} {to_code}",
                    "ok",
                )
                return redirect(url_for("profile"))

        balances = get_balances(user["id"])
        profile_data = get_user_profile(int(user["id"]))
        return render_template(
            "profile.html",
            login=user["login"],
            balances=balances,
            rates_uah_per_1=RATES_UAH_PER_1,
            profile_data=profile_data,
            avatar_url=avatar_url_for(str(profile_data.get("avatar_filename") or "")),
        )

    @app.get("/profile/index.html")
    def profile_legacy():
        return redirect(url_for("profile"))

    @app.route("/profile/details", methods=["GET", "POST"])
    @login_required
    def profile_details():
        user = current_user()
        assert user is not None
        user_id = int(user["id"])
        profile_data = get_user_profile(user_id)

        if request.method == "POST":
            first_name = (request.form.get("first_name") or "").strip()
            last_name = (request.form.get("last_name") or "").strip()
            age_raw = (request.form.get("age") or "").strip()
            age: int | None = None

            if age_raw:
                try:
                    age = int(age_raw)
                except ValueError:
                    flash("Возраст должен быть числом.", "error")
                    return redirect(url_for("profile_details"))
                if age < 1 or age > 120:
                    flash("Возраст должен быть в диапазоне 1-120.", "error")
                    return redirect(url_for("profile_details"))

            avatar_file = request.files.get("avatar")
            avatar_filename_to_save: str | None = None
            if avatar_file and avatar_file.filename:
                safe_name = secure_filename(avatar_file.filename)
                ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
                if ext not in ALLOWED_AVATAR_EXT:
                    flash("Разрешены только изображения: png, jpg, jpeg, webp, gif.", "error")
                    return redirect(url_for("profile_details"))
                avatars_dir = Path(app.static_folder or "") / "assets" / "avatars"
                avatars_dir.mkdir(parents=True, exist_ok=True)
                avatar_filename_to_save = f"user_{user_id}_{uuid4().hex[:10]}.{ext}"
                avatar_file.save(avatars_dir / avatar_filename_to_save)

            save_user_profile(
                user_id=user_id,
                first_name=first_name,
                last_name=last_name,
                age=age,
                avatar_filename=avatar_filename_to_save,
            )
            flash("Профиль обновлен.", "ok")
            return redirect(url_for("profile_details"))

        balances = get_balances(user_id)
        users = get_all_user_logins(exclude_user_id=user_id)
        profile_data = get_user_profile(user_id)
        return render_template(
            "profile_details.html",
            login=user["login"],
            balances=balances,
            users=users,
            profile_data=profile_data,
            avatar_url=avatar_url_for(str(profile_data.get("avatar_filename") or "")),
        )

    @app.get("/profile/rates")
    @app.get("/profile/rates.php")
    @login_required
    def rates():
        user = current_user()
        assert user is not None
        user_profile = get_user_profile(int(user["id"]))

        fallback = [
            {"code": "USD", "name": "Доллар США", "uah_per_1": 39.50},
            {"code": "EUR", "name": "Евро", "uah_per_1": 43.00},
            {"code": "JPY", "name": "Японская иена", "uah_per_1": 0.2600},
            {"code": "KRW", "name": "Южнокорейская вона", "uah_per_1": 0.0300},
            {"code": "CNY", "name": "Китайский юань", "uah_per_1": 5.40},
        ]
        payload = rates_payload()
        return render_template(
            "rates.html",
            login=user["login"],
            payload=payload,
            fallback=fallback,
            profile_data=user_profile,
            avatar_url=avatar_url_for(str(user_profile.get("avatar_filename") or "")),
        )

    @app.get("/profile/rates/data")
    @login_required
    def rates_data():
        fallback = [
            {"code": "USD", "name": "Доллар США", "uah_per_1": 39.50},
            {"code": "EUR", "name": "Евро", "uah_per_1": 43.00},
            {"code": "JPY", "name": "Японская иена", "uah_per_1": 0.2600},
            {"code": "KRW", "name": "Южнокорейская вона", "uah_per_1": 0.0300},
            {"code": "CNY", "name": "Китайский юань", "uah_per_1": 5.40},
        ]
        payload = rates_payload()
        rates = payload.get("rates") if payload.get("ok") else fallback
        return jsonify(
            {
                "ok": bool(payload.get("ok")),
                "date": payload.get("date") or "",
                "error": payload.get("error") or "",
                "rates": rates,
            }
        )

    @app.get("/profile/virtual-card")
    @login_required
    def virtual_card():
        user = current_user()
        assert user is not None
        card = build_virtual_card(str(user["login"]))
        user_profile = get_user_profile(int(user["id"]))
        return render_template(
            "virtual_card.html",
            login=user["login"],
            card=card,
            profile_data=user_profile,
            avatar_url=avatar_url_for(str(user_profile.get("avatar_filename") or "")),
        )
