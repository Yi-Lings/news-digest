/* Choose a reading edition before first paint; share the choice across reader pages. */
(function () {
  "use strict";
  var root = document.documentElement;
  var key = "news-digest-layout";
  var preference = "auto";
  var viewport = document.querySelector('meta[name="viewport"]');
  try {
    var saved = localStorage.getItem(key);
    if (saved === "mobile" || saved === "desktop") preference = saved;
  } catch (error) { /* Storage is optional in private browsing. */ }

  function touchPhone() {
    return navigator.maxTouchPoints > 0 && window.screen.width <= 900;
  }

  function updateViewportInsets() {
    var visible = window.visualViewport;
    // Fixed controls must stay inside the visible area when browser chrome or zoom changes.
    var bottom = visible ? Math.max(0, window.innerHeight - visible.height - visible.offsetTop) : 0;
    var right = visible ? Math.max(0, document.documentElement.clientWidth - visible.width - visible.offsetLeft) : 0;
    root.style.setProperty("--viewport-bottom", bottom + "px");
    root.style.setProperty("--viewport-right", right + "px");
  }

  function applyLayout() {
    var mobile = preference === "mobile"
      || (preference === "auto" && (touchPhone() || window.innerWidth <= 900));
    root.setAttribute("data-layout", mobile ? "mobile" : "desktop");
    root.setAttribute("data-layout-preference", preference);
    if (viewport) {
      // Numeric width also helps touch browsers that initially request a desktop viewport.
      var content = preference === "desktop" && touchPhone() ? "width=1200"
        : "width=" + (touchPhone() ? window.screen.width : "device-width")
          + ", initial-scale=1, viewport-fit=cover";
      if (viewport.content !== content) viewport.content = content;
    }
    document.querySelectorAll("[data-layout-btn]").forEach(function (button) {
      button.setAttribute("aria-pressed", String(button.dataset.layoutBtn === preference));
    });
    updateViewportInsets();
  }
  applyLayout();
  window.addEventListener("resize", applyLayout);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", updateViewportInsets, { passive: true });
    window.visualViewport.addEventListener("scroll", updateViewportInsets, { passive: true });
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyLayout();
    var toggle = document.querySelector("[data-nav-toggle]");
    if (toggle) {
      root.setAttribute("data-nav", "closed");
      toggle.addEventListener("click", function () {
        var open = root.getAttribute("data-nav") !== "open";
        root.setAttribute("data-nav", open ? "open" : "closed");
        toggle.setAttribute("aria-expanded", String(open));
        toggle.textContent = open ? "收起" : "菜单";
      });
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && root.getAttribute("data-nav") === "open") {
          root.setAttribute("data-nav", "closed");
          toggle.setAttribute("aria-expanded", "false");
          toggle.textContent = "菜单";
          toggle.focus();
        }
      });
    }
    document.querySelectorAll("[data-layout-btn]").forEach(function (button) {
      button.addEventListener("click", function () {
        preference = button.dataset.layoutBtn;
        try { localStorage.setItem(key, preference); } catch (error) { /* Optional. */ }
        applyLayout();
      });
    });
  });
}());
