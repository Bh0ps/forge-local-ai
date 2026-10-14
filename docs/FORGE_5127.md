# Forge 5.1.27

## Clearer Usage

Usage keeps Today, This month and All time together, with readable supporting
labels, aligned numbers and compact period, model and project controls. The
existing font, warm palettes and workspace layout remain familiar.

Loading and failed reads show unavailable totals instead of suggesting zero
usage. Refresh keeps a previously loaded snapshot visible for the same filters
and explains when it is still waiting or has failed. Retry loads those filters
again. Changing the period, model, project or timezone retires the earlier read;
a delayed response cannot overwrite the newly selected scope. Replacement
transports and reopened pages also retain their own response ownership.

A selected model or project remains visibly identified if it disappears from
the available choices. It is marked unavailable until you choose another filter.
The screen does not silently label those results as All models or All projects.

The daily chart shows its date range. Longer histories explicitly show the
latest 31 recorded days while totals still cover the selected period. Bar heights
use that visible range, so an older peak outside it cannot flatten the chart.
Input uses a neutral fill and output retains the selected accent. Thin plot-colored
separators keep the stacked series distinct, with matching, readable legend marks.
The model table has a clear caption and aligned numeric columns. Recorded totals
without daily entries are distinguished from a period with no usage.

This revision retains Library improvements, saved drafts, chat and tray fixes,
the grouped composer, workspace tabs, searchable Settings, keyboard controls,
HUD and reduced-motion behavior. It adds no providers, background model actions
or additional accounting filters.

## Validation

Component regressions cover delayed responses, repeated refresh, filter and
timezone changes, transport replacement, unmount, retry, acknowledged zero
counts, unavailable selected filters and the visible chart range. Production
fixtures use synthetic accounting data to inspect themes, palette contrast,
narrow and enlarged layouts, keyboard focus, loading, failure and retry states.

Full frontend and production UI evidence is recorded during implementation.
Final Windows CI, packaged lifecycle, installer, publisher and privacy checks
remain separate requirements before publication and installation.
