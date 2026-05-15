// Extends the design system's Tailwind tokens but skips its bundled style.css
// (6.5MB, applies global preflight resets that would leak into the host page).
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
    // Host page is plain HTML with its own resets; don't override them.
    preflight: false,
  },
  theme: {
    extend: dsExtend,
  },
};
