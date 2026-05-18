## 2024-05-18 - [Optimize File Reading Memory Usage]
**Learning:** `path.read_text().splitlines()` is an anti-pattern for large files like session histories or logs because it loads the entire file into memory as a single string and then duplicates that memory footprint by creating a massive list of strings.
**Action:** Use `with path.open("r") as f:` and iterate over the file handle directly (`for line in f:`) to parse JSONL logs line-by-line, drastically reducing peak memory consumption.
