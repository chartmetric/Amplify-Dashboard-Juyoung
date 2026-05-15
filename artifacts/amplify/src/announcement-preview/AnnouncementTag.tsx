// Slimmed-down copy of chartmetric-announcement/artifacts/web/src/components/announcement/components/AnnouncementTag.tsx.
// The translation lookup is dropped; the admin preview shows category names verbatim
// from the form.

import type { AnnouncementCategoryInfo } from "./types";
import { DEFAULT_CATEGORY_COLOR, hexToRgba } from "./utils";

interface Props {
  category: AnnouncementCategoryInfo;
}

function AnnouncementTag({ category }: Props) {
  const borderColor = category.color || DEFAULT_CATEGORY_COLOR;
  const backgroundColor = hexToRgba(borderColor, 0.18);

  return (
    <span
      className="inline-block text-xs font-medium px-2.5 py-0.5 rounded-xs border border-solid leading-relaxed tracking-wide"
      style={{ backgroundColor, borderColor, color: borderColor }}
    >
      {category.name}
    </span>
  );
}

export default AnnouncementTag;
