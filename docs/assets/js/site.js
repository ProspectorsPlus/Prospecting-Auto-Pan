/* Prospector Lite site, shared vanilla JS. No dependencies, no network. */
(function () {
  "use strict";
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* nav scrolled state */
  var nav = document.querySelector(".nav");
  if (nav) {
    var onScroll = function () {
      nav.classList.toggle("scrolled", window.scrollY > 8);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  /* mobile nav toggle */
  var toggle = document.getElementById("navtoggle");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    });
    nav.addEventListener("click", function (e) {
      if (e.target.closest(".nav-links a")) {
        nav.classList.remove("open");
        toggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  /* reveal on scroll */
  var revealed = document.querySelectorAll(".reveal");
  if (revealed.length && "IntersectionObserver" in window && !reduce) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) {
          en.target.classList.add("in");
          io.unobserve(en.target);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    revealed.forEach(function (el) { io.observe(el); });
  } else {
    revealed.forEach(function (el) { el.classList.add("in"); });
  }

  /* scroll spy for on-page nav links ([data-spy] holds same-page anchors) */
  var spy = document.querySelector("[data-spy]");
  if (spy) {
    var links = Array.prototype.slice.call(spy.querySelectorAll('a[href^="#"]'));
    var targets = links
      .map(function (a) { return document.getElementById(a.hash.slice(1)); })
      .filter(Boolean);
    /* activate a section once it clears its own scroll-margin-top, so the
       highlight agrees with where anchor navigation actually lands */
    var spyOffset = 120;
    if (targets.length) {
      var margin = parseFloat(getComputedStyle(targets[0]).scrollMarginTop);
      if (margin) spyOffset = margin + 4;
    }
    var setActive = function () {
      var y = window.scrollY + spyOffset;
      var cur = null;
      for (var i = 0; i < targets.length; i++) {
        if (targets[i].offsetTop <= y) cur = targets[i];
      }
      links.forEach(function (a) {
        if (cur && a.hash.slice(1) === cur.id) a.setAttribute("aria-current", "true");
        else a.removeAttribute("aria-current");
      });
    };
    window.addEventListener("scroll", setActive, { passive: true });
    setActive();
  }

  /* lightbox for zoomable screenshots */
  var lb = document.getElementById("lightbox");
  if (lb) {
    var lbimg = document.getElementById("lbimg");
    var lbcap = document.getElementById("lbcap");
    var openLb = function (img) {
      lbimg.src = img.currentSrc || img.src;
      lbimg.alt = img.alt || "";
      var fig = img.closest("figure");
      var cap = fig && fig.querySelector("figcaption");
      lbcap.textContent = cap ? cap.textContent : "";
      lb.showModal();
    };
    document.querySelectorAll("img.zoomable").forEach(function (img) {
      img.setAttribute("tabindex", "0");
      img.setAttribute("role", "button");
      img.setAttribute("aria-label", "Enlarge screenshot");
      img.addEventListener("click", function () { openLb(img); });
      img.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openLb(img); }
      });
    });
    document.getElementById("lbclose").addEventListener("click", function () { lb.close(); });
    lb.addEventListener("click", function (e) { if (e.target === lb) lb.close(); });
  }

  /* docs sidebar toggle (mobile) */
  var docsBtn = document.getElementById("docsmenu");
  var docsSide = document.querySelector(".docs-side");
  if (docsBtn && docsSide) {
    docsBtn.addEventListener("click", function () {
      var open = docsSide.classList.toggle("open");
      docsBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  /* ---- copy buttons ----
     Any element with [data-copy] copies that attribute's text on click.
     Announced through a polite live region created up front, so assistive
     tech sees the region before its first update; falls back to
     execCommand when the async Clipboard API is unavailable. */
  var liveRegion = null;
  if (document.querySelector("[data-copy]")) {
    liveRegion = document.createElement("span");
    liveRegion.className = "sr-only";
    liveRegion.setAttribute("role", "status");
    liveRegion.setAttribute("aria-live", "polite");
    document.body.appendChild(liveRegion);
  }
  var announce = function (msg) {
    if (!liveRegion) return;
    liveRegion.textContent = "";
    window.setTimeout(function () { liveRegion.textContent = msg; }, 40);
  };
  var writeClipboard = function (text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-200px";
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      document.body.removeChild(ta);
      if (ok) resolve(); else reject(new Error("copy failed"));
    });
  };
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    writeClipboard(btn.getAttribute("data-copy")).then(function () {
      var label = btn.querySelector(".cb-label");
      if (label && !btn.dataset.cbOrig) btn.dataset.cbOrig = label.textContent;
      btn.classList.add("is-copied");
      if (label) label.textContent = "Copied";
      announce("Copied to clipboard");
      window.clearTimeout(btn._cbTimer);
      btn._cbTimer = window.setTimeout(function () {
        btn.classList.remove("is-copied");
        if (label) label.textContent = btn.dataset.cbOrig;
      }, 1700);
    }, function () {
      announce("Copy failed. Select the text and copy it manually.");
    });
  });

  /* ---- download checklist, stored per page in this browser only ---- */
  var checks = document.querySelectorAll(".dl-check input[type=checkbox][id]");
  if (checks.length) {
    var checkKey = "pp-checklist:" + location.pathname;
    var savedChecks = {};
    try { savedChecks = JSON.parse(localStorage.getItem(checkKey) || "{}") || {}; } catch (err) { savedChecks = {}; }
    checks.forEach(function (c) {
      if (savedChecks[c.id]) c.checked = true;
      c.addEventListener("change", function () {
        savedChecks[c.id] = c.checked;
        try { localStorage.setItem(checkKey, JSON.stringify(savedChecks)); } catch (err) { /* private mode */ }
      });
    });
  }

  /* ---- platform hint on the download chooser (suggests, never forces) ----
     iPads report "MacIntel" with a multi-touch screen; skip the hint there,
     since no build runs on them. */
  if (document.querySelector("[data-platform-hint]")) {
    var uaPlat = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "";
    var isIpad = uaPlat === "MacIntel" && navigator.maxTouchPoints > 1;
    var osGuess = isIpad ? "" : (/^mac/i.test(uaPlat) ? "macos" : (/^win/i.test(uaPlat) ? "windows" : ""));
    if (osGuess) {
      var detected = document.querySelector('.plat-card[data-os="' + osGuess + '"]');
      if (detected) detected.classList.add("is-detected");
    }
  }
})();
