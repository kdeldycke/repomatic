---
orphan: true
---

# {octicon}`desktop-download` Binaries

All standalone executables published by this repository, one row per binary, newest release first. The version links to its GitHub release, the platform to the direct binary download, and the VirusTotal cell to the file's public analysis.

Compiled Python binaries are regularly flagged by heuristic antivirus engines, so every release is submitted to [VirusTotal](https://www.virustotal.com/): this seeds vendor databases with the new signatures and keeps false positives in check. The VirusTotal cell tracks those false positives: a green check marks binaries no engine flags, and flagged binaries show the share of engine verdicts flagging them, snapshotted minutes after publication and before false-positive reports get processed. The live analysis behind the link supersedes it. An empty cell means the binary was never submitted, so VirusTotal holds no analysis to link to: this covers every release predating the scan pipeline.

## Minimum OS requirements

Binaries are dynamically linked against the C runtime of the environment they are compiled in, so each target carries a minimum OS requirement. Linux builds run inside `manylinux_2_28` containers and macOS builds pin their deployment target, which keeps these floors stable across runner image upgrades. Every build measures the floors it actually links against (with `repomatic verify-binary`) and fails if they exceed the values below:

| Target          | Floor                | Opens execution to                                                                                                                                         |
| --------------- | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `linux-x64`     | glibc `2.28`         | RHEL, AlmaLinux and Rocky Linux 8 and later, Debian 10, Ubuntu 20.04, openSUSE Leap 15.3 and SLES 15 SP3, Fedora 29, Amazon Linux 2023, and anything newer |
| `linux-arm64`   | glibc `2.28`         | The same distributions, on 64-bit ARM                                                                                                                      |
| `macos-arm64`   | macOS 11 Big Sur     | Every Apple-silicon Mac                                                                                                                                    |
| `macos-x64`     | macOS 10.15 Catalina | Intel Macs                                                                                                                                                 |
| `windows-x64`   | Windows 10           | The floor of CPython itself on x64                                                                                                                         |
| `windows-arm64` | Windows 11           | ARM PCs                                                                                                                                                    |

Systems below these floors (CentOS and RHEL 7 with glibc `2.17`, Ubuntu 18.04 with `2.27`, Amazon Linux 2 with `2.26`) and musl-based distributions like Alpine are not covered by the pre-built binaries. Install the package with [`uv`](https://docs.astral.sh/uv/) instead: `uv tool install <package>` works down to glibc `2.17`, on musl, and without any system Python.

The `macos-x64` row stays for as long as GitHub Actions offers an Intel macOS runner image to compile it on. That is a separate schedule from Apple's: an Intel Mac stops at macOS 26 Tahoe and keeps running it, so these builds outlast the Mac App Store accepting Intel apps.

## Development builds

Fresh binaries are compiled by the [release workflow](https://github.com/kdeldycke/repomatic/actions/workflows/release.yaml). A push to the default branch only rebuilds the canary subset (`linux-arm64` by default); every target is compiled on release commits, on the weekly Monday schedule, and on manual dispatch, so a given platform is at most a week behind. See [](nuitka.md#build-cadence) for the full cadence. To try the latest development build: open the most recent successful run covering your platform and download its artifact (a GitHub account is required, and the binary comes wrapped in a zip). The same builds are also attached to a rolling dev pre-release, a draft only visible to repository maintainers.

<!-- binaries-chart -->

## VirusTotal detections

Share of antivirus engine verdicts flagging the binaries of each release, at scan time. Colors follow the catalog shields: green for zero detections, amber below 10%, red from there up.

```{raw} html
<div style="height: 320px;"><canvas id="vt-trend"></canvas></div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.min.js" integrity="sha384-XcdcwHqIPULERb2yDEM4R0XaQKU3YnDsrTmjACBZyfdVVqjh6xQ4/DCMd7XLcA6Y" crossorigin="anonymous"></script>
<script>
const VT_TREND = [{"date": "2026-04-15", "flagged": 26, "pct": 6.9, "tag": "v6.13.0", "total": 379}, {"date": "2026-04-20", "flagged": 32, "pct": 8.1, "tag": "v6.14.0", "total": 394}, {"date": "2026-04-27", "flagged": 30, "pct": 7.7, "tag": "v6.15.0", "total": 392}, {"date": "2026-04-29", "flagged": 30, "pct": 7.7, "tag": "v6.16.0", "total": 392}, {"date": "2026-05-04", "flagged": 28, "pct": 7.3, "tag": "v6.17.0", "total": 382}, {"date": "2026-05-07", "flagged": 28, "pct": 7.3, "tag": "v6.18.0", "total": 382}, {"date": "2026-05-08", "flagged": 28, "pct": 7.3, "tag": "v6.18.1", "total": 384}, {"date": "2026-05-08", "flagged": 29, "pct": 7.5, "tag": "v6.18.2", "total": 385}, {"date": "2026-05-11", "flagged": 9, "pct": 2.3, "tag": "v6.18.3", "total": 387}, {"date": "2026-05-14", "flagged": 10, "pct": 2.6, "tag": "v6.18.4", "total": 389}, {"date": "2026-05-21", "flagged": 10, "pct": 2.6, "tag": "v6.19.0", "total": 390}, {"date": "2026-05-24", "flagged": 5, "pct": 1.3, "tag": "v6.20.0", "total": 381}, {"date": "2026-05-25", "flagged": 7, "pct": 1.8, "tag": "v6.21.0", "total": 386}, {"date": "2026-05-25", "flagged": 5, "pct": 1.3, "tag": "v6.22.0", "total": 387}, {"date": "2026-05-28", "flagged": 7, "pct": 1.8, "tag": "v6.24.0", "total": 382}, {"date": "2026-06-13", "flagged": 23, "pct": 6.0, "tag": "v6.25.0", "total": 383}, {"date": "2026-06-13", "flagged": 22, "pct": 5.8, "tag": "v6.25.1", "total": 378}, {"date": "2026-06-17", "flagged": 22, "pct": 5.9, "tag": "v6.26.0", "total": 375}, {"date": "2026-06-18", "flagged": 20, "pct": 5.2, "tag": "v6.27.0", "total": 381}, {"date": "2026-06-19", "flagged": 16, "pct": 4.4, "tag": "v6.28.0", "total": 360}, {"date": "2026-06-19", "flagged": 17, "pct": 4.5, "tag": "v6.28.1", "total": 377}, {"date": "2026-06-22", "flagged": 22, "pct": 5.9, "tag": "v6.29.0", "total": 372}, {"date": "2026-06-24", "flagged": 14, "pct": 4.5, "tag": "v6.30.0", "total": 312}, {"date": "2026-06-27", "flagged": 18, "pct": 4.7, "tag": "v6.31.0", "total": 385}, {"date": "2026-07-02", "flagged": 20, "pct": 5.3, "tag": "v7.0.0", "total": 380}, {"date": "2026-07-08", "flagged": 24, "pct": 6.5, "tag": "v7.1.0", "total": 372}, {"date": "2026-07-16", "flagged": 22, "pct": 5.9, "tag": "v7.2.0", "total": 374}, {"date": "2026-07-23", "flagged": 18, "pct": 4.7, "tag": "v7.3.0", "total": 384}, {"date": "2026-07-28", "flagged": 26, "pct": 6.8, "tag": "v7.3.1", "total": 383}, {"date": "2026-07-31", "flagged": 5, "pct": 2.0, "tag": "v7.4.0", "total": 245}, {"date": "2026-08-01", "flagged": 25, "pct": 6.5, "tag": "v7.4.1", "total": 385}, {"date": "2026-08-07", "flagged": 2, "pct": 0.8, "tag": "v7.5.0", "total": 247}, {"date": "2026-08-08", "flagged": 25, "pct": 6.6, "tag": "v7.6.0", "total": 379}, {"date": "2026-08-09", "flagged": 23, "pct": 6.1, "tag": "v7.8.0", "total": 377}, {"date": "2026-08-10", "flagged": 21, "pct": 5.4, "tag": "v7.9.0", "total": 386}, {"date": "2026-08-12", "flagged": 21, "pct": 5.5, "tag": "v7.10.0", "total": 383}, {"date": "2026-08-13", "flagged": 22, "pct": 5.7, "tag": "v7.11.0", "total": 384}, {"date": "2026-08-14", "flagged": 19, "pct": 5.1, "tag": "v7.12.0", "total": 375}, {"date": "2026-08-15", "flagged": 18, "pct": 4.7, "tag": "v7.12.1", "total": 382}, {"date": "2026-08-17", "flagged": 19, "pct": 5.1, "tag": "v7.13.0", "total": 372}];
const VT_DANGER_PCT = 10;
const vtCss = getComputedStyle(document.documentElement);
const vtColor = (name, fallback) =>
    vtCss.getPropertyValue(name).trim() || fallback;
const vtTint = (p) => {
    if (p.pct === 0) { return vtColor("--sd-color-success", "#28a745"); }
    return p.pct >= VT_DANGER_PCT
        ? vtColor("--sd-color-danger", "#dc3545")
        : vtColor("--sd-color-warning", "#f0b37e");
};
new Chart(document.getElementById("vt-trend"), {
    type: "line",
    data: {
        datasets: [{
            data: VT_TREND.map((p) => ({x: Date.parse(p.date), y: p.pct})),
            borderColor: "#88888866",
            pointBackgroundColor: VT_TREND.map(vtTint),
            pointBorderColor: VT_TREND.map(vtTint),
            pointRadius: 4,
            tension: 0.2,
        }],
    },
    options: {
        maintainAspectRatio: false,
        plugins: {
            legend: {display: false},
            tooltip: {callbacks: {
                title: (items) => VT_TREND[items[0].dataIndex].tag,
                label: (item) => {
                    const p = VT_TREND[item.dataIndex];
                    return p.flagged + " / " + p.total
                        + " verdicts flagged (" + p.pct + "%)";
                },
            }},
        },
        scales: {
            x: {
                type: "linear",
                ticks: {
                    maxTicksLimit: 8,
                    callback: (value) =>
                        new Date(value).toISOString().slice(0, 10),
                },
            },
            y: {
                beginAtZero: true,
                title: {display: true, text: "Flagged verdicts (%)"},
            },
        },
    },
});
</script>
```

<!-- binaries-chart-end -->

## Catalog

The table is searchable and sortable on the documentation site; the raw data lives in [`binaries.csv`](assets/binaries.csv).

```{csv-table}
:file: assets/binaries.csv
:header-rows: 1
:class: sphinx-datatable
```
