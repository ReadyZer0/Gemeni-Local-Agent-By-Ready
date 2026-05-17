## 2024-06-25 - Improve discoverability of custom clickable elements in PySide6

**Learning:** In PySide6 UI development, custom `QPushButton`s or clickable elements often don't inherit expected web-like hover behaviors (like pointer cursor) by default. Also, terse or brand-specific labels like 'Nano Banana' can be confusing without additional context, breaking accessibility/discoverability guidelines for screen readers and tooltips.

**Action:** Proactively apply `setToolTip()` for non-obvious UI elements to provide context, and use `setCursor(Qt.PointingHandCursor)` on all clickable custom widgets or buttons to ensure visual discoverability and a web-like interactive feel for desktop users.