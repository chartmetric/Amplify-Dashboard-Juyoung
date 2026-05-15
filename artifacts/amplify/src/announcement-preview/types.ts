// Slimmed-down copy of chartmetric-announcement/artifacts/web/src/components/announcement/types.ts.
// Reactions, comments, boost actions, and translation maps are dropped because the
// admin preview only renders the static modal content (title + categories + body + image).

export interface TextNode {
  text: string;
  bold?: boolean;
  italic?: boolean;
  underline?: boolean;
  strikethrough?: boolean;
  code?: boolean;
}

export interface LinkNode {
  type: "link";
  url: string;
  children: TextNode[];
}

export type InlineNode = TextNode | LinkNode;

export interface ParagraphBlock {
  type: "paragraph";
  children: InlineNode[];
}

export interface HeadingBlock {
  // The chartmetric-announcement renderer uses "heading-1" / "heading-2".
  // Amplify's existing form HTML->slate serializer emits "heading-one" / "heading-two" /
  // "heading-three" — both shapes are accepted so the preview can render either.
  type:
    | "heading-1"
    | "heading-2"
    | "heading-one"
    | "heading-two"
    | "heading-three";
  children: InlineNode[];
}

export interface ListItemBlock {
  type: "list-item";
  children: InlineNode[];
}

export interface BulletedListBlock {
  type: "bulleted-list";
  children: ListItemBlock[];
}

export interface NumberedListBlock {
  type: "numbered-list";
  children: ListItemBlock[];
}

export interface DividerBlock {
  type: "divider";
  children: [{ text: "" }];
}

export interface ImageBlock {
  type: "image";
  url: string;
  alt?: string;
  children: [{ text: "" }];
}

export interface VideoBlock {
  type: "video";
  url: string;
  children: [{ text: "" }];
}

export interface AnnouncementTitleBlock {
  type: "announcement-title";
  children: InlineNode[];
}

export type ContentBlock =
  | ParagraphBlock
  | HeadingBlock
  | BulletedListBlock
  | NumberedListBlock
  | DividerBlock
  | ImageBlock
  | VideoBlock
  | AnnouncementTitleBlock;

export interface AnnouncementCategoryInfo {
  name: string;
  color: string;
}

export interface AnnouncementPreviewData {
  title: string;
  content: ContentBlock[];
  image_url: string | null;
  is_pinned: boolean;
  published_at: string | null;
  categories: AnnouncementCategoryInfo[];
}
