function attachPasswordToggle(buttonId, inputId) {
  const button = document.getElementById(buttonId);
  const input = document.getElementById(inputId);

  if (!button || !input) {
    return;
  }

  button.addEventListener("click", () => {
    const isText = input.type === "text";
    input.type = isText ? "password" : "text";
    button.textContent = isText ? "Показать" : "Скрыть";
    button.setAttribute("aria-pressed", String(!isText));
  });
}

attachPasswordToggle("toggle1", "password");
attachPasswordToggle("toggle2", "confirm_password");
