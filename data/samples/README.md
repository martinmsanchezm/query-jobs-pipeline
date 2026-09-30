# Sample payloads

Synthetic API responses in the format of each source, used by the offline demo and the tests. **All companies are fictional** and every URL uses the reserved `.example` domain, so nothing points to a real posting.

- `day1/` and `day2/` are two consecutive runs: the second repeats some jobs, adds new ones and drops others, to show incremental loading and cross-run deduplication.
- Some records are invalid on purpose (missing URL, future date, inverted salary range, `javascript:` URL, missing title) to exercise the data quality rules.
