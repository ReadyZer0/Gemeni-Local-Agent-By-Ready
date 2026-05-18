
## 2024-05-18 - Added ToolTips and Pointer Cursor to PySide6 UI Elements
**Learning:** In native desktop apps built with PySide6, custom buttons or generic widgets often lack clear accessibility features out-of-the-box compared to HTML elements. The `setToolTip` function significantly improves discoverability for action buttons (like generic 'Send', 'Stop', 'Attach'), acting similar to an `aria-label` or `title` attribute. Using `setCursor(Qt.CursorShape.PointingHandCursor)` on clickables gives immediate visual feedback.
**Action:** When adding new interactive components or UI actions in the app via PySide6, proactively apply `setToolTip` to clarify the behavior and ensure the cursor turns to a pointer so it's obviously clickable.
