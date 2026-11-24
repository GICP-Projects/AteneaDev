# Staff dashboard

`/dashboard/` opens the activity overview. Access requires an authenticated staff
session, as do Search, Jobs, Schedules, and the dashboard lookup views.

## Recover message processing

Each Job's status describes its own Batches. A completed Scan can still have a
message-processing child waiting on NER or indexing. Its detail page keeps the
live downstream notice and **Retry pending processing** action visible. The
status filter also finds root Jobs whose direct child has the selected status.

After restoring a shared dependency, **Retry all pending message processing**
on the Job list requeues the recoverable failed message Jobs across the database,
independently of the list filters. It reports the number of Jobs and Batches.
Successful stages are preserved, so an index failure resumes at indexing.
These staff-only POST actions require CSRF protection. Cancelled and explicitly
abandoned failures are excluded from ordinary retry actions.
The global message retry preserves manually paused Jobs; resume those
explicitly when they should run again. Failed Batches can also be retried
while other Batches in the same Job continue processing.

A completed Job with no failed Batches shows its retained error as **Last
recovered error**, without an error alert. Other states retain **Latest error**.
The historical text is preserved; a parent's recovered error does not hide
its child's pending processing or retry controls.

## Explore message activity

The overview displays message frequency and an optional z-score overlay in one
chart. Frequency uses the left axis and a solid line. The mean uses neutral dots;
z-score uses warm brown, longer dashes, and a matching right axis and legend.
A transparent brown band marks the z-score range from −2 to +2 behind the series.
The frequency area uses a separate neutral gray tint to distinguish the fills.
The brown is lighter in dark mode. The small **Z-score** switch hides or shows
its line, axis, shaded band, reference lines and
tooltip values immediately, without querying data. This preference is remembered
in the browser. It starts with the last calendar month, including today in the
server's configured time zone. Date boundaries are inclusive.

- Quick ranges to the left of the date inputs are **1D**, **1W**, **1M**, **3M**, **1Y**, **5Y**,
  and **All**. They apply the current filters and reload immediately. Months and
  years use calendar boundaries, including leap years; **1D** selects today.
  **All** resolves the earliest matching message within the supported history
  (from January 2000) through the selected end date, using the chosen scope filters.
- Dates and scope filters share one control panel. Edit **From**, **To**,
  **Interval**, or any scope filter, then select **Reload**. The button is enabled
  and highlighted when there are unapplied changes. It is disabled after a
  successful load until the selection changes. Failed loads can be retried.
  Reverting to the loaded selection disables Reload again. Pending edits are
  indicated by the button's enabled state and outline.
  The compact panel keeps the date controls and scope fields close together;
  the chart toolbar and summary cards also use less height on desktop.
- **Interval** selects daily, weekly, monthly, or yearly aggregation. **Auto**
  chooses daily for up to 180 days, weekly for up to 730 days, monthly for up to
  7,300 days, and yearly for longer ranges. Queries are limited to 1,000 calendar
  periods; choose a wider interval when an explicit selection exceeds that limit.
- Use the existing room and tag selectors, any/all tag matching, reply selection,
  and stored-since date to narrow the messages. These filters affect both series.
  Selected rooms and tags appear below their input fields.
  **Filters** starts closed. Expand it to reveal these controls.
- Hover, tap, or focus the chart and use the left/right arrow keys to inspect both
  values at the same period. Home/End inspect the first/last period.
  When the selection spans multiple years, the x-axis and inspection readout
  include the year. Monthly ticks use month/year labels; yearly ticks use years.
- Drag across the chart to select a date range and reload immediately when the
  drag ends. **Reset zoom** appears after a zoom successfully loads and restores
  the original date range with an immediate reload. The `+`/`-` keys prepare a
  zoom selection in a focused chart, applied with **Reload**.
- **View period data** displays the values in pages of 50 rows. **Export CSV**
  exports all periods in the last successfully loaded selection.
- **Browse messages** opens Search in retrieval mode with the loaded selection's
  dates and filters. Filter selections are stored in the URL for sharing and
  browser back/forward navigation.
- Search has a matching **Browse analytics** link beside its search controls.
  It carries dates, rooms, tags, replies and the retrieval stored-since filter
  into the overview.

The HTML page renders before its statistics query. Loading placeholders and an
animated update indicator provide feedback, and filter controls remain usable.
Refreshes retain the last successful charts with a notice while new data loads.
New selections cancel older browser requests and ignore stale responses. Failed
requests show an error and can be retried; retained charts are clearly marked.
Animations honor the browser's reduced-motion preference. Charts use Atenea's
existing light/dark tokens, fonts, square borders, and hard shadows.

## Frequency and z-score semantics

Statistics reuse `app_stats.services.api.get_message_stats` and
`StatsMsgSerializer`, including their room matching and tag filtering behavior.
The public `/api/v1/stats/msg` contract is unchanged.

Frequency fills missing calendar periods with a count of zero. Z-scores preserve
the service's baseline: the mean and population standard deviation are calculated
over periods containing matching messages. Missing periods have no z-score and
appear as gaps in that chart. A single populated period, or a constant populated
series, has a z-score of zero. The average card and dotted frequency mean use this
same populated-period baseline.

The z-score chart shows reference lines at ±2 standard deviations. **Unusual
periods** counts populated periods with an absolute z-score of at least 2; these
are indicators of unusual volume within this selection. Partial weeks, months,
and years include only messages within the selected dates. The tooltips and CSV
show their clipped date boundaries.

## Data stores and entity rankings

Elasticsearch serves the message frequency/z-score charts and lexical search.
ClickHouse serves entity rankings at `GET /api/v1/tg/msg/entities`: message
frequency counts messages containing an entity, while appearance count sums
all its occurrences. The request can select dates, rooms/tags, replies, storage
dates and entity filters, and order by either counter. Results are exact signed
aggregations of the matching facts. Each cursor page recomputes its scope;
`LIMIT 60` bounds the output, not the number of rows aggregated.

PostgreSQL remains the source for message annotations and entity metadata.
The INDEX stage maintains the search documents and analytical facts; see
[message indexing](pipelines/telegram-index-messages.md) for their storage and
retry semantics. Entity ranking and message activity use different grouping
keys and have different costs.

### Backend choice and query sizing

Grouping hundreds of millions of message/entity facts by entity exceeded the
interactive budget in Elasticsearch. Removing `SUM(count)` from a historical
comparison only reduced median time from 60.26 s to 56.15 s; the entity grouping
remained expensive. ClickHouse's columnar aggregation handles both counters
while Elasticsearch remains responsible for lexical retrieval.

On the development host (36 physical CPU cores, 376 GiB RAM, ClickHouse
26.8.27.9), the same **444.3M-fact** dataset gave these client medians for a
top 60. Ranges cover the two orderings; each had one warmup and three measured,
sequential queries with the ClickHouse result cache disabled:

| Scope | Elasticsearch | ClickHouse: 8 threads / 8 GiB / spill at 2 GiB | ClickHouse: 16 threads / 32 GiB / spill at 16 GiB |
| --- | ---: | ---: | ---: |
| Five years, 400.6M rows read by ClickHouse | 84.38–90.31 s | 21.58–22.64 s | 5.13–5.37 s |
| All history, 444.3M rows read by ClickHouse | Request circuit breaker | 21.53–21.76 s | 5.53–5.59 s |

Both ClickHouse configurations returned identical top-60 IDs and counters.
The five-year results also matched Elasticsearch; its all-history query could
not complete for comparison. The larger configuration used 6.5–8.7 GiB peak
memory per query with no aggregation spills, improving latency approximately
4× over the earlier configuration. This combines more threads and more memory
for aggregation; concurrent dashboard load was not measured. Configure the
[query resource limits](enviroments.md#clickhouse-connection-and-query-resources)
for the host and expected concurrency.

## Dashboard data route

`GET /dashboard/stats/` accepts HTML date strings (`YYYY-MM-DD`) and the stats
filters `room`, `tag`, `tag_match`, `is_reply`, `stored_since`, `createdat_min`, and
`createdat_max`, plus `group_by=auto|day|week|month|year`. Room and tag values may be
repeated or separated by commas. `range=1d|1w|1m|3m|1y|5y|all|custom` supplies
defaults when dates are omitted. The previous aliases `7d`, `30d`, `90d`, and
`365d` map to `1w`, `1m`, `3m`, and `1y`. The dashboard always requests both counts
and z-scores; the switch controls their presentation. `all` derives the start date
from the matching messages and selects Auto's interval after resolving that date.

The response contains the complete calendar `series`, resolved interval and date
range, time zone, update time, and summary values. Empty calendar rows have
`z_score: null`. Invalid filters return HTTP 400 before querying statistics; query
failures return HTTP 503 with a retry message. Responses use `Cache-Control:
no-store`. The browser authenticates through the staff session.

Relevant automated checks are in `app_dashboard/tests/test_statistics.py` and
`app_stats/tests/test_statistics.py`. Run them using the isolated PostgreSQL
environment described in [testing.md](testing.md).

Spanish strings are maintained in `app_dashboard/locale/es/LC_MESSAGES/django.po`.
After changing translations, compile the catalogue from `src/atenea_api` with
`uv run pybabel compile -d app_dashboard/locale -l es -D django`.
