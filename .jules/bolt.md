## 2024-05-17 - Read files line-by-line instead of loading to memory
**Learning:** Storing string events for all lines using `.read_text().splitlines()` will cause a massive memory footprint issue, since `read_text` reads the entire file content into memory. For large session JSONL files, this wastes a large amount of memory allocation and slows down iteration.
**Action:** Replaced `.read_text().splitlines()` with `with path.open("r") as f: for line in f:` in `history_store.py` to only keep one line at a time in memory while building the `items` array.
