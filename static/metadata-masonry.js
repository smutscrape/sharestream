// Masonry packing for the video player's metadata boxes.
//
// The boxes (.metadata-section) are a fixed column width and vary only in
// height. A CSS grid keeps rows aligned, so a short box is pinned below a tall
// neighbour, leaving a gap; CSS multi-column is column-major and can't lift a
// later box into an earlier column. This script does a real masonry pass:
// distribute the boxes across N columns, always appending the next box to the
// currently shortest column, so short boxes rise to fill vertical gaps.
//
// The full-width Description box stays a direct child of .metadata-grid (above
// the columns) and is not packed.
(function () {
    "use strict";

    var grid = document.querySelector(".metadata-grid");
    if (!grid) return;

    var GAP = 20;        // must match the CSS column gap
    var TARGET_COL = 240; // approx column width; column count derives from this

    var description = grid.querySelector(":scope > .video-description");
    var sections = Array.prototype.filter.call(
        grid.querySelectorAll(":scope > .metadata-section"),
        function (node) { return node !== description; }
    );
    if (!sections.length) return;

    var lastWidth = -1;
    var lastCols = -1;

    function columnCount(width) {
        return Math.max(1, Math.min(
            sections.length,
            Math.floor((width + GAP) / (TARGET_COL + GAP))
        ));
    }

    function layout() {
        var width = grid.clientWidth;
        if (width <= 0) return;
        var cols = columnCount(width);
        // Re-pack only when the width actually changed. Moving boxes changes the
        // grid's HEIGHT (not its width), so this guard stops a ResizeObserver
        // feedback loop.
        if (width === lastWidth && cols === lastCols) return;
        lastWidth = width;
        lastCols = cols;

        var existing = grid.querySelector(".metadata-columns");
        if (existing) existing.remove();

        var container = document.createElement("div");
        container.className = "metadata-columns";
        var colEls = [];
        var colHeights = [];
        for (var i = 0; i < cols; i++) {
            var col = document.createElement("div");
            col.className = "metadata-col";
            container.appendChild(col);
            colEls.push(col);
            colHeights.push(0);
        }
        grid.appendChild(container);

        sections.forEach(function (section) {
            var shortest = 0;
            for (var i = 1; i < cols; i++) {
                if (colHeights[i] < colHeights[shortest]) shortest = i;
            }
            colEls[shortest].appendChild(section);
            colHeights[shortest] += section.offsetHeight + GAP;
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", layout);
    } else {
        layout();
    }
    // Web fonts can change box heights after first paint; re-pack on load.
    window.addEventListener("load", layout);
    // Re-pack when the grid's width changes — viewport resize, or the player
    // resizing and reflowing the metadata column beside it.
    if (typeof ResizeObserver !== "undefined") {
        new ResizeObserver(layout).observe(grid);
    } else {
        window.addEventListener("resize", layout);
    }
})();
