// Entry point for the announcement preview widget.
//
// The /announcements admin page in this repo is a Flask + Jinja template with vanilla JS
// (templates/announcements.html). This bundle is a small React island that the template
// mounts inside its "Live preview" pane. The host page drives it through a global API:
//
//   window.AnnouncementPreview.update({ data, show });
//
// `data` is the current form values converted into the chartmetric-announcement content
// shape, and `show` toggles the modal-style overlay (true when the "Popup" boost type is
// selected). The widget never makes network calls — it's purely presentational.

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

function render() {
  if (!root) return;
  root.render(
    <AnnouncementPopupPreview
      announcement={currentState.data}
      show={currentState.show}
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
  };
  // Let the host page know the widget is ready, so it can push the current form
  // state (e.g. when an existing post was loaded before the bundle finished
  // downloading).
  document.dispatchEvent(new CustomEvent("announcement-preview:ready"));
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
