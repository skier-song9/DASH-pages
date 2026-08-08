/* Header "기능" dropdown.
   No JS  -> the trigger is a plain link to #features, and the menu still
             opens on hover / focus-within via CSS alone.
   With JS -> click or Enter toggles (works on touch), Escape closes,
             outside click closes, aria-expanded stays in sync. */
(function () {
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
      e.preventDefault();
      if (drop.getAttribute("data-open") === "true") close(drop);
      else open(drop);
    });

    drop.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        close(drop);
        btn.focus();
      }
    });

    // pointer leaves the whole group -> collapse the toggled-open state too
    drop.addEventListener("mouseleave", function () { close(drop); });
  });

  document.addEventListener("click", function (e) {
    drops.forEach(function (drop) {
      if (!drop.contains(e.target)) close(drop);
    });
  });

  document.addEventListener("focusin", function (e) {
    drops.forEach(function (drop) {
      if (!drop.contains(e.target)) close(drop);
    });
  });
})();
