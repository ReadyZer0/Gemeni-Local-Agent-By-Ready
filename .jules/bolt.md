## 2024-05-19 - Avoid `splitlines()` for large JSONL file parsing
**Learning:** `path.read_text().splitlines()` loads the entire file contents into memory simultaneously as a string, and then creates a duplicate mass of string data in a list structure. For session files, this produces significant memory spikes.
**Action:** Always process log or session files using a file iterator (`with path.open("r") as f: for line in f:`) to minimize RAM allocation and avoid arbitrary file size limits on parsing.
