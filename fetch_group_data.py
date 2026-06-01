from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

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

  const styledTexts = Array.from(root.querySelectorAll('div[style*="font-size"], div[style*="text-align"]'))
    .map((node) => {
      const style = window.getComputedStyle(node);
      const value = cleanupText(node.innerText || node.textContent);
      return {
        value,
        fontSize: parseFloat(style.fontSize || "0"),
        fontWeight: Number(style.fontWeight) || 0,
        rect: node.getBoundingClientRect()
      };
    })
    .filter((item) => item.value && item.value.length > 3)
    .filter((item) => item.fontSize >= 20 || item.fontWeight >= 600 || item.rect.height > 80)
    .map((item) => item.value)
    .filter((value, index, arr) => arr.indexOf(value) === index);

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

  let text = styledTexts.join("\\n").trim() || rawText;
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
  const rect = root.getBoundingClientRect();
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

  return { author, postedAt, reactionsText, text, rawText, ariaLabel, images, left: Math.round(rect.left), top: Math.round(rect.top) };
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
  const backgroundImages = Array.from(root.querySelectorAll('*'))
    .map((node) => {
      const style = window.getComputedStyle(node);
      const bg = style.backgroundImage || "";
      const match = bg.match(/url\\(["']?(.*?)["']?\\)/);
      const rect = node.getBoundingClientRect();
      return match ? {
        src: match[1],
        alt: "",
        width: Math.round(rect.width) || null,
        height: Math.round(rect.height) || null,
        perfLogName: "backgroundImage"
      } : null;
    })
    .filter(Boolean);
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
  return [...images, ...backgroundImages].filter((img, index, arr) => {
    const key = `${img.src}|${img.alt}`;
    return arr.findIndex((other) => `${other.src}|${other.alt}` === key) === index;
  });
}
"""

STYLED_TEXT_SCRIPT = """
(root) => {
  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
  return Array.from(root.querySelectorAll('div[style*="font-size"], div[style*="text-align"]'))
    .map((node) => {
      const style = window.getComputedStyle(node);
      const value = clean(node.innerText || node.textContent);
      const rect = node.getBoundingClientRect();
      return {
        value,
        fontSize: parseFloat(style.fontSize || "0"),
        fontWeight: Number(style.fontWeight) || 0,
        height: rect.height
      };
    })
    .filter((item) => item.value && item.value.length > 3)
    .filter((item) => item.fontSize >= 20 || item.fontWeight >= 600 || item.height > 80)
    .map((item) => item.value)
    .filter((value, index, arr) => arr.indexOf(value) === index)
    .join("\\n")
    .trim();
}
"""

MAIN_POST_TEXT_SCRIPT = """
(root, { postId }) => {
  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
  const noise = new Set([
    "Like", "Reply", "Share", "Comment", "Send", "Follow", "Following",
    "Author", "Admin", "Top contributor", "Most relevant", "All comments",
    "See translation", "Write a comment...", "View more comments", "View previous comments"
  ]);
  const isNoise = (value) => {
    if (!value || noise.has(value)) return true;
    if (/^\\d+\\s*(?:comments?|shares?|likes?|reactions?)?$/i.test(value)) return true;
    if (/^(?:just now|yesterday|today|\\d+\\s*(?:m|h|d|w|mo|y))$/i.test(value)) return true;
    return false;
  };
  const isVisible = (node) => {
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const inComment = (node) => {
    const comment = node.closest('[data-commentid], [aria-label^="Comment by"], [aria-label*=" comment by "]');
    if (comment) return true;
    const link = node.closest('a[href*="comment_id="]');
    return Boolean(link);
  };
  const inControl = (node) => Boolean(node.closest('[role="button"], button, form, input, textarea, [aria-label="Like"], [aria-label="Comment"], [aria-label="Share"]'));
  const candidateRoots = [];
  if (postId) {
    for (const link of Array.from(root.querySelectorAll(`a[href*="/posts/${postId}"], a[href*="/permalink/${postId}"]`))) {
      if ((link.href || "").includes("comment_id=")) continue;
      let node = link;
      for (let depth = 0; depth < 12 && node; depth += 1) {
        if (node.querySelector('[data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"], strong.html-strong')) {
          candidateRoots.push(node);
          break;
        }
        node = node.parentElement;
      }
    }
  }
  candidateRoots.push(root);

  const selectors = [
    '[data-ad-rendering-role="story_message"]',
    '[data-ad-comet-preview="message"]',
    '[data-testid="post_message"]',
    'strong.html-strong',
    'div[dir="auto"][style*="text-align"]'
  ];
  const candidates = [];
  for (const searchRoot of candidateRoots) {
    for (const selector of selectors) {
      for (const node of Array.from(searchRoot.querySelectorAll(selector))) {
        const value = clean(node.innerText || node.textContent);
        if (isNoise(value) || inComment(node) || inControl(node) || !isVisible(node)) continue;
        candidates.push({ value, y: node.getBoundingClientRect().top, selector });
      }
    }
  }
  candidates.sort((a, b) => a.y - b.y);
  const unique = candidates
    .map((item) => item.value)
    .filter((value, index, arr) => arr.indexOf(value) === index);
  return unique[0] || "";
}
"""

ENGAGEMENT_COUNTERS_SCRIPT = """
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
  const counterNear = (selector, label) => {
    const targets = Array.from(root.querySelectorAll(selector))
      .filter((node) => !node.closest('[data-commentid], [aria-label^="Comment by"], [aria-label*=" comment by "]'));
    let best = null;
    for (const target of targets) {
      let node = target;
      for (let depth = 0; depth < 8 && node; depth += 1) {
        const rect = node.getBoundingClientRect();
        if (depth > 0 && (rect.width > 800 || rect.height > 160)) break;
        const parts = textParts(node);
        const labelled = parts.find((part) => new RegExp(`^\\\\d+(?:[,.]\\\\d+)*(?:\\\\.\\\\d+)?\\\\s+${label}s?$`, "i").test(part));
        const numberOnly = parts.find((part) => /^[\\d,.]+$/.test(part));
        if (labelled) best = betterCountText(best, labelled);
        const actionCount = node.querySelectorAll('[data-ad-rendering-role$="_button"], [aria-label="Like"], [aria-label="React"], [aria-label="Leave a comment"], [aria-label="Comment"], [aria-label="Share"], [aria-label="Send"]').length;
        if (numberOnly && actionCount <= 1) best = betterCountText(best, `${numberOnly} ${label}${numberOnly === "1" ? "" : "s"}`);
        node = node.parentElement;
      }
    }
    return best;
  };
  const ariaReactions = Array.from(root.querySelectorAll('[aria-label]'))
    .map((node) => clean(node.getAttribute("aria-label")))
    .filter((value) => /^(?:Like|Love|Care|Haha|Wow|Sad|Angry):\\s*[\\d,.]+\\s+people?/i.test(value));
  const ariaReaction = ariaReactions.sort((a, b) => countFromText(b) - countFromText(a))[0] || null;
  const visibleReaction = counterNear('[data-ad-rendering-role="like_button"], [aria-label="Like"], [aria-label="React"]', "reaction");
  return {
    reactions_text: betterCountText(visibleReaction, ariaReaction),
    comment_count_text: counterNear('[data-ad-rendering-role="comment_button"], [aria-label="Leave a comment"], [aria-label="Comment"]', "comment"),
    share_count_text: counterNear('[data-ad-rendering-role="share_button"], [aria-label="Share"], [aria-label="Send"]', "share")
  };
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
  const postedAt = unique.find((line) => line.length <= 40 && /\\b(\\d+\\s*(m|h|d|w)|\\d{1,2}\\s+[A-Z][a-z]+\\s+at\\s+\\d{1,2}:\\d{2}|Yesterday|Just now)\\b/i.test(line)) || null;
  const rootRect = root.getBoundingClientRect();
  const headerPostedAt = () => {
    const nodes = Array.from(root.querySelectorAll('a[role="link"], a, span, div'));
    for (const node of nodes) {
      const nodeRect = node.getBoundingClientRect();
      if (nodeRect.top < rootRect.top - 5 || nodeRect.top > rootRect.top + 240) continue;
      const labelledBy = node.getAttribute("aria-labelledby");
      const labelText = labelledBy
        ? clean(labelledBy.split(/\\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" "))
        : "";
      const text = clean(labelText || node.getAttribute("aria-label") || node.getAttribute("title") || node.innerText || "");
      if (!text || text.length > 80) continue;
      const direct = text.match(/^(Just now|Yesterday|\\d+\\s*(?:m|h|d|w|mo|y))$/i);
      if (direct) return direct[1].replace(/\\s+/g, "");
      const relative = text.match(/^(?:a|an|\\d+)\\s+(?:second|minute|hour|day|week|month|year)s?\\s+ago$/i);
      if (relative) return text;
      const fullDate = text.match(/^[A-Z][a-z]+,\\s+[A-Z][a-z]+\\s+\\d{1,2}(?:,\\s*\\d{4})?(?:\\s+at\\s+.+)?$/i);
      if (fullDate) return text;
      const shortDate = text.match(/^[A-Z][a-z]{2,8}\\s+\\d{1,2}(?:,\\s*\\d{4})?(?:\\s+at\\s+.+)?$/i);
      if (shortDate) return text;
    }
    return null;
  };
  const visualPostedAt = () => {
    const rows = new Map();
    Array.from(root.querySelectorAll('span, a, div')).forEach((node) => {
      const text = clean(node.innerText || node.textContent);
      if (!/^(\\d+|[mhdw]|mo|y|·)$/.test(text)) return;
      const rect = node.getBoundingClientRect();
      const style = window.getComputedStyle(node);
      if (rect.width <= 0 || rect.height <= 0 || style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) return;
      if (style.position === "absolute") return;
      if (rect.y > rootRect.top + 240) return;
      const y = Math.round(rect.y);
      if (!rows.has(y)) rows.set(y, []);
      rows.get(y).push({ text, x: rect.x });
    });
    for (const items of rows.values()) {
      const value = items.sort((a, b) => a.x - b.x).map((item) => item.text).join("").replace(/·/g, "");
      const match = value.match(/\\d+(?:m|h)/i);
      if (match) return match[0];
    }
    return null;
  };
  const isObfuscated = (value) => {
    const tokens = value.split(/\\s+/);
    if (tokens.length < 8) return false;
    const singleChar = tokens.filter((t) => t.length <= 2 && /^[a-zA-Z0-9]+$/.test(t)).length;
    return singleChar / tokens.length >= 0.6;
  };
  const urlNode = Array.from(root.querySelectorAll('a[href]')).find((node) => {
    const href = node.href || "";
    if (href.includes("comment_id=")) return false;
    return href.includes("/posts/") || href.includes("/permalink/") || href.includes("story_fbid");
  });
  const styledTexts = Array.from(root.querySelectorAll('div[style*="font-size"], div[style*="text-align"]'))
    .map((node) => {
      const style = window.getComputedStyle(node);
      const value = clean(`${node.innerText || node.textContent || ""} ${Array.from(node.querySelectorAll('img[alt]')).map((img) => img.getAttribute('alt') || '').join(" ")}`);
      return {
        value,
        fontSize: parseFloat(style.fontSize || "0"),
        fontWeight: Number(style.fontWeight) || 0,
        rect: node.getBoundingClientRect()
      };
    })
    .filter((item) => item.value && item.value.length > 3)
    .filter((item) => !isObfuscated(item.value))
    .filter((item) => item.fontSize >= 20 || item.fontWeight >= 600 || item.rect.height > 80)
    .map((item) => item.value)
    .filter((value, index, arr) => arr.indexOf(value) === index)
    .filter((value, index, arr) => !arr.some((other, otherIndex) => otherIndex !== index && other.includes(value) && other.length > value.length));
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
    postedAt: headerPostedAt() || postedAt || visualPostedAt(),
    url: urlNode ? urlNode.href : null,
    parts: [...styledTexts, ...unique],
    text: styledTexts.join("\\n").trim(),
    rawText: unique.join("\\n"),
    reactionsText: reactionFromAria,
    images
  };
}
"""

FEED_CARDS_FROM_PAGE_SCRIPT = """
() => {
  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();

  const timeRegex = /^(?:just now|yesterday(?: at .*)?|today(?: at .*)?|(?:a|an|\\d+)\\s+(?:second|minute|hour|day|week|month|year)s?\\s+ago|\\d+\\s*(?:m|h|d|w|mo|y)|\\d{1,2}\\s+[A-Z][a-z]{2,8}(?:\\s+at\\s+.*)?|[A-Z][a-z]{2,8}\\s+\\d{1,2}(?:\\s+at\\s+.*)?|[A-Z][a-z]+,\\s+[A-Z][a-z]+\\s+\\d{1,2}(?:,\\s*\\d{4})?(?:\\s+at\\s+.*)?)$/i;

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

  const styledTextNodes = () => Array.from(document.querySelectorAll('div[style*="font-size"], div[style*="text-align"]'))
    .filter((node) => {
      const style = window.getComputedStyle(node);
      const text = clean(`${node.innerText || node.textContent || ""} ${Array.from(node.querySelectorAll('img[alt]')).map((img) => img.getAttribute('alt') || '').join(" ")}`);
      const rect = node.getBoundingClientRect();
      return text.length > 3 && (parseFloat(style.fontSize || "0") >= 20 || Number(style.fontWeight) >= 600 || rect.height > 80);
    });

  const findCardRoot = (storyNode) => {
    let node = storyNode;
    for (let depth = 0; depth < 18 && node && node.parentElement; depth += 1) {
      const hasStory = !!node.querySelector('[data-ad-rendering-role="story_message"]') || styledTextNodes().some((story) => node.contains(story));
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
    const target = Array.from(root.querySelectorAll(selector))
      .find((node) => !node.closest('[data-commentid], [aria-label^="Comment by"], [aria-label*=" comment by "]'));
    if (!target) return null;

    let node = target;
    for (let depth = 0; depth < 7 && node; depth += 1) {
      const rect = node.getBoundingClientRect();
      if (depth > 0 && (rect.width > 800 || rect.height > 160)) break;
      const parts = textParts(node);
      const numberOnly = parts.find((part) => /^[\\d,.]+$/.test(part));
      const labelled = parts.find((part) => new RegExp(`^\\\\d+(?:[,.]\\\\d+)*(?:\\\\.\\\\d+)?\\\\s+${label}s?$`, "i").test(part));
      if (labelled) return labelled;
      const actionCount = node.querySelectorAll('[data-ad-rendering-role$="_button"], [aria-label="Like"], [aria-label="React"], [aria-label="Leave a comment"], [aria-label="Comment"], [aria-label="Share"], [aria-label="Send"]').length;
      if (numberOnly && actionCount <= 1) return `${numberOnly} ${label}${numberOnly === "1" ? "" : "s"}`;
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
  const storyNodes = [
    ...Array.from(document.querySelectorAll('[data-ad-rendering-role="story_message"]')),
    ...styledTextNodes()
  ];

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

    // Detect Facebook's anti-scraping obfuscation: text where 60%+ of space-separated
    // tokens are single alphanumeric chars (e.g. "e S s d p o t r n o 4 9 5 9 2 0 ...").
    const isObfuscated = (value) => {
      const tokens = value.split(/\\s+/);
      if (tokens.length < 8) return false;
      const singleChar = tokens.filter((t) => t.length <= 2 && /^[a-zA-Z0-9]+$/.test(t)).length;
      return singleChar / tokens.length >= 0.6;
    };
    const styleTexts = styledTextNodes()
      .filter((node) => card.contains(node))
      .map((node) => clean(`${node.innerText || node.textContent || ""} ${Array.from(node.querySelectorAll('img[alt]')).map((img) => img.getAttribute('alt') || '').join(" ")}`))
      .filter(Boolean)
      .filter((value) => !isObfuscated(value));
    const storyTexts = [
      ...styleTexts,
      ...Array.from(card.querySelectorAll('[data-ad-rendering-role="story_message"]'))
      .map((node) => clean(node.innerText || node.textContent))
      .filter(Boolean)
      .filter((value) => !isObfuscated(value))
    ]
      .filter((value, index, arr) => arr.indexOf(value) === index)
      .filter((value, index, arr) => !arr.some((other, otherIndex) => otherIndex !== index && other.includes(value) && other.length > value.length));

    const urlNode = Array.from(card.querySelectorAll('a[href]')).find((node) => {
      const href = node.href || node.getAttribute("href") || "";
      return href.includes("/posts/") || href.includes("/permalink/") || href.includes("story_fbid");
    });

    // A genuinely embedded shared post is a role="article" that is itself nested inside
    // another role="article" within the same card. Regular post articles are not nested.
    const nestedArticles = Array.from(card.querySelectorAll('[role="article"]')).filter((a) => {
      let node = a.parentElement;
      while (node && node !== card) {
        if (node.getAttribute("role") === "article") return true;
        node = node.parentElement;
      }
      return false;
    });
    const isInsideSharedPost = nestedArticles.length > 0
      ? (node) => nestedArticles.some((art) => art.contains(node))
      : () => false;

    const headerPostedAt = () => {
      // Include a, span, div — Facebook sometimes renders time in div elements.
      // Do NOT filter by visibility/dimensions: time links can be hidden or zero-size
      // yet still carry the correct aria-label (e.g. anti-scraping obfuscation).
      const nodes = Array.from(card.querySelectorAll('a[role="link"], a, span, div'))
        .filter((node) => !isInsideSharedPost(node));
      for (const node of nodes) {
        const nodeRect = node.getBoundingClientRect();
        if (nodeRect.top < rect.top - 5 || nodeRect.top > rect.top + 240) continue;
        const labelledBy = node.getAttribute("aria-labelledby");
        const labelText = labelledBy
          ? clean(labelledBy.split(/\\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" "))
          : "";
        const text = clean(labelText || node.getAttribute("aria-label") || node.getAttribute("title") || node.innerText || "");
        if (!text || text.length > 80) continue;
        const direct = text.match(/^(Just now|Yesterday|\\d+\\s*(?:m|h|d|w|mo|y))$/i);
        if (direct) return direct[1].replace(/\\s+/g, "");
        const relative = text.match(/^(?:a|an|\\d+)\\s+(?:second|minute|hour|day|week|month|year)s?\\s+ago$/i);
        if (relative) return text;
        const fullDate = text.match(/^[A-Z][a-z]+,\\s+[A-Z][a-z]+\\s+\\d{1,2}(?:,\\s*\\d{4})?(?:\\s+at\\s+.+)?$/i);
        if (fullDate) return text;
        const shortDate = text.match(/^[A-Z][a-z]{2,8}\\s+\\d{1,2}(?:,\\s*\\d{4})?(?:\\s+at\\s+.+)?$/i);
        if (shortDate) return text;
      }
      return null;
    };

    const postLinkTimeNode = Array.from(card.querySelectorAll('a[href]')).find((node) => {
      if (isInsideSharedPost(node)) return false;
      const href = node.href || "";
      if (!(href.includes("/posts/") || href.includes("/permalink/") || href.includes("story_fbid"))) return false;
      if (href.includes("comment_id=")) return false;
      const nodeRect = node.getBoundingClientRect();
      if (nodeRect.top > rect.top + 240) return false;
      const value = clean(node.getAttribute("aria-label") || node.getAttribute("title") || node.innerText || node.textContent || "");
      return value.length <= 80 && timeRegex.test(value);
    });

    const timeNode = postLinkTimeNode || Array.from(card.querySelectorAll('a, span, div')).find((node) => {
      if (isInsideSharedPost(node)) return false;
      const nodeRect = node.getBoundingClientRect();
      if (nodeRect.top < rect.top - 5 || nodeRect.top > rect.top + 240) return false;
      const value = clean(node.getAttribute("aria-label") || node.getAttribute("title") || node.innerText || node.textContent || "");
      return value.length <= 80 && timeRegex.test(value);
    });

    const postedAt = timeNode
      ? (() => {
          const ariaLabel = clean(timeNode.getAttribute("aria-label") || "");
          const titleAttr = clean(timeNode.getAttribute("title") || "");
          const absDateRe = /[A-Z][a-z]+\s+\d{1,2}(?:,\s*\d{4})?(?:\s+at\s+.+)?/i;
          if (absDateRe.test(titleAttr)) return titleAttr;
          if (absDateRe.test(ariaLabel)) return ariaLabel;
          return ariaLabel || titleAttr || clean(timeNode.innerText || timeNode.textContent);
        })()
      : null;
    const visualPostedAt = () => {
      const rows = new Map();
      Array.from(card.querySelectorAll('span, a, div')).forEach((node) => {
        const text = clean(node.innerText || node.textContent);
        if (!/^(\\d+|[mhdw]|mo|y|·)$/.test(text)) return;
        const nodeRect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        if (nodeRect.width <= 0 || nodeRect.height <= 0 || style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) return;
        if (style.position === "absolute") return;
        if (nodeRect.y > rect.top + 240) return;
        const y = Math.round(nodeRect.y);
        if (!rows.has(y)) rows.set(y, []);
        rows.get(y).push({ text, x: nodeRect.x });
      });
      for (const items of rows.values()) {
        const value = items.sort((a, b) => a.x - b.x).map((item) => item.text).join("").replace(/·/g, "");
        const match = value.match(/\\d+(?:m|h)/i);
        if (match) return match[0];
      }
      return null;
    };

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

    const headerTime = headerPostedAt();
    const parts = [author, headerTime || postedAt, ...storyTexts].filter(Boolean);
    const backgroundImages = Array.from(card.querySelectorAll('*'))
      .map((node) => {
        const style = window.getComputedStyle(node);
        const bg = style.backgroundImage || "";
        const match = bg.match(/url\\(["']?(.*?)["']?\\)/);
        const nodeRect = node.getBoundingClientRect();
        return match ? {
          src: match[1],
          alt: "",
          width: Math.round(nodeRect.width) || null,
          height: Math.round(nodeRect.height) || null,
          perfLogName: "backgroundImage"
        } : null;
      })
      .filter(Boolean);
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
    const allImages = [...images, ...backgroundImages].filter((img, imageIndex, arr) => {
      const key = `${img.src}|${img.alt}`;
      return arr.findIndex((other) => `${other.src}|${other.alt}` === key) === imageIndex;
    });

    cards.push({
      author,
      postedAt: headerTime || postedAt || visualPostedAt(),
      url: urlNode ? urlNode.href : null,
      parts,
      text: storyTexts.join("\\n").trim(),
      rawText: rawParts.join("\\n"),
      reactionsText: reactionText,
      commentCountText,
      shareCountText,
      comments,
      images: allImages,
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

OPEN_RELEVANT_COMMENTS_SORT_SCRIPT = """
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
  if (/^Most relevant$/i.test(current)) return "already_relevant_comments";
  trigger.click();
  return `opened_${current}`;
}
"""

SELECT_RELEVANT_COMMENTS_SORT_SCRIPT = """
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
      return /^Most relevant$/i.test(text) || /^Most relevant\\s+/i.test(text);
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

OPEN_NEW_POSTS_SORT_SCRIPT = """
() => {
  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
  const visible = (node) => {
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const labels = ["Most relevant", "Recent activity", "New posts"];
  const matchingLabel = (text) => labels.find((label) => text === label || text.startsWith(`${label} `));
  const candidates = Array.from(document.querySelectorAll('span, div, [role="button"]'))
    .filter((node) => matchingLabel(clean(node.textContent)))
    .filter(visible)
    .filter((node) => {
      const rect = node.getBoundingClientRect();
      return rect.width < 700 && rect.height < 160;
    });
  const trigger = candidates[candidates.length - 1];
  if (!trigger) return "not_found";
  const current = matchingLabel(clean(trigger.textContent));
  if (/^New posts$/i.test(current)) return "already_new_posts";
  trigger.click();
  return `opened_${current}`;
}
"""

SELECT_NEW_POSTS_SORT_SCRIPT = """
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
      return /^New posts$/i.test(text) || /^New posts\\s+/i.test(text);
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
        "--max-subcomments",
        type=int,
        default=int(os.getenv("MAX_SUBCOMMENTS_PER_COMMENT", "3")),
        help="Maximum replies/subcomments to keep under each parent comment.",
    )
    parser.add_argument(
        "--comment-expand-rounds",
        type=int,
        default=int(os.getenv("COMMENT_EXPAND_ROUNDS", "1")),
        help="How many times to click comment/reply expansion controls per visible post. Use 0 to avoid expansion in fast mode.",
    )
    parser.add_argument(
        "--comment-sort",
        choices=("relevant", "all"),
        default=os.getenv("COMMENT_SORT", "relevant").lower(),
        help="Use Facebook's relevant comments for speed, or all comments for exhaustive scraping.",
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
    parser.add_argument(
        "--parallel-workers",
        type=int,
        default=int(os.getenv("PARALLEL_WORKERS", "1")),
        help="Number of post permalink pages to scrape in parallel. Keep this low; 2-4 is usually safest.",
    )
    parser.add_argument(
        "--parallel-profile-dirs",
        default=os.getenv("PARALLEL_PROFILE_DIRS", ""),
        help="Comma/newline-separated browser profile directories for true parallel scraping. Each profile must already be logged into Facebook.",
    )
    parser.add_argument(
        "--today-only",
        action=argparse.BooleanOptionalAction,
        default=env_bool("TODAY_ONLY", True),
        help="Stop scanning the New posts feed once a Yesterday/older post is reached.",
    )
    parser.add_argument(
        "--recover-urls",
        action=argparse.BooleanOptionalAction,
        default=env_bool("RECOVER_URLS", False),
        help="Try slower click/HTML recovery for feed posts that do not expose permalinks.",
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


def chronological_group_url(group_url: str) -> str:
    parsed = urlparse(group_url)
    query = parse_qs(parsed.query)
    query["sorting_setting"] = ["CHRONOLOGICAL"]
    return urlunparse(
        parsed._replace(query=urlencode(query, doseq=True))
    )


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


def parse_profile_dirs(value: str, base_dir: Path) -> list[Path]:
    profiles: list[Path] = []
    seen: set[Path] = set()
    for item in re.split(r"[\n,]+", value or ""):
        item = item.strip()
        if not item:
            continue
        path = Path(item)
        if not path.is_absolute():
            path = base_dir / path
        path = path.resolve()
        if path in seen:
            continue
        seen.add(path)
        profiles.append(path)
    return profiles


def warn_if_profile_locked(profile_dir: Path) -> None:
    lock_paths = [
        profile_dir / "SingletonLock",
        profile_dir / "SingletonSocket",
        profile_dir / "SingletonCookie",
    ]
    existing = [path.name for path in lock_paths if path.exists()]
    if existing:
        print(
            f"Profile {profile_dir} has active Chromium lock files ({', '.join(existing)}). "
            "Avoid running two jobs with the same profile at the same time."
        )


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


def post_urls_from_blob(blob: str, group_id: str) -> list[str]:
    blob = blob.replace("\\/", "/").replace("&amp;", "&")
    candidates: list[str] = []
    candidates += re.findall(
        rf"(?:https?:\/\/(?:www\.|m\.)?facebook\.com)?\/groups\/{re.escape(group_id)}\/(?:posts|permalink)\/([A-Za-z0-9_.:-]+)",
        blob,
    )
    candidates += re.findall(r'["?&](?:story_fbid|multi_permalinks)=([A-Za-z0-9_.:-]+)', blob)

    urls: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = f"https://www.facebook.com/groups/{group_id}/posts/{candidate.split(',')[0]}/"
        url = normalize_post_url(value, group_id)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def recover_visible_post_url(
    page: Page,
    group_id: str,
    raw_url: str | None,
    text: str,
    author: str | None = None,
    article: Locator | None = None,
) -> str | None:
    url = normalize_post_url(raw_url or "", group_id)
    if url:
        return url

    blobs: list[str] = []
    if article is not None:
        try:
            blobs.append(article.evaluate("(node) => node.outerHTML || ''"))
        except Exception:
            pass

    try:
        html = page.content()
        needle = (text or author or "").strip()
        if needle and needle in html:
            index = html.find(needle)
            blobs.append(html[max(0, index - 180_000): index + 180_000])
        elif author and author in html:
            index = html.find(author)
            blobs.append(html[max(0, index - 180_000): index + 180_000])
        if len(html) < 3_000_000:
            blobs.append(html)
    except Exception:
        pass

    for blob in blobs:
        urls = post_urls_from_blob(blob, group_id)
        if urls:
            return urls[0]
    return None


def recover_visible_post_url_by_time_click(
    page: Page,
    group_id: str,
    group_url: str,
    left: object,
    top: object,
) -> str | None:
    try:
        x = int(float(left)) + 60
        y = int(float(top)) + 34
    except (TypeError, ValueError):
        return None

    before_url = page.url
    try:
        page.mouse.click(x, y)
        page.wait_for_timeout(2_500)
        url = normalize_post_url(page.url, group_id)
        if url:
            safe_goto(page, group_url)
            page.wait_for_timeout(2_000)
            set_feed_sort_recent(page)
            return url
        if page.url != before_url:
            safe_goto(page, group_url)
            page.wait_for_timeout(2_000)
            set_feed_sort_recent(page)
    except Exception:
        try:
            safe_goto(page, group_url)
            page.wait_for_timeout(1_000)
            set_feed_sort_recent(page)
        except Exception:
            pass
    return None


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


def safe_goto(
    page: Page,
    url: str,
    wait_until: str = "domcontentloaded",
    timeout: int = 90_000,
    retries: int = 2,
) -> bool:
    for attempt in range(retries + 1):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
            return True
        except Exception as exc:
            print(f"Navigation attempt {attempt + 1}/{retries + 1} failed for {url}: {exc}")
            if attempt >= retries:
                try:
                    page.goto(url, wait_until="commit", timeout=30_000)
                    page.wait_for_timeout(5_000)
                    return True
                except Exception as fallback_exc:
                    print(f"Fallback navigation failed for {url}: {fallback_exc}")
                    return False
            page.wait_for_timeout(3_000)
    return False


def click_visible_buttons(page: Page, patterns: list[str], max_clicks: int = 10) -> int:
    clicked = 0
    for pattern in patterns:
        regex = re.compile(pattern, re.IGNORECASE)
        candidates = [
            page.get_by_role("button", name=regex),
            page.get_by_text(regex),
            page.locator("span, div, a").filter(has_text=regex),
        ]
        for buttons in candidates:
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
    try:
        state = page.evaluate(OPEN_NEW_POSTS_SORT_SCRIPT)
        if state == "already_new_posts":
            print("Feed sort already set to New posts.")
            return
        if state and state.startswith("opened_"):
            page.wait_for_timeout(700)
            if page.evaluate(SELECT_NEW_POSTS_SORT_SCRIPT):
                page.wait_for_timeout(2_000)
                print("Feed sort set to New posts.")
                return
    except Exception:
        pass

    sort_labels = re.compile(r"^(Most relevant|Recent activity|New posts)$", re.IGNORECASE)
    try:
        clicked = click_first_visible(page.get_by_text(sort_labels), timeout=3_000, from_last=True)
        if clicked:
            page.wait_for_timeout(700)
    except Exception:
        pass

    try:
        if click_first_visible(page.get_by_text("New posts", exact=True), timeout=3_000, from_last=True):
            page.wait_for_timeout(2_000)
            print("Feed sort set to New posts.")
            return
    except Exception:
        pass

    print("Could not switch feed sort to New posts. Continuing with chronological URL sort.")


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


def expand_visible_post_bodies(page: Page, max_clicks: int = 8) -> int:
    """Expand visible post body truncation without touching comment controls."""
    clicked = 0
    selectors = [
        '[data-ad-rendering-role="story_message"] div[role="button"]:has-text("See more")',
        '[data-ad-rendering-role="story_message"] span[role="button"]:has-text("See more")',
        '[data-ad-rendering-role="story_message"] div:text-is("See more")',
        '[data-ad-rendering-role="story_message"] span:text-is("See more")',
    ]
    for selector in selectors:
        controls = page.locator(selector)
        count = min(controls.count(), max_clicks - clicked)
        for index in range(count):
            try:
                control = controls.nth(index)
                if control.is_visible(timeout=500):
                    control.scroll_into_view_if_needed(timeout=1_000)
                    control.click(timeout=1_500)
                    clicked += 1
                    page.wait_for_timeout(400)
            except Exception:
                continue
            if clicked >= max_clicks:
                return clicked
    return clicked


def expand_article_post_body(article: Locator, max_clicks: int = 2) -> int:
    clicked = 0
    selectors = [
        '[data-ad-rendering-role="story_message"] div[role="button"]:has-text("See more")',
        '[data-ad-rendering-role="story_message"] span[role="button"]:has-text("See more")',
        '[data-ad-rendering-role="story_message"] div:text-is("See more")',
        '[data-ad-rendering-role="story_message"] span:text-is("See more")',
    ]
    for selector in selectors:
        controls = article.locator(selector)
        count = min(controls.count(), max_clicks - clicked)
        for index in range(count):
            try:
                control = controls.nth(index)
                if control.is_visible(timeout=500):
                    control.scroll_into_view_if_needed(timeout=1_000)
                    control.click(timeout=1_500)
                    clicked += 1
            except Exception:
                continue
            if clicked >= max_clicks:
                return clicked
    return clicked


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


def set_comments_sort_relevant(page: Page) -> None:
    try:
        state = page.evaluate(OPEN_RELEVANT_COMMENTS_SORT_SCRIPT)
        if state == "already_relevant_comments":
            print("Comments sort already set to Most relevant.")
            return
        if state and state.startswith("opened_"):
            page.wait_for_timeout(500)
            if page.evaluate(SELECT_RELEVANT_COMMENTS_SORT_SCRIPT):
                page.wait_for_timeout(800)
                print("Comments sort set to Most relevant.")
                return
    except Exception:
        pass

    sort_label = re.compile(r"^(Most relevant|Newest|All comments)$", re.IGNORECASE)
    try:
        if click_first_visible(page.get_by_text(sort_label), timeout=2_000, from_last=True):
            page.wait_for_timeout(500)
            if click_first_visible(page.get_by_text("Most relevant", exact=True), timeout=2_000, from_last=True):
                page.wait_for_timeout(800)
                print("Comments sort set to Most relevant.")
    except Exception:
        pass


def set_comments_sort(page: Page, comment_sort: str) -> None:
    if comment_sort == "all":
        set_comments_sort_all(page)
    else:
        set_comments_sort_relevant(page)


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


def expand_post_page_comments(page: Page, rounds: int, comment_sort: str = "relevant") -> None:
    set_comments_sort(page, comment_sort)
    max_rounds = max(0, rounds)
    idle_rounds = 0
    for round_index in range(max_rounds):
        clicked = expand_visible_content(page)
        if round_index == 0 or round_index % 3 == 2:
            set_comments_sort(page, comment_sort)
        clicked += click_visible_buttons(
            page,
            [
                r"View more comments",
                r"View previous comments",
                r"View more replies",
                r"View \d+ more repl",
                r"\d+\s+repl",
                r"See more",
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
    text = str(comment.get("text") or "")
    text = re.sub(r"^(?:Â·|·)\s*", "", text).strip()
    return "|".join(
        [
            comment.get("author") or "",
            comment.get("posted_at") or "",
            text,
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


def strip_comment_internals(comment: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in comment.items() if not str(key).startswith("_") and key not in {"subcomments", "subcomments_found"}}


def count_nested_subcomments(comments: list[dict[str, object]]) -> int:
    total = 0
    for comment in comments:
        subcomments = comment.get("subcomments") if isinstance(comment, dict) else None
        if isinstance(subcomments, list):
            total += len(subcomments)
            total += count_nested_subcomments([item for item in subcomments if isinstance(item, dict)])
    return total


def nest_subcomments(comments: list[dict[str, object]], max_subcomments: int) -> list[dict[str, object]]:
    if max_subcomments <= 0:
        return [strip_comment_internals(comment) for comment in comments]

    left_values = [
        int(comment.get("_left") or 0)
        for comment in comments
        if isinstance(comment.get("_left"), int) and int(comment.get("_left") or 0) > 0
    ]
    baseline_left = min(left_values) if left_values else 0
    nested: list[dict[str, object]] = []
    current_parent: dict[str, object] | None = None

    for comment in comments:
        left = int(comment.get("_left") or baseline_left or 0)
        is_reply = bool(baseline_left and left > baseline_left + 24 and current_parent is not None)
        if is_reply:
            replies = current_parent.setdefault("subcomments", [])
            if isinstance(replies, list) and len(replies) < max_subcomments:
                replies.append(strip_comment_internals(comment))
            continue

        parent = dict(comment)
        parent.setdefault("subcomments", [])
        nested.append(parent)
        current_parent = parent

    return [strip_comment_internals(comment) for comment in nested]


def limit_parent_comments(comments: list[dict[str, object]], max_comments: int) -> list[dict[str, object]]:
    if max_comments <= 0:
        return comments
    return comments[:max_comments]


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
    comment_sort: str = "relevant",
) -> list[dict[str, str | None]]:
    comments: list[dict[str, str | None]] = []
    set_comments_sort(page, comment_sort)
    max_rounds = max(0, rounds)
    idle_rounds = 0

    comments = merge_comment_lists(comments, extract_comments_from_page(page, max_comments), max_comments)
    if max_comments > 0 and len(comments) >= max_comments:
        print(f"Progressive comment scan collected {len(comments)} unique rows.")
        return comments

    for round_index in range(max_rounds):
        clicked = expand_visible_content(page)
        if round_index == 0 or round_index % 3 == 2:
            set_comments_sort(page, comment_sort)
        clicked += click_visible_buttons(
            page,
            [
                r"View more comments",
                r"View previous comments",
                r"View more replies",
                r"View \d+ more repl",
                r"\d+\s+repl",
                r"See more",
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
        value = re.sub(r"\b(Like|Reply|Share|Follow|Following|See translation)\b", " ", line, flags=re.IGNORECASE)
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
    cleaned = re.sub(r"^(?:Â·|·)\s*", "", cleaned).strip()
    return cleaned


def clean_comment_author(author: object) -> str | None:
    value = re.sub(r"\s+", " ", str(author or "")).strip()
    if not value:
        return None
    value = re.sub(r"\s+about$", "", value, flags=re.IGNORECASE).strip()
    if COMMENT_TIME_PATTERN.match(value):
        return None
    if re.match(r"^(Like|Reply|Share|Comment|Follow|Following|See translation)$", value, re.IGNORECASE):
        return None
    return value or None


def clean_comment_payload(details: dict[str, object]) -> tuple[str | None, str | None, str]:
    author = clean_comment_author(details.get("author"))
    posted_at = str(details.get("postedAt") or "").strip() or None
    text = clean_comment_text(str(details.get("text") or "").strip(), author, posted_at)
    if author:
        text = re.sub(rf"^{re.escape(author)}\s*", "", text).strip()
    return author, posted_at, text


def is_nested_article(article: Locator) -> bool:
    try:
        return bool(article.evaluate("(node) => !!node.parentElement?.closest('[role=\"article\"]')"))
    except Exception:
        return True


def is_comment_article(article: Locator) -> bool:
    try:
        return bool(
            article.evaluate(
                """
                (node) => {
                  const aria = (node.getAttribute("aria-label") || "").toLowerCase();
                  if (aria.startsWith("comment by ") || aria.includes(" comment by ")) return true;
                  if (node.matches("[data-commentid]") || node.querySelector("[data-commentid]")) return true;
                  const text = (node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim();
                  return /^comment by\\s+/i.test(text);
                }
                """
            )
        )
    except Exception:
        return False


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
        dom_counts = article.evaluate(ENGAGEMENT_COUNTERS_SCRIPT)
    except Exception:
        pass
    try:
        page_counts = page.locator("body").evaluate(ENGAGEMENT_COUNTERS_SCRIPT)
        dom_counts = {
            "reactions_text": better_count_text(dom_counts.get("reactions_text"), page_counts.get("reactions_text")),
            "comment_count_text": better_count_text(dom_counts.get("comment_count_text"), page_counts.get("comment_count_text")),
            "share_count_text": better_count_text(dom_counts.get("share_count_text"), page_counts.get("share_count_text")),
        }
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


def extract_styled_text(locator: Locator) -> str:
    try:
        value = locator.evaluate(STYLED_TEXT_SCRIPT)
        return value if isinstance(value, str) else ""
    except Exception:
        return ""


def extract_main_post_text(locator: Locator, post_id: str | None = None) -> str:
    try:
        value = locator.evaluate(MAIN_POST_TEXT_SCRIPT, {"postId": post_id})
        return value if isinstance(value, str) else ""
    except Exception:
        return ""


def extract_owner_post_text_from_page_text(page_text: str) -> str:
    post_marker = re.search(r"'s post\b", page_text)
    if not post_marker:
        return ""
    owner = re.sub(r"\s+", " ", page_text[max(0, post_marker.start() - 140):post_marker.start()]).strip()
    for marker in ("Create group chat", "Group chats", "Contacts", "Facebook"):
        if marker in owner:
            owner = owner.rsplit(marker, 1)[-1].strip()
    owner = owner[-80:].strip()
    if not owner:
        return ""

    pattern = re.compile(
        rf"\bGold Now\s+{re.escape(owner)}\s+·.+?·\s+(.+?)(?:\s+See translation\b|\s+\d+\s+\d+\s+(?:Most relevant|All comments)\b|\s+(?:Most relevant|All comments)\b)",
        re.DOTALL,
    )
    match = pattern.search(page_text)
    if not match:
        return ""

    text = re.sub(r"\s+", " ", match.group(1)).strip()
    text = re.sub(r"\b(?:Like|Reply|Share|Comment|Follow|Following)\b.*$", "", text).strip()
    text = re.sub(r"\s+\d+\s+\d+\s*$", "", text).strip()
    return text


def extract_owner_post_summary_from_page_text(page_text: str) -> dict[str, str | None]:
    post_marker = re.search(r"'s post\b", page_text)
    empty = {"text": None, "reactions_text": None, "comment_count_text": None, "share_count_text": None}
    if not post_marker:
        return empty

    owner = re.sub(r"\s+", " ", page_text[max(0, post_marker.start() - 140):post_marker.start()]).strip()
    for marker in ("Create group chat", "Group chats", "Contacts", "Facebook"):
        if marker in owner:
            owner = owner.rsplit(marker, 1)[-1].strip()
    owner = owner[-80:].strip()
    if not owner:
        return empty

    sep = f"(?:{re.escape('·')}|{re.escape('Â·')})"
    count_pattern = re.compile(
        rf"\bGold Now\s+{re.escape(owner)}\s+{sep}.+?{sep}\s+(.+?)(?:\s+See translation\b)?\s+(\d+(?:[,.]\d+)*)\s+(\d+(?:[,.]\d+)*)\s+(?:Most relevant|All comments)\b",
        re.DOTALL,
    )
    match = count_pattern.search(page_text)
    if match:
        text = re.sub(r"\s+", " ", match.group(1)).strip()
        text = re.sub(r"\b(?:Like|Reply|Share|Comment|Follow|Following)\b.*$", "", text).strip()
        text = re.sub(r"\s+\d+\s+\d+\s*$", "", text).strip()
        return {
            "text": text,
            "reactions_text": f"{match.group(2).replace(',', '')} reactions",
            "comment_count_text": f"{match.group(3).replace(',', '')} comments",
            "share_count_text": None,
        }

    text_pattern = re.compile(
        rf"\bGold Now\s+{re.escape(owner)}\s+{sep}.+?{sep}\s+(.+?)(?:\s+See translation\b|\s+\d+\s+\d+\s+(?:Most relevant|All comments)\b|\s+(?:Most relevant|All comments)\b)",
        re.DOTALL,
    )
    text_match = text_pattern.search(page_text)
    if not text_match:
        return empty
    text = re.sub(r"\s+", " ", text_match.group(1)).strip()
    text = re.sub(r"\b(?:Like|Reply|Share|Comment|Follow|Following)\b.*$", "", text).strip()
    text = re.sub(r"\s+\d+\s+\d+\s*$", "", text).strip()
    return {"text": text, "reactions_text": None, "comment_count_text": None, "share_count_text": None}


def extract_owner_post_text_from_page_text(page_text: str) -> str:
    return extract_owner_post_summary_from_page_text(page_text).get("text") or ""


def extract_owner_post_summary_from_page_text(page_text: str) -> dict[str, str | None]:
    empty = {
        "author": None,
        "posted_at": None,
        "text": None,
        "reactions_text": None,
        "comment_count_text": None,
        "share_count_text": None,
    }
    post_marker = re.search(r"'s post\b", page_text)
    if not post_marker:
        return empty

    owner = re.sub(r"\s+", " ", page_text[max(0, post_marker.start() - 140):post_marker.start()]).strip()
    for marker in ("Create group chat", "Group chats", "Contacts", "Facebook"):
        if marker in owner:
            owner = owner.rsplit(marker, 1)[-1].strip()
    owner = owner[-80:].strip()
    if not owner:
        return empty

    result = dict(empty)
    result["author"] = owner
    dot = chr(0x00B7)
    mojibake_dot = chr(0x00C2) + chr(0x00B7)
    sep = f"(?:{re.escape(dot)}|{re.escape(mojibake_dot)})"

    def clean_owner_text(value: str) -> str:
        value = re.sub(r"\s+", " ", value).strip()
        value = re.sub(r"\b(?:Like|Reply|Share|Comment|Follow|Following)\b.*$", "", value).strip()
        return re.sub(r"\s+\d+\s+\d+\s*$", "", value).strip()

    def posted_at_from_meta(meta: str) -> str | None:
        meta = re.sub(r"\s+", " ", meta).strip()
        match = re.search(r"\b(Just now|Yesterday|\d+\s*(?:m|h|d|w|mo|y))\b", meta, re.IGNORECASE)
        return match.group(1).replace(" ", "") if match else None

    count_pattern = re.compile(
        rf"\bGold Now\s+{re.escape(owner)}\s+{sep}\s+(.+?){sep}\s+(.+?)(?:\s+See translation\b)?\s+(\d+(?:[,.]\d+)*)\s+(\d+(?:[,.]\d+)*)\s+(?:Most relevant|All comments)\b",
        re.DOTALL,
    )
    match = count_pattern.search(page_text)
    if match:
        result["posted_at"] = posted_at_from_meta(match.group(1))
        result["text"] = clean_owner_text(match.group(2))
        result["reactions_text"] = f"{match.group(3).replace(',', '')} reactions"
        result["comment_count_text"] = f"{match.group(4).replace(',', '')} comments"
        return result

    text_pattern = re.compile(
        rf"\bGold Now\s+{re.escape(owner)}\s+{sep}\s+(.+?){sep}\s+(.+?)(?:\s+See translation\b|\s+\d+\s+\d+\s+(?:Most relevant|All comments)\b|\s+(?:Most relevant|All comments)\b)",
        re.DOTALL,
    )
    text_match = text_pattern.search(page_text)
    if text_match:
        result["posted_at"] = posted_at_from_meta(text_match.group(1))
        result["text"] = clean_owner_text(text_match.group(2))
    return result


def extract_owner_post_text_from_page_text(page_text: str) -> str:
    return extract_owner_post_summary_from_page_text(page_text).get("text") or ""


def strip_spaced_noise_prefix(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if not text:
        return ""

    separator_match = re.search(r"\s*(?:Â·|·|\u00b7)\s*", text)
    if not separator_match:
        return text

    prefix = text[: separator_match.start()].strip()
    suffix = text[separator_match.end() :].strip()
    tokens = prefix.split()
    if not suffix or len(tokens) < 12:
        return text

    short_noise_tokens = [
        token
        for token in tokens
        if re.fullmatch(r"[A-Za-z0-9]", token) or re.fullmatch(r"\d{1,2}", token)
    ]
    if len(short_noise_tokens) / max(1, len(tokens)) >= 0.85:
        return suffix
    return text


def clean_post_body_text(value: str | None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = strip_spaced_noise_prefix(text)
    return re.sub(r"\s+", " ", text).strip()


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
    text = clean_post_body_text(text)

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
    body = page.locator("body")
    page_text = text_from(body)
    fallback_text = extract_styled_text(body)
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
        safe_goto(page, url)
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


def expand_post_body_see_more(page: Page) -> None:
    """Click the 'See more' button that expands the main post body text."""
    selectors = [
        '[data-ad-rendering-role="story_message"] div[role="button"]:has-text("See more")',
        '[data-ad-rendering-role="story_message"] span[role="button"]:has-text("See more")',
        '[data-ad-rendering-role="story_message"] div:text-is("See more")',
        '[data-ad-rendering-role="story_message"] span:text-is("See more")',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=800):
                btn.click(timeout=1_500)
                page.wait_for_timeout(600)
                return
        except Exception:
            continue
    # Fallback: any exact "See more" inside the first non-comment article
    try:
        articles = page.locator("div[role='article']")
        for i in range(min(articles.count(), 3)):
            article = articles.nth(i)
            btn = article.get_by_text("See more", exact=True).first
            if btn.is_visible(timeout=400):
                btn.click(timeout=1_500)
                page.wait_for_timeout(600)
                return
    except Exception:
        pass


def scrape_post_page(
    page: Page,
    post_url: str,
    max_comments: int,
    max_subcomments: int,
    comment_expand_rounds: int,
    debug_dir: str,
    comment_sort: str = "relevant",
) -> Post | None:
    print(f"Opening post: {post_url}")
    safe_goto(page, post_url)
    page.wait_for_timeout(3_000)
    expand_post_body_see_more(page)
    raw_comment_limit = max_comments * (max_subcomments + 1) if max_comments > 0 and max_subcomments > 0 else max_comments
    progressive_comments = collect_comments_while_expanding(
        page,
        raw_comment_limit,
        comment_expand_rounds,
        comment_sort,
    )

    top_articles = [
        page.locator("div[role='article']").nth(index)
        for index in range(page.locator("div[role='article']").count())
    ]

    for article in top_articles:
        if is_nested_article(article) or is_comment_article(article):
            continue

        if comment_expand_rounds > 0:
            expand_article_comments(page, article, comment_expand_rounds)
        details = extract_post_details(article)
        text_parts = extract_text_parts(article)
        details = enrich_details_from_parts(details, text_parts)
        engagement = extract_engagement_text(page, article)
        details["url"] = post_url
        page_text = text_from(page.locator("body"))
        owner_summary = extract_owner_post_summary_from_page_text(page_text)
        if owner_summary.get("author"):
            details["author"] = owner_summary["author"]
        if owner_summary.get("posted_at"):
            details["posted_at"] = owner_summary["posted_at"]
        if owner_summary.get("text"):
            details["text"] = owner_summary["text"]
        for key in ("reactions_text", "comment_count_text", "share_count_text"):
            if owner_summary.get(key):
                engagement[key] = owner_summary[key]
        if len(details["text"] or details["raw_text"] or "") < 3:
            continue

        comments = merge_comment_lists(progressive_comments, extract_comments(article, raw_comment_limit), raw_comment_limit)
        page_comments = extract_comments_from_page(page, raw_comment_limit)
        comments = merge_comment_lists(comments, page_comments, raw_comment_limit)
        comments = limit_parent_comments(nest_subcomments(comments, max_subcomments), max_comments)

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
        if not post.comments and parse_count_text(post.comment_count_text):
            print("No comments found on desktop post page. Trying mobile post page.")
            safe_goto(page, mobile_url(post_url))
            page.wait_for_timeout(3_000)
            mobile_rounds = max(3, comment_expand_rounds // 2) if comment_expand_rounds > 0 else 0
            post.comments = merge_comment_lists(
                post.comments,
                collect_comments_while_expanding(page, raw_comment_limit, mobile_rounds, comment_sort),
                raw_comment_limit,
            )
            post.comments = limit_parent_comments(nest_subcomments(post.comments, max_subcomments), max_comments)
            if not post.comments:
                debug_paths = write_debug_files(page, debug_dir, post.id)
                print(
                    "No comments extracted for this post. "
                    f"Debug text: {debug_paths.text_dump}, screenshot: {debug_paths.screenshot}"
                )
        return post

    page_comments = merge_comment_lists(
        progressive_comments,
        extract_comments_from_page(page, raw_comment_limit),
        raw_comment_limit,
    )
    page_comments = limit_parent_comments(nest_subcomments(page_comments, max_subcomments), max_comments)
    body = page.locator("body")
    page_text = text_from(body)
    fallback_post_id = post_id_from_url(post_url)
    owner_summary = extract_owner_post_summary_from_page_text(page_text)
    fallback_text = (
        owner_summary.get("text")
        or extract_main_post_text(body, fallback_post_id)
        or extract_styled_text(body)
    )
    fallback_engagement = extract_engagement_text(page, body)
    for key in ("reactions_text", "comment_count_text", "share_count_text"):
        if owner_summary.get(key):
            fallback_engagement[key] = owner_summary[key]
    debug_paths = write_debug_files(page, debug_dir, fallback_post_id)
    print(
        "Could not extract a clean main post article. "
        f"Debug text: {debug_paths.text_dump}, screenshot: {debug_paths.screenshot}"
    )

    # Return only a low-priority row. merge_posts() will keep this only when it has
    # useful comments and no better visible-card version exists. It will not save a
    # giant Facebook shell dump as the final post text.
    return Post(
        id=post_id_from_url(post_url),
        author=owner_summary.get("author"),
        posted_at=owner_summary.get("posted_at"),
        url=post_url,
        text=fallback_text,
        raw_text=page_text[:1200],
        text_parts=[fallback_text] if fallback_text else [],
        reactions_text=fallback_engagement["reactions_text"],
        comment_count_text=fallback_engagement["comment_count_text"],
        share_count_text=fallback_engagement["share_count_text"],
        comments=page_comments,
        images=[],
    )


def collect_posts(
    page: Page,
    max_posts: int,
    max_comments: int,
    comment_expand_rounds: int,
    today_only: bool = True,
) -> list[Post]:
    posts: list[Post] = []
    seen: set[str] = set()
    article_count = page.locator("div[role='article']").count()

    for index in range(article_count):
        article = page.locator("div[role='article']").nth(index)
        if is_nested_article(article) or is_comment_article(article):
            continue

        expand_article_comments(page, article, comment_expand_rounds)
        details = extract_post_details(article)
        text_parts = extract_text_parts(article)
        details = enrich_details_from_parts(details, text_parts)
        if today_only and is_yesterday_or_older_post(details.get("posted_at")):
            print(f"Skipping older fallback row ({details.get('posted_at')}).")
            continue
        if details.get("url") and "comment_id=" in str(details.get("url")):
            group_id = group_id_from_url(page.url) if "/groups/" in page.url else ""
            details["url"] = normalize_post_url(str(details["url"]), group_id) if group_id else None
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
    ignored = {
        value
        for value in [
            author,
            posted_at,
            "Admin",
            "Author",
            "·",
            "See translation",
            "Edited",
            "Follow",
            "Following",
        ]
        if value
    }
    body_parts = []
    for part in parts:
        if part in ignored:
            continue
        if re.search(r"^(Admin|Author|Like|Reply|Share|Comment|GIF|Follow|Following|See translation|Edited)$", part, re.I):
            continue
        if re.search(r"^\d+\s*(comments?|shares?|likes?|reactions?)?$", part, re.I):
            continue
        if re.search(r"^Comment as ", part, re.I):
            continue
        body_parts.append(part)

    text = " ".join(body_parts).strip()
    if author and text.startswith(author):
        text = text[len(author) :].strip()
    return clean_post_body_text(text)


def posted_at_from_text(value: str) -> str | None:
    match = re.search(
        r"\b(Just now|Yesterday|\d+\s*(?:m|h|d|w|mo|y))\b",
        value or "",
        re.IGNORECASE,
    )
    return match.group(1).replace(" ", "") if match else None


def is_yesterday_or_older_post(posted_at: str | None) -> bool:
    from datetime import datetime as _dt
    value = " ".join((posted_at or "").split()).strip().lower()
    if not value:
        return False
    if value.startswith("yesterday"):
        return True
    if re.match(r"^(?:a|an|\d+)\s+(?:day|week|month|year)s?\s+ago$", value):
        return True
    if re.match(r"^\d+\s*(?:d|w|mo|y)\b", value):
        return True
    # Parse absolute date strings: "May 31 at 5:49 AM", "Saturday, May 31, 2026 at 5:49 AM"
    _months = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})", value)
    if m:
        try:
            month = _months[m.group(1)[:3]]
            day = int(m.group(2))
            yr_m = re.search(r"\b(20\d{2})\b", value)
            year = int(yr_m.group(1)) if yr_m else _dt.now().year
            post_date = _dt(year, month, day)
            today_midnight = _dt.now().replace(hour=0, minute=0, second=0, microsecond=0)
            return post_date < today_midnight
        except Exception:
            return True  # if parse fails, treat as old
    return False


def normalize_group_url(value: str | None) -> str:
    try:
        group_id = group_id_from_url(value or "")
    except Exception:
        return (value or "").split("?")[0].rstrip("/")
    return f"https://www.facebook.com/groups/{group_id}"


def new_posts_heading_bottom(page: Page) -> float | None:
    try:
        value = page.evaluate(
            """
            () => {
              const clean = (text) => (text || "").replace(/\\s+/g, " ").trim();
              const candidates = Array.from(document.querySelectorAll('span, div, h2, h3'))
                .filter((node) => /^New posts$/i.test(clean(node.innerText || node.textContent)))
                .map((node) => {
                  const rect = node.getBoundingClientRect();
                  const style = window.getComputedStyle(node);
                  return {
                    bottom: rect.bottom,
                    top: rect.top,
                    width: rect.width,
                    height: rect.height,
                    visible: rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none"
                  };
                })
                .filter((item) => item.visible)
                .sort((a, b) => a.top - b.top);
              const heading = candidates[0];
              return heading ? heading.bottom : null;
            }
            """
        )
        return float(value) if value is not None else None
    except Exception:
        return None


def collect_visible_feed_posts(
    page: Page,
    max_posts: int,
    max_comments: int,
    today_only: bool = True,
    recover_urls: bool = False,
) -> tuple[list[Post], bool]:
    posts: list[Post] = []
    seen: set[str] = set()
    reached_old_posts = False
    older_cards_seen = 0
    old_stop_threshold = 1 if today_only else 3
    expanded_bodies = expand_visible_post_bodies(page)
    if expanded_bodies:
        page.wait_for_timeout(500)
    try:
        visual_cards = page.evaluate(FEED_CARDS_FROM_PAGE_SCRIPT)
    except Exception:
        visual_cards = []
    if today_only:
        heading_bottom = new_posts_heading_bottom(page)
        if heading_bottom is not None:
            before_count = len(visual_cards)
            visual_cards = [
                card
                for card in visual_cards
                if float(card.get("top") or 0) > heading_bottom
            ]
            skipped = before_count - len(visual_cards)
            if skipped:
                print(f"Skipped {skipped} cards above the New posts feed.")

    group_id = group_id_from_url(page.url) if "/groups/" in page.url else ""
    for index, card in enumerate(visual_cards):
        parts = [part for part in card.get("parts", []) if isinstance(part, str)]
        author = card.get("author")
        posted_at = card.get("postedAt")
        text = post_text_from_feed_parts(parts, author, posted_at)
        raw_text = card.get("rawText") or ""
        if not posted_at:
            # Only scan the post's own styled text, not raw_text which includes comment timestamps.
            # A yesterday post with a comment from "2h ago" would otherwise appear to be recent.
            posted_at = posted_at_from_text(card.get("text") or "")

        # Use inline feed-card comments as a secondary age signal.
        # If every visible comment in the card is from yesterday/older, the post itself is old.
        if today_only and not is_yesterday_or_older_post(posted_at):
            feed_comments = card.get("comments") or []
            if feed_comments:
                comment_times = [c.get("postedAt") or "" for c in feed_comments]
                if all(is_yesterday_or_older_post(t) for t in comment_times if t):
                    posted_at = posted_at or "1d"  # signal old to stop-logic below

        if today_only and is_yesterday_or_older_post(posted_at):
            older_cards_seen += 1
            print(f"Skipping older post card ({posted_at}) during today-only feed scan.")
            if older_cards_seen >= old_stop_threshold:
                print(f"Reached {older_cards_seen} older post cards; stopping today-only feed scan.")
                reached_old_posts = True
                break
            continue
        if today_only and not posted_at and older_cards_seen > 0:
            # Past the old-post boundary — null-time posts here are likely old bumped posts.
            continue
        older_cards_seen = 0

        if not text and not raw_text:
            continue

        url = normalize_post_url(card.get("url") or "", group_id) if group_id else None
        if recover_urls and not url and group_id:
            url = recover_visible_post_url(
                page,
                group_id,
                card.get("url"),
                card.get("text") or text or raw_text,
                author,
            )
        if recover_urls and not url and group_id:
            url = recover_visible_post_url_by_time_click(
                page,
                group_id,
                page.url,
                card.get("left"),
                card.get("top"),
            )
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
            return posts, reached_old_posts

    # Reset the counter so the articles fallback can find image-only posts that were
    # invisible to FEED_CARDS_FROM_PAGE_SCRIPT (no story_message element).
    older_cards_seen = 0
    articles = page.locator("div[role='article']")

    for index in range(articles.count()):
        article = articles.nth(index)
        if is_nested_article(article) or is_comment_article(article):
            continue

        if expand_article_post_body(article):
            page.wait_for_timeout(300)

        try:
            card = article.evaluate(FEED_CARD_SCRIPT)
        except Exception:
            continue

        parts = [part for part in card.get("parts", []) if isinstance(part, str)]
        author = card.get("author")
        posted_at = card.get("postedAt")
        text = post_text_from_feed_parts(parts, author, posted_at)
        raw_text = card.get("rawText") or text_from(article)
        if not posted_at:
            posted_at = posted_at_from_text(card.get("text") or "")

        if today_only and not is_yesterday_or_older_post(posted_at):
            feed_comments = card.get("comments") or []
            if feed_comments:
                comment_times = [c.get("postedAt") or "" for c in feed_comments]
                if all(is_yesterday_or_older_post(t) for t in comment_times if t):
                    posted_at = posted_at or "1d"

        if today_only and is_yesterday_or_older_post(posted_at):
            older_cards_seen += 1
            print(f"Skipping older post card ({posted_at}) during today-only feed scan.")
            if older_cards_seen >= old_stop_threshold:
                print(f"Reached {older_cards_seen} older post cards; stopping today-only feed scan.")
                reached_old_posts = True
                break
            continue
        if today_only and not posted_at and older_cards_seen > 0:
            # Past the old-post boundary — null-time posts here are likely old bumped posts.
            continue
        older_cards_seen = 0

        if not text and not raw_text:
            continue

        group_id = group_id_from_url(page.url) if "/groups/" in page.url else ""
        url = normalize_post_url(card.get("url") or "", group_id) if group_id else None
        if recover_urls and not url and group_id:
            url = recover_visible_post_url(
                page,
                group_id,
                card.get("url"),
                text or raw_text,
                author,
                article,
            )
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

    return posts, reached_old_posts


def collect_visible_feed_posts_over_scrolls(
    page: Page,
    group_url: str,
    max_posts: int,
    max_comments: int,
    scrolls: int,
    today_only: bool = True,
    recover_urls: bool = False,
) -> list[Post]:
    if normalize_group_url(page.url) != normalize_group_url(group_url):
        safe_goto(page, group_url)
    page.wait_for_timeout(1_200)
    set_feed_sort_recent(page)

    posts: list[Post] = []
    for scroll in range(scrolls):
        close_floating_popups(page)
        visible_posts, reached_old_posts = collect_visible_feed_posts(
            page,
            max_posts,
            max_comments,
            today_only,
            recover_urls,
        )
        posts = merge_posts(posts, visible_posts, max_posts)
        print(f"Visible feed scan {scroll + 1}/{scrolls}: collected {len(posts)} post cards")
        if reached_old_posts:
            print("Today-only feed scan stopped at Yesterday/older post.")
            break
        if len(posts) >= max_posts:
            break
        page.mouse.wheel(0, 3_000)
        page.wait_for_timeout(700)

    return posts


def scrape_post_urls(
    context,
    first_page: Page,
    post_urls: list[str],
    max_comments: int,
    max_subcomments: int,
    comment_expand_rounds: int,
    debug_dir: str,
    parallel_workers: int,
    profile_dir: Path,
    parallel_profile_dirs: list[Path],
    group_url: str,
    headless: bool,
    comment_sort: str = "relevant",
) -> list[Post]:
    if not post_urls:
        return []

    usable_profiles = [
        path
        for path in parallel_profile_dirs
        if path.resolve() != profile_dir.resolve()
    ]
    worker_count = max(1, min(parallel_workers, 8, len(post_urls), len(usable_profiles) or 1))
    if parallel_workers > 1 and not usable_profiles:
        print(
            "Parallel workers requested, but no separate parallel_profile_dirs were provided. "
            "Using sequential scraping to protect result accuracy."
        )
        worker_count = 1
    elif parallel_workers > 1 and len(usable_profiles) < min(parallel_workers, len(post_urls)):
        print(
            f"Parallel workers requested: {parallel_workers}, but only {len(usable_profiles)} separate "
            f"parallel profiles are available. Using {worker_count} workers."
        )
    if worker_count == 1:
        posts = []
        for post_url in post_urls:
            post = scrape_post_page(
                first_page,
                post_url,
                max_comments,
                max_subcomments,
                comment_expand_rounds,
                debug_dir,
                comment_sort,
            )
            if post:
                posts.append(post)
        return posts

    print(f"Scraping {len(post_urls)} post URLs with {worker_count} separate logged-in profiles.")

    def scrape_one(post_url: str, worker_profile: Path) -> Post | None:
        with tempfile.TemporaryDirectory(prefix="fb-worker-") as tmp_dir:
            tmp_path = Path(tmp_dir)
            worker_output = tmp_path / "post.json"
            worker_csv = tmp_path / "post.csv"
            worker_debug = tmp_path / "debug"

            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--group-url",
                group_url,
                "--profile-dir",
                str(worker_profile),
                "--output-json",
                str(worker_output),
                "--output-csv",
                str(worker_csv),
                "--max-posts",
                "1",
                "--max-comments",
                str(max_comments),
                "--max-subcomments",
                str(max_subcomments),
                "--comment-expand-rounds",
                str(comment_expand_rounds),
                "--comment-sort",
                comment_sort,
                "--scrolls",
                "0",
                "--debug-dir",
                str(worker_debug),
                "--extra-post-urls",
                post_url,
                "--parallel-workers",
                "1",
                "--headless" if headless else "--no-headless",
            ]
            result = subprocess.run(
                command,
                cwd=str(Path(__file__).resolve().parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
            )
            for line in result.stdout.splitlines()[-20:]:
                print(f"[worker {post_id_from_url(post_url)}] {line}")
            if result.returncode != 0 or not worker_output.exists():
                raise RuntimeError(f"Worker exited with code {result.returncode}")

            data = json.loads(worker_output.read_text(encoding="utf-8"))
            rows = data.get("posts") or []
            if not rows:
                return None
            row = rows[0]
            return Post(
                id=str(row.get("id") or post_id_from_url(post_url)),
                author=row.get("author"),
                posted_at=row.get("posted_at"),
                url=row.get("url") or post_url,
                text=row.get("text") or row.get("post_text") or "",
                raw_text=row.get("raw_text") or "",
                text_parts=row.get("text_parts") or [],
                reactions_text=row.get("reactions_text"),
                comment_count_text=row.get("comment_count_text"),
                share_count_text=row.get("share_count_text"),
                comments=row.get("comments") or [],
                images=row.get("images") or row.get("post_images") or [],
            )

    posts: list[Post] = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_url = {
            executor.submit(scrape_one, post_url, usable_profiles[index % worker_count]): post_url
            for index, post_url in enumerate(post_urls)
        }
        for future in as_completed(future_to_url):
            post_url = future_to_url[future]
            try:
                post = future.result()
                if post:
                    posts.append(post)
                    print(f"Parallel scrape finished: {post_url}")
            except Exception as exc:
                print(f"Parallel scrape failed for {post_url}: {exc}")

    return posts


def click_comment_control_in_article(page: Page, article: Locator) -> bool:
    try:
        article.scroll_into_view_if_needed(timeout=1_000)
        page.wait_for_timeout(300)
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(500)
    except Exception:
        pass

    try:
        clicked = article.evaluate(
            """
            (root) => {
              const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const visible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
              };
              const candidates = Array.from(root.querySelectorAll('[data-ad-rendering-role="comment_button"], [aria-label], [role="button"], span, div'))
                .filter((node) => {
                  const label = clean(node.getAttribute("aria-label") || node.innerText || node.textContent);
                  return /^Comment$/i.test(label) || /^Leave a comment$/i.test(label) || /^\\d+\\s+comments?$/i.test(label);
                })
                .filter(visible)
                .sort((a, b) => b.getBoundingClientRect().y - a.getBoundingClientRect().y);
              const target = candidates[0];
              if (!target) return false;
              target.scrollIntoView({ block: "center", inline: "center" });
              target.click();
              return true;
            }
            """
        )
        if clicked:
            page.wait_for_timeout(2_000)
            return True
    except Exception:
        pass

    selectors = [
        "[aria-label='Comment']",
        "[aria-label*='Comment']",
        "[data-ad-rendering-role='comment_button']",
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
    max_subcomments: int,
    comment_expand_rounds: int,
    scrolls: int,
    debug_dir: str,
    today_only: bool = True,
    comment_sort: str = "relevant",
) -> list[Post]:
    posts: list[Post] = []
    safe_goto(page, group_url)
    page.wait_for_timeout(3_000)
    set_feed_sort_recent(page)

    for scroll in range(scrolls):
        close_floating_popups(page)
        articles = page.locator("div[role='article']")
        top_indices: list[int] = []
        for index in range(articles.count()):
            article = articles.nth(index)
            if not is_nested_article(article) and not is_comment_article(article):
                top_indices.append(index)

        print(f"Click scan {scroll + 1}/{scrolls}: {len(top_indices)} visible post cards")
        for index in top_indices:
            if len(posts) >= max_posts:
                return posts

            articles = page.locator("div[role='article']")
            if index >= articles.count():
                continue
            article = articles.nth(index)
            if is_nested_article(article) or is_comment_article(article):
                continue

            visible_post = None
            visible = collect_visible_post_from_article(page, article, index, max_comments)
            if visible:
                if today_only and is_yesterday_or_older_post(visible.posted_at):
                    print(f"Reached older post ({visible.posted_at}); stopping today-only click scan.")
                    return posts
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
                    max_subcomments,
                    comment_expand_rounds,
                    debug_dir,
                    comment_sort,
                )
                safe_goto(page, group_url)
                page.wait_for_timeout(2_000)
                for _ in range(scroll + 1):
                    page.mouse.wheel(0, 2_500)
                    page.wait_for_timeout(300)
            elif clicked:
                modal_comments = collect_comments_while_expanding(
                    page,
                    max_comments,
                    comment_expand_rounds,
                    comment_sort,
                )
                if visible_post and modal_comments:
                    modal_comments = nest_subcomments(modal_comments, max_subcomments)
                    visible_post.comments = merge_comment_lists(
                        visible_post.comments,
                        modal_comments,
                        max_comments,
                    )
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
    if not posted_at:
        posted_at = posted_at_from_text(raw_text)
    if not text and not raw_text:
        return None

    group_id = group_id_from_url(page.url) if "/groups/" in page.url else ""
    url = recover_visible_post_url(
        page,
        group_id,
        card.get("url"),
        text or raw_text,
        author,
        article,
    ) if group_id else None
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
    content_key = post_content_key(post)
    if content_key:
        keys.add(content_key)
    return {key for key in keys if key}


def normalize_post_key_text(value: str | None) -> str:
    text = re.sub(r"\s+", " ", value or "").strip().lower()
    return text[:300]


def first_content_image_src(post: Post) -> str:
    images = content_images(post.images or [])
    if not images:
        return ""
    return str(images[0].get("src") or "").split("?")[0]


def post_content_key(post: Post) -> str | None:
    text = normalize_post_key_text(post.text)
    image_src = first_content_image_src(post)
    author = normalize_post_key_text(post.author)
    posted_at = normalize_post_key_text(post.posted_at)
    if not text and not image_src:
        return None
    # Facebook sometimes hides permalink/time for image-background posts; use
    # stable visible content so the same card does not appear twice.
    if post.url:
        return None
    return f"content:{author}|{posted_at}|{text}|{image_src}"


def merge_posts(
    primary: list[Post],
    secondary: list[Post],
    max_posts: int,
    replace_when_full: bool = True,
) -> list[Post]:
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

            def _best_text(a: str | None, b: str | None) -> str | None:
                """Prefer the full non-truncated version; among equals pick the longer one."""
                a = (a or "").strip()
                b = (b or "").strip()
                a_truncated = "… See more" in a or "... See more" in a or a.endswith("See more")
                b_truncated = "… See more" in b or "... See more" in b or b.endswith("See more")
                if a_truncated and not b_truncated and b:
                    return b
                if b_truncated and not a_truncated and a:
                    return a
                return a if len(a) >= len(b) else b or None

            if post_quality_score(post) > post_quality_score(current):
                # Keep useful fields from both rows.
                if len(current.comments or []) > len(post.comments or []):
                    post.comments = current.comments
                if current.author and (not post.author or post.url == current.url):
                    post.author = current.author
                if current.posted_at and post.url == current.url:
                    post.posted_at = current.posted_at
                post.text = _best_text(current.text, post.text) if post.url == current.url else post.text
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
                current.text = _best_text(current.text, post.text) if post.url == current.url else current.text
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
            if not replace_when_full:
                continue
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

        author, posted_at, text = clean_comment_payload(details)
        images = details.get("images") or []
        if len(text) < 2:
            text = comment_image_text(images)
        key = "|".join(
            [
                author or "",
                posted_at or "",
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
                "author": author,
                "posted_at": posted_at,
                "text": text,
                "reactions": clean_comment_reaction(details.get("reactionsText")),
                "images": images,
                "_left": details.get("left"),
                "_top": details.get("top"),
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

            author, posted_at, text = clean_comment_payload(details)
            images = details.get("images") or []
            if len(text) < 2:
                text = comment_image_text(images)
            key = "|".join(
                [
                    author or "",
                    posted_at or "",
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
                    "author": author,
                    "posted_at": posted_at,
                    "text": text,
                    "reactions": clean_comment_reaction(details.get("reactionsText")),
                    "images": images,
                    "_left": details.get("left"),
                    "_top": details.get("top"),
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
        candidates = []
        for encoding in ("latin1", "cp1252"):
            try:
                candidates.append(value.encode(encoding, errors="ignore").decode("utf-8", errors="ignore"))
            except UnicodeError:
                pass
        thai_before = len(re.findall(r"[\u0E00-\u0E7F]", value))
        best = value
        best_score = thai_before
        for repaired in candidates:
            thai_after = len(re.findall(r"[\u0E00-\u0E7F]", repaired))
            mojibake_after = sum(repaired.count(marker) for marker in ("à", "Â", "Ã", "�"))
            score = thai_after - mojibake_after
            if thai_after > thai_before and score > best_score:
                best = repaired
                best_score = score
        return best
    if isinstance(value, list):
        return [repair_mojibake(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_mojibake(item) for key, item in value.items()}
    return value


def repair_mojibake(value: object) -> object:
    if isinstance(value, str):
        markers = (
            chr(0x00E0),
            chr(0x00C2),
            chr(0x00C3),
            chr(0x00F0),
            chr(0x0178),
            chr(0x02DC),
            chr(0xFFFD),
        )
        if not any(marker in value for marker in markers):
            return value
        candidates = [value]
        for encoding in ("cp1252", "latin1"):
            try:
                candidates.append(value.encode(encoding, errors="strict").decode("utf-8", errors="strict"))
            except UnicodeError:
                pass

        def score(candidate: str) -> int:
            thai = len(re.findall(r"[\u0E00-\u0E7F]", candidate))
            non_bmp = sum(1 for char in candidate if ord(char) > 0xFFFF)
            mojibake = sum(candidate.count(marker) for marker in markers)
            replacement = candidate.count("?") + candidate.count(chr(0xFFFD))
            return thai * 4 + non_bmp * 3 - mojibake * 8 - replacement * 2

        best = max(candidates, key=score)
        return best
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


def should_skip_image_ocr(src: str, width: int, height: int) -> bool:
    if not src:
        return True
    if "images/emoji.php" in src or "/emoji/" in src:
        return True
    if "static.xx.fbcdn.net/rsrc.php" in src:
        return True
    if width and height and width < 160 and height < 120:
        return True
    return False


def tesseract_path() -> str | None:
    configured = os.getenv("TESSERACT_CMD", "").strip()
    if configured:
        return configured
    return shutil.which("tesseract")


def ocr_image_url(src: str, width: int = 0, height: int = 0) -> str | None:
    if env_bool("IMAGE_OCR", True) is False:
        return None
    if should_skip_image_ocr(src, width, height):
        return None

    executable = tesseract_path()
    if not executable:
        return None

    timeout = int(os.getenv("IMAGE_OCR_TIMEOUT", "20"))
    lang = os.getenv("IMAGE_OCR_LANG", "eng+tha").strip() or "eng+tha"
    request = Request(
        src,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            )
        },
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            image_bytes = response.read(8_000_000)
    except Exception:
        return None

    suffix = ".jpg"
    parsed_path = urlparse(src).path.lower()
    if parsed_path.endswith(".png"):
        suffix = ".png"
    elif parsed_path.endswith(".webp"):
        suffix = ".webp"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
        image_path = Path(handle.name)
        handle.write(image_bytes)

    try:
        result = subprocess.run(
            [executable, str(image_path), "stdout", "-l", lang, "--psm", os.getenv("IMAGE_OCR_PSM", "6")],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0 and "tha" in lang:
            result = subprocess.run(
                [executable, str(image_path), "stdout", "-l", "eng", "--psm", os.getenv("IMAGE_OCR_PSM", "6")],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    except Exception:
        return None
    finally:
        try:
            image_path.unlink()
        except OSError:
            pass

    text = clean_post_body_text(result.stdout if result.returncode == 0 else "")
    return text if len(text) >= 2 else None


def enrich_images_with_ocr(images: list[dict[str, object]]) -> list[dict[str, object]]:
    max_images = int(os.getenv("IMAGE_OCR_MAX_IMAGES_PER_POST", "2"))
    if max_images <= 0:
        return images

    enriched: list[dict[str, object]] = []
    ocr_count = 0
    for image in images:
        copied = dict(image)
        if ocr_count < max_images and not copied.get("ocr_text"):
            src = str(copied.get("src") or "")
            width = int(copied.get("width") or 0)
            height = int(copied.get("height") or 0)
            ocr_text = ocr_image_url(src, width, height)
            if ocr_text:
                copied["ocr_text"] = ocr_text
            ocr_count += 1
        enriched.append(copied)
    return enriched


def content_images(images: list[dict[str, object]]) -> list[dict[str, object]]:
    filtered: list[dict[str, object]] = []
    seen: set[str] = set()
    for image in images:
        if not isinstance(image, dict):
            continue
        src = str(image.get("src") or "")
        alt = str(image.get("alt") or "")
        width = int(image.get("width") or 0)
        height = int(image.get("height") or 0)
        perf_name = str(image.get("perfLogName") or "")
        if not src:
            continue
        if "images/emoji.php" in src or "/emoji/" in src:
            continue
        if "static.xx.fbcdn.net/rsrc.php" in src:
            continue
        if width and height and width < 80 and height < 80:
            continue
        key = f"{src}|{alt}"
        if key in seen:
            continue
        seen.add(key)
        filtered.append(image)
    return filtered


def image_texts(images: list[dict[str, object]]) -> list[str]:
    texts: list[str] = []
    seen: set[str] = set()
    for image in images:
        if not isinstance(image, dict):
            continue
        candidates = [
            re.sub(r"\s+", " ", str(image.get("alt") or "")).strip(),
            re.sub(r"\s+", " ", str(image.get("ocr_text") or "")).strip(),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            if candidate in {"Image", "May be an image", "No photo description available."}:
                continue
            candidate = clean_post_body_text(candidate)
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            texts.append(candidate)
    return texts


def combined_analysis_text(post_text: str | None, images: list[dict[str, object]]) -> str:
    parts = []
    if post_text:
        parts.append(clean_post_body_text(post_text))
    parts.extend(image_texts(images))
    cleaned_parts = []
    seen: set[str] = set()
    for part in parts:
        text = re.sub(r"\s+", " ", str(part or "")).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned_parts.append(text)
    return "\n".join(cleaned_parts)


def serialize_post(post: Post) -> dict:
    data = post.__dict__.copy()
    comments = [strip_comment_internals(comment) for comment in (post.comments or []) if isinstance(comment, dict)]
    data["comments"] = comments
    images = enrich_images_with_ocr(content_images(post.images or []))
    post_text = clean_post_body_text(post.text)
    reaction_count = parse_count_text(post.reactions_text)
    total_comment_count = parse_count_text(post.comment_count_text)
    share_count = parse_count_text(post.share_count_text)
    data["text"] = post_text
    data["content_type"] = "text_image" if post_text and images else "image" if images else "text" if post_text else "unknown"
    data["post_text"] = post_text
    data["post_images"] = images
    data["image_texts"] = image_texts(images)
    data["analysis_text"] = combined_analysis_text(post_text, images)
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
    group_url = chronological_group_url(validate_facebook_group_url(args.group_url))
    group_id = group_id_from_url(group_url)
    profile_dir = Path(args.profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    parallel_profile_dirs = parse_profile_dirs(args.parallel_profile_dirs, Path.cwd())
    warn_if_profile_locked(profile_dir)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=args.headless,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(15_000)
        page.set_default_navigation_timeout(45_000)

        safe_goto(page, FACEBOOK_HOME)
        if is_login_or_checkpoint(page):
            prefill_login_email(page, args.facebook_email)
            input("Finish Facebook login/security checks in the browser, then press Enter...")

        print(f"Opening group: {group_url}")
        safe_goto(page, group_url)
        page.wait_for_timeout(2_000)
        set_feed_sort_recent(page)
        metadata = get_group_metadata(page, group_url)
        if metadata.member_count_text:
            print(f"Group members: {metadata.member_count_text}")
        else:
            print("Group member count not found in the loaded page text.")

        posts: list[Post] = []
        extra_post_urls = parse_extra_post_urls(args.extra_post_urls, group_id)
        if extra_post_urls:
            print(f"Adding {len(extra_post_urls)} extra post URLs from EXTRA_POST_URLS.")
            explicit_posts = scrape_post_urls(
                context,
                page,
                extra_post_urls[: args.max_posts],
                args.max_comments,
                args.max_subcomments,
                args.comment_expand_rounds,
                args.debug_dir,
                args.parallel_workers,
                profile_dir,
                parallel_profile_dirs,
                group_url,
                args.headless,
                args.comment_sort,
            )
            for post in explicit_posts:
                if len(posts) >= args.max_posts:
                    break
                posts = merge_posts(posts, [post], args.max_posts, replace_when_full=False)
                print(f"Collected/merged explicit post URL {len(posts)} total rows")

        parallel_group_mode = args.parallel_workers > 1 and bool(parallel_profile_dirs) and len(posts) < args.max_posts
        if parallel_group_mode:
            print(
                "Parallel group mode enabled: reading visible New posts first, "
                "then scraping matching post/comment pages across separate logged-in profiles."
            )
            explicit_keys = set().union(*(post_keys(post) for post in posts)) if posts else set()

            seed_posts = collect_visible_feed_posts_over_scrolls(
                page,
                group_url,
                args.max_posts,
                1,
                max(3, args.scrolls // 3),
                args.today_only,
                args.recover_urls,
            )
            if seed_posts:
                posts = merge_posts(posts, seed_posts, args.max_posts, replace_when_full=False)

            needs_feed_comment_click = any(
                (not post.url or parse_count_text(post.comment_count_text)) and not post.comments
                for post in seed_posts
            ) and args.comment_expand_rounds > 0
            if needs_feed_comment_click:
                click_posts = collect_posts_by_clicking_feed_comments(
                    page,
                    group_url,
                    args.max_posts,
                    args.max_comments,
                    args.max_subcomments,
                    args.comment_expand_rounds,
                    max(3, args.scrolls // 3),
                    args.debug_dir,
                    args.today_only,
                    args.comment_sort,
                )
                posts = merge_posts(posts, click_posts, args.max_posts, replace_when_full=False)

            seed_urls = [post.url for post in seed_posts if post.url]
            if not args.today_only and len(posts) < args.max_posts and len(seed_urls) < args.max_posts:
                discovered_urls = collect_post_urls_from_group_surfaces(
                    page,
                    group_url,
                    group_id,
                    args.max_posts - len(seed_urls),
                    args.scrolls,
                )
                seed_urls = merge_urls(seed_urls, discovered_urls)

            missing_urls = []
            for post_url in seed_urls:
                if len(missing_urls) >= args.max_posts:
                    break
                if post_url in explicit_keys or post_id_from_url(post_url) in explicit_keys:
                    continue
                missing_urls.append(post_url)

            url_posts = scrape_post_urls(
                context,
                page,
                missing_urls[: args.max_posts],
                args.max_comments,
                args.max_subcomments,
                args.comment_expand_rounds,
                args.debug_dir,
                args.parallel_workers,
                profile_dir,
                parallel_profile_dirs,
                group_url,
                args.headless,
                args.comment_sort,
            )
            for post in url_posts:
                posts = merge_posts(posts, [post], args.max_posts)
                print(f"Collected/merged parallel post URL {len(posts)} total rows")
            if len(posts) < args.max_posts and seed_posts:
                posts = merge_posts(posts, seed_posts, args.max_posts, replace_when_full=False)

        if args.today_only and not parallel_group_mode and len(posts) < args.max_posts:
            feed_posts = collect_visible_feed_posts_over_scrolls(
                page,
                group_url,
                args.max_posts,
                args.max_comments,
                max(3, args.scrolls // 3),
                args.today_only,
                args.recover_urls,
            )
            posts = merge_posts(posts, feed_posts, args.max_posts, replace_when_full=False)
            if args.max_comments > 0 and args.comment_expand_rounds > 0:
                comment_urls = [
                    post.url
                    for post in posts
                    if post.url
                    and len(post.comments or []) < min(
                        args.max_comments,
                        parse_count_text(post.comment_count_text) or args.max_comments,
                    )
                ]
                if comment_urls:
                    print(
                        f"Fetching up to {args.max_comments} {args.comment_sort} comments "
                        f"for {len(comment_urls)} feed post URLs."
                    )
                    comment_posts = scrape_post_urls(
                        context,
                        page,
                        comment_urls,
                        args.max_comments,
                        args.max_subcomments,
                        args.comment_expand_rounds,
                        args.debug_dir,
                        1,
                        profile_dir,
                        parallel_profile_dirs,
                        group_url,
                        args.headless,
                        args.comment_sort,
                    )
                    posts = merge_posts(posts, comment_posts, args.max_posts, replace_when_full=False)

        if not args.today_only and not parallel_group_mode and len(posts) < args.max_posts:
            click_posts = collect_posts_by_clicking_feed_comments(
                page,
                group_url,
                args.max_posts,
                args.max_comments,
                args.max_subcomments,
                args.comment_expand_rounds,
                args.scrolls,
                args.debug_dir,
                args.today_only,
                args.comment_sort,
            )
            print(f"Comment-click scan collected {len(click_posts)} rows.")

            feed_posts = collect_visible_feed_posts_over_scrolls(
                page,
                group_url,
                args.max_posts,
                args.max_comments,
                max(3, args.scrolls // 3),
                args.today_only,
                args.recover_urls,
            )
            posts = merge_posts(posts, merge_posts(click_posts, feed_posts, args.max_posts), args.max_posts, replace_when_full=False)

        if not args.today_only and not parallel_group_mode and len(posts) < args.max_posts:
            post_urls = collect_post_urls_from_group_surfaces(
                page,
                group_url,
                group_id,
                args.max_posts,
                args.scrolls,
            )
            post_urls = merge_urls(extra_post_urls, post_urls)
            known_keys = set().union(*(post_keys(post) for post in posts)) if posts else set()
            missing_urls = []
            for post_url in post_urls:
                if len(posts) + len(missing_urls) >= args.max_posts:
                    break
                if post_url in known_keys or post_id_from_url(post_url) in known_keys:
                    continue
                missing_urls.append(post_url)

            url_posts = scrape_post_urls(
                context,
                page,
                missing_urls,
                args.max_comments,
                args.max_subcomments,
                args.comment_expand_rounds,
                args.debug_dir,
                args.parallel_workers,
                profile_dir,
                parallel_profile_dirs,
                group_url,
                args.headless,
                args.comment_sort,
            )
            for post in url_posts:
                if len(posts) >= args.max_posts:
                    break
                if post:
                    posts = merge_posts(posts, [post], args.max_posts, replace_when_full=False)
                    known_keys.update(post_keys(post))
                    print(f"Collected/merged post URL {len(posts)} total rows")

        if not posts and not args.today_only:
            print("No post permalink data found. Falling back to visible feed extraction.")
            safe_goto(page, group_url)
            page.wait_for_timeout(3_000)
            for scroll in range(args.scrolls):
                expand_visible_content(page)
                scroll_posts = collect_posts(
                    page,
                    args.max_posts,
                    args.max_comments,
                    args.comment_expand_rounds,
                    args.today_only,
                )
                posts = merge_posts(posts, scroll_posts, args.max_posts, replace_when_full=False)
                print(f"Scroll {scroll + 1}/{args.scrolls}: collected {len(posts)} posts")
                if len(posts) >= args.max_posts:
                    break
                if args.today_only and posts and scroll >= 2:
                    print("Today-only fallback scan stopped after stable visible post collection.")
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
