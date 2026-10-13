# Stratified sampling of search results: method and limitations

This note describes how `sampling = "stratified"` selects hits, and the threats to validity to
report when a dataset is built with it. Configuration details are in
[heuristics.md](heuristics.md#stratified-sampling-sampling--stratified).

## Why sampling is needed

The GitHub search API returns at most 1000 results per query. A broad query such as
`"model.fit(" language:Python` reports about 1.6 million matching files. `auto_partition`
reaches all of them by splitting the query on a qualifier range (`size:` for code search), but
this costs about `total_count / 100` calls (about 16000 here, at 10 code-search calls per minute
and per token). A fixed-size sample is cheaper. Stopping the collection after the first N
results would not give a usable sample, because the slices are walked in increasing order of
size: the sample would hold only the smallest files.

## Method

For one search query with a sample size `max_hits`:

1. **Sampling fraction.** The first call on the full range (e.g. `size:0..1000000`) gives the
   total number of results `T`. The sampling fraction is `f = max_hits / T`. If `max_hits >= T`,
   or no `max_hits` is set, `f = 1`.
2. **Strata.** The range is split in two recursively (geometric midpoint for sizes, linear
   for dates). A slice with `n` results is split while `n > 800` and its share
   `f x n > per_page` (100). The slices that are not split are the strata. Each has at most
   `per_page / f` results (or at most 800 when `f = 1`), so the number of strata grows with the
   sample size. A slice of a single value (e.g. `size:42..42`) cannot be split, and is a stratum
   whatever its size.
3. **Allocation.** Proportional: a stratum with `n` results contributes `round(f x n)` hits.
   With `f = 1`, each stratum contributes all its results, which is a full collection.
4. **Selection inside a stratum.** The first `round(f x n)` results of the stratum, in the
   order returned by the API.
5. **Unit and deduplication.** The sampled unit is a search hit: one file for code search, one
   repository for repository search. Duplicate hits (same repository and path) are counted once.

Example: `T = 1,601,536` and `max_hits = 20,000` give `f ≈ 1.25%`, about 200 strata of about
8000 files each, about 100 hits per stratum, and about 400 API calls.

### Resuming and changing the sample size

Every call is logged in `queries.csv` with the slice, its `total_count`, the number of items
kept and a status (`split`, `sampled` or `done`). Every call also stores the results it
received in `hits.csv`, including the first page of a slice that is split. For the current
`max_hits`, the strata are rebuilt from these rows without calling the API.

- **Same `max_hits`:** nothing is fetched again.
- **Higher `max_hits`:** strata that are now too large are split, and strata that need more
  items are fetched again by `search`. Above `T`, the result is a full collection.
- **Lower `max_hits`:** strata merge back into larger slices, which were all fetched before (at
  least their first page). Each one is trimmed to its first `round(f x n)` items. `filter` alone
  gives the new sample, with no API call.

`filter` always builds the sample from the current strata, using the most recent fetch of each
one. Hits from slices that are now split, from superseded fetches, or beyond a stratum's share
stay in `hits.csv` but are left out of the sample. After a higher `max_hits`, run `search` (or
`run`) before `filter`. Otherwise the new strata are missing from the sample, and `filter` logs
`stratified sample incomplete`. The `stratified sample of N hit(s)` line in `run.log` gives the
final size.

## Limitations (threats to validity)

**1. Selection inside a stratum is not random.** The API has no random ordering. Each stratum
gives its first results in GitHub's "best match" order, which is undocumented and may favour
some repositories or files (e.g. by popularity or recency). The stratification removes the
bias along the stratification variable (file size) at the scale of the strata, but not inside
a stratum. A stratum keeps about `f` of its results, so most of its results can never be
selected. Within each stratum, the sample is a convenience sample, not a probability sample.

**2. `total_count` is an estimate.** GitHub reports an approximate `total_count`. We observed
a slice reporting 136 results that returned 200, and one reporting 876 that returned 1000. Both
`f` and each stratum's allocation come from these estimates. As a result:
- the final sample size is close to `max_hits`, not exactly equal to it;
- the allocation is only roughly proportional to the true size of each stratum;
- full collection is triggered when `max_hits` is at least the *estimated* `T`.

**3. Rounding.** Allocations are rounded to whole items. A stratum whose share is below 0.5
contributes no hit, so very sparse parts of the range are under-represented when `f` is small.
The relative error is largest for small samples and small strata.

**4. Stratification on one variable.** The strata are ranges of file size for code search
(or creation date, stars, etc. for repository search). The sample is balanced on this variable
only. Representativeness along other dimensions (language, topic, organisation, age) is not
controlled.

**5. Hits, not repositories, are sampled.** Each matching file has about the same chance to be
selected. A repository's chance of appearing in the sample therefore grows with the number of
its files that match the query. A repository-level analysis inherits this bias towards
repositories with many matching files, unless it is corrected (e.g. by weighting) or is
acknowledged.

**6. The sampling frame is GitHub's search index, not GitHub.** Code search only covers what
GitHub indexes. Files larger than 384 KB are not indexed, and only the default branch is
searched. Forks are mostly excluded. The population is "files matching the query in GitHub's
index at collection time".

**7. Time.** The index changes while a run is in progress, and resuming with a higher `max_hits`
fetches some strata later than others. `total_count` values and result orders from different
times are combined. `queries.csv` gives `started_at` and `finished_at` for every call, so the
collection window can be reported.

**8. The first-page shortcut.** A stratum's items come from the page fetched to read its
`total_count`. When the share is at most 100, only that first page is used, which strengthens
limitation 1. Fetching a random page among the up to 10 reachable pages of each stratum would
soften it, at one more call per stratum. This option is **not implemented**.

**9. Several queries.** Sampling is done per query. A file matched by several queries can be
selected by each of them independently. Each query's sample is proportional within that query,
but the combined set of hits across queries is not a sample of a single population.

**10. The `ordered` mode is not a sample.** With `sampling = "ordered"`, `max_hits` keeps the
lowest part of the range (the smallest files for code search). Use it to bound the cost of a
query, not to draw a sample.

## What to report

- For each query: the query string, `T` (root `total_count` and date), `max_hits`, `f`, the
  number of strata, and the final number of sampled hits (from `run.log` / `queries.csv`).
- The stratification variable, the split rule (geometric midpoint, threshold of 800 results,
  one page per stratum) and the proportional allocation.
- Limitations 1, 2, 5 and 6 at a minimum, as they affect the interpretation of results.
- The config files saved under `configs/` in the run directory, for reproducibility.
