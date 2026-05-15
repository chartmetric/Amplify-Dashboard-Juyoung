// Content-only adaptation of the in-app announcement modal: no reactions, no
// comments, no network calls — just the static popup chrome around the post body.
import { faXmark } from "@fortawesome/pro-solid-svg-icons/faXmark";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";

import { CMFlex } from "@chartmetric/chartmetric-design-system/components/CMFlex";
import { CMText } from "@chartmetric/chartmetric-design-system/components/CMText";

import AnnouncementContentRenderer from "./AnnouncementContentRenderer";
import AnnouncementTag from "./AnnouncementTag";
import { formatPublishedDate } from "./utils";

import type { AnnouncementPreviewData } from "./types";

interface Props {
  announcement: AnnouncementPreviewData;
  show: boolean;
}

function AnnouncementPopupPreview({ announcement, show }: Props) {
  if (!show) return null;

  const dateLabel = formatPublishedDate(announcement.published_at);
  const heroImage = announcement.image_url || null;

  return (
    <div className="absolute inset-0 z-10 flex items-center justify-center overflow-auto bg-black/60 rounded-[10px]">
      <div
        className="relative w-[90%] max-h-[90%] overflow-auto bg-white dark:bg-cm-gray-dark-2 rounded-lg shadow-2xl"
        role="dialog"
        aria-modal="true"
        aria-label="Announcement preview"
      >
        <div className="flex items-start justify-between px-5 pt-5 pb-2 border-b border-cm-gray-200 dark:border-cm-gray-dark-3">
          <CMText
            value={announcement.title || "(untitled)"}
            variant="h4"
            _className="pr-6"
          />
          <button
            type="button"
            aria-label="Close preview (visual only)"
            className="text-cm-gray-500 hover:text-cm-gray-700 dark:text-cm-gray-dark-5 cursor-default"
            tabIndex={-1}
          >
            <FontAwesomeIcon icon={faXmark} size="lg" />
          </button>
        </div>

        <CMFlex vertical gap="md" p="lg">
          {announcement.categories && announcement.categories.length > 0 && (
            <CMFlex align="center" gap="sm" wrap>
              {announcement.categories.map((cat) => (
                <AnnouncementTag key={cat.name} category={cat} />
              ))}
            </CMFlex>
          )}

          {dateLabel && (
            <CMText value={dateLabel} variant="mini-1" color="tertiary" />
          )}

          {heroImage && (
            <figure className="m-0 w-full">
              <img
                src={heroImage}
                alt=""
                className="w-full h-auto rounded-lg block"
              />
            </figure>
          )}

          <AnnouncementContentRenderer content={announcement.content} />
        </CMFlex>
      </div>
    </div>
  );
}

export default AnnouncementPopupPreview;
