from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget


class GeminiPage(QWebEnginePage):
    def __init__(self, profile: QWebEngineProfile, parent=None):
        super().__init__(profile, parent)
        self.queued_uploads: list[str] = []

    def chooseFiles(self, mode, old_files, accepted_mime_types):  # noqa: N802 - Qt virtual method
        files = list(self.queued_uploads)
        self.queued_uploads = []
        return files


class GeminiBrowser(QWidget):
    status_changed = Signal(str)
    url_changed_text = Signal(str)
    composer_rect_changed = Signal(dict)

    def __init__(self, config: dict, project_dir: str | Path, parent=None):
        super().__init__(parent)
        self.config = config
        self.project_dir = Path(project_dir)
        profile_path = self.project_dir / config.get("gemini", {}).get("browser_profile", "data/browser_profile/gemini")
        profile_path.mkdir(parents=True, exist_ok=True)

        self.profile = QWebEngineProfile("gemini-local-agent", self)
        self.profile.setPersistentStoragePath(str(profile_path))
        self.profile.setCachePath(str(profile_path / "cache"))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)

        self.view = QWebEngineView(self)
        self.page = GeminiPage(self.profile, self.view)
        self.view.setPage(self.page)
        self.view.urlChanged.connect(lambda url: self.url_changed_text.emit(url.toString()))
        self.view.loadFinished.connect(self._handle_load_finished)

        self.unified_composer_timer = QTimer(self)
        self.unified_composer_timer.timeout.connect(self.install_unified_composer_mode)
        self.unified_composer_timer.start(1800)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self.load_gemini()

    def load_gemini(self) -> None:
        self.view.setUrl(QUrl(self.config.get("gemini", {}).get("url", "https://gemini.google.com/app")))

    def _handle_load_finished(self, ok: bool) -> None:
        self.status_changed.emit("Gemini page loaded" if ok else "Gemini page load failed")
        if ok:
            QTimer.singleShot(700, self.install_unified_composer_mode)

    def install_unified_composer_mode(self) -> None:
        if not self.config.get("gemini", {}).get("hide_native_composer", True):
            return
        script = r"""
(() => {
  function roots() {
    const items = [document];
    const queue = [document];
    const seen = new Set(items);
    while (queue.length) {
      const root = queue.shift();
      const nodes = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (const node of nodes) {
        if (node && node.shadowRoot && !seen.has(node.shadowRoot)) {
          seen.add(node.shadowRoot);
          items.push(node.shadowRoot);
          queue.push(node.shadowRoot);
        }
      }
    }
    return items;
  }
  function queryAll(selectors) {
    const found = [];
    const seen = new Set();
    for (const root of roots()) {
      for (const selector of selectors) {
        let matches = [];
        try {
          matches = root.querySelectorAll ? Array.from(root.querySelectorAll(selector)) : [];
        } catch (_err) {
          matches = [];
        }
        for (const node of matches) {
          if (!seen.has(node)) {
            seen.add(node);
            found.push(node);
          }
        }
      }
    }
    return found;
  }
  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  }
  function parentOf(node) {
    if (!node) return null;
    if (node.parentElement) return node.parentElement;
    const root = node.getRootNode && node.getRootNode();
    return root && root.host ? root.host : null;
  }
  function restoreOldRoots(activeRoot) {
    for (const root of roots()) {
      const oldRoots = root.querySelectorAll ? Array.from(root.querySelectorAll('[data-gla-native-composer-root="true"]')) : [];
      for (const oldRoot of oldRoots) {
        if (oldRoot === activeRoot) continue;
        oldRoot.style.opacity = oldRoot.dataset.glaPreviousOpacity || '';
        oldRoot.style.pointerEvents = oldRoot.dataset.glaPreviousPointerEvents || '';
        oldRoot.removeAttribute('data-gla-native-composer-root');
      }
    }
  }
  const selectors = [
    'rich-textarea .ql-editor[contenteditable="true"]',
    'rich-textarea [contenteditable="true"][role="textbox"]',
    '[aria-label="Enter a prompt for Gemini"]',
    '[data-placeholder="Ask Gemini"]',
    'div[role="textbox"][contenteditable="true"]',
    'textarea'
  ];
  const inputs = queryAll(selectors)
    .filter(visible)
    .sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom);
  const input = inputs[0];
  if (!input) {
    restoreOldRoots(null);
    return {ok: false, reason: 'composer input not found'};
  }
  let candidate = null;
  let node = input;
  for (let depth = 0; node && depth < 10; depth += 1) {
    if (node.getBoundingClientRect) {
      const rect = node.getBoundingClientRect();
      const hasControls = !!(node.querySelector && node.querySelector('button,[role="button"],textarea,[contenteditable="true"],rich-textarea'));
      if (hasControls && rect.width >= 300 && rect.height >= 60 && rect.height <= 190) {
        candidate = node;
      }
    }
    node = parentOf(node);
  }
  if (!candidate) candidate = input;
  restoreOldRoots(candidate);
  if (!candidate.dataset.glaPreviousOpacity) {
    candidate.dataset.glaPreviousOpacity = candidate.style.opacity || '';
  }
  if (!candidate.dataset.glaPreviousPointerEvents) {
    candidate.dataset.glaPreviousPointerEvents = candidate.style.pointerEvents || '';
  }
  candidate.setAttribute('data-gla-native-composer-root', 'true');
  candidate.style.opacity = '0';
  candidate.style.pointerEvents = 'none';
  const rect = candidate.getBoundingClientRect();
  return {
    ok: true,
    hidden: true,
    rect: {
      x: rect.left,
      y: rect.top,
      width: rect.width,
      height: rect.height
    }
  };
})();
"""
        def handle_result(result):
            if isinstance(result, dict) and result.get("ok") and isinstance(result.get("rect"), dict):
                self.composer_rect_changed.emit(result["rect"])

        self.page.runJavaScript(script, handle_result)

    def new_chat(self, callback: Callable[[dict], None] | None = None) -> None:
        script = r"""
(() => {
  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  }
  const labels = ['new chat', 'new conversation', 'start new chat', 'new prompt', 'new'];
  const nodes = Array.from(document.querySelectorAll('button,a,[role="button"]'));
  for (const node of nodes) {
    if (!visible(node)) continue;
    const text = String(node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('title') || '').trim().toLowerCase();
    if (labels.some((label) => text === label || text.includes(label))) {
      node.click();
      return {ok: true, label: text, url: location.href};
    }
  }
  location.href = 'https://gemini.google.com/app';
  return {ok: false, fallback: 'reload', url: location.href};
})();
"""
        self.page.runJavaScript(script, callback or (lambda _result: None))

    def activate_feature(self, labels: list[str], callback: Callable[[dict], None] | None = None) -> None:
        script = self._activate_feature_script(labels)
        self.page.runJavaScript(script, callback or (lambda _result: None))

    def activate_model_mode(self, labels: list[str], callback: Callable[[dict], None] | None = None) -> None:
        script = self._activate_model_mode_script(labels)
        self.page.runJavaScript(script, callback or (lambda _result: None))

    def send_prompt(self, prompt: str, attachments: list[str] | None = None, callback: Callable[[dict], None] | None = None) -> None:
        attachments = [str(Path(path)) for path in attachments or [] if Path(path).exists()]
        self.page.queued_uploads = attachments
        if attachments:
            self._attempt_upload_files()
            delay = int(self.config.get("gemini", {}).get("send_attachment_delay_ms", 2500))
            QTimer.singleShot(delay, lambda: self._send_text(prompt, callback))
        else:
            self._send_text(prompt, callback)

    def read_state(self, callback: Callable[[dict], None]) -> None:
        def handle_script_result(result):
            state = result if isinstance(result, dict) else {}
            if self._state_has_text(state):
                callback(state)
                return
            self.page.toPlainText(lambda text: callback(self._plain_text_state(state, text)))

        self.page.runJavaScript(self._read_state_script(), handle_script_result)

    @staticmethod
    def _state_has_text(state: dict) -> bool:
        return bool(str((state or {}).get("latestReply") or "").strip() or str((state or {}).get("bodyText") or "").strip())

    @staticmethod
    def _latest_sigil_block(text: str) -> str:
        value = str(text or "")
        pattern = re.compile(r"~@[A-Za-z_][A-Za-z0-9_:-]*\d*@~[\s\S]{0,200000}?~@exit\d*@~", re.IGNORECASE)
        matches = list(pattern.finditer(value))
        return matches[-1].group(0).strip() if matches else ""

    def _plain_text_state(self, state: dict, text: str) -> dict:
        body = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        merged = dict(state or {})
        merged["ok"] = True
        merged["bodyText"] = body[-30000:]
        merged["latestReply"] = self._latest_sigil_block(body) or body[-12000:]
        merged["plainTextFallback"] = True
        return merged

    def _attempt_upload_files(self) -> None:
        script = r"""
(() => {
  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  }
  const input = document.querySelector('input[type="file"]');
  if (input) {
    input.click();
    return {ok: true, target: 'input[type=file]'};
  }
  const nodes = Array.from(document.querySelectorAll('button,[role="button"],[aria-label],[title]'));
  for (const node of nodes) {
    const label = String(node.getAttribute('aria-label') || node.getAttribute('title') || node.innerText || node.textContent || '').toLowerCase();
    if (visible(node) && /(attach|upload|file|image|add)/.test(label)) {
      node.click();
      return {ok: true, target: label};
    }
  }
  return {ok: false, reason: 'no upload control found'};
})();
"""
        self.page.runJavaScript(script)

    def _send_text(self, prompt: str, callback: Callable[[dict], None] | None = None) -> None:
        script = self._send_text_script(prompt)
        self.page.runJavaScript(script, callback or (lambda _result: None))

    @staticmethod
    def _send_text_script(prompt: str) -> str:
        prompt_json = json.dumps(prompt)
        return f"""
(async () => {{
  const prompt = {prompt_json};
  function visible(el) {{
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  }}
  function clean(value) {{
    return String(value || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim().toLowerCase();
  }}
  function labelOf(el) {{
    return clean(el && [
      el.getAttribute && el.getAttribute('aria-label'),
      el.getAttribute && el.getAttribute('title'),
      el.getAttribute && el.getAttribute('data-tooltip'),
      el.className,
      el.innerText,
      el.textContent
    ].filter(Boolean).join(' '));
  }}
  function roots() {{
    const items = [document];
    const queue = [document];
    const seen = new Set(items);
    while (queue.length) {{
      const root = queue.shift();
      const nodes = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (const node of nodes) {{
        if (node.shadowRoot && !seen.has(node.shadowRoot)) {{
          seen.add(node.shadowRoot);
          items.push(node.shadowRoot);
          queue.push(node.shadowRoot);
        }}
      }}
    }}
    return items;
  }}
  function queryAll(selectors) {{
    const found = [];
    for (const root of roots()) {{
      for (const selector of selectors) {{
        try {{
          found.push(...Array.from(root.querySelectorAll(selector)));
        }} catch (_err) {{}}
      }}
    }}
    return found;
  }}
  const inputSelectors = [
    'rich-textarea .ql-editor[contenteditable="true"]',
    'rich-textarea [contenteditable="true"][role="textbox"]',
    '[aria-label="Enter a prompt for Gemini"]',
    '[data-placeholder="Ask Gemini"]',
    'div[role="textbox"][contenteditable="true"]',
    'div[contenteditable="true"]',
    'textarea'
  ];
  const inputs = queryAll(inputSelectors).filter(visible).sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom);
  const input = inputs[0];
  if (!input) return {{ok: false, error: 'composer not found', url: location.href}};
  input.focus();
  function setNativeValue(el, value) {{
    const tag = el.tagName && el.tagName.toLowerCase();
    if (tag === 'textarea' || tag === 'input') {{
      const proto = tag === 'textarea' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value') && Object.getOwnPropertyDescriptor(proto, 'value').set;
      if (setter) setter.call(el, value);
      else el.value = value;
    }} else {{
      el.innerText = value;
      el.textContent = value;
    }}
  }}
  try {{
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, prompt);
  }} catch (_err) {{}}
  setNativeValue(input, prompt);
  input.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'insertText', data: prompt}}));
  input.dispatchEvent(new Event('change', {{bubbles: true}}));
  await new Promise((resolve) => setTimeout(resolve, 350));
  const sendSelectors = [
    'button.send-button[aria-label="Send message"]',
    '.send-button-container button.send-button',
    'button[aria-label*="Send"]',
    'button[title*="Send"]'
  ];
  const buttons = queryAll(sendSelectors)
    .filter(visible)
    .filter((node) => !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({{node, label: labelOf(node), rect: node.getBoundingClientRect()}}))
    .filter((item) => /send/.test(item.label) || /send-button/.test(item.label))
    .filter((item) => !/(stop|cancel|pause|interrupt|generating)/.test(item.label))
    .sort((a, b) => b.rect.right - a.rect.right);
  const button = buttons.length ? buttons[0].node : null;
  if (button) {{
    const beforeClickLabel = labelOf(button);
    button.click();
    return {{ok: true, method: 'button', button: beforeClickLabel, url: location.href}};
  }}
  input.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', code: 'Enter', bubbles: true, cancelable: true}}));
  input.dispatchEvent(new KeyboardEvent('keyup', {{key: 'Enter', code: 'Enter', bubbles: true, cancelable: true}}));
  return {{ok: true, method: 'enter', url: location.href}};
}})();
"""

    @staticmethod
    def _activate_feature_script(labels: list[str]) -> str:
        labels_json = json.dumps([str(label).strip().lower() for label in labels if str(label).strip()])
        return f"""
(async () => {{
  const targets = {labels_json};
  const toolLabels = ['tools', 'tool', 'gemini tools'];
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  function clean(value) {{
    return String(value || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim().toLowerCase();
  }}
  function roots() {{
    const items = [document];
    const queue = [document];
    const seen = new Set(items);
    while (queue.length) {{
      const root = queue.shift();
      const nodes = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (const node of nodes) {{
        if (node && node.shadowRoot && !seen.has(node.shadowRoot)) {{
          seen.add(node.shadowRoot);
          items.push(node.shadowRoot);
          queue.push(node.shadowRoot);
        }}
      }}
    }}
    return items;
  }}
  function queryAll(selectors) {{
    const found = [];
    const seen = new Set();
    for (const root of roots()) {{
      for (const selector of selectors) {{
        let matches = [];
        try {{
          matches = root.querySelectorAll ? Array.from(root.querySelectorAll(selector)) : [];
        }} catch (_err) {{
          matches = [];
        }}
        for (const node of matches) {{
          if (!seen.has(node)) {{
            seen.add(node);
            found.push(node);
          }}
        }}
      }}
    }}
    return found;
  }}
  function visible(el) {{
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  }}
  function labelOf(el) {{
    return clean(el && [
      el.getAttribute && el.getAttribute('aria-label'),
      el.getAttribute && el.getAttribute('title'),
      el.getAttribute && el.getAttribute('data-tooltip'),
      el.innerText,
      el.textContent
    ].filter(Boolean).join(' '));
  }}
  function scoreLabel(text, wanted) {{
    if (!text) return 0;
    let score = 0;
    for (const target of wanted) {{
      if (!target) continue;
      if (text === target) score = Math.max(score, 1000 + target.length);
      if (text.includes(target)) score = Math.max(score, 700 + target.length);
      if (target.includes(text) && text.length >= 4) score = Math.max(score, 450 + text.length);
    }}
    return score;
  }}
  function clickBest(wanted) {{
    const selectors = [
      'button',
      'a',
      '[role="button"]',
      '[role="menuitem"]',
      '[role="option"]',
      '[aria-label]',
      '[title]'
    ];
    const candidates = queryAll(selectors)
      .filter(visible)
      .map((node) => {{
        const rect = node.getBoundingClientRect();
        const label = labelOf(node);
        return {{
          node,
          label,
          score: scoreLabel(label, wanted),
          bottom: rect.bottom,
          right: rect.right
        }};
      }})
      .filter((item) => item.score > 0)
      .sort((a, b) => (b.score - a.score) || (b.bottom - a.bottom) || (b.right - a.right));
    if (!candidates.length) return null;
    const best = candidates[0];
    best.node.click();
    return {{label: best.label, score: best.score}};
  }}

  const direct = clickBest(targets);
  if (direct) return {{ok: true, method: 'direct', target: direct.label, url: location.href}};

  const tools = clickBest(toolLabels);
  if (tools) {{
    await sleep(650);
    const fromMenu = clickBest(targets);
    if (fromMenu) {{
      return {{
        ok: true,
        method: 'tools-menu',
        target: fromMenu.label,
        opened: tools.label,
        url: location.href
      }};
    }}
  }}

  return {{ok: false, error: 'feature control not found', labels: targets, url: location.href}};
}})();
"""

    @staticmethod
    def _activate_model_mode_script(labels: list[str]) -> str:
        labels_json = json.dumps([str(label).strip().lower() for label in labels if str(label).strip()])
        return f"""
(async () => {{
  const targets = {labels_json};
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  function clean(value) {{
    return String(value || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim().toLowerCase();
  }}
  function roots() {{
    const items = [document];
    const queue = [document];
    const seen = new Set(items);
    while (queue.length) {{
      const root = queue.shift();
      const nodes = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (const node of nodes) {{
        if (node && node.shadowRoot && !seen.has(node.shadowRoot)) {{
          seen.add(node.shadowRoot);
          items.push(node.shadowRoot);
          queue.push(node.shadowRoot);
        }}
      }}
    }}
    return items;
  }}
  function queryAll(selectors) {{
    const found = [];
    const seen = new Set();
    for (const root of roots()) {{
      for (const selector of selectors) {{
        let matches = [];
        try {{
          matches = root.querySelectorAll ? Array.from(root.querySelectorAll(selector)) : [];
        }} catch (_err) {{
          matches = [];
        }}
        for (const node of matches) {{
          if (!seen.has(node)) {{
            seen.add(node);
            found.push(node);
          }}
        }}
      }}
    }}
    return found;
  }}
  function visible(el) {{
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
  }}
  function labelOf(el) {{
    return clean(el && [
      el.getAttribute && el.getAttribute('aria-label'),
      el.getAttribute && el.getAttribute('title'),
      el.getAttribute && el.getAttribute('data-tooltip'),
      el.innerText,
      el.textContent
    ].filter(Boolean).join(' '));
  }}
  function scoreOption(label, wanted) {{
    let score = 0;
    for (const target of wanted) {{
      if (!target) continue;
      if (label === target) score = Math.max(score, 2000 + target.length);
      if (label.includes(target)) score = Math.max(score, 1200 + target.length);
      if (target.includes(label) && label.length >= 4) score = Math.max(score, 800 + label.length);
    }}
    if (/subscription|business|about gemini|gemini app|sign in|new chat/.test(label)) score -= 3000;
    return score;
  }}
  function controls() {{
    return queryAll(['button','[role="button"]','[role="menuitem"]','[role="option"]','[aria-label]','[title]'])
      .filter(visible)
      .map((node) => {{
        const rect = node.getBoundingClientRect();
        const label = labelOf(node);
        return {{node, label, rect, bottom: rect.bottom, right: rect.right}};
      }});
  }}
  function clickNode(item) {{
    item.node.click();
    return {{label: item.label, x: item.rect.left, y: item.rect.top}};
  }}
  const currentModeLabels = ['fast', 'pro', 'thinking', 'think', 'model'];
  const modeButtons = controls()
    .map((item) => {{
      let score = scoreOption(item.label, currentModeLabels);
      if (item.bottom > window.innerHeight * 0.55) score += 500;
      if (item.right > window.innerWidth * 0.45) score += 200;
      return {{...item, score}};
    }})
    .filter((item) => item.score > 0)
    .sort((a, b) => (b.score - a.score) || (b.bottom - a.bottom) || (b.right - a.right));
  const opener = modeButtons[0];
  if (!opener) return {{ok: false, error: 'model mode opener not found', labels: targets, url: location.href}};
  const opened = clickNode(opener);
  await sleep(700);
  const options = controls()
    .map((item) => {{
      const score = scoreOption(item.label, targets);
      return {{...item, score}};
    }})
    .filter((item) => item.score > 0)
    .sort((a, b) => (b.score - a.score) || (b.bottom - a.bottom) || (b.right - a.right));
  if (!options.length) {{
    return {{ok: false, error: 'model mode option not found', opened: opened.label, labels: targets, url: location.href}};
  }}
  const selected = clickNode(options[0]);
  return {{ok: true, method: 'model-menu', opened: opened.label, target: selected.label, url: location.href}};
}})();
"""

    @staticmethod
    def _read_state_script() -> str:
        return r"""
(() => {
  function clean(value) {
    return String(value || '').replace(/\u00a0/g, ' ').replace(/\r\n/g, '\n').replace(/\r/g, '\n').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  }
  function parentOf(node) {
    if (!node) return null;
    if (node.parentElement) return node.parentElement;
    const root = node.getRootNode && node.getRootNode();
    return root && root.host ? root.host : null;
  }
  function insideHiddenComposer(el) {
    let node = el;
    for (let depth = 0; node && depth < 12; depth += 1) {
      if (node.getAttribute && node.getAttribute('data-gla-native-composer-root') === 'true') {
        return true;
      }
      node = parentOf(node);
    }
    return false;
  }
  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    if (insideHiddenComposer(el)) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
  }
  function labelOf(el) {
    return clean(el && (el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || el.textContent || ''));
  }
  function roots() {
    const items = [document];
    const queue = [document];
    const seen = new Set(items);
    while (queue.length) {
      const root = queue.shift();
      const nodes = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (const node of nodes) {
        if (node && node.shadowRoot && !seen.has(node.shadowRoot)) {
          seen.add(node.shadowRoot);
          items.push(node.shadowRoot);
          queue.push(node.shadowRoot);
        }
      }
    }
    return items;
  }
  function queryAll(selectors) {
    const found = [];
    const seen = new Set();
    for (const root of roots()) {
      for (const selector of selectors) {
        let matches = [];
        try {
          matches = root.querySelectorAll ? Array.from(root.querySelectorAll(selector)) : [];
        } catch (_err) {
          matches = [];
        }
        for (const node of matches) {
          if (!seen.has(node)) {
            seen.add(node);
            found.push(node);
          }
        }
      }
    }
    return found;
  }
  function allPageText() {
    const chunks = [];
    const seen = new Set();
    for (const root of roots()) {
      let text = '';
      if (root === document) {
        text = document.body && document.body.innerText || '';
      } else {
        text = root.textContent || '';
      }
      text = clean(text);
      const noise = ['gemini', 'new chat', 'tools', 'pro', 'fast', 'thinking'];
      if (text && !seen.has(text) && !noise.includes(text)) {
        seen.add(text);
        chunks.push(text);
      }
    }
    return clean(chunks.join('\n\n'));
  }
  const bodyText = allPageText();
  const controls = queryAll(['button','[role="button"]','[aria-label]','[title]']).filter(visible);
  const busy = controls.some((el) => /stop|pause|cancel response|stop generating/i.test(labelOf(el))) || /Gemini is typing/i.test(bodyText);
  const responseNodes = queryAll(['model-response','message-content','.model-response-text','[class*="response-container"]','article','main article','pre','code','[data-test-id*="response"]','[class*="markdown"]'])
    .filter(visible)
    .map((el) => {
      const text = clean(el.innerText || el.textContent || '');
      const rect = el.getBoundingClientRect();
      let score = rect.bottom + Math.min(text.length, 2000);
      if (/~@[A-Za-z_][A-Za-z0-9_:-]*\d*@~/.test(text)) score += 4000;
      if (/~@exit\d*@~/.test(text)) score += 4000;
      return {text, score, bottom: rect.bottom};
    })
    .filter((item) => item.text.length > 0)
    .sort((a, b) => (b.score - a.score) || (b.bottom - a.bottom));
  function latestSigilBlock(source) {
    const text = clean(source);
    const pattern = /~@[A-Za-z_][A-Za-z0-9_:-]*\d*@~[\s\S]{0,200000}?~@exit\d*@~/g;
    let match = null;
    let current = null;
    while ((current = pattern.exec(text)) !== null) {
      match = current[0];
    }
    return match || '';
  }
  let latestReply = '';
  for (const item of responseNodes) {
    const block = latestSigilBlock(item.text);
    if (block) {
      latestReply = block;
      break;
    }
  }
  if (!latestReply) latestReply = latestSigilBlock(bodyText);
  if (!latestReply) latestReply = responseNodes.length ? responseNodes[0].text : '';
  if (!latestReply) {
    const markers = ['Gemini said', 'Gemini replied', 'Gemini says'];
    let index = -1;
    for (const marker of markers) {
      const found = bodyText.lastIndexOf(marker);
      if (found > index) index = found + marker.length;
    }
    latestReply = index >= 0 ? bodyText.slice(index).trim() : bodyText.slice(-12000);
  }
  const canvasActive = /canvas/i.test(bodyText) || !!document.querySelector('[aria-label*="Canvas"],[data-test-id*="canvas"],[class*="canvas"]');
  const stopped = /you stopped this response/i.test(latestReply);
  return {
    ok: true,
    url: location.href,
    title: document.title,
    busy,
    stopped,
    latestReply,
    bodyText: bodyText.slice(-30000),
    canvasActive,
    responseCount: responseNodes.length
  };
})();
"""
