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
    var setActive = function () {
      var y = window.scrollY + 120;
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
})();
