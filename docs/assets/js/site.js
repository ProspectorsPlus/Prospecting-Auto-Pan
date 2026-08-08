/* Prospector Lite site — vanilla JS, no tracking, no external requests. */
(function () {
  'use strict';

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---- Sticky nav state ---- */
  var nav = document.getElementById('nav');
  function onScroll() {
    nav.classList.toggle('scrolled', window.scrollY > 8);
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  /* ---- Mobile menu ---- */
  var toggle = document.getElementById('navtoggle');
  toggle.addEventListener('click', function () {
    var open = nav.classList.toggle('open');
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
  });
  document.querySelectorAll('#navlinks a').forEach(function (a) {
    a.addEventListener('click', function () {
      nav.classList.remove('open');
      toggle.setAttribute('aria-expanded', 'false');
    });
  });

  /* ---- Reveal on scroll ---- */
  var revealed = document.querySelectorAll('.reveal');
  if (reduceMotion || !('IntersectionObserver' in window)) {
    revealed.forEach(function (el) { el.classList.add('in'); });
  } else {
    var ro = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('in'); ro.unobserve(e.target); }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.06 });
    revealed.forEach(function (el) { ro.observe(el); });
  }

  /* ---- Scroll spy: main nav + calibration rail ---- */
  function spy(linkSel, activeClass) {
    var links = Array.prototype.slice.call(document.querySelectorAll(linkSel));
    var map = [];
    links.forEach(function (a) {
      var id = a.getAttribute('href');
      if (id && id.charAt(0) === '#') {
        var sec = document.querySelector(id);
        if (sec) map.push([sec, a]);
      }
    });
    if (!map.length) return;
    function update() {
      var y = window.scrollY + 120;
      var current = null;
      map.forEach(function (pair) {
        if (pair[0].offsetTop <= y) current = pair[1];
      });
      links.forEach(function (a) {
        var on = a === current;
        a.classList.toggle(activeClass, on);
        if (on) { a.setAttribute('aria-current', 'true'); } else { a.removeAttribute('aria-current'); }
      });
    }
    window.addEventListener('scroll', update, { passive: true });
    update();
  }
  spy('#navlinks a', 'active');
  spy('#calibrail a', 'active');

  /* ---- Lightbox ---- */
  var lb = document.getElementById('lightbox');
  var lbimg = document.getElementById('lbimg');
  var lbcap = document.getElementById('lbcap');
  if (lb && typeof lb.showModal === 'function') {
    document.querySelectorAll('img.zoomable').forEach(function (img) {
      img.setAttribute('tabindex', '0');
      img.setAttribute('role', 'button');
      img.setAttribute('aria-label', 'Enlarge screenshot: ' + img.alt);
      function open() {
        lbimg.src = img.currentSrc || img.src;
        lbimg.alt = img.alt;
        var fig = img.closest('figure');
        var cap = fig && fig.querySelector('figcaption');
        lbcap.textContent = cap ? cap.textContent : '';
        lb.showModal();
      }
      img.addEventListener('click', open);
      img.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
      });
    });
    lb.addEventListener('click', function (e) {
      if (e.target === lb) lb.close(); /* backdrop click */
    });
    document.getElementById('lbclose').addEventListener('click', function () { lb.close(); });
    lb.addEventListener('close', function () { lbimg.src = ''; });
  }
})();
