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
