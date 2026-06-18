(function () {
  const root = document.querySelector("[data-notifications-root]");
  if (!root) {
    return;
  }

  const bellButton = root.querySelector("#notification-bell");
  const badgeEl = root.querySelector("#notification-badge");
  const panelEl = root.querySelector("#notification-panel");
  const listEl = root.querySelector("#notification-list");
  const notificationsUrl = root.dataset.notificationsUrl || "";

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderBadge(count) {
    if (!badgeEl) {
      return;
    }
    if (!count) {
      badgeEl.hidden = true;
      badgeEl.textContent = "";
      return;
    }
    badgeEl.hidden = false;
    badgeEl.textContent = count < 10 ? String(count) : "9+";
  }

  function renderNotifications(notifications) {
    if (!listEl) {
      return;
    }

    if (!Array.isArray(notifications) || !notifications.length) {
      listEl.innerHTML = '<div class="notification-empty">Пока уведомлений нет.</div>';
      renderBadge(0);
      return;
    }

    renderBadge(notifications.length);
    listEl.innerHTML = notifications.map(function (notification) {
      const kind = escapeHtml(notification.kind || "info");
      return `
        <div class="notification-item notification-item--${kind}">
          <span class="notification-item__icon">✔</span>
          <div>
            <strong>${escapeHtml(notification.title)}</strong>
            <span>${escapeHtml(notification.created_at_label || "")}</span>
          </div>
        </div>
      `;
    }).join("");
  }

  async function refreshNotifications(openAfterRefresh) {
    if (!notificationsUrl) {
      return;
    }

    try {
      const response = await fetch(notificationsUrl, {
        headers: {
          "X-Requested-With": "fetch"
        },
        cache: "no-store"
      });

      if (!response.ok) {
        throw new Error("notifications fetch failed");
      }

      const payload = await response.json();
      renderNotifications(payload.notifications || []);

      if (openAfterRefresh && panelEl) {
        panelEl.hidden = false;
        bellButton && bellButton.setAttribute("aria-expanded", "true");
      }
    } catch (error) {
      if (listEl) {
        listEl.innerHTML = '<div class="notification-empty">Не удалось загрузить уведомления.</div>';
      }
    }
  }

  function closePanel() {
    if (!panelEl) {
      return;
    }
    panelEl.hidden = true;
    root.classList.remove("notifications--open");
    bellButton && bellButton.setAttribute("aria-expanded", "false");
  }

  if (bellButton) {
    bellButton.addEventListener("click", async function (event) {
      event.preventDefault();
      event.stopPropagation();
      if (!panelEl) {
        return;
      }

      if (panelEl.hidden) {
        await refreshNotifications(true);
        root.classList.add("notifications--open");
      } else {
        closePanel();
      }
    });
  }

  document.addEventListener("click", function (event) {
    if (!root.contains(event.target)) {
      closePanel();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closePanel();
    }
  });

  window.FelixNotifications = {
    refresh: function () {
      return refreshNotifications(false);
    },
  };
})();
