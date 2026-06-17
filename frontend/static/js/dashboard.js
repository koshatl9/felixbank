(function () {
  const root = document.querySelector("[data-dashboard-root]");
  if (!root) {
    return;
  }

  const sectionNodes = Array.from(root.querySelectorAll("[data-dashboard-section]"));
  const linkNodes = Array.from(root.querySelectorAll("[data-dashboard-target]"));
  const validSections = new Set(sectionNodes.map((node) => node.dataset.dashboardSection).filter(Boolean));
  const defaultSection = root.dataset.dashboardDefaultSection || "overview";

  function normalizeSection(value) {
    const normalized = String(value || "").trim().toLowerCase();
    if (validSections.has(normalized)) {
      return normalized;
    }
    return defaultSection;
  }

  function currentSectionFromUrl() {
    const url = new URL(window.location.href);
    const queryValue = url.searchParams.get("section");
    const hashValue = window.location.hash.replace(/^#/, "");

    if (queryValue && validSections.has(normalizeSection(queryValue))) {
      return normalizeSection(queryValue);
    }
    if (hashValue && validSections.has(normalizeSection(hashValue))) {
      return normalizeSection(hashValue);
    }
    return normalizeSection(defaultSection);
  }

  function updateUrl(section, pushHistory) {
    const url = new URL(window.location.href);
    url.searchParams.set("section", section);
    url.hash = section;
    if (pushHistory) {
      window.history.pushState({ section }, "", url.toString());
    } else {
      window.history.replaceState({ section }, "", url.toString());
    }
  }

  function setLinkActiveState(link, isActive) {
    if (link.classList.contains("menu__subitem")) {
      link.classList.toggle("menu__subitem--active", isActive);
      return;
    }
    link.classList.toggle("menu__item--active", isActive);
  }

  function applySection(section, pushHistory) {
    const normalizedSection = normalizeSection(section);

    sectionNodes.forEach(function (node) {
      node.hidden = node.dataset.dashboardSection !== normalizedSection;
    });

    linkNodes.forEach(function (link) {
      const fallbackTarget = normalizeSection(link.dataset.dashboardTarget);
      const matchList = String(link.dataset.dashboardMatch || fallbackTarget)
        .split(",")
        .map(function (item) { return normalizeSection(item); });
      setLinkActiveState(link, matchList.includes(normalizedSection));
    });

    updateUrl(normalizedSection, pushHistory);
    window.scrollTo({ top: 0, behavior: pushHistory ? "smooth" : "auto" });
  }

  linkNodes.forEach(function (link) {
    link.addEventListener("click", function (event) {
      event.preventDefault();
      applySection(link.dataset.dashboardTarget, true);
    });
  });

  window.addEventListener("popstate", function () {
    applySection(currentSectionFromUrl(), false);
  });

  applySection(currentSectionFromUrl(), false);
})();
