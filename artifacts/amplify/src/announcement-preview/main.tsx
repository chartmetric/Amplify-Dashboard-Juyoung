// React island mounted by templates/announcements.html. Driven by the host
// page via `window.AnnouncementPreview.update({ data, show })`.
import { createRoot, type Root } from "react-dom/client";

import AnnouncementPopupPreview from "./AnnouncementPopupPreview";
import "./index.css";

import type { AnnouncementPreviewData, ContentBlock } from "./types";

const MOUNT_ID = "announcement-popup-preview-root";

interface UpdatePayload {
  data: AnnouncementPreviewData;
  show: boolean;
}

interface PreviewApi {
  update: (payload: UpdatePayload) => void;
  open: () => void;
  close: () => void;
}

declare global {
  interface Window {
    AnnouncementPreview?: PreviewApi;
  }
}

const EMPTY_DATA: AnnouncementPreviewData = {
  title: "",
  content: [{ type: "paragraph", children: [{ text: "" }] }] as ContentBlock[],
  image_url: null,
  is_pinned: false,
  published_at: null,
  categories: [],
};

let root: Root | null = null;
let currentState: UpdatePayload = { data: EMPTY_DATA, show: false };

function handleClose() {
  currentState = { ...currentState, show: false };
  render();
}

function render() {
  if (!root) return;
  root.render(
    <AnnouncementPopupPreview
      announcement={currentState.data}
      show={currentState.show}
      onClose={handleClose}
    />,
  );
}

function ensureMounted() {
  const el = document.getElementById(MOUNT_ID);
  if (!el) return;
  if (!root) {
    root = createRoot(el);
    render();
  }
}

function init() {
  ensureMounted();
  window.AnnouncementPreview = {
    update(payload: UpdatePayload) {
      ensureMounted();
      currentState = payload;
      render();
    },
    open() {
      currentState = { ...currentState, show: true };
      render();
    },
    close() {
      handleClose();
    },
  };
  document.dispatchEvent(new CustomEvent("announcement-preview:ready"));
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
