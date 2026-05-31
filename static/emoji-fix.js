// Restore color emoji inside gradient-clipped headings.
//
// Headings (h1/h2/h3) use `background-clip: text` + `-webkit-text-fill-color:
// transparent` for their gradient look. That clip applies to EVERY glyph,
// including emoji, so an emoji in a (Stash-supplied) title renders as a flat
// gradient silhouette instead of its real colors. There's no pure-CSS way to
// exclude only the emoji within the same clipped element, so we wrap each emoji
// run in `<span class="emoji">`, which styles.css resets back to normal fill.
(function () {
    "use strict";

    // Prefer the sequence-aware RGI emoji matcher (handles ZWJ sequences, skin
    // tones, flags, keycaps). Fall back to an Extended_Pictographic matcher on
    // engines without the `v` flag / \p{RGI_Emoji}.
    var EMOJI_RE;
    try {
        EMOJI_RE = new RegExp("\\p{RGI_Emoji}", "gv");
    } catch (e) {
        EMOJI_RE = /\p{Extended_Pictographic}(?:\uFE0F|\u200D\p{Extended_Pictographic}|[\u{1F3FB}-\u{1F3FF}])*/gu;
    }
    // Cheap per-node pretest so we only touch text that actually has an emoji.
    var HAS_EMOJI = /\p{Extended_Pictographic}/u;

    function wrapEmoji(root) {
        var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
        var targets = [];
        var node;
        while ((node = walker.nextNode())) {
            if (node.nodeValue && HAS_EMOJI.test(node.nodeValue)) {
                targets.push(node);
            }
        }
        targets.forEach(function (textNode) {
            var text = textNode.nodeValue;
            var frag = document.createDocumentFragment();
            var last = 0;
            var match;
            EMOJI_RE.lastIndex = 0;
            while ((match = EMOJI_RE.exec(text))) {
                if (match.index > last) {
                    frag.appendChild(document.createTextNode(text.slice(last, match.index)));
                }
                var span = document.createElement("span");
                span.className = "emoji";
                span.textContent = match[0];
                frag.appendChild(span);
                last = match.index + match[0].length;
                // Guard against a zero-length match pinning lastIndex.
                if (match[0].length === 0) EMOJI_RE.lastIndex++;
            }
            if (last < text.length) {
                frag.appendChild(document.createTextNode(text.slice(last)));
            }
            textNode.parentNode.replaceChild(frag, textNode);
        });
    }

    function run() {
        document.querySelectorAll("h1, h2, h3").forEach(wrapEmoji);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", run);
    } else {
        run();
    }
})();
