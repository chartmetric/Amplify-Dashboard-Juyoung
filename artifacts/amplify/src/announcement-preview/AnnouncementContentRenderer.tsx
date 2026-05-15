// Slimmed-down copy of
// chartmetric-announcement/artifacts/web/src/components/announcement/components/AnnouncementContentRenderer.tsx.
// Differences vs. the source:
// - Accepts both "heading-1" / "heading-2" (chartmetric-announcement format) and
//   "heading-one" / "heading-two" / "heading-three" (Amplify's form HTML->slate serializer).

import React from "react";

import type {
  ContentBlock,
  InlineNode,
  LinkNode,
  TextNode,
} from "./types";

function hash(str: string): number {
  return str.split("").reduce((a, b) => (a << 5) - a + b.charCodeAt(0), 0);
}

function getBlockKey(block: ContentBlock, index: number): string {
  if (block.type === "image" && "url" in block) return `img-${block.url}`;
  if (block.type === "video" && "url" in block) return `vid-${block.url}`;
  return `${block.type}-${hash(JSON.stringify(block))}-${index}`;
}

function isLinkNode(node: InlineNode): node is LinkNode {
  return (node as LinkNode).type === "link";
}

function renderTextNode(node: TextNode, index: number): React.ReactNode {
  let element: React.ReactNode = node.text;

  if (node.bold) element = <strong key={index}>{element}</strong>;
  if (node.italic) element = <em key={index}>{element}</em>;
  if (node.underline) element = <u key={index}>{element}</u>;
  if (node.strikethrough) element = <s key={index}>{element}</s>;
  if (node.code) element = <code key={index}>{element}</code>;

  return <React.Fragment key={index}>{element}</React.Fragment>;
}

function renderInlineNode(node: InlineNode, index: number): React.ReactNode {
  if (isLinkNode(node)) {
    return (
      <a
        key={index}
        href={node.url}
        target="_blank"
        rel="noopener noreferrer"
        className="text-cm-teal-600 underline hover:text-cm-teal-700"
      >
        {node.children.map((child, i) => renderTextNode(child, i))}
      </a>
    );
  }
  return renderTextNode(node as TextNode, index);
}

function ContentBlockComponent({
  block,
  index,
}: {
  block: ContentBlock;
  index: number;
}) {
  switch (block.type) {
    case "paragraph":
      return (
        <p key={index} className="mb-3">
          {block.children.map((child, i) => renderInlineNode(child, i))}
        </p>
      );

    case "heading-1":
    case "heading-one":
      return (
        <h2
          key={index}
          className="text-xl font-bold mt-6 mb-3 text-cm-gray-900"
        >
          {block.children.map((child, i) => renderInlineNode(child, i))}
        </h2>
      );

    case "heading-2":
    case "heading-two":
      return (
        <h3
          key={index}
          className="text-lg font-semibold mt-5 mb-2.5 text-cm-gray-900"
        >
          {block.children.map((child, i) => renderInlineNode(child, i))}
        </h3>
      );

    case "heading-three":
      return (
        <h4
          key={index}
          className="text-base font-semibold mt-4 mb-2 text-cm-gray-900"
        >
          {block.children.map((child, i) => renderInlineNode(child, i))}
        </h4>
      );

    case "bulleted-list":
      return (
        <ul key={index} className="list-disc pl-6 mb-3">
          {block.children.map((item) => (
            <li key={hash(JSON.stringify(item))} className="mb-1">
              {item.children.map((child, j) => renderInlineNode(child, j))}
            </li>
          ))}
        </ul>
      );

    case "numbered-list":
      return (
        <ol key={index} className="list-decimal pl-6 mb-3">
          {block.children.map((item) => (
            <li key={hash(JSON.stringify(item))} className="mb-1">
              {item.children.map((child, j) => renderInlineNode(child, j))}
            </li>
          ))}
        </ol>
      );

    case "divider":
      return <hr key={index} className="border-t border-cm-gray-300 my-5" />;

    case "image":
      return (
        <figure key={index} className="m-0 w-full">
          <img
            src={block.url}
            alt={block.alt || ""}
            loading="lazy"
            className="w-full h-auto rounded-lg block"
          />
        </figure>
      );

    case "video":
      return (
        <figure key={index} className="my-4">
          <iframe
            src={block.url}
            title="Video"
            allowFullScreen
            frameBorder="0"
            className="w-full aspect-video rounded-lg border-0"
          />
        </figure>
      );

    case "announcement-title":
      return null;

    default:
      return null;
  }
}

interface AnnouncementContentRendererProps {
  content: ContentBlock[];
}

function AnnouncementContentRenderer({
  content,
}: AnnouncementContentRendererProps) {
  if (!content || !Array.isArray(content)) return null;

  return (
    <div className="text-sm leading-relaxed text-cm-charcoal-2 dark:text-cm-charcoal-dark-2">
      {content.map((block, index) => (
        <ContentBlockComponent
          key={getBlockKey(block, index)}
          block={block}
          index={index}
        />
      ))}
    </div>
  );
}

export default AnnouncementContentRenderer;
