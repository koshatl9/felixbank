(function () {
  const form = document.getElementById("transfer-form");
  if (!form) {
    return;
  }

  const cardInput = form.querySelector('input[name="recipient_card_number"]');
  const amountInput = form.querySelector('input[name="amount"]');
  const confirmedInput = document.getElementById("transfer-confirmed-input");
  const hiddenPinInput = document.getElementById("transfer-pin-hidden");
  const clientErrorEl = document.getElementById("transfer-client-error");
  const modalEl = document.getElementById("transfer-confirm-modal");
  const modalCardEl = document.getElementById("transfer-confirm-card");
  const modalAmountEl = document.getElementById("transfer-confirm-amount");
  const modalErrorEl = document.getElementById("transfer-modal-error");
  const pinInput = document.getElementById("transfer-pin-modal");
  const cancelButton = document.getElementById("transfer-cancel-button");
  const confirmButton = document.getElementById("transfer-confirm-button");
  const availableBalance = Number.parseFloat(String(form.dataset.availableBalance || "0").replace(",", "."));
  const transferMinAmount = Number.parseFloat(String(form.dataset.transferMin || "10").replace(",", "."));
  const transferMaxAmount = Number.parseFloat(String(form.dataset.transferMax || "50000").replace(",", "."));
  const transferDailyLimit = Number.parseFloat(String(form.dataset.transferDailyLimit || "100000").replace(",", "."));
  const transferDailyTotal = Number.parseFloat(String(form.dataset.transferDailyTotal || "0").replace(",", "."));
  const amountFormatter = new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2
  });
  let confirmedSubmit = false;

  function resetModalStateOnLoad() {
    if (modalEl) {
      modalEl.hidden = true;
    }
    if (confirmedInput) {
      confirmedInput.value = "0";
    }
    if (hiddenPinInput) {
      hiddenPinInput.value = "";
    }
    if (pinInput) {
      pinInput.value = "";
    }
    document.body.classList.remove("body--modal-open");
    hideError(clientErrorEl);
    hideError(modalErrorEl);
  }

  function digitsOnly(value) {
    return String(value || "").replace(/\D/g, "");
  }

  function formatCardNumber(value) {
    const digits = digitsOnly(value).slice(0, 16);
    return digits.replace(/(\d{4})(?=\d)/g, "$1 ").trim();
  }

  function maskCardNumber(value) {
    const digits = digitsOnly(value);
    const lastFour = digits.slice(-4).padStart(4, "*");
    return `**** **** **** ${lastFour}`;
  }

  function parseAmount(value) {
    const normalized = String(value || "").trim().replace(/\s+/g, "").replace(",", ".");
    const amount = Number.parseFloat(normalized);
    return Number.isFinite(amount) ? amount : NaN;
  }

  function showError(element, message) {
    if (!element) {
      return;
    }
    element.textContent = message;
    element.hidden = false;
  }

  function hideError(element) {
    if (!element) {
      return;
    }
    element.hidden = true;
    element.textContent = "";
  }

  function validateTransfer(targetErrorEl) {
    const cardDigits = digitsOnly(cardInput ? cardInput.value : "");
    const amount = parseAmount(amountInput ? amountInput.value : "");

    if (!cardDigits) {
      showError(targetErrorEl, "Введите номер виртуальной карты получателя.");
      cardInput && cardInput.focus();
      return null;
    }

    if (cardDigits.length !== 16) {
      showError(targetErrorEl, "Номер карты должен содержать 16 цифр.");
      cardInput && cardInput.focus();
      return null;
    }

    if (!Number.isFinite(amount) || amount <= 0) {
      showError(targetErrorEl, "Введите корректную сумму больше нуля.");
      amountInput && amountInput.focus();
      return null;
    }

    if (Number.isFinite(transferMinAmount) && amount < transferMinAmount) {
      showError(targetErrorEl, "Минимальная сумма перевода — 10 UAH");
      amountInput && amountInput.focus();
      return null;
    }

    if (Number.isFinite(transferMaxAmount) && amount > transferMaxAmount) {
      showError(targetErrorEl, "Максимальная сумма перевода — 50 000 UAH");
      amountInput && amountInput.focus();
      return null;
    }

    if (Number.isFinite(availableBalance) && amount > availableBalance) {
      showError(targetErrorEl, "Сумма перевода превышает доступный баланс.");
      amountInput && amountInput.focus();
      return null;
    }

    if (Number.isFinite(transferDailyLimit) && Number.isFinite(transferDailyTotal) && transferDailyTotal + amount > transferDailyLimit) {
      showError(targetErrorEl, "Дневной лимит переводов превышен");
      amountInput && amountInput.focus();
      return null;
    }

    hideError(targetErrorEl);
    return { cardDigits, amount };
  }

  function openModal(summary) {
    if (!modalEl) {
      return;
    }
    hideError(clientErrorEl);
    hideError(modalErrorEl);
    modalCardEl.textContent = maskCardNumber(summary.cardDigits);
    modalAmountEl.textContent = `${amountFormatter.format(summary.amount)} UAH`;
    modalEl.hidden = false;
    document.body.classList.add("body--modal-open");
    if (pinInput) {
      pinInput.value = "";
      window.setTimeout(function () {
        pinInput.focus();
      }, 0);
    }
  }

  function closeModal(resetPin = true) {
    if (!modalEl) {
      return;
    }
    modalEl.hidden = true;
    document.body.classList.remove("body--modal-open");
    hideError(modalErrorEl);
    if (resetPin && pinInput) {
      pinInput.value = "";
    }
  }

  resetModalStateOnLoad();

  if (cardInput) {
    cardInput.addEventListener("input", function () {
      cardInput.value = formatCardNumber(cardInput.value);
      hideError(clientErrorEl);
    });
  }

  if (amountInput) {
    amountInput.addEventListener("input", function () {
      hideError(clientErrorEl);
    });
  }

  if (pinInput) {
    pinInput.addEventListener("input", function () {
      pinInput.value = digitsOnly(pinInput.value).slice(0, 4);
      hideError(modalErrorEl);
    });

    pinInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        confirmButton && confirmButton.click();
      }
    });
  }

  form.addEventListener("submit", function (event) {
    if (confirmedSubmit) {
      return;
    }

    event.preventDefault();
    const summary = validateTransfer(clientErrorEl);
    if (!summary) {
      return;
    }

    confirmedInput.value = "0";
    hiddenPinInput.value = "";
    openModal(summary);
  });

  if (cancelButton) {
    cancelButton.addEventListener("click", function () {
      confirmedInput.value = "0";
      hiddenPinInput.value = "";
      closeModal();
    });
  }

  if (confirmButton) {
    confirmButton.addEventListener("click", function () {
      const summary = validateTransfer(modalErrorEl);
      const transferPin = String(pinInput ? pinInput.value : "").trim();

      if (!summary) {
        return;
      }

      if (!/^\d{4}$/.test(transferPin)) {
        showError(modalErrorEl, "Введите 4-значный PIN для подтверждения перевода.");
        pinInput && pinInput.focus();
        return;
      }

      confirmedInput.value = "1";
      hiddenPinInput.value = transferPin;
      confirmedSubmit = true;
      closeModal(false);
      form.requestSubmit();
    });
  }

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && modalEl && !modalEl.hidden) {
      confirmedInput.value = "0";
      hiddenPinInput.value = "";
      closeModal();
    }
  });

  if (modalEl) {
    modalEl.addEventListener("click", function (event) {
      if (event.target === modalEl) {
        confirmedInput.value = "0";
        hiddenPinInput.value = "";
        closeModal();
      }
    });
  }
})();
