from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.sync_api import Locator, Page, sync_playwright


FACEBOOK_HOME = "https://www.facebook.com/"
DEFAULT_ENV_FILE = ".env"
EMAIL_SELECTOR = "input[name='email'], input#email"
POST_CLEANUP_SCRIPT = """
(root) => {
  const cleanupText = (value) => {
    const noise = new Set([
      "Like", "Reply", "Share", "Comment", "Send", "Follow", "Following",
      "Author", "Admin", "Top contributor", "All reactions:", "Most relevant",
      "Write a comment...", "View more comments", "View previous comments"
    ]);
    return (value || "")
      .split(/\\n+/)
      .map((line) => line.replace(/\\s+/g, " ").trim())
      .filter((line) => line && !noise.has(line))
      .filter((line) => !/^\\d+\\s*(Like|Reply|Share|Comment)s?$/i.test(line))
      .filter((line) => !/^(Like|Reply|Share|Comment)\\s*/i.test(line))
      .join("\\n")
      .trim();
  };

  const clone = root.cloneNode(true);
  clone.querySelectorAll('[role="article"]').forEach((node) => {
    if (node !== clone) node.remove();
  });
  clone.querySelectorAll('[role="button"], button, form, input, textarea, [aria-label="Like"], [aria-label="Comment"], [aria-label="Share"]').forEach((node) => node.remove());

  const rawText = cleanupText(clone.innerText);
  const authorNode = root.querySelector('h2 a, h3 a, strong a, a[role="link"]');
  const author = authorNode ? cleanupText(authorNode.innerText) : null;
  const urlNode = Array.from(root.querySelectorAll('a[href]')).find((node) => {
    const href = node.href || "";
    return href.includes("/posts/") || href.includes("story_fbid") || href.includes("permalink");
  });
  const timeNode = Array.from(root.querySelectorAll('a, span')).find((node) => {
    const value = node.getAttribute("aria-label") || node.getAttribute("title") || node.innerText || "";
    return /\\b(Just now|\\d+\\s*(m|h|d|w|mo|y)|Yesterday|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\\b/i.test(value);
  });
  const postedAt = timeNode ? cleanupText(timeNode.getAttribute("aria-label") || timeNode.getAttribute("title") || timeNode.innerText) : null;

  let text = rawText;
  for (const value of [author, postedAt]) {
    if (value) text = text.replace(value, "").trim();
  }

  return {
    author,
    postedAt,
    url: urlNode ? urlNode.href : null,
    text: cleanupText(text),
    rawText
  };
}
"""

COMMENT_CLEANUP_SCRIPT = """
(root) => {
  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
  const timeRegex = /^(?:just now|yesterday(?: at .*)?|today(?: at .*)?|(?:a|an|\\d+)\\s+(?:second|minute|hour|day|week|month|year)s?\\s+ago|\\d+\\s*(?:m|h|d|w|mo|y)|[A-Z][a-z]{2,8}\\s+\\d{1,2}(?:\\s+at\\s+.*)?|\\d{1,2}\\s+[A-Z][a-z]{2,8}(?:\\s+at\\s+.*)?)$/i;

  const cleanupText = (value, author, postedAt) => {
    const noise = new Set([
      "Like", "Reply", "Share", "Comment", "Author", "Admin", "Top contributor",
      "Edited", "Follow", "Following", "Write a comment..."
    ]);

    return (value || "")
      .split(/\\n+/)
      .map((line) => clean(line.replace(/\\b(Like|Reply|Share)\\b/g, " ")))
      .filter((line) => line && !noise.has(line))
      .filter((line) => !/^\\d+\\s*(like|reaction)s?$/i.test(line))
      .filter((line) => !timeRegex.test(line))
      .filter((line) => !author || line !== author)
      .filter((line) => !postedAt || line !== postedAt)
      .join("\\n")
      .trim();
  };

  const ariaLabel = clean(root.getAttribute("aria-label") || "");
  let ariaAuthor = null;
  let ariaTime = null;
  const ariaMatch = ariaLabel.match(/^Comment by\\s+(.+?)\\s+((?:just now|yesterday(?: at .*)?|today(?: at .*)?|(?:a|an|\\d+)\\s+(?:second|minute|hour|day|week|month|year)s?\\s+ago|\\d+\\s*(?:m|h|d|w|mo|y)))$/i);
  if (ariaMatch) {
    ariaAuthor = clean(ariaMatch[1]);
    ariaTime = clean(ariaMatch[2]);
  }

  const authorNode =
    root.querySelector('a[role="link"][href*="/user/"]') ||
    root.querySelector('strong a, span[dir="auto"] a, a[role="link"]');
  const author = authorNode ? clean(authorNode.innerText) : ariaAuthor;

  const timeNode = Array.from(root.querySelectorAll('a[href*="comment_id"], a[href*="/posts/"], a, span')).find((node) => {
    const value = clean(node.getAttribute("aria-label") || node.getAttribute("title") || node.innerText || node.textContent || "");
    return timeRegex.test(value);
  });
  const postedAt = timeNode
    ? clean(timeNode.getAttribute("aria-label") || timeNode.getAttribute("title") || timeNode.innerText || timeNode.textContent)
    : ariaTime;

  const reactionNode = Array.from(root.querySelectorAll('[aria-label], span, div')).find((node) => {
    const value = clean(node.getAttribute("aria-label") || node.innerText || node.textContent || "");
    return /^\\d+\\s*(?:like|reaction)s?$/i.test(value) || /^\\d+$/.test(value);
  });
  const reactionsText = reactionNode ? clean(reactionNode.getAttribute("aria-label") || reactionNode.innerText || reactionNode.textContent) : null;

  const clone = root.cloneNode(true);
  clone.querySelectorAll('[role="article"]').forEach((node) => {
    if (node !== clone) node.remove();
  });
  clone.querySelectorAll('[role="button"], button, form, input, textarea').forEach((node) => node.remove());

  const rawText = clean(clone.innerText);
  let text = cleanupText(rawText, author, postedAt);
  const images = Array.from(root.querySelectorAll('img'))
    .map((img) => ({
      src: img.currentSrc || img.src || null,
      alt: clean(img.getAttribute("alt") || ""),
      width: Number(img.getAttribute("width") || img.naturalWidth || 0) || null,
      height: Number(img.getAttribute("height") || img.naturalHeight || 0) || null,
      perfLogName: img.getAttribute("data-imgperflogname") || null
    }))
    .filter((img) => img.src && !img.src.startsWith("data:image/svg"))
    .filter((img) => img.alt || /fbcdn|scontent/i.test(img.src || ""));

  return { author, postedAt, reactionsText, text, rawText, ariaLabel, images };
}
"""

TEXT_PARTS_SCRIPT = """
(root) => {
  const noise = new Set([
    "Like", "Reply", "Share", "Comment", "Send", "Follow", "Following",
    "Author", "Admin", "Top contributor", "Most relevant", "All comments",
    "Write a comment...", "View more comments", "View previous comments"
  ]);
  const parts = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const value = walker.currentNode.textContent.replace(/\\s+/g, " ").trim();
    if (!value || noise.has(value)) continue;
    if (/^(Like|Reply|Share|Comment|Send)$/i.test(value)) continue;
    parts.push(value);
  }
  return [...new Set(parts)];
}
"""

IMAGE_DETAILS_SCRIPT = """
(root) => {
  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
  const images = Array.from(root.querySelectorAll('img'))
    .map((img) => ({
      src: img.currentSrc || img.src || null,
      alt: clean(img.getAttribute("alt") || ""),
      width: Number(img.getAttribute("width") || img.naturalWidth || 0) || null,
      height: Number(img.getAttribute("height") || img.naturalHeight || 0) || null,
      perfLogName: img.getAttribute("data-imgperflogname") || null
    }))
    .filter((img) => img.src && !img.src.startsWith("data:image/svg"))
    .filter((img) => img.alt || /fbcdn|scontent/i.test(img.src || ""));
  return images.filter((img, index, arr) => {
    const key = `${img.src}|${img.alt}`;
    return arr.findIndex((other) => `${other.src}|${other.alt}` === key) === index;
  });
}
"""

FEED_CARD_SCRIPT = """
(root) => {
  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
  const ignored = new Set([
    "Like", "Reply", "Share", "Comment", "Send", "GIF", "Author", "Admin",
    "Comment as Dev", "Write a comment..."
  ]);
  const lines = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const value = clean(walker.currentNode.textContent);
    if (!value || ignored.has(value)) continue;
    if (/^(Like|Reply|Share|Comment|Send|GIF)$/i.test(value)) continue;
    if (/^\\.\\s*\\.$/.test(value)) continue;
    lines.push(value);
  }
  const unique = [...new Set(lines)];
  const author = unique.find((line) => !/^(\\d+|\\.|Admin|Author|created the group)/i.test(line)) || null;
  const postedAt = unique.find((line) => /\\b(\\d+\\s*(m|h|d|w)|\\d{1,2}\\s+[A-Z][a-z]+\\s+at\\s+\\d{1,2}:\\d{2}|Yesterday|Just now)\\b/i.test(line)) || null;
  const urlNode = Array.from(root.querySelectorAll('a[href]')).find((node) => {
    const href = node.href || "";
    return href.includes("/posts/") || href.includes("/permalink/") || href.includes("story_fbid");
  });
  const reactionFromAria = Array.from(root.querySelectorAll('[aria-label]'))
    .map((node) => clean(node.getAttribute("aria-label")))
    .find((value) => /^(?:Like|Love|Care|Haha|Wow|Sad|Angry):\\s*[\\d,.]+\\s+people?/i.test(value));
  const images = Array.from(root.querySelectorAll('img'))
    .map((img) => ({
      src: img.currentSrc || img.src || null,
      alt: clean(img.getAttribute("alt") || ""),
      width: Number(img.getAttribute("width") || img.naturalWidth || 0) || null,
      height: Number(img.getAttribute("height") || img.naturalHeight || 0) || null,
      perfLogName: img.getAttribute("data-imgperflogname") || null
    }))
    .filter((img) => img.src && !img.src.startsWith("data:image/svg"))
    .filter((img) => img.alt || /fbcdn|scontent/i.test(img.src || ""));
  return {
    author,
    postedAt,
    url: urlNode ? urlNode.href : null,
    parts: unique,
    rawText: unique.join("\\n"),
    reactionsText: reactionFromAria,
    images
  };
}
"""

FEED_CARDS_FROM_PAGE_SCRIPT = """
() => {
  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();

  const timeRegex = /^(?:just now|yesterday(?: at .*)?|today(?: at .*)?|(?:a|an|\\d+)\\s+(?:second|minute|hour|day|week|month|year)s?\\s+ago|\\d+\\s*(?:m|h|d|w|mo|y)|\\d{1,2}\\s+[A-Z][a-z]{2,8}(?:\\s+at\\s+.*)?|[A-Z][a-z]{2,8}\\s+\\d{1,2}(?:\\s+at\\s+.*)?)$/i;

  const noise = new Set([
    "Facebook", "Like", "Reply", "Share", "Comment", "Send", "GIF",
    "Author", "Admin", "Top contributor", "Comment as Dev", "Write a comment...",
    "Visit Group", "Shared with Private group", "Insert an emoji",
    "Comment with a GIF", "Comment with a sticker", "Comment with an avatar sticker",
    "Edit or delete this"
  ]);

  const textParts = (root) => {
    const parts = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const value = clean(walker.currentNode.textContent);
      if (!value || noise.has(value)) continue;
      if (/^(Like|Reply|Share|Comment|Send|GIF|\\.\\.\\.)$/i.test(value)) continue;
      if (/^[a-zA-Z]$/.test(value)) continue;
      parts.push(value);
    }
    return [...new Set(parts)];
  };

  const findCardRoot = (storyNode) => {
    let node = storyNode;
    for (let depth = 0; depth < 18 && node && node.parentElement; depth += 1) {
      const hasStory = !!node.querySelector('[data-ad-rendering-role="story_message"]');
      const hasActions = !!node.querySelector('[aria-label^="Actions for this post by"]');
      const hasCommentButton =
        !!node.querySelector('[data-ad-rendering-role="comment_button"]') ||
        !!node.querySelector('[aria-label="Leave a comment"]') ||
        !!node.querySelector('[aria-label="Comment"]');

      if (hasStory && hasActions && hasCommentButton) {
        return node;
      }
      node = node.parentElement;
    }
    return storyNode.closest('[data-virtualized="false"]') || storyNode.parentElement || storyNode;
  };

  const parseCommentAria = (ariaLabel) => {
    const value = clean(ariaLabel || "");
    const match = value.match(/^Comment by\\s+(.+?)\\s+((?:just now|yesterday(?: at .*)?|today(?: at .*)?|(?:a|an|\\d+)\\s+(?:second|minute|hour|day|week|month|year)s?\\s+ago|\\d+\\s*(?:m|h|d|w|mo|y)))$/i);
    if (match) {
      return { author: clean(match[1]), postedAt: clean(match[2]) };
    }
    if (value.toLowerCase().startsWith("comment by ")) {
      return { author: clean(value.slice("Comment by ".length)), postedAt: null };
    }
    return { author: null, postedAt: null };
  };

  const extractComment = (commentRoot) => {
    const article =
      commentRoot.matches('[role="article"][aria-label^="Comment by"]')
        ? commentRoot
        : commentRoot.querySelector('[role="article"][aria-label^="Comment by"]') || commentRoot;

    const commentIdNode = commentRoot.closest('[data-commentid]') || commentRoot.querySelector('[data-commentid]');
    const commentId = commentIdNode ? commentIdNode.getAttribute("data-commentid") : null;

    const ariaLabel = clean(article.getAttribute("aria-label") || "");
    const parsed = parseCommentAria(ariaLabel);

    const authorNode =
      article.querySelector('a[role="link"][href*="/user/"] span[dir="auto"]') ||
      article.querySelector('a[role="link"][href*="/user/"]') ||
      article.querySelector('strong a, span[dir="auto"] a, a[role="link"]');

    const author = authorNode ? clean(authorNode.innerText || authorNode.textContent) : parsed.author;

    const timeNode = Array.from(article.querySelectorAll('a[href*="comment_id"], a, span')).find((node) => {
      const value = clean(node.getAttribute("aria-label") || node.getAttribute("title") || node.innerText || node.textContent || "");
      return timeRegex.test(value);
    });

    const postedAt = timeNode
      ? clean(timeNode.getAttribute("aria-label") || timeNode.getAttribute("title") || timeNode.innerText || timeNode.textContent)
      : parsed.postedAt;

    const commentUrlNode = article.querySelector('a[href*="comment_id"]');

    const parts = textParts(article).filter((part) => {
      if (author && part === author) return false;
      if (postedAt && part === postedAt) return false;
      if (timeRegex.test(part)) return false;
      if (/^(Author|Admin|Top contributor)$/i.test(part)) return false;
      if (/^(Like|Reply|Share|Comment)$/i.test(part)) return false;
      return true;
    });

    const text = parts.join(" ").trim();
    const images = Array.from(article.querySelectorAll('img'))
      .map((img) => ({
        src: img.currentSrc || img.src || null,
        alt: clean(img.getAttribute("alt") || ""),
        width: Number(img.getAttribute("width") || img.naturalWidth || 0) || null,
        height: Number(img.getAttribute("height") || img.naturalHeight || 0) || null,
        perfLogName: img.getAttribute("data-imgperflogname") || null
      }))
      .filter((img) => img.src && !img.src.startsWith("data:image/svg"))
      .filter((img) => img.alt || /fbcdn|scontent/i.test(img.src || ""));

    const reactionNode = Array.from(article.querySelectorAll('[aria-label], span, div')).find((node) => {
      const value = clean(node.getAttribute("aria-label") || node.innerText || node.textContent || "");
      return /^\\d+\\s*(?:like|likes|reaction|reactions)$/i.test(value);
    });

    return {
      comment_id: commentId,
      author,
      postedAt,
      text,
      url: commentUrlNode ? commentUrlNode.href : null,
      reactionsText: reactionNode ? clean(reactionNode.getAttribute("aria-label") || reactionNode.innerText || reactionNode.textContent) : null,
      images,
      rawText: textParts(article).join("\\n"),
      ariaLabel
    };
  };

  const extractCounterNear = (root, selector, label) => {
    const target = root.querySelector(selector);
    if (!target) return null;

    let node = target;
    for (let depth = 0; depth < 7 && node; depth += 1) {
      const parts = textParts(node);
      const numberOnly = parts.find((part) => /^[\\d,.]+$/.test(part));
      const labelled = parts.find((part) => new RegExp(`^\\\\d+(?:[,.]\\\\d+)*(?:\\\\.\\\\d+)?\\\\s+${label}s?$`, "i").test(part));
      if (labelled) return labelled;
      if (numberOnly) return `${numberOnly} ${label}${numberOnly === "1" ? "" : "s"}`;
      node = node.parentElement;
    }
    return null;
  };

  const countFromText = (value) => {
    const match = clean(value).replace(/,/g, "").match(/(\\d+(?:\\.\\d+)?)/);
    return match ? Number(match[1]) : null;
  };

  const betterCountText = (current, candidate) => {
    const currentCount = countFromText(current);
    const candidateCount = countFromText(candidate);
    if (candidateCount === null) return current;
    if (currentCount === null || candidateCount > currentCount) return candidate;
    return current;
  };

  const extractReactionText = (root, rawParts) => {
    const labelled =
      rawParts.find((part) => /^All reactions:/i.test(part)) ||
      rawParts.find((part) => /^\\d+\\s*(?:like|likes|reaction|reactions)$/i.test(part));
    if (labelled) return labelled;

    const ariaLabel = Array.from(root.querySelectorAll('[aria-label]'))
      .map((node) => clean(node.getAttribute("aria-label")))
      .filter((value) => /^(?:Like|Love|Care|Haha|Wow|Sad|Angry):\\s*[\\d,.]+\\s+people?/i.test(value))
      .sort((a, b) => {
        const count = (value) => Number((value.match(/[\\d,.]+/) || ["0"])[0].replace(/,/g, ""));
        return count(b) - count(a);
      })[0];
    return betterCountText(
      extractCounterNear(root, '[data-ad-rendering-role="like_button"], [aria-label="Like"], [aria-label="React"]', "reaction"),
      ariaLabel
    );
  };

  const cards = [];
  const seen = new Set();
  const storyNodes = Array.from(document.querySelectorAll('[data-ad-rendering-role="story_message"]'));

  for (const storyNode of storyNodes) {
    const card = findCardRoot(storyNode);
    if (!card) continue;

    const rect = card.getBoundingClientRect();
    const key = `${Math.round(rect.top)}:${Math.round(rect.left)}:${clean(storyNode.innerText || storyNode.textContent)}`;
    if (seen.has(key)) continue;
    seen.add(key);

    const actionNode = card.querySelector('[aria-label^="Actions for this post by"]');
    const actionLabel = actionNode ? clean(actionNode.getAttribute("aria-label")) : "";
    const actionAuthor = actionLabel.replace(/^Actions for this post by\\s+/i, "").trim() || null;

    const authorNode =
      card.querySelector('a[role="link"][aria-label]') ||
      card.querySelector('a[role="link"][href*="/user/"] span[dir="auto"]') ||
      card.querySelector('a[role="link"][href*="/user/"]');

    const author = actionAuthor || (authorNode ? clean(authorNode.getAttribute("aria-label") || authorNode.innerText || authorNode.textContent) : null);

    const storyTexts = Array.from(card.querySelectorAll('[data-ad-rendering-role="story_message"]'))
      .map((node) => clean(node.innerText || node.textContent))
      .filter(Boolean);

    const urlNode = Array.from(card.querySelectorAll('a[href]')).find((node) => {
      const href = node.href || node.getAttribute("href") || "";
      return href.includes("/posts/") || href.includes("/permalink/") || href.includes("story_fbid");
    });

    const timeNode = Array.from(card.querySelectorAll('a, span')).find((node) => {
      const value = clean(node.getAttribute("aria-label") || node.getAttribute("title") || node.innerText || node.textContent || "");
      return timeRegex.test(value);
    });

    const postedAt = timeNode
      ? clean(timeNode.getAttribute("aria-label") || timeNode.getAttribute("title") || timeNode.innerText || timeNode.textContent)
      : null;

    const comments = Array.from(card.querySelectorAll('[data-commentid], [role="article"][aria-label^="Comment by"]'))
      .map(extractComment)
      .filter((comment, index, arr) => {
        if (!comment.text && !comment.author) return false;
        const key = `${comment.comment_id || ""}|${comment.author || ""}|${comment.postedAt || ""}|${comment.text || ""}`;
        return arr.findIndex((other) => `${other.comment_id || ""}|${other.author || ""}|${other.postedAt || ""}|${other.text || ""}` === key) === index;
      });

    const rawParts = textParts(card);
    const reactionText = extractReactionText(card, rawParts);

    const commentCountText =
      rawParts.find((part) => /^\\d+\\s+comments?$/i.test(part)) ||
      extractCounterNear(card, '[data-ad-rendering-role="comment_button"], [aria-label="Leave a comment"]', "comment");

    const shareCountText =
      rawParts.find((part) => /^\\d+\\s+shares?$/i.test(part)) ||
      extractCounterNear(card, '[data-ad-rendering-role="share_button"], [aria-label="Share"]', "share");

    const parts = [author, postedAt, ...storyTexts].filter(Boolean);
    const images = Array.from(card.querySelectorAll('img'))
      .map((img) => ({
        src: img.currentSrc || img.src || null,
        alt: clean(img.getAttribute("alt") || ""),
        width: Number(img.getAttribute("width") || img.naturalWidth || 0) || null,
        height: Number(img.getAttribute("height") || img.naturalHeight || 0) || null,
        perfLogName: img.getAttribute("data-imgperflogname") || null
      }))
      .filter((img) => img.src && !img.src.startsWith("data:image/svg"))
      .filter((img) => img.alt || /fbcdn|scontent/i.test(img.src || ""));

    cards.push({
      author,
      postedAt,
      url: urlNode ? urlNode.href : null,
      parts,
      text: storyTexts.join("\\n").trim(),
      rawText: rawParts.join("\\n"),
      reactionsText: reactionText,
      commentCountText,
      shareCountText,
      comments,
      images,
      top: rect.top,
      left: rect.left
    });
  }

  return cards.sort((a, b) => a.top - b.top || a.left - b.left);
}
"""

OPEN_ALL_COMMENTS_SORT_SCRIPT = """
() => {
  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
  const visible = (node) => {
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const labels = ["Most relevant", "Newest", "All comments"];
  const matchingLabel = (text) => labels.find((label) => text === label || text.startsWith(`${label} `));
  const candidates = Array.from(document.querySelectorAll('span, div, [role="button"]'))
    .filter((node) => matchingLabel(clean(node.textContent)))
    .filter(visible)
    .filter((node) => {
      const rect = node.getBoundingClientRect();
      return rect.width < 600 && rect.height < 120;
    });
  const trigger = candidates[candidates.length - 1];
  if (!trigger) return "not_found";
  const current = matchingLabel(clean(trigger.textContent));
  if (/^All comments$/i.test(current)) return "already_all_comments";
  trigger.click();
  return `opened_${current}`;
}
"""

SELECT_ALL_COMMENTS_SORT_SCRIPT = """
() => {
  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
  const visible = (node) => {
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const candidates = Array.from(document.querySelectorAll('span, div, [role="menuitem"], [role="option"]'))
    .filter((node) => {
      const text = clean(node.textContent);
      return /^All comments$/i.test(text) || /^All comments\\s+/i.test(text);
    })
    .filter(visible)
    .filter((node) => {
      const rect = node.getBoundingClientRect();
      return rect.width < 700 && rect.height < 180;
    });
  const option = candidates[candidates.length - 1];
  if (!option) return false;
  option.click();
  return true;
}
"""



@dataclass
class Post:
    id: str
    author: str | None
    posted_at: str | None
    url: str | None
    text: str
    raw_text: str
    text_parts: list[str] = field(default_factory=list)
    reactions_text: str | None = None
    comment_count_text: str | None = None
    share_count_text: str | None = None
    comments: list[dict[str, str | None]] = field(default_factory=list)
    images: list[dict[str, object]] = field(default_factory=list)


@dataclass
class GroupMetadata:
    url: str
    member_count_text: str | None = None


@dataclass
class DebugPaths:
    text_dump: str | None = None
    screenshot: str | None = None


def load_env_file(path: str = DEFAULT_ENV_FILE) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def parse_args() -> argparse.Namespace:
    load_env_file()
    parser = argparse.ArgumentParser(
        description="Export visible Facebook group posts and comments using a saved browser session."
    )
    parser.add_argument("--group-url", default=os.getenv("FACEBOOK_GROUP_URL"))
    parser.add_argument(
        "--profile-dir",
        default=os.getenv("BROWSER_PROFILE_DIR", "browser-profile"),
    )
    parser.add_argument("--facebook-email", default=os.getenv("FACEBOOK_EMAIL"))
    parser.add_argument(
        "--output-json",
        default=os.getenv("OUTPUT_JSON", "fb_group_posts.json"),
    )
    parser.add_argument(
        "--output-csv",
        default=os.getenv("OUTPUT_CSV", "fb_group_posts.csv"),
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=int(os.getenv("MAX_POSTS", "25")),
    )
    parser.add_argument(
        "--max-comments",
        type=int,
        default=int(os.getenv("MAX_COMMENTS_PER_POST", "100")),
        help="Maximum comments/replies per post. Use 0 for no scraper-side limit.",
    )
    parser.add_argument(
        "--comment-expand-rounds",
        type=int,
        default=int(os.getenv("COMMENT_EXPAND_ROUNDS", "6")),
        help="How many times to click comment/reply expansion controls per visible post. Use 0 to expand until no more controls are found.",
    )
    parser.add_argument(
        "--scrolls",
        type=int,
        default=int(os.getenv("SCROLLS", "12")),
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=env_bool("HEADLESS", False),
    )
    parser.add_argument(
        "--debug-dir",
        default=os.getenv("DEBUG_DIR", "debug"),
        help="Directory for page text dumps and screenshots when extraction is incomplete.",
    )
    parser.add_argument(
        "--extra-post-urls",
        default=os.getenv("EXTRA_POST_URLS", ""),
        help="Comma/newline-separated Facebook post or permalink URLs to include.",
    )
    return parser.parse_args()


def validate_facebook_group_url(group_url: str | None) -> str:
    if not group_url:
        raise SystemExit("Missing group URL. Add FACEBOOK_GROUP_URL to .env or pass --group-url.")

    parsed = urlparse(group_url)
    host = parsed.netloc.lower()

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Group URL must start with http:// or https://.")

    if host not in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
        raise ValueError("Group URL must be on facebook.com.")

    if not parsed.path.startswith("/groups/"):
        raise ValueError("Group URL path must start with /groups/.")

    return group_url


def group_id_from_url(group_url: str) -> str:
    match = re.search(r"/groups/([^/?#]+)", group_url)
    if not match:
        raise ValueError("Could not read group ID from group URL.")
    return match.group(1)


def normalize_post_url(href: str, group_id: str) -> str | None:
    href = urljoin("https://www.facebook.com", href)
    parsed = urlparse(href)
    path = parsed.path
    query = parse_qs(parsed.query)

    patterns = [
        rf"/groups/{re.escape(group_id)}/posts/([^/?#]+)",
        rf"/groups/{re.escape(group_id)}/permalink/([^/?#]+)",
    ]
    post_id = None

    for pattern in patterns:
        match = re.search(pattern, path)
        if match:
            post_id = match.group(1)
            break

    if not post_id:
        for key in ("story_fbid", "multi_permalinks"):
            values = query.get(key)
            if values:
                post_id = values[0].split(",")[0]
                break

    if not post_id:
        match = re.search(r"/permalink\.php$", path)
        if match and query.get("story_fbid"):
            post_id = query["story_fbid"][0]

    if not post_id or post_id == group_id:
        return None

    post_id = re.sub(r"[^A-Za-z0-9_:.-]", "", post_id)
    return f"https://www.facebook.com/groups/{group_id}/posts/{post_id}/"


def mobile_url(url: str) -> str:
    parsed = urlparse(url)
    return f"https://m.facebook.com{parsed.path}"


def parse_extra_post_urls(value: str, group_id: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[\s,]+", value):
        if not item.strip():
            continue
        url = normalize_post_url(item.strip(), group_id)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def merge_urls(*url_groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for urls in url_groups:
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            merged.append(url)
    return merged


def is_login_or_checkpoint(page: Page) -> bool:
    url = page.url.lower()
    return "/login" in url or "/checkpoint" in url


def prefill_login_email(page: Page, email: str | None) -> None:
    if not email:
        return

    email_input = page.locator(EMAIL_SELECTOR).first
    try:
        email_input.wait_for(state="visible", timeout=5_000)
        email_input.fill(email)
        print("Prefilled Facebook email. Enter your password/2FA manually in the browser.")
    except Exception:
        print("Could not find a visible Facebook email field to prefill.")


def click_visible_buttons(page: Page, patterns: list[str], max_clicks: int = 10) -> int:
    clicked = 0
    for pattern in patterns:
        buttons = page.get_by_role("button", name=pattern)
        for index in range(min(buttons.count(), max_clicks - clicked)):
            try:
                button = buttons.nth(index)
                if button.is_visible(timeout=500):
                    button.click(timeout=1_000)
                    clicked += 1
                    page.wait_for_timeout(600)
            except Exception:
                continue

            if clicked >= max_clicks:
                return clicked
    return clicked


def click_visible_buttons_in(locator: Locator, patterns: list[str], max_clicks: int = 10) -> int:
    clicked = 0
    for pattern in patterns:
        controls = locator.get_by_text(re.compile(pattern, re.IGNORECASE))
        count = min(controls.count(), max_clicks - clicked)
        for index in range(count):
            try:
                control = controls.nth(index)
                if control.is_visible(timeout=500):
                    control.click(timeout=1_000)
                    clicked += 1
            except Exception:
                continue

            if clicked >= max_clicks:
                return clicked

    return clicked


def click_first_visible(locator: Locator, timeout: int = 1_000, from_last: bool = False) -> bool:
    count = locator.count()
    indexes = range(count - 1, -1, -1) if from_last else range(count)
    for index in indexes:
        try:
            item = locator.nth(index)
            if item.is_visible(timeout=500):
                item.scroll_into_view_if_needed(timeout=timeout)
                item.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def set_feed_sort_recent(page: Page) -> None:
    sort_labels = re.compile(r"^(Most relevant|Recent activity|New posts)$", re.IGNORECASE)
    try:
        clicked = click_first_visible(page.get_by_text(sort_labels), timeout=3_000, from_last=True)
        if clicked:
            page.wait_for_timeout(700)
    except Exception:
        pass

    for label in ("New posts", "Recent activity"):
        try:
            if click_first_visible(page.get_by_text(label, exact=True), timeout=3_000, from_last=True):
                page.wait_for_timeout(2_000)
                print(f"Feed sort set to {label}.")
                return
        except Exception:
            pass

    print("Could not switch feed sort. Continuing with the current Facebook sort.")


def close_floating_popups(page: Page) -> None:
    for name in ("Close", "Dismiss"):
        try:
            page.get_by_role("button", name=name).last.click(timeout=700)
            page.wait_for_timeout(300)
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    except Exception:
        pass


def expand_visible_content(page: Page) -> int:
    return click_visible_buttons(
        page,
        [
            "See more",
            "View more comments",
            "View previous comments",
        ],
        max_clicks=12,
    )


def set_comments_sort_all(page: Page) -> None:
    try:
        state = page.evaluate(OPEN_ALL_COMMENTS_SORT_SCRIPT)
        if state == "already_all_comments":
            print("Comments sort already set to All comments.")
            return
        if state and state.startswith("opened_"):
            page.wait_for_timeout(700)
            if page.evaluate(SELECT_ALL_COMMENTS_SORT_SCRIPT):
                page.wait_for_timeout(1_200)
                print("Comments sort set to All comments.")
                return
    except Exception:
        pass

    sort_label = re.compile(r"^(Most relevant|Newest|All comments)$", re.IGNORECASE)
    triggers = [
        page.get_by_role("button", name=sort_label),
        page.locator("span, div").filter(has_text=sort_label),
        page.get_by_text(sort_label),
    ]

    opened = False
    for trigger in triggers:
        if click_first_visible(trigger, timeout=2_000, from_last=True):
            opened = True
            page.wait_for_timeout(700)
            break

    if not opened:
        return

    all_comments = [
        page.get_by_role("menuitem", name=re.compile(r"^All comments$", re.IGNORECASE)),
        page.get_by_text("All comments", exact=True),
        page.locator("span, div").filter(has_text=re.compile(r"^All comments$", re.IGNORECASE)),
    ]
    for option in all_comments:
        if click_first_visible(option, timeout=2_000, from_last=True):
            page.wait_for_timeout(1_200)
            print("Comments sort set to All comments.")
            return


def scroll_comments_down(page: Page) -> dict[str, int]:
    try:
        return page.evaluate(
            """
            () => {
              const beforeY = Math.round(window.scrollY);
              const beforeHeight = Math.round(document.documentElement.scrollHeight || document.body.scrollHeight || 0);
              window.scrollBy(0, Math.max(1600, Math.floor(window.innerHeight * 1.8)));
              const containers = Array.from(document.querySelectorAll('[role="dialog"], [role="main"], div'))
                .filter((node) => node.scrollHeight > node.clientHeight + 300)
                .sort((a, b) => b.scrollHeight - a.scrollHeight);
              for (const container of containers.slice(0, 5)) {
                container.scrollTop = Math.min(container.scrollTop + Math.max(1200, container.clientHeight * 1.6), container.scrollHeight);
              }
              return {
                beforeY,
                afterY: Math.round(window.scrollY),
                beforeHeight,
                afterHeight: Math.round(document.documentElement.scrollHeight || document.body.scrollHeight || 0)
              };
            }
            """
        )
    except Exception:
        page.mouse.wheel(0, 2_500)
        return {"beforeY": 0, "afterY": 0, "beforeHeight": 0, "afterHeight": 0}


def expand_post_page_comments(page: Page, rounds: int) -> None:
    set_comments_sort_all(page)
    max_rounds = rounds if rounds > 0 else 100
    idle_rounds = 0
    for round_index in range(max_rounds):
        clicked = expand_visible_content(page)
        if round_index == 0 or round_index % 3 == 2:
            set_comments_sort_all(page)
        clicked += click_visible_buttons(
            page,
            [
                "View more comments",
                "View previous comments",
                "View more replies",
                "See more",
            ],
            max_clicks=20,
        )
        scroll_state = scroll_comments_down(page)
        page.wait_for_timeout(1_000)
        if rounds <= 0:
            scrolled = (
                scroll_state.get("afterY") != scroll_state.get("beforeY")
                or scroll_state.get("afterHeight") != scroll_state.get("beforeHeight")
            )
            if clicked == 0 and not scrolled:
                idle_rounds += 1
            else:
                idle_rounds = 0
            if idle_rounds >= 3:
                break


def expand_article_comments(page: Page, article: Locator, rounds: int) -> None:
    patterns = [
        r"see more",
        r"view more comments",
        r"view previous comments",
        r"view more replies",
        r"view \d+ more repl",
        r"\d+ repl",
        r"more comments",
    ]

    max_rounds = rounds if rounds > 0 else 100
    idle_rounds = 0
    for _ in range(max_rounds):
        clicked = click_visible_buttons_in(article, patterns, max_clicks=20)
        try:
            article.evaluate(
                """
                (node) => {
                  node.scrollIntoView({ block: "center" });
                  const containers = Array.from(node.querySelectorAll('div'))
                    .filter((item) => item.scrollHeight > item.clientHeight + 200)
                    .sort((a, b) => b.scrollHeight - a.scrollHeight);
                  for (const container of containers.slice(0, 3)) {
                    container.scrollTop = Math.min(container.scrollTop + Math.max(800, container.clientHeight * 1.5), container.scrollHeight);
                  }
                }
                """
            )
        except Exception:
            pass
        if rounds > 0 and clicked == 0:
            break
        if rounds <= 0:
            if clicked == 0:
                idle_rounds += 1
            else:
                idle_rounds = 0
            if idle_rounds >= 3:
                break
        page.wait_for_timeout(900)


def comment_key(comment: dict[str, str | None]) -> str:
    return "|".join(
        [
            comment.get("author") or "",
            comment.get("posted_at") or "",
            comment.get("text") or "",
        ]
    )


def merge_comment_lists(
    primary: list[dict[str, str | None]],
    secondary: list[dict[str, str | None]],
    max_comments: int,
) -> list[dict[str, str | None]]:
    merged: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for comment in [*primary, *secondary]:
        key = comment_key(comment)
        if not key.strip("|") or key in seen:
            continue
        seen.add(key)
        merged.append(comment)
        if max_comments > 0 and len(merged) >= max_comments:
            break
    return merged


def better_count_text(current: str | None, candidate: str | None) -> str | None:
    current_count = parse_count_text(current) if current else None
    candidate_count = parse_count_text(candidate) if candidate else None
    if candidate_count is None:
        return current
    if current_count is None or candidate_count > current_count:
        return candidate
    return current


def comment_image_text(images: object) -> str:
    if not isinstance(images, list):
        return ""

    parts = []
    for image in images:
        if not isinstance(image, dict):
            continue
        alt = str(image.get("alt") or "").strip()
        if alt:
            parts.append(alt)
    return " ".join(parts).strip()


def collect_comments_while_expanding(
    page: Page,
    max_comments: int,
    rounds: int,
) -> list[dict[str, str | None]]:
    comments: list[dict[str, str | None]] = []
    set_comments_sort_all(page)
    max_rounds = rounds if rounds > 0 else 100
    idle_rounds = 0

    for round_index in range(max_rounds):
        comments = merge_comment_lists(comments, extract_comments_from_page(page, max_comments), max_comments)
        if max_comments > 0 and len(comments) >= max_comments:
            break

        clicked = expand_visible_content(page)
        if round_index == 0 or round_index % 3 == 2:
            set_comments_sort_all(page)
        clicked += click_visible_buttons(
            page,
            [
                "View more comments",
                "View previous comments",
                "View more replies",
                "See more",
            ],
            max_clicks=20,
        )
        page.wait_for_timeout(700)
        comments = merge_comment_lists(comments, extract_comments_from_page(page, max_comments), max_comments)
        scroll_state = scroll_comments_down(page)
        page.wait_for_timeout(1_000)
        comments = merge_comment_lists(comments, extract_comments_from_page(page, max_comments), max_comments)

        if rounds <= 0:
            scrolled = (
                scroll_state.get("afterY") != scroll_state.get("beforeY")
                or scroll_state.get("afterHeight") != scroll_state.get("beforeHeight")
            )
            if clicked == 0 and not scrolled:
                idle_rounds += 1
            else:
                idle_rounds = 0
            if idle_rounds >= 3:
                break
        elif clicked == 0:
            break

    print(f"Progressive comment scan collected {len(comments)} unique rows.")
    return comments


def text_from(locator: Locator) -> str:
    try:
        return " ".join(locator.inner_text(timeout=1_500).split())
    except Exception:
        return ""


COMMENT_TIME_PATTERN = re.compile(
    r"^(?:just now|yesterday(?: at .*)?|today(?: at .*)?|(?:a|an|\d+)\s+"
    r"(?:second|minute|hour|day|week|month|year)s?\s+ago|\d+\s*(?:m|h|d|w|mo|y)|"
    r"[A-Z][a-z]{2,8}\s+\d{1,2}(?:\s+at\s+.*)?|\d{1,2}\s+[A-Z][a-z]{2,8}(?:\s+at\s+.*)?)$",
    re.IGNORECASE,
)


def parse_comment_aria_label(aria_label: str) -> tuple[str | None, str | None]:
    value = " ".join((aria_label or "").split())
    if not value.lower().startswith("comment by "):
        return None, None

    body = value[len("Comment by ") :]
    patterns = [
        r"^(.+?)\s+(just now)$",
        r"^(.+?)\s+(yesterday(?: at .*)?)$",
        r"^(.+?)\s+(today(?: at .*)?)$",
        r"^(.+?)\s+((?:a|an|\d+)\s+(?:second|minute|hour|day|week|month|year)s?\s+ago)$",
        r"^(.+?)\s+(\d+\s*(?:m|h|d|w|mo|y))$",
    ]
    for pattern in patterns:
        match = re.match(pattern, body, re.IGNORECASE)
        if match:
            return match.group(1).strip() or None, match.group(2).strip() or None

    return body.strip() or None, None


def clean_comment_text(text: str, author: str | None, posted_at: str | None) -> str:
    cleaned_lines: list[str] = []
    for line in (text or "").splitlines():
        value = re.sub(r"\b(Like|Reply|Share)\b", " ", line, flags=re.IGNORECASE)
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            continue
        if author and value == author:
            continue
        if posted_at and value == posted_at:
            continue
        if COMMENT_TIME_PATTERN.match(value):
            continue
        if re.match(r"^(Author|Admin|Top contributor|Edited|Comment)$", value, re.IGNORECASE):
            continue
        if re.match(r"^\d+\s*(like|reaction)s?$", value, re.IGNORECASE):
            continue
        cleaned_lines.append(value)

    cleaned = "\n".join(cleaned_lines).strip()
    if author and cleaned.startswith(author):
        cleaned = cleaned[len(author) :].strip()
    if posted_at and cleaned.endswith(posted_at):
        cleaned = cleaned[: -len(posted_at)].strip()
    return cleaned


def is_nested_article(article: Locator) -> bool:
    try:
        return bool(article.evaluate("(node) => !!node.parentElement?.closest('[role=\"article\"]')"))
    except Exception:
        return True


def extract_post_details(article: Locator) -> dict[str, str | None]:
    try:
        details = article.evaluate(POST_CLEANUP_SCRIPT)
    except Exception:
        raw_text = text_from(article)
        details = {
            "author": None,
            "postedAt": None,
            "url": None,
            "text": raw_text,
            "rawText": raw_text,
        }

    result = {
        "author": details.get("author"),
        "posted_at": details.get("postedAt"),
        "url": details.get("url"),
        "text": details.get("text") or "",
        "raw_text": details.get("rawText") or "",
    }
    return clean_post_details(result)


def extract_text_parts(article: Locator) -> list[str]:
    try:
        parts = article.evaluate(TEXT_PARTS_SCRIPT)
    except Exception:
        return []
    return [part for part in parts if isinstance(part, str) and part.strip()]


def enrich_details_from_parts(
    details: dict[str, str | None],
    parts: list[str],
) -> dict[str, str | None]:
    if not parts:
        return details

    author = details.get("author")
    posted_at = details.get("posted_at")

    if not author:
        author = next(
            (
                part
                for part in parts[:8]
                if not re.search(r"\b(\d+[mhdw]|members?|comments?|shares?)\b", part, re.I)
            ),
            None,
        )

    if not posted_at:
        posted_at = next(
            (
                part
                for part in parts
                if re.fullmatch(r"(Just now|\d+\s*(m|h|d|w|mo|y)|Yesterday)", part, re.I)
            ),
            None,
        )

    ignored = {
        value
        for value in [
            author,
            posted_at,
            "Author",
            "Admin",
            "Top contributor",
        ]
        if value
    }
    body_parts = [
        part
        for part in parts
        if part not in ignored
        and not re.search(r"^(Like|Reply|Share|Comment|Send)$", part, re.I)
        and not re.search(r"^\d+\s*(comment|share|reaction|like)s?$", part, re.I)
        and not re.search(r"^All reactions:", part, re.I)
    ]

    text = details.get("text") or " ".join(body_parts).strip()
    if author and text.startswith(author):
        text = text[len(author) :].strip()

    enriched = dict(details)
    enriched["author"] = author
    enriched["posted_at"] = posted_at
    enriched["text"] = re.sub(r"\s+", " ", text).strip()
    return enriched


def extract_engagement_text(page: Page, article: Locator) -> dict[str, str | None]:
    text = text_from(article) or text_from(page.locator("body"))
    dom_counts = {"reactions_text": None, "comment_count_text": None, "share_count_text": None}
    try:
        dom_counts = article.evaluate(
            """
            (root) => {
              const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const textParts = (node) => {
                const parts = [];
                const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
                while (walker.nextNode()) {
                  const value = clean(walker.currentNode.textContent);
                  if (value) parts.push(value);
                }
                return [...new Set(parts)];
              };
              const counterNear = (selector, label) => {
                const target = root.querySelector(selector);
                if (!target) return null;
                let node = target;
                for (let depth = 0; depth < 7 && node; depth += 1) {
                  const parts = textParts(node);
                  const labelled = parts.find((part) => new RegExp(`^\\\\d+(?:[,.]\\\\d+)*(?:\\\\.\\\\d+)?\\\\s+${label}s?$`, "i").test(part));
                  const numberOnly = parts.find((part) => /^[\\d,.]+$/.test(part));
                  if (labelled) return labelled;
                  if (numberOnly) return `${numberOnly} ${label}${numberOnly === "1" ? "" : "s"}`;
                  node = node.parentElement;
                }
                return null;
              };
              const countFromText = (value) => {
                const match = clean(value).replace(/,/g, "").match(/(\\d+(?:\\.\\d+)?)/);
                return match ? Number(match[1]) : null;
              };
              const betterCountText = (current, candidate) => {
                const currentCount = countFromText(current);
                const candidateCount = countFromText(candidate);
                if (candidateCount === null) return current;
                if (currentCount === null || candidateCount > currentCount) return candidate;
                return current;
              };
              const ariaReactions = Array.from(root.querySelectorAll('[aria-label]'))
                .map((node) => clean(node.getAttribute("aria-label")))
                .filter((value) => /^(?:Like|Love|Care|Haha|Wow|Sad|Angry):\\s*[\\d,.]+\\s+people?/i.test(value));
              const ariaReaction = ariaReactions.sort((a, b) => {
                const count = (value) => Number((value.match(/[\\d,.]+/) || ["0"])[0].replace(/,/g, ""));
                return count(b) - count(a);
              })[0] || null;
              const visibleReaction = counterNear('[data-ad-rendering-role="like_button"], [aria-label="Like"], [aria-label="React"]', "reaction");
              return {
                reactions_text: betterCountText(visibleReaction, ariaReaction),
                comment_count_text: counterNear('[data-ad-rendering-role="comment_button"], [aria-label="Leave a comment"], [aria-label="Comment"]', "comment"),
                share_count_text: counterNear('[data-ad-rendering-role="share_button"], [aria-label="Share"]', "share")
              };
            }
            """
        )
    except Exception:
        pass
    reaction_match = re.search(
        r"(All reactions:\s*[^\n]+|\b\d+\s*(?:reaction|like)s?\b)",
        text,
        re.IGNORECASE,
    )
    comment_match = re.search(r"\b\d+\s+comments?\b", text, re.IGNORECASE)
    share_match = re.search(r"\b\d+\s+shares?\b", text, re.IGNORECASE)

    return {
        "reactions_text": dom_counts.get("reactions_text") or (reaction_match.group(1) if reaction_match else None),
        "comment_count_text": dom_counts.get("comment_count_text") or (comment_match.group(0) if comment_match else None),
        "share_count_text": dom_counts.get("share_count_text") or (share_match.group(0) if share_match else None),
    }


def extract_images(locator: Locator) -> list[dict[str, object]]:
    try:
        images = locator.evaluate(IMAGE_DETAILS_SCRIPT)
        return images if isinstance(images, list) else []
    except Exception:
        return []


def clean_post_details(details: dict[str, str | None]) -> dict[str, str | None]:
    raw_text = details.get("raw_text") or ""
    author = (details.get("author") or "").strip() or None
    posted_at = (details.get("posted_at") or "").strip() or None
    text = details.get("text") or raw_text

    if not author:
        match = re.match(r"^(.+?)(?:Author|Admin|Top contributor)", raw_text)
        if match:
            author = match.group(1).strip()

    text = re.sub(r"(Author|Admin|Top contributor)", " ", text)
    if author:
        text = text.replace(author, " ")
    if posted_at:
        text = text.replace(posted_at, " ")

    text = re.sub(r"\b(Like|Reply|Share|Comment|Send|Follow|Following)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return {
        "author": author,
        "posted_at": posted_at,
        "url": details.get("url"),
        "text": text,
        "raw_text": re.sub(r"\s+", " ", raw_text).strip(),
    }


def make_post_id(details: dict[str, str | None]) -> str:
    source = "|".join(
        [
            details.get("url") or "",
            details.get("author") or "",
            details.get("posted_at") or "",
            details.get("text") or "",
        ]
    )
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


def post_id_from_url(post_url: str) -> str:
    parsed = urlparse(post_url)
    match = re.search(r"/posts/([^/?#]+)", parsed.path)
    if match:
        return match.group(1)
    return hashlib.sha1(post_url.encode("utf-8")).hexdigest()[:16]


def make_feed_post_id(author: str | None, posted_at: str | None, text: str, index: int) -> str:
    source = "|".join([author or "", posted_at or "", text, str(index)])
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


def get_group_metadata(page: Page, group_url: str) -> GroupMetadata:
    page_text = text_from(page.locator("body"))
    match = re.search(
        r"((?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?:K|M|B)?\s+members?)",
        page_text,
        re.IGNORECASE,
    )
    return GroupMetadata(
        url=group_url,
        member_count_text=match.group(1) if match else None,
    )


def extract_post_urls_from_current_page(page: Page, group_id: str) -> list[str]:
    hrefs = page.locator("a[href]").evaluate_all(
        """(nodes) => nodes.flatMap((node) => [
            node.getAttribute('href') || '',
            node.href || '',
            node.getAttribute('ajaxify') || '',
            node.getAttribute('data-hovercard') || ''
        ]).filter(Boolean)"""
    )

    # Facebook often keeps post ids in hydrated HTML/JSON without normal hrefs.
    try:
        html = page.content()
    except Exception:
        html = ""

    raw_candidates = list(hrefs)
    raw_candidates += re.findall(
        rf"(?:https?:\/\/www\.facebook\.com)?\/groups\/{re.escape(group_id)}\/(?:posts|permalink)\/([A-Za-z0-9_.:-]+)",
        html,
    )
    raw_candidates += re.findall(
        rf"(?:https?://www\.facebook\.com)?/groups/{re.escape(group_id)}/(?:posts|permalink)/([A-Za-z0-9_.:-]+)",
        html,
    )

    urls: list[str] = []
    seen: set[str] = set()
    for value in raw_candidates:
        value = str(value).replace("\\/", "/")
        if re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            value = f"https://www.facebook.com/groups/{group_id}/posts/{value}/"
        url = normalize_post_url(value, group_id)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    print(f"Scanned {len(hrefs)} links plus hydrated HTML, found {len(urls)} post links here.")
    return urls


def collect_post_urls_from_feed(
    page: Page,
    group_id: str,
    max_posts: int,
    scrolls: int,
) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    for scroll in range(scrolls):
        for url in extract_post_urls_from_current_page(page, group_id):
            if url in seen:
                continue

            seen.add(url)
            found.append(url)
            print(f"Found post URL: {url}")

            if len(found) >= max_posts:
                return found

        print(f"Post URL scan {scroll + 1}/{scrolls}: found {len(found)} posts")
        page.mouse.wheel(0, 2_500)
        page.wait_for_timeout(2_000)

    return found


def collect_post_urls_by_clicking_comments(
    page: Page,
    group_id: str,
    max_posts: int,
) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    comment_buttons = page.get_by_text(re.compile(r"^Comment$", re.IGNORECASE))
    count = min(comment_buttons.count(), max_posts * 2)

    for index in range(count):
        try:
            before_url = page.url
            comment_buttons.nth(index).click(timeout=1_500)
            page.wait_for_timeout(1_500)
            url = normalize_post_url(page.url, group_id)
            if not url:
                for href in page.locator("a[href]").evaluate_all(
                    "(nodes) => nodes.map((node) => node.href).filter(Boolean)"
                ):
                    url = normalize_post_url(href, group_id)
                    if url:
                        break

            if url and url not in seen:
                seen.add(url)
                found.append(url)
                print(f"Found post URL from Comment click: {url}")

            if page.url != before_url:
                page.go_back(wait_until="domcontentloaded")
                page.wait_for_timeout(1_000)
        except Exception:
            continue

        if len(found) >= max_posts:
            break

    return found


def collect_post_urls_from_group_surfaces(
    page: Page,
    group_url: str,
    group_id: str,
    max_posts: int,
    scrolls: int,
) -> list[str]:
    surfaces = [
        ("desktop", group_url),
        ("mobile", f"https://m.facebook.com/groups/{group_id}?sorting_setting=CHRONOLOGICAL"),
        ("mbasic", f"https://mbasic.facebook.com/groups/{group_id}"),
    ]
    found: list[str] = []
    seen: set[str] = set()

    for label, url in surfaces:
        print(f"Scanning {label} group surface: {url}")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3_000)
        if label == "desktop":
            set_feed_sort_recent(page)

        surface_posts = collect_post_urls_from_feed(
            page,
            group_id,
            max_posts - len(found),
            scrolls,
        )
        clicked_posts = collect_post_urls_by_clicking_comments(
            page,
            group_id,
            max_posts - len(found),
        )
        surface_posts = merge_urls(surface_posts, clicked_posts)
        for post_url in surface_posts:
            if post_url in seen:
                continue
            seen.add(post_url)
            found.append(post_url)
            if len(found) >= max_posts:
                return found

        if len(found) > 1:
            break

    return found


def scrape_post_page(
    page: Page,
    post_url: str,
    max_comments: int,
    comment_expand_rounds: int,
    debug_dir: str,
) -> Post | None:
    print(f"Opening post: {post_url}")
    page.goto(post_url, wait_until="domcontentloaded")
    page.wait_for_timeout(3_000)
    progressive_comments = collect_comments_while_expanding(
        page,
        max_comments,
        comment_expand_rounds,
    )

    top_articles = [
        page.locator("div[role='article']").nth(index)
        for index in range(page.locator("div[role='article']").count())
    ]

    for article in top_articles:
        if is_nested_article(article):
            continue

        expand_article_comments(page, article, comment_expand_rounds)
        details = extract_post_details(article)
        text_parts = extract_text_parts(article)
        details = enrich_details_from_parts(details, text_parts)
        engagement = extract_engagement_text(page, article)
        details["url"] = post_url
        if len(details["text"] or details["raw_text"] or "") < 3:
            continue

        comments = merge_comment_lists(progressive_comments, extract_comments(article, max_comments), max_comments)
        page_comments = extract_comments_from_page(page, max_comments)
        comments = merge_comment_lists(comments, page_comments, max_comments)

        post = Post(
            id=make_post_id(details),
            author=details["author"],
            posted_at=details["posted_at"],
            url=post_url,
            text=details["text"] or "",
            raw_text=details["raw_text"] or "",
            text_parts=text_parts,
            reactions_text=engagement["reactions_text"],
            comment_count_text=engagement["comment_count_text"],
            share_count_text=engagement["share_count_text"],
            comments=comments,
            images=extract_images(article),
        )
        if not post.comments:
            print("No comments found on desktop post page. Trying mobile post page.")
            page.goto(mobile_url(post_url), wait_until="domcontentloaded")
            page.wait_for_timeout(3_000)
            mobile_rounds = max(3, comment_expand_rounds // 2) if comment_expand_rounds > 0 else 0
            post.comments = merge_comment_lists(
                post.comments,
                collect_comments_while_expanding(page, max_comments, mobile_rounds),
                max_comments,
            )
            if not post.comments:
                debug_paths = write_debug_files(page, debug_dir, post.id)
                print(
                    "No comments extracted for this post. "
                    f"Debug text: {debug_paths.text_dump}, screenshot: {debug_paths.screenshot}"
                )
        return post

    page_comments = merge_comment_lists(
        progressive_comments,
        extract_comments_from_page(page, max_comments),
        max_comments,
    )
    page_text = text_from(page.locator("body"))
    debug_paths = write_debug_files(page, debug_dir, post_id_from_url(post_url))
    print(
        "Could not extract a clean main post article. "
        f"Debug text: {debug_paths.text_dump}, screenshot: {debug_paths.screenshot}"
    )

    # Return only a low-priority row. merge_posts() will keep this only when it has
    # useful comments and no better visible-card version exists. It will not save a
    # giant Facebook shell dump as the final post text.
    return Post(
        id=post_id_from_url(post_url),
        author=None,
        posted_at=None,
        url=post_url,
        text="",
        raw_text=page_text[:1200],
        text_parts=[],
        reactions_text=None,
        comment_count_text=None,
        share_count_text=None,
        comments=page_comments,
        images=extract_images(page.locator("body")),
    )


def collect_posts(
    page: Page,
    max_posts: int,
    max_comments: int,
    comment_expand_rounds: int,
) -> list[Post]:
    posts: list[Post] = []
    seen: set[str] = set()
    article_count = page.locator("div[role='article']").count()

    for index in range(article_count):
        article = page.locator("div[role='article']").nth(index)
        if is_nested_article(article):
            continue

        expand_article_comments(page, article, comment_expand_rounds)
        details = extract_post_details(article)
        text_parts = extract_text_parts(article)
        details = enrich_details_from_parts(details, text_parts)
        engagement = extract_engagement_text(page, article)
        if len(details["text"] or details["raw_text"] or "") < 3:
            continue

        post_id = make_post_id(details)
        if post_id in seen:
            continue

        seen.add(post_id)
        comments = extract_comments(article, max_comments)
        posts.append(
            Post(
                id=post_id,
                author=details["author"],
                posted_at=details["posted_at"],
                url=details["url"],
                text=details["text"] or "",
                raw_text=details["raw_text"] or "",
                text_parts=text_parts,
                reactions_text=engagement["reactions_text"],
                comment_count_text=engagement["comment_count_text"],
                share_count_text=engagement["share_count_text"],
                comments=comments,
                images=extract_images(article),
            )
        )

        if len(posts) >= max_posts:
            break

    return posts


def post_text_from_feed_parts(parts: list[str], author: str | None, posted_at: str | None) -> str:
    ignored = {value for value in [author, posted_at, "Admin", "Author"] if value}
    body_parts = []
    for part in parts:
        if part in ignored:
            continue
        if re.search(r"^(Admin|Author|Like|Reply|Share|Comment|GIF)$", part, re.I):
            continue
        if re.search(r"^\d+\s*(comments?|shares?|likes?|reactions?)?$", part, re.I):
            continue
        if re.search(r"^Comment as ", part, re.I):
            continue
        body_parts.append(part)

    text = " ".join(body_parts).strip()
    if author and text.startswith(author):
        text = text[len(author) :].strip()
    return re.sub(r"\s+", " ", text)


def collect_visible_feed_posts(page: Page, max_posts: int, max_comments: int) -> list[Post]:
    posts: list[Post] = []
    seen: set[str] = set()
    try:
        visual_cards = page.evaluate(FEED_CARDS_FROM_PAGE_SCRIPT)
    except Exception:
        visual_cards = []

    group_id = group_id_from_url(page.url) if "/groups/" in page.url else ""
    for index, card in enumerate(visual_cards):
        parts = [part for part in card.get("parts", []) if isinstance(part, str)]
        author = card.get("author")
        posted_at = card.get("postedAt")
        text = post_text_from_feed_parts(parts, author, posted_at)
        raw_text = card.get("rawText") or ""

        if not text and not raw_text:
            continue

        url = normalize_post_url(card.get("url") or "", group_id) if group_id else None
        post_id = post_id_from_url(url) if url else make_feed_post_id(author, posted_at, text or raw_text, index)
        if post_id in seen:
            continue

        seen.add(post_id)
        card_comments = []
        for comment in card.get("comments", []) or []:
            if not isinstance(comment, dict):
                continue
            comment_text = clean_comment_text(
                str(comment.get("text") or ""),
                comment.get("author"),
                comment.get("postedAt"),
            )
            if not comment_text and not comment.get("author"):
                continue
            card_comments.append(
                {
                    "author": comment.get("author"),
                    "posted_at": comment.get("postedAt"),
                    "text": comment_text,
                    "reactions": comment.get("reactionsText"),
                }
            )

        posts.append(
            Post(
                id=post_id,
                author=author,
                posted_at=posted_at,
                url=url,
                text=card.get("text") or text,
                raw_text=raw_text,
                text_parts=parts,
                reactions_text=card.get("reactionsText"),
                comment_count_text=card.get("commentCountText"),
                share_count_text=card.get("shareCountText"),
                comments=card_comments,
                images=card.get("images") or [],
            )
        )
        if len(posts) >= max_posts:
            return posts

    articles = page.locator("div[role='article']")

    for index in range(articles.count()):
        article = articles.nth(index)
        if is_nested_article(article):
            continue

        try:
            card = article.evaluate(FEED_CARD_SCRIPT)
        except Exception:
            continue

        parts = [part for part in card.get("parts", []) if isinstance(part, str)]
        author = card.get("author")
        posted_at = card.get("postedAt")
        text = post_text_from_feed_parts(parts, author, posted_at)
        raw_text = card.get("rawText") or text_from(article)

        if not text and not raw_text:
            continue

        url = normalize_post_url(card.get("url") or "", group_id_from_url(page.url)) if "/groups/" in page.url else None
        post_id = post_id_from_url(url) if url else make_feed_post_id(author, posted_at, text or raw_text, index)
        if post_id in seen:
            continue

        seen.add(post_id)
        engagement = extract_engagement_text(page, article)
        posts.append(
            Post(
                id=post_id,
                author=author,
                posted_at=posted_at,
                url=url,
                text=text,
                raw_text=raw_text,
                text_parts=parts,
                reactions_text=engagement["reactions_text"],
                comment_count_text=engagement["comment_count_text"],
                share_count_text=engagement["share_count_text"],
                comments=extract_comments(article, max_comments),
                images=card.get("images") or extract_images(article),
            )
        )

        if len(posts) >= max_posts:
            break

    return posts


def collect_visible_feed_posts_over_scrolls(
    page: Page,
    group_url: str,
    max_posts: int,
    max_comments: int,
    scrolls: int,
) -> list[Post]:
    page.goto(group_url, wait_until="domcontentloaded")
    page.wait_for_timeout(3_000)
    set_feed_sort_recent(page)

    posts: list[Post] = []
    for scroll in range(scrolls):
        close_floating_popups(page)
        visible_posts = collect_visible_feed_posts(page, max_posts, max_comments)
        posts = merge_posts(posts, visible_posts, max_posts)
        print(f"Visible feed scan {scroll + 1}/{scrolls}: collected {len(posts)} post cards")
        if len(posts) >= max_posts:
            break
        page.mouse.wheel(0, 2_500)
        page.wait_for_timeout(1_500)

    return posts


def click_comment_control_in_article(page: Page, article: Locator) -> bool:
    selectors = [
        "[aria-label='Comment']",
        "[aria-label*='Comment']",
        "div[role='button']:has-text('Comment')",
        "span:has-text('Comment')",
    ]
    for selector in selectors:
        controls = article.locator(selector)
        for index in range(min(controls.count(), 3)):
            try:
                control = controls.nth(index)
                if control.is_visible(timeout=500):
                    control.scroll_into_view_if_needed(timeout=1_000)
                    control.click(timeout=1_500)
                    page.wait_for_timeout(2_000)
                    return True
            except Exception:
                continue
    return False


def collect_posts_by_clicking_feed_comments(
    page: Page,
    group_url: str,
    max_posts: int,
    max_comments: int,
    comment_expand_rounds: int,
    scrolls: int,
    debug_dir: str,
) -> list[Post]:
    posts: list[Post] = []
    page.goto(group_url, wait_until="domcontentloaded")
    page.wait_for_timeout(3_000)
    set_feed_sort_recent(page)

    for scroll in range(scrolls):
        close_floating_popups(page)
        articles = page.locator("div[role='article']")
        top_indices: list[int] = []
        for index in range(articles.count()):
            article = articles.nth(index)
            if not is_nested_article(article):
                top_indices.append(index)

        print(f"Click scan {scroll + 1}/{scrolls}: {len(top_indices)} visible post cards")
        for index in top_indices:
            if len(posts) >= max_posts:
                return posts

            articles = page.locator("div[role='article']")
            if index >= articles.count():
                continue
            article = articles.nth(index)
            if is_nested_article(article):
                continue

            visible_post = None
            visible = collect_visible_post_from_article(page, article, index, max_comments)
            if visible:
                visible_post = visible

            before_url = page.url
            clicked = click_comment_control_in_article(page, article)
            opened_url = normalize_post_url(page.url, group_id_from_url(group_url))
            opened_post = None

            if clicked and opened_url and opened_url != normalize_post_url(before_url, group_id_from_url(group_url)):
                opened_post = scrape_post_page(
                    page,
                    opened_url,
                    max_comments,
                    comment_expand_rounds,
                    debug_dir,
                )
                page.goto(group_url, wait_until="domcontentloaded")
                page.wait_for_timeout(2_000)
                for _ in range(scroll + 1):
                    page.mouse.wheel(0, 2_500)
                    page.wait_for_timeout(300)
            elif clicked:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)

            # Prefer the visible feed card first. If the opened post page fails and returns
            # a noisy placeholder, it should not replace the clean card data.
            posts = merge_posts(posts, [post for post in [visible_post, opened_post] if post], max_posts)

        page.mouse.wheel(0, 2_500)
        page.wait_for_timeout(1_500)

    return posts


def collect_visible_post_from_article(
    page: Page,
    article: Locator,
    index: int,
    max_comments: int,
) -> Post | None:
    try:
        card = article.evaluate(FEED_CARD_SCRIPT)
    except Exception:
        return None

    parts = [part for part in card.get("parts", []) if isinstance(part, str)]
    author = card.get("author")
    posted_at = card.get("postedAt")
    text = post_text_from_feed_parts(parts, author, posted_at)
    raw_text = card.get("rawText") or text_from(article)
    if not text and not raw_text:
        return None

    group_id = group_id_from_url(page.url) if "/groups/" in page.url else ""
    url = normalize_post_url(card.get("url") or "", group_id) if group_id else None
    post_id = post_id_from_url(url) if url else make_feed_post_id(author, posted_at, text or raw_text, index)
    engagement = extract_engagement_text(page, article)

    return Post(
        id=post_id,
        author=author,
        posted_at=posted_at,
        url=url,
        text=text,
        raw_text=raw_text,
        text_parts=parts,
        reactions_text=engagement["reactions_text"],
        comment_count_text=engagement["comment_count_text"],
        share_count_text=engagement["share_count_text"],
        comments=extract_comments(article, max_comments),
        images=card.get("images") or extract_images(article),
    )


def post_quality_score(post: Post) -> int:
    """Higher score means this row has real post data, not just a noisy Facebook shell page."""
    score = 0
    text = (post.text or "").strip()
    raw_text = (post.raw_text or "").strip()

    if post.url:
        score += 10
    if post.author:
        score += 30
    if post.posted_at:
        score += 20
    if text:
        score += min(80, len(text))
    if post.comments:
        score += 40 + min(40, len(post.comments) * 4)
    if post.reactions_text:
        score += 10
    if post.comment_count_text:
        score += 10
    if post.share_count_text:
        score += 5
    if post.images:
        score += min(15, len(post.images) * 5)

    # Big body dumps are usually failed extraction pages, not useful post rows.
    if not text and len(raw_text) > 1200:
        score -= 120
    if "Number of unread notifications" in raw_text or "Facebook menu" in raw_text:
        score -= 100
    if not post.author and not post.posted_at and not text and not post.comments:
        score -= 100

    return score


def post_keys(post: Post) -> set[str]:
    keys = {post.id}
    if post.url:
        keys.add(post.url)
        try:
            keys.add(post_id_from_url(post.url))
        except Exception:
            pass
    return {key for key in keys if key}


def merge_posts(primary: list[Post], secondary: list[Post], max_posts: int) -> list[Post]:
    # Merge by post id / URL, but keep the richer record when duplicates appear.
    merged: list[Post] = []
    key_to_index: dict[str, int] = {}

    def rebuild_index() -> None:
        key_to_index.clear()
        for item_index, item in enumerate(merged):
            for item_key in post_keys(item):
                key_to_index[item_key] = item_index

    for post in [*primary, *secondary]:
        if not post:
            continue

        keys = post_keys(post)
        matching_indexes = {key_to_index[key] for key in keys if key in key_to_index}

        if matching_indexes:
            target_index = min(matching_indexes)
            current = merged[target_index]

            if post_quality_score(post) > post_quality_score(current):
                # Keep useful fields from both rows.
                if len(current.comments or []) > len(post.comments or []):
                    post.comments = current.comments
                post.reactions_text = better_count_text(post.reactions_text, current.reactions_text)
                post.comment_count_text = better_count_text(post.comment_count_text, current.comment_count_text)
                post.share_count_text = better_count_text(post.share_count_text, current.share_count_text)
                if not post.images and current.images:
                    post.images = current.images
                merged[target_index] = post
            else:
                if not current.images and post.images:
                    current.images = post.images
                if len(post.comments or []) > len(current.comments or []):
                    current.comments = post.comments
                current.reactions_text = better_count_text(current.reactions_text, post.reactions_text)
                current.comment_count_text = better_count_text(current.comment_count_text, post.comment_count_text)
                current.share_count_text = better_count_text(current.share_count_text, post.share_count_text)

            for key in keys:
                key_to_index[key] = target_index
            continue

        if post_quality_score(post) < -50:
            # Do not save broken placeholder rows such as full Facebook shell/page dumps.
            continue

        if len(merged) >= max_posts:
            weakest_index = min(range(len(merged)), key=lambda item_index: post_quality_score(merged[item_index]))
            if post_quality_score(post) > post_quality_score(merged[weakest_index]):
                merged[weakest_index] = post
                rebuild_index()
            continue

        index = len(merged)
        merged.append(post)
        for key in keys:
            key_to_index[key] = index

    return merged[:max_posts]


def extract_comments(article: Locator, max_comments: int) -> list[dict[str, str | None]]:
    comments: list[dict[str, str | None]] = []
    seen: set[str] = set()
    comment_blocks = article.locator(
        "div[role='article'], div[aria-label^='Comment by'], div[aria-label*=' comment by ']"
    )

    for index in range(comment_blocks.count()):
        comment = comment_blocks.nth(index)
        aria_label = ""
        try:
            aria_label = comment.get_attribute("aria-label") or ""
        except Exception:
            pass

        if not aria_label and not is_nested_article(comment):
            continue

        try:
            details = comment.evaluate(COMMENT_CLEANUP_SCRIPT)
        except Exception:
            details = {"author": None, "text": text_from(comment)}

        aria_author, aria_time = parse_comment_aria_label(aria_label)
        if not details.get("author"):
            details["author"] = aria_author
        if not details.get("postedAt"):
            details["postedAt"] = aria_time

        text = clean_comment_text(
            (details.get("text") or "").strip(),
            details.get("author"),
            details.get("postedAt"),
        )
        images = details.get("images") or []
        if len(text) < 2:
            text = comment_image_text(images)
        key = "|".join(
            [
                details.get("author") or "",
                details.get("postedAt") or "",
                text,
                comment_image_text(images),
            ]
        )
        if (len(text) < 2 and not images) or key in seen:
            continue
        if text.startswith("Like Reply") or text == "Comment":
            continue

        seen.add(key)
        comments.append(
            {
                "author": details.get("author"),
                "posted_at": details.get("postedAt"),
                "text": text,
                "reactions": clean_comment_reaction(details.get("reactionsText")),
                "images": images,
            }
        )

        if max_comments > 0 and len(comments) >= max_comments:
            break

    return comments


def extract_comments_from_page(page: Page, max_comments: int) -> list[dict[str, str | None]]:
    comments: list[dict[str, str | None]] = []
    seen: set[str] = set()
    selectors = [
        "div[aria-label^='Comment by']",
        "div[aria-label*=' comment by ']",
        "div[role='article']",
    ]

    for selector in selectors:
        blocks = page.locator(selector)
        for index in range(blocks.count()):
            block = blocks.nth(index)
            aria_label = ""
            try:
                aria_label = block.get_attribute("aria-label") or ""
            except Exception:
                pass

            try:
                if selector == "div[role='article']" and not is_nested_article(block):
                    continue
            except Exception:
                continue

            try:
                details = block.evaluate(COMMENT_CLEANUP_SCRIPT)
            except Exception:
                details = {"author": None, "text": text_from(block)}

            aria_author, aria_time = parse_comment_aria_label(aria_label)
            if not details.get("author"):
                details["author"] = aria_author
            if not details.get("postedAt"):
                details["postedAt"] = aria_time

            text = clean_comment_text(
                (details.get("text") or "").strip(),
                details.get("author"),
                details.get("postedAt"),
            )
            images = details.get("images") or []
            if len(text) < 2:
                text = comment_image_text(images)
            key = "|".join(
                [
                    details.get("author") or "",
                    details.get("postedAt") or "",
                    text,
                    comment_image_text(images),
                ]
            )
            if (len(text) < 2 and not images) or key in seen:
                continue
            if re.search(r"^(Like|Reply|Share|Comment|Write a comment)", text, re.I):
                continue

            seen.add(key)
            comments.append(
                {
                    "author": details.get("author"),
                    "posted_at": details.get("postedAt"),
                    "text": text,
                    "reactions": clean_comment_reaction(details.get("reactionsText")),
                    "images": images,
                }
            )
            if max_comments > 0 and len(comments) >= max_comments:
                return comments

    return comments


def write_debug_files(page: Page, debug_dir: str, post_id: str) -> DebugPaths:
    path = Path(debug_dir)
    path.mkdir(parents=True, exist_ok=True)
    text_path = path / f"{post_id}.txt"
    screenshot_path = path / f"{post_id}.png"

    try:
        text_path.write_text(text_from(page.locator("body")), encoding="utf-8")
    except Exception:
        text_path = None

    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        screenshot_path = None

    return DebugPaths(
        text_dump=str(text_path) if text_path else None,
        screenshot=str(screenshot_path) if screenshot_path else None,
    )


def parse_count_text(value: str | None) -> int | None:
    if not value:
        return None

    match = re.search(r"(\d+(?:\.\d+)?)\s*([KMB])?", value.replace(",", ""), re.I)
    if not match:
        return None

    number = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return int(number * multiplier)


def repair_mojibake(value: object) -> object:
    if isinstance(value, str):
        if not any(marker in value for marker in ("à", "Â", "Ã", "�")):
            return value
        try:
            repaired = value.encode("latin1").decode("utf-8")
        except UnicodeError:
            return value
        thai_before = len(re.findall(r"[\u0E00-\u0E7F]", value))
        thai_after = len(re.findall(r"[\u0E00-\u0E7F]", repaired))
        if thai_after > thai_before:
            return repaired
        return value
    if isinstance(value, list):
        return [repair_mojibake(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_mojibake(item) for key, item in value.items()}
    return value


def clean_comment_reaction(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.match(r"^\d+\s*(like|likes|reaction|reactions)$", text, re.IGNORECASE):
        return text
    if re.match(r"^[1-9]\d{0,2}$", text):
        return text
    return None


def serialize_post(post: Post) -> dict:
    data = post.__dict__.copy()
    comments = post.comments or []
    reaction_count = parse_count_text(post.reactions_text)
    total_comment_count = parse_count_text(post.comment_count_text)
    share_count = parse_count_text(post.share_count_text)
    data["reaction_count"] = reaction_count if reaction_count is not None else 0
    data["total_comment_count"] = total_comment_count if total_comment_count is not None else len(comments)
    data["share_count"] = share_count if share_count is not None else 0
    data["comments_found"] = len(comments)
    data["missing_comment_count"] = max(0, data["total_comment_count"] - len(comments))
    data["comments_complete"] = len(comments) >= data["total_comment_count"]
    return repair_mojibake(data)


def save_json(path: str, metadata: GroupMetadata, posts: list[Post]) -> Path:
    data = {
        "group": metadata.__dict__,
        "posts": [serialize_post(post) for post in posts],
    }
    output_path = writable_output_path(path)
    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def writable_output_path(path: str) -> Path:
    output_path = Path(path)
    try:
        with output_path.open("a", encoding="utf-8"):
            pass
        return output_path
    except PermissionError:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        fallback = output_path.with_name(
            f"{output_path.stem}_{timestamp}{output_path.suffix}"
        )
        print(f"{output_path} is locked. Writing {fallback} instead.")
        return fallback


def save_csv(path: str, posts: list[Post]) -> Path:
    output_path = writable_output_path(path)
    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=[
                "post_id",
                "post_author",
                "posted_at",
                "post_url",
                "post_text",
                "reactions",
                "comment_count",
                "share_count",
                "comment_author",
                "comment_posted_at",
                "comment_text",
                "comment_reactions",
                "comments_json",
                "comments_found",
            ],
        )
        writer.writeheader()
        for post in posts:
            comment_authors = [
                comment.get("author") or ""
                for comment in post.comments
                if comment.get("author") or comment.get("text")
            ]
            comment_times = [
                comment.get("posted_at") or ""
                for comment in post.comments
                if comment.get("author") or comment.get("text")
            ]
            comment_texts = [
                comment.get("text") or ""
                for comment in post.comments
                if comment.get("author") or comment.get("text")
            ]
            comment_reactions = [
                comment.get("reactions") or ""
                for comment in post.comments
                if comment.get("author") or comment.get("text")
            ]
            writer.writerow(
                {
                    "post_id": post.id,
                    "post_author": post.author,
                    "posted_at": post.posted_at,
                    "post_url": post.url,
                    "post_text": post.text,
                    "reactions": post.reactions_text,
                    "comment_count": post.comment_count_text,
                    "share_count": post.share_count_text,
                    "comment_author": " | ".join(comment_authors),
                    "comment_posted_at": " | ".join(comment_times),
                    "comment_text": " | ".join(comment_texts),
                    "comment_reactions": " | ".join(comment_reactions),
                    "comments_json": json.dumps(post.comments, ensure_ascii=False),
                    "comments_found": len(post.comments),
                }
            )
    return output_path


def save_comment_csv(path: str, posts: list[Post]) -> Path:
    comment_path = writable_output_path(
        str(Path(path).with_name(f"{Path(path).stem}_comments{Path(path).suffix}"))
    )
    with comment_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=[
                "post_id",
                "post_author",
                "posted_at",
                "post_url",
                "post_text",
                "reactions",
                "comment_count",
                "share_count",
                "comment_author",
                "comment_posted_at",
                "comment_text",
                "comment_reactions",
            ],
        )
        writer.writeheader()
        for post in posts:
            if not post.comments:
                writer.writerow(
                    {
                        "post_id": post.id,
                        "post_author": post.author,
                        "posted_at": post.posted_at,
                        "post_url": post.url,
                        "post_text": post.text,
                        "reactions": post.reactions_text,
                        "comment_count": post.comment_count_text,
                        "share_count": post.share_count_text,
                        "comment_author": "",
                        "comment_posted_at": "",
                        "comment_text": "",
                        "comment_reactions": "",
                    }
                )
                continue

            for comment in post.comments:
                writer.writerow(
                    {
                        "post_id": post.id,
                        "post_author": post.author,
                        "posted_at": post.posted_at,
                        "post_url": post.url,
                        "post_text": post.text,
                        "reactions": post.reactions_text,
                        "comment_count": post.comment_count_text,
                        "share_count": post.share_count_text,
                        "comment_author": comment.get("author"),
                        "comment_posted_at": comment.get("posted_at"),
                        "comment_text": comment.get("text"),
                        "comment_reactions": comment.get("reactions"),
                    }
                )
    return comment_path


def main() -> None:
    args = parse_args()
    group_url = validate_facebook_group_url(args.group_url)
    group_id = group_id_from_url(group_url)
    profile_dir = Path(args.profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=args.headless,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = context.pages[0] if context.pages else context.new_page()

        page.goto(FACEBOOK_HOME, wait_until="domcontentloaded")
        if is_login_or_checkpoint(page):
            prefill_login_email(page, args.facebook_email)
            input("Finish Facebook login/security checks in the browser, then press Enter...")

        print(f"Opening group: {group_url}")
        page.goto(group_url, wait_until="domcontentloaded")
        page.wait_for_timeout(4_000)
        set_feed_sort_recent(page)
        metadata = get_group_metadata(page, group_url)
        if metadata.member_count_text:
            print(f"Group members: {metadata.member_count_text}")
        else:
            print("Group member count not found in the loaded page text.")

        post_urls = collect_post_urls_from_group_surfaces(
            page,
            group_url,
            group_id,
            args.max_posts,
            args.scrolls,
        )
        extra_post_urls = parse_extra_post_urls(args.extra_post_urls, group_id)
        if extra_post_urls:
            print(f"Adding {len(extra_post_urls)} extra post URLs from EXTRA_POST_URLS.")
        post_urls = merge_urls(extra_post_urls, post_urls)[: args.max_posts]

        click_posts = collect_posts_by_clicking_feed_comments(
            page,
            group_url,
            args.max_posts,
            args.max_comments,
            args.comment_expand_rounds,
            args.scrolls,
            args.debug_dir,
        )
        print(f"Comment-click scan collected {len(click_posts)} rows.")

        feed_posts = collect_visible_feed_posts_over_scrolls(
            page,
            group_url,
            args.max_posts,
            args.max_comments,
            max(3, args.scrolls // 3),
        )

        posts: list[Post] = merge_posts(click_posts, feed_posts, args.max_posts)
        for post_url in post_urls:
            post = scrape_post_page(
                page,
                post_url,
                args.max_comments,
                args.comment_expand_rounds,
                args.debug_dir,
            )
            if post:
                posts = merge_posts(posts, [post], args.max_posts)
                print(f"Collected/merged post URL {len(posts)} total rows")

        if not posts:
            print("No post permalink data found. Falling back to visible feed extraction.")
            page.goto(group_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3_000)
            for scroll in range(args.scrolls):
                expand_visible_content(page)
                posts = collect_posts(
                    page,
                    args.max_posts,
                    args.max_comments,
                    args.comment_expand_rounds,
                )
                print(f"Scroll {scroll + 1}/{args.scrolls}: collected {len(posts)} posts")
                if len(posts) >= args.max_posts:
                    break

                page.mouse.wheel(0, 2_500)
                page.wait_for_timeout(2_000)

        json_path = save_json(args.output_json, metadata, posts)
        csv_path = save_csv(args.output_csv, posts)
        comment_csv_path = save_comment_csv(args.output_csv, posts)
        context.close()

    print(f"Saved {len(posts)} posts to {json_path} and {csv_path}")
    print(f"Saved comment-level rows to {comment_csv_path}")
    print("Only content visible to your logged-in account and loaded in the page was exported.")


if __name__ == "__main__":
    started_at = time.time()
    main()
    print(f"Done in {time.time() - started_at:.1f}s")
