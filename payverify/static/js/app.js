/* PayVerify front-end helpers. No framework: pages are rendered on the server. */
(function () {
  "use strict";

  // Installable app + offline page.
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () {});
    });
  }

  function showBusy(message) {
    var overlay = document.createElement("div");
    overlay.className = "busy-overlay";
    overlay.setAttribute("role", "status");
    overlay.innerHTML = '<span class="spinner" aria-hidden="true"></span><p></p>';
    overlay.querySelector("p").textContent = message;
    document.body.appendChild(overlay);
  }

  // Confirmation prompts and "please wait" overlays for forms.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var question = form.getAttribute("data-confirm");
    if (question && !window.confirm(question)) {
      event.preventDefault();
      return;
    }
    var busy = form.getAttribute("data-busy");
    if (busy) {
      showBusy(busy);
      form.querySelectorAll("button[type=submit]").forEach(function (button) {
        window.setTimeout(function () { button.disabled = true; }, 0);
      });
    }
  });

  // Coming back with the browser's back button must not leave the overlay up.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll(".busy-overlay").forEach(function (el) { el.remove(); });
    document.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = false; });
  });

  // Copy buttons.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button) return;
    var target = document.querySelector(button.getAttribute("data-copy"));
    if (!target) return;
    var text = "value" in target ? target.value : target.textContent;
    var label = button.querySelector("span");
    var done = function () {
      if (!label) return;
      var old = label.textContent;
      label.textContent = "Copied";
      window.setTimeout(function () { label.textContent = old; }, 1500);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () {});
    } else if (target.select) {
      target.select();
      document.execCommand("copy");
      done();
    }
  });

  // Screenshot preview before uploading.
  document.querySelectorAll("input[type=file][data-preview]").forEach(function (input) {
    var preview = document.querySelector(input.getAttribute("data-preview"));
    var zone = input.closest(".dropzone");
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (!file || !preview) return;
      if (preview.src) URL.revokeObjectURL(preview.src);
      preview.src = URL.createObjectURL(file);
      preview.hidden = false;
      if (zone) zone.classList.add("has-file");
    });
    if (zone) {
      ["dragenter", "dragover"].forEach(function (name) {
        input.addEventListener(name, function () { zone.classList.add("dragging"); });
      });
      ["dragleave", "drop"].forEach(function (name) {
        input.addEventListener(name, function () { zone.classList.remove("dragging"); });
      });
    }
  });

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-reload]")) window.location.reload();
  });

  // Notice new WhatsApp payments / bank credits while a page is open.
  var pollUrl = document.body.getAttribute("data-poll-url");
  if (pollUrl && window.fetch) {
    var stamp = null;
    var processing = document.body.classList.contains("is-processing");
    var check = function () {
      if (document.visibilityState !== "visible") return;
      fetch(pollUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) return;
          if (stamp === null) { stamp = data.stamp; return; }
          if (data.stamp === stamp) return;
          stamp = data.stamp;
          if (processing) { window.location.reload(); return; }
          var toast = document.getElementById("update-toast");
          if (toast) toast.hidden = false;
        })
        .catch(function () {});
    };
    check();
    window.setInterval(check, processing ? 4000 : 20000);
    document.addEventListener("visibilitychange", check);
  }

  // "Install app" button where the browser supports it (Chrome, Edge, Android).
  var installPrompt = null;
  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    installPrompt = event;
    document.querySelectorAll("[data-install]").forEach(function (b) { b.hidden = false; });
  });
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-install]");
    if (!button || !installPrompt) return;
    installPrompt.prompt();
    installPrompt = null;
    button.hidden = true;
  });
})();
