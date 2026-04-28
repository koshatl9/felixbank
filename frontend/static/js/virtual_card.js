function digitsOnly(value) {
  return String(value || "").replace(/\D/g, "");
}

function groupedCardNumber(value) {
  return value.replace(/(.{4})/g, "$1 ").trim();
}

function maskedCardNumber(value) {
  const digits = digitsOnly(value);
  if (!digits) return "****";
  if (digits.length <= 4) return digits;

  const masked = "*".repeat(digits.length - 4) + digits.slice(-4);
  return groupedCardNumber(masked);
}

document.querySelectorAll(".card-number-reveal").forEach((element) => {
  const cardNumber = digitsOnly(element.dataset.cardNumber);
  const fullValue = groupedCardNumber(cardNumber);
  const maskedValue = maskedCardNumber(cardNumber);

  element.textContent = maskedValue;
  element.title = "Наведите, чтобы показать полностью";

  if (!cardNumber) return;

  element.addEventListener("mouseenter", () => {
    element.textContent = fullValue;
  });
  element.addEventListener("mouseleave", () => {
    element.textContent = maskedValue;
  });
});

document.querySelectorAll(".cvv-reveal").forEach((element) => {
  const maskedValue = "***";
  const actualValue = element.dataset.cvv || maskedValue;

  element.textContent = maskedValue;
  element.addEventListener("mouseenter", () => {
    element.textContent = actualValue;
  });
  element.addEventListener("mouseleave", () => {
    element.textContent = maskedValue;
  });
});

const cardLockButton = document.getElementById("card-lock-toggle");
const cardLockHint = document.getElementById("card-lock-hint");

if (cardLockButton) {
  let isLocked = cardLockButton.dataset.blocked === "true";
  const lockUrl = cardLockButton.dataset.lockUrl || "";

  function applyLockState(locked) {
    cardLockButton.classList.toggle("is-locked", locked);
    cardLockButton.setAttribute("aria-pressed", String(locked));
    cardLockButton.textContent = locked ? "Разблокировать карту" : "Заблокировать карту";

    if (cardLockHint) {
      cardLockHint.textContent = locked
        ? "Карта заблокирована. Вы всегда можете её разблокировать."
        : "Вы всегда можете её разблокировать.";
    }
  }

  applyLockState(isLocked);

  cardLockButton.addEventListener("click", async () => {
    const nextState = !isLocked;
    cardLockButton.disabled = true;

    try {
      const response = await fetch(lockUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "fetch"
        },
        body: JSON.stringify({ blocked: nextState })
      });

      if (!response.ok) {
        throw new Error("lock toggle failed");
      }

      const data = await response.json();
      isLocked = Boolean(data.blocked);
      cardLockButton.dataset.blocked = String(isLocked);
      applyLockState(isLocked);
    } catch (error) {
      if (cardLockHint) {
        cardLockHint.textContent = "Не удалось обновить статус карты. Попробуйте ещё раз.";
      }
    } finally {
      cardLockButton.disabled = false;
    }
  });
}
