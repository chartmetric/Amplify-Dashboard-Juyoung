// Tailwind config for the announcement preview widget.
//
// We can't reuse the design system's bundled style.css verbatim — it's 6.5 MB and
// applies preflight resets / element selectors that leak into the rest of the
// admin page (which is plain HTML, not a Tailwind app).
//
// Instead we extend the DS Tailwind config (so all cm-* colors, spacing tokens
// and text sizes used by CMFlex/CMText resolve correctly) and point `content` at
// both the widget source and the DS dist so any class string emitted by a CM*
// component at runtime is picked up by Tailwind's JIT.
import designSystemConfig from "@chartmetric/chartmetric-design-system/tailwind";

const dsExtend = designSystemConfig.theme?.extend ?? {};
const dsSafelist = designSystemConfig.safelist ?? [];

export default {
  content: [
    "./src/announcement-preview/**/*.{ts,tsx}",
    "./node_modules/@chartmetric/chartmetric-design-system/dist/**/*.{js,mjs}",
  ],
  safelist: dsSafelist,
  corePlugins: {
    // The host page (templates/announcements.html) already has its own resets
    // and element styles. Disable Tailwind's preflight so we don't override
    // them globally.
    preflight: false,
  },
  theme: {
    extend: dsExtend,
  },
};
