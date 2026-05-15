// Slimmed-down copy of chartmetric-announcement/artifacts/web/src/components/announcement/utils.ts.
// i18n is dropped; the admin preview always renders in English using the source content.

export const DEFAULT_CATEGORY_COLOR = "#6B7280";

export function formatPublishedDate(dateString: string | null): string {
  if (!dateString) return "";
  const date = new Date(dateString);
  return date.toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

export function hexToRgba(hex: string, alpha: number): string {
  const normalized = (hex || "").replace("#", "");
  const value =
    normalized.length === 6
      ? normalized
      : DEFAULT_CATEGORY_COLOR.replace("#", "");
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
