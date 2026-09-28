# Experiment report design

Audience: the bundle owner comparing completed work across the four arms.
Primary job: answer whether correctness, time and total cost improve together,
with access to the measured cases and explicit coverage.

Palette: paper white `#f7f9fc`, ink `#202a43`, ultramarine `#3156d8`,
sea green `#006c67`, loss red `#b74546`, soft blue `#e7edff`.
Type: Avenir Next when available, then a local system sans; 64/40/24/16px scale.
No external font or chart service. Tabular numbers only for comparable metrics.

Layout: left-aligned report with a generous opening, a continuous four-lane
experiment diagram, then a compact comparison table and an inspectable issue
matrix. Earlier microbenchmarks live in a separate interactive section.

    title + evidence state      four experiment lanes
    coverage / cost / quality comparison table
    task search + filters      selected issue receipt
    measured small examples    paired horizontal bars
    method + download

Review: a generic KPI-card hero would hide that the full study is pending. Use
the actual experiment lanes and coverage instead. Avoid decorative gradients,
fake traffic, animated counters, inferred savings and a leaderboard winner
until the data supports one. The distinctive element is the 500-issue matrix:
every square represents a real scheduled issue, with four visible arm states.

Accessibility: keyboard-operable selection and filters, visible focus, text
alternatives to status colors, responsive columns, reduced-motion support.
