// Masonry packing for gallery-mode tag shares.
//
// CSS multi-column fills column-major (top-to-bottom, then the next column), so
// the top row isn't the first N cards left-to-right and a tall card can overflow
// the multicol box and bleed its shadow over the pagination. This script does a
// real masonry pass instead: distribute the .video-card elements across N flex
// columns, always appending the next card to the currently shortest column, so
// cards fill left-to-right and every card stays bounded inside its column.
//
// Each card reserves its height via --card-aspect (set server-side from the
// video's native dimensions), so the shortest-column math is correct before the
// thumbnails finish loading and the layout doesn't reflow as they arrive.
(function () {
    "use strict";

    var gallery = document.querySelector(".gallery-masonry");
    if (!gallery) return;

    var GAP = 30;        // must match the CSS column gap
    var TARGET_COL = 340; // approx column width; column count derives from this

    // Snapshot the original card order once; re-packing reparents them.
    var cards = Array.prototype.slice.call(gallery.querySelectorAll(".video-card"));
    if (!cards.length) return;

    var lastCols = -1;

    function columnCount(width) {
        return Math.max(1, Math.min(
            cards.length,
            Math.floor((width + GAP) / (TARGET_COL + GAP))
        ));
    }

    // Estimate a card's height from its reserved aspect ratio plus the title bar,
    // so packing is stable before images load. Falls back to measured height.
    function cardWeight(card) {
        var thumb = card.querySelector(".thumb");
        var aspect = parseFloat(getComputedStyle(card).getPropertyValue("--card-aspect")) || 1.7778;
        var colW = card.parentNode ? card.parentNode.clientWidth : TARGET_COL;
        if (colW <= 0) colW = TARGET_COL;
        var thumbH = thumb ? colW / aspect : 0;
        var titleH = 40; // approximate single-line title + padding
        return thumbH + titleH;
    }

    function layout() {
        var width = gallery.clientWidth;
        if (width <= 0) return;
        var cols = columnCount(width);
        if (cols === lastCols) return;
        lastCols = cols;

        gallery.innerHTML = "";
        var colEls = [];
        var colHeights = [];
        for (var i = 0; i < cols; i++) {
            var col = document.createElement("div");
            col.className = "masonry-col";
            gallery.appendChild(col);
            colEls.push(col);
            colHeights.push(0);
        }

        cards.forEach(function (card) {
            var shortest = 0;
            for (var i = 1; i < cols; i++) {
                if (colHeights[i] < colHeights[shortest]) shortest = i;
            }
            colEls[shortest].appendChild(card);
            colHeights[shortest] += cardWeight(card) + GAP;
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", layout);
    } else {
        layout();
    }
    // Re-pack when the column count would change (viewport resize).
    if (typeof ResizeObserver !== "undefined") {
        new ResizeObserver(layout).observe(gallery);
    } else {
        window.addEventListener("resize", layout);
    }
})();
