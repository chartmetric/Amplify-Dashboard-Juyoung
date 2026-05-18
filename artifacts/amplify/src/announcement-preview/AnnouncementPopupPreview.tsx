// Content-only adaptation of the in-app announcement modal: no reactions, no
// comments, no network calls — just the static popup chrome around the post body.
// CMFlex / CMText from the design system are replaced with plain Tailwind
// equivalents to avoid the React-18-only dependency crashing the React 19 host.
import { faXmark } from "@fortawesome/pro-solid-svg-icons/faXmark";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";

import AnnouncementContentRenderer from "./AnnouncementContentRenderer";
import AnnouncementTag from "./AnnouncementTag";
import { formatPublishedDate } from "./utils";

import type { AnnouncementPreviewData } from "./types";

interface Props {
  announcement: AnnouncementPreviewData;
  show: boolean;
  onClose?: () => void;
}

function AnnouncementPopupPreview({ announcement, show, onClose }: Props) {
  if (!show) return null;

  const dateLabel = formatPublishedDate(announcement.published_at);
  const heroImage = announcement.image_url || null;

  return (
    <div
      className="absolute inset-0 flex items-center justify-center bg-black/60"
      onClick={(e) => { if (e.target === e.currentTarget) onClose?.(); }}
    >
      <div
        className="relative w-[90%] max-w-2xl max-h-[90vh] overflow-auto bg-white dark:bg-cm-gray-dark-2 rounded-lg shadow-2xl"
        role="dialog"
        aria-modal="true"
        aria-label="Announcement preview"
      >
        <div className="flex items-start justify-between px-5 pt-5 pb-2 border-b border-cm-gray-200 dark:border-cm-gray-dark-3">
          <h4 className="text-base font-semibold text-cm-gray-900 dark:text-white pr-6">
            {announcement.title || "(untitled)"}
          </h4>
          <button
            type="button"
            aria-label="Close preview"
            className="text-cm-gray-500 hover:text-cm-gray-700 dark:text-cm-gray-dark-5 cursor-pointer"
            onClick={onClose}
          >
            <FontAwesomeIcon icon={faXmark} size="lg" />
          </button>
        </div>

        <div className="flex flex-col gap-4 p-5">
          {announcement.categories && announcement.categories.length > 0 && (
            <div className="flex items-center gap-2 flex-wrap">
              {announcement.categories.map((cat) => (
                <AnnouncementTag key={cat.name} category={cat} />
              ))}
            </div>
          )}

          {dateLabel && (
            <span className="text-xs text-cm-gray-500 dark:text-cm-gray-dark-5">
              {dateLabel}
            </span>
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
        </div>
      </div>
    </div>
  );
}

export default AnnouncementPopupPreview;
