/* Header navigation — desktop dropdown + phone drawer.
   No JS  -> the hamburger stays hidden, the links wrap onto their own row,
             and the "기능" menu still opens on hover / focus-within via CSS.
   With JS -> below 820px the links collapse behind the hamburger and the
             submenu renders as a plain indented list (nothing to toggle);
             above it, click or Enter toggles the dropdown, Escape closes,
             outside click closes, aria-expanded stays in sync. */
(function () {
  var DRAWER = window.matchMedia("(max-width: 820px)");
  var HOVER = window.matchMedia("(hover: hover)");
  function isDrawer() { return DRAWER.matches; }
  function onBreakpoint(fn) {
    if (DRAWER.addEventListener) DRAWER.addEventListener("change", fn);
    else if (DRAWER.addListener) DRAWER.addListener(fn);
  }

  /* ---- phone drawer ---- */
  var header = document.querySelector("header");
  var toggle = document.querySelector(".navtoggle");
  var links = document.getElementById("site-nav");

  if (header && toggle && links) {
    var setNav = function (open) {
      header.setAttribute("data-nav-open", open ? "true" : "false");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    };
    var navOpen = function () { return header.getAttribute("data-nav-open") === "true"; };

    setNav(false);
    toggle.addEventListener("click", function () { setNav(!navOpen()); });

    // every link in the drawer navigates — close behind it so the target
    // section isn't hidden under an open panel after an in-page jump
    links.addEventListener("click", function (e) {
      if (isDrawer() && e.target.closest && e.target.closest("a")) setNav(false);
    });
    document.addEventListener("click", function (e) {
      if (navOpen() && !header.contains(e.target)) setNav(false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && navOpen()) { setNav(false); toggle.focus(); }
    });
    // crossing back to the wide layout must not strand the header open
    onBreakpoint(function () { setNav(false); });
  }

  /* ---- "기능" dropdown (wide layout only) ---- */
  var drops = document.querySelectorAll(".navdrop");
  if (!drops.length) return;

  function close(drop) {
    drop.setAttribute("data-open", "false");
    drop.querySelector(".navdrop-btn").setAttribute("aria-expanded", "false");
  }
  function open(drop) {
    drops.forEach(function (d) { if (d !== drop) close(d); });
    drop.setAttribute("data-open", "true");
    drop.querySelector(".navdrop-btn").setAttribute("aria-expanded", "true");
  }

  drops.forEach(function (drop) {
    var btn = drop.querySelector(".navdrop-btn");
    close(drop);

    btn.addEventListener("click", function (e) {
      // in the drawer the menu is already expanded, so the trigger is what it
      // looks like: a link to #features
      if (isDrawer()) return;
      e.preventDefault();
      if (drop.getAttribute("data-open") === "true") close(drop);
      else open(drop);
    });

    drop.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !isDrawer()) {
        close(drop);
        btn.focus();
      }
    });

    // pointer leaves the whole group -> collapse the toggled-open state too.
    // Touch devices fire a stray mouseleave on tap, so only wire it where a
    // real pointer exists.
    if (HOVER.matches) {
      drop.addEventListener("mouseleave", function () { close(drop); });
    }
  });

  document.addEventListener("click", function (e) {
    if (isDrawer()) return;
    drops.forEach(function (drop) {
      if (!drop.contains(e.target)) close(drop);
    });
  });

  document.addEventListener("focusin", function (e) {
    if (isDrawer()) return;
    drops.forEach(function (drop) {
      if (!drop.contains(e.target)) close(drop);
    });
  });
})();
