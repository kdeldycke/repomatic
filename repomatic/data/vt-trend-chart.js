const VT_TREND = __VT_TREND_PAYLOAD__;
const VT_DANGER_PCT = __VT_DANGER_PCT__;
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
