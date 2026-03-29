/* Wyoming Pulse — Bloomberg Terminal Frontend */
/* D3.js map, Chart.js v4, dense data rendering */

// ── State ──
let charts = {};
let currentPage = 'dashboard';
let articleOffset = 0;
let articleTotal = 0;
let articleCache = {};  // id -> article data for detail panel
let mapView = 'none';        // 'none' | 'us' | 'county'
let mapActiveState = null;    // 'wyoming' | 'texas' | null
let mapTopoData = null;       // cached TopoJSON
let mapStateSentiment = {};   // cached state-level sentiment

// ── Chart.js Defaults ──
Chart.defaults.color = '#6b7280';
Chart.defaults.borderColor = 'rgba(26, 31, 46, 0.5)';
Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
Chart.defaults.font.size = 11;

// ══════════════════════════════════════════════
//  HELPERS
// ══════════════════════════════════════════════
function sentimentColor(score) {
    if (score === null || score === undefined) return '#374151';
    // Smooth gradient: 1.0 (red) → 3.0 (yellow) → 5.0 (green)
    const t = Math.max(0, Math.min(1, (score - 1) / 4)); // normalize 1-5 to 0-1
    // Color stops: #ef4444 (0) → #f97316 (0.25) → #eab308 (0.5) → #84cc16 (0.75) → #22c55e (1.0)
    const stops = [
        [0.00, [239, 68, 68]],   // red
        [0.25, [249, 115, 22]],  // orange
        [0.50, [234, 179, 8]],   // yellow
        [0.75, [132, 204, 22]],  // lime
        [1.00, [34, 197, 94]],   // green
    ];
    let i = 0;
    while (i < stops.length - 1 && stops[i + 1][0] < t) i++;
    if (i >= stops.length - 1) return `rgb(${stops[stops.length - 1][1].join(',')})`;
    const [t0, c0] = stops[i], [t1, c1] = stops[i + 1];
    const f = (t - t0) / (t1 - t0);
    const r = Math.round(c0[0] + f * (c1[0] - c0[0]));
    const g = Math.round(c0[1] + f * (c1[1] - c0[1]));
    const b = Math.round(c0[2] + f * (c1[2] - c0[2]));
    return `rgb(${r},${g},${b})`;
}

function sentimentPillClass(label) {
    const map = {
        'strongly_positive': 'pill-s5',
        'slightly_positive': 'pill-s4',
        'neutral': 'pill-s3',
        'slightly_negative': 'pill-s2',
        'strongly_negative': 'pill-s1',
    };
    return map[label] || 'pill-s3';
}

function sentimentLabel(label) {
    const map = {
        'strongly_positive': 'V.POS',
        'slightly_positive': 'POS',
        'neutral': 'NEU',
        'slightly_negative': 'NEG',
        'strongly_negative': 'V.NEG',
    };
    return map[label] || label || '--';
}

function scoreToLabel(score) {
    if (score === null || score === undefined) return '--';
    if (score >= 4.5) return 'V.POS';
    if (score >= 3.5) return 'POS';
    if (score >= 2.5) return 'NEU';
    if (score >= 1.5) return 'NEG';
    return 'V.NEG';
}

function fmtDate(iso) {
    if (!iso) return '--';
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function fmtDateTime(iso) {
    if (!iso) return '--';
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function capitalize(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ''; }

function topicDisplay(tag) {
    const map = {
        'energy_ratepayer': 'Energy/Ratepayer', 'water': 'Water',
        'jobs_economic': 'Jobs/Econ', 'land_use_wildlife': 'Land/Wildlife',
        'regulation_transparency': 'Regulation', 'tax_incentives': 'Tax',
        'national_security_ai': 'NatSec/AI', 'community_impact': 'Community',
    };
    return map[tag] || tag;
}

function escapeHtml(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

async function fetchJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`API ${r.status}`);
    return r.json();
}

function destroyChart(id) {
    if (charts[id]) { charts[id].destroy(); delete charts[id]; }
}

function timeAgo(iso) {
    if (!iso) return '--';
    const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
    if (m < 1) return 'just now';
    if (m < 60) return m + 'm ago';
    const h = Math.floor(m / 60);
    if (h < 24) return h + 'h ago';
    return Math.floor(h / 24) + 'd ago';
}

// Shared tooltip config
const ttCfg = {
    backgroundColor: '#0a0f1a',
    borderColor: '#14b8a6',
    borderWidth: 1,
    cornerRadius: 0,
    titleFont: { family: "'JetBrains Mono', monospace", size: 10, weight: '500' },
    bodyFont: { family: "'JetBrains Mono', monospace", size: 10 },
    padding: 8,
};

// ══════════════════════════════════════════════
//  PAGE SWITCHING
// ══════════════════════════════════════════════
function switchPage(name) {
    currentPage = name;
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelector(`.nav-item[data-page="${name}"]`).classList.add('active');
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById(`page-${name}`).classList.add('active');

    const render = { dashboard: renderDashboard, sentiment: renderSentiment,
                     articles: () => { articleOffset = 0; renderArticles(); }, system: renderSystem,
                     control: renderControl };
    if (render[name]) render[name]();
}

// ══════════════════════════════════════════════
//  D3.js MAP — US overview + state drill-down
// ══════════════════════════════════════════════
const STATE_CONFIG = {
    wyoming: {
        fips: '56',
        name: 'Wyoming',
        cities: {
            evanston:  [-110.9632, 41.2683],
            casper:    [-106.3131, 42.8666],
            cheyenne:  [-104.8202, 41.1400],
        },
        cityCountyFips: { evanston: '56041', casper: '56025', cheyenne: '56021' },
    },
    texas: {
        fips: '48',
        name: 'Texas',
        cities: {
            dallas: [-96.7970, 32.7767],
        },
        cityCountyFips: { dallas: '48113' },
    },
    michigan: {
        fips: '26',
        name: 'Michigan',
        cities: {
            ann_arbor:      [-83.7430, 42.2808],
            van_buren:      [-83.4858, 42.2182],
            benton_harbor:  [-86.4542, 42.1167],
        },
        cityCountyFips: { ann_arbor: '26161', van_buren: '26163', benton_harbor: '26021' },
        labelOffsets: { ann_arbor: [8, -10], van_buren: [8, 16] },
    },
};

const STATE_NAME_TO_KEY = {
    'Wyoming': 'wyoming',
    'Texas': 'texas',
    'Michigan': 'michigan',
};

// ── Tooltip positioning helper ──
function positionTooltip(event, container) {
    const tooltip = document.getElementById('mapTooltip');
    const rect = container.getBoundingClientRect();
    tooltip.style.left = (event.clientX - rect.left + 10) + 'px';
    tooltip.style.top = (event.clientY - rect.top - 10) + 'px';
}

// ── Ensure TopoJSON is loaded (cached) ──
async function ensureTopoData() {
    if (!mapTopoData) {
        mapTopoData = await d3.json('https://cdn.jsdelivr.net/npm/us-atlas@3/counties-10m.json');
    }
    return mapTopoData;
}

// ── US Map (default view) ──
async function renderUSMap(stateSentiment) {
    const container = document.getElementById('wyomingMap');
    if (!container) return;
    container.innerHTML = '';

    try {
        const us = await ensureTopoData();
        const states = topojson.feature(us, us.objects.states);

        const width = container.clientWidth;
        const height = container.clientHeight || 330;
        const projection = d3.geoAlbersUsa().fitSize([width, height], states);
        const path = d3.geoPath().projection(projection);

        const svg = d3.select(container)
            .append('svg')
            .attr('width', width)
            .attr('height', height);

        svg.selectAll('.state-path')
            .data(states.features)
            .enter()
            .append('path')
            .attr('class', 'state-path')
            .attr('d', path)
            .attr('data-state-name', d => d.properties.name)
            .attr('fill', d => {
                const key = STATE_NAME_TO_KEY[d.properties.name];
                if (key && stateSentiment[key] && stateSentiment[key].avg !== null) {
                    return sentimentColor(stateSentiment[key].avg);
                }
                return '#0f1520';
            })
            .attr('stroke', d => {
                const key = STATE_NAME_TO_KEY[d.properties.name];
                return (key && stateSentiment[key]) ? '#14b8a6' : 'rgba(148,163,184,0.2)';
            })
            .attr('stroke-width', d => {
                const key = STATE_NAME_TO_KEY[d.properties.name];
                return (key && stateSentiment[key]) ? 0.8 : 0.3;
            })
            .attr('cursor', 'pointer')
            .on('mouseenter', function(event, d) {
                const tooltip = document.getElementById('mapTooltip');
                const key = STATE_NAME_TO_KEY[d.properties.name];
                if (key && stateSentiment[key] && stateSentiment[key].avg !== null) {
                    const s = stateSentiment[key];
                    tooltip.textContent = `${d.properties.name} | avg: ${s.avg.toFixed(1)} | n=${s.count}`;
                } else {
                    tooltip.textContent = d.properties.name;
                }
                tooltip.classList.add('visible');
            })
            .on('mousemove', function(event) { positionTooltip(event, container); })
            .on('mouseleave', function() {
                document.getElementById('mapTooltip').classList.remove('visible');
            })
            .on('click', function(event, d) {
                const key = STATE_NAME_TO_KEY[d.properties.name];
                if (key && STATE_CONFIG[key]) {
                    drillIntoState(key);
                } else {
                    const tooltip = document.getElementById('mapTooltip');
                    tooltip.textContent = `${d.properties.name} — No data yet`;
                    tooltip.classList.add('visible');
                    setTimeout(() => tooltip.classList.remove('visible'), 1500);
                }
            });

        mapView = 'us';
        mapActiveState = null;
        mapStateSentiment = stateSentiment;
        document.getElementById('mapTitle').textContent = 'United States';
        document.getElementById('mapBackBtn').style.display = 'none';

    } catch (err) {
        console.error('US map render error:', err);
        container.innerHTML = '<div class="no-data">Map unavailable</div>';
    }
}

// ── State drill-down (county view) ──
async function drillIntoState(stateKey) {
    const config = STATE_CONFIG[stateKey];
    if (!config) return;

    const container = document.getElementById('wyomingMap');
    container.innerHTML = '';

    try {
        const us = await ensureTopoData();
        const counties = topojson.feature(us, us.objects.counties);
        const statesGeo = topojson.feature(us, us.objects.states);

        const stateCounties = {
            type: 'FeatureCollection',
            features: counties.features.filter(f => String(f.id).startsWith(config.fips))
        };
        const stateOutline = {
            type: 'FeatureCollection',
            features: statesGeo.features.filter(f => String(f.id) === config.fips)
        };

        const width = container.clientWidth;
        const height = container.clientHeight || 330;
        const projection = d3.geoAlbersUsa().fitSize([width, height], stateOutline);
        const path = d3.geoPath().projection(projection);

        const svg = d3.select(container)
            .append('svg')
            .attr('width', width)
            .attr('height', height);

        // Neighboring state outlines (rendered first, behind counties)
        const neighborStates = statesGeo.features.filter(f => String(f.id) !== config.fips);
        svg.selectAll('.neighbor-state')
            .data(neighborStates)
            .enter()
            .append('path')
            .attr('class', 'neighbor-state')
            .attr('d', path)
            .attr('fill', '#0a0e17')
            .attr('stroke', 'rgba(148,163,184,0.15)')
            .attr('stroke-width', 0.5);

        // Fetch location sentiment for this state
        const locations = await fetchJSON(`/api/locations?state=${stateKey}`);
        const cityFips = config.cityCountyFips;

        // Reverse lookup: FIPS -> city key
        const fipsToCityKey = {};
        Object.entries(cityFips).forEach(([city, fips]) => { fipsToCityKey[fips] = city; });

        // Draw counties
        svg.selectAll('.county-path')
            .data(stateCounties.features)
            .enter()
            .append('path')
            .attr('class', 'county-path')
            .attr('d', path)
            .attr('data-fips', d => d.id)
            .on('mouseenter', function(event, d) {
                const fips = String(d.id);
                const countyName = (d.properties && d.properties.name) ? d.properties.name : `FIPS ${fips}`;
                const tooltip = document.getElementById('mapTooltip');
                const cityKey = fipsToCityKey[fips];
                if (cityKey && locations[cityKey] && locations[cityKey].avg !== null) {
                    tooltip.textContent = `${countyName} Co. | avg: ${locations[cityKey].avg.toFixed(1)} | n=${locations[cityKey].count}`;
                } else {
                    tooltip.textContent = `${countyName} Co.`;
                }
                tooltip.classList.add('visible');
            })
            .on('mousemove', function(event) { positionTooltip(event, container); })
            .on('mouseleave', function() {
                document.getElementById('mapTooltip').classList.remove('visible');
            });

        // Color counties with city data
        Object.entries(cityFips).forEach(([loc, fips]) => {
            const el = document.querySelector(`.county-path[data-fips="${fips}"]`);
            if (el && locations[loc] && locations[loc].avg !== null) {
                el.style.fill = sentimentColor(locations[loc].avg);
                el.style.stroke = '#14b8a6';
                el.style.strokeWidth = '0.8';
            }
        });

        // State outline
        if (stateOutline.features[0]) {
            svg.append('path')
                .datum(stateOutline.features[0])
                .attr('d', path)
                .attr('fill', 'none')
                .attr('stroke', '#14b8a6')
                .attr('stroke-width', 1)
                .attr('stroke-opacity', 0.3);
        }

        // City markers
        const labelOffsets = config.labelOffsets || {};
        Object.entries(config.cities).forEach(([city, coords]) => {
            const projected = projection(coords);
            if (projected) {
                const [x, y] = projected;
                const [lx, ly] = labelOffsets[city] || [8, 4];
                svg.append('circle').attr('cx', x).attr('cy', y).attr('r', 3).attr('class', 'map-marker-pulse');
                svg.append('circle').attr('cx', x).attr('cy', y).attr('r', 3).attr('class', 'map-marker-dot');
                svg.append('text')
                    .attr('x', x + lx).attr('y', y + ly)
                    .attr('fill', '#e2e8f0')
                    .attr('font-family', "'JetBrains Mono', monospace")
                    .attr('font-size', '9px')
                    .attr('font-weight', '600')
                    .style('filter', 'drop-shadow(0 0 3px rgba(0,0,0,0.8)) drop-shadow(0 0 6px rgba(0,0,0,0.5))')
                    .text(city.replace(/_/g, ' ').toUpperCase());
            }
        });

        mapView = 'county';
        mapActiveState = stateKey;
        document.getElementById('mapTitle').textContent = `${config.name} Counties`;
        document.getElementById('mapBackBtn').style.display = '';

    } catch (err) {
        console.error('State map render error:', err);
        container.innerHTML = '<div class="no-data">Map unavailable</div>';
    }
}

// ── Back to US map ──
async function showUSMap() {
    if (!mapStateSentiment || Object.keys(mapStateSentiment).length === 0) {
        mapStateSentiment = await fetchJSON('/api/state-sentiment');
    }
    renderUSMap(mapStateSentiment);
}

// ══════════════════════════════════════════════
//  PAGE 1: DASHBOARD
// ══════════════════════════════════════════════
async function renderDashboard() {
    try {
        const [overview, trend, voice, stateSentiment] = await Promise.all([
            fetchJSON('/api/overview'),
            fetchJSON('/api/sentiment-trend'),
            fetchJSON('/api/voice-comparison'),
            fetchJSON('/api/state-sentiment'),
        ]);

        // Metrics
        document.getElementById('metricTotal').textContent = overview.total_articles;
        document.getElementById('metricAnalyzed').textContent = overview.analyzed_articles || 0;

        const avgEl = document.getElementById('metricSentiment');
        if (overview.avg_sentiment !== null) {
            avgEl.textContent = overview.avg_sentiment.toFixed(2);
            avgEl.style.color = sentimentColor(overview.avg_sentiment);
        } else { avgEl.textContent = '--'; }

        const eliteEl = document.getElementById('metricElite');
        if (voice.elite.avg !== null) {
            eliteEl.textContent = voice.elite.avg.toFixed(2);
            eliteEl.style.color = sentimentColor(voice.elite.avg);
        } else { eliteEl.textContent = '--'; }

        const pubEl = document.getElementById('metricPublic');
        if (voice.public.avg !== null) {
            pubEl.textContent = voice.public.avg.toFixed(2);
            pubEl.style.color = sentimentColor(voice.public.avg);
        } else { pubEl.textContent = '--'; }

        // Sync
        document.getElementById('lastSync').textContent = overview.last_ingestion ? timeAgo(overview.last_ingestion) : '--';

        // Map — default to US view, but don't reset if user drilled into a state
        if (mapView === 'none' || mapView === 'us') {
            renderUSMap(stateSentiment);
        }

        // Charts
        renderTrendChart(trend.data);
        renderVoiceChart(voice);

    } catch (err) { console.error('Dashboard error:', err); }
}

function renderTrendChart(data) {
    const canvas = document.getElementById('trendChart');
    const noData = document.getElementById('trendNoData');
    destroyChart('trend');

    if (!data || data.length === 0) {
        canvas.style.display = 'none';
        noData.style.display = 'block';
        return;
    }
    canvas.style.display = 'block';
    noData.style.display = 'none';

    charts.trend = new Chart(canvas, {
        type: 'line',
        data: {
            labels: data.map(d => d.date),
            datasets: [{
                data: data.map(d => d.avg_sentiment),
                borderColor: '#14b8a6',
                backgroundColor: 'transparent',
                fill: false,
                tension: 0.2,
                pointRadius: data.length === 1 ? 4 : 2,
                pointHoverRadius: 5,
                pointBackgroundColor: '#14b8a6',
                pointBorderColor: '#050810',
                pointBorderWidth: 1,
                borderWidth: 1.5,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            plugins: {
                legend: { display: false },
                tooltip: { ...ttCfg, callbacks: { afterLabel: ctx => `n=${data[ctx.dataIndex].count}` } }
            },
            scales: {
                y: { min: 1, max: 5, grid: { color: 'rgba(26,31,46,0.5)' },
                     ticks: { stepSize: 1, font: { family: "'JetBrains Mono', monospace", size: 9 },
                              callback: v => ({ 1:'V.NEG', 2:'NEG', 3:'NEU', 4:'POS', 5:'V.POS' }[v] || v) } },
                x: { grid: { color: 'rgba(26,31,46,0.3)' },
                     ticks: { maxTicksLimit: 10, font: { family: "'JetBrains Mono', monospace", size: 9 } } }
            }
        }
    });
}

function renderVoiceChart(voice) {
    const canvas = document.getElementById('voiceChart');
    const noData = document.getElementById('voiceNoData');
    destroyChart('voice');

    if (voice.elite.avg === null && voice.public.avg === null) {
        canvas.style.display = 'none';
        noData.style.display = 'block';
        return;
    }
    canvas.style.display = 'block';
    noData.style.display = 'none';

    charts.voice = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: ['Elite', 'Public'],
            datasets: [{
                data: [voice.elite.avg || 0, voice.public.avg || 0],
                backgroundColor: [
                    voice.elite.avg ? sentimentColor(voice.elite.avg) : '#1a1f2e',
                    voice.public.avg ? sentimentColor(voice.public.avg) : '#1a1f2e',
                ],
                borderRadius: 0,
                barThickness: 18,
            }]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: true,
            plugins: { legend: { display: false }, tooltip: { ...ttCfg,
                callbacks: { afterLabel: ctx => `n=${ctx.dataIndex === 0 ? voice.elite.count : voice.public.count}` } } },
            scales: {
                x: { min: 0, max: 5, grid: { color: 'rgba(26,31,46,0.5)' },
                     ticks: { font: { family: "'JetBrains Mono', monospace", size: 9 } } },
                y: { grid: { display: false },
                     ticks: { font: { family: "'JetBrains Mono', monospace", size: 10 } } }
            }
        }
    });
}

// ══════════════════════════════════════════════
//  PAGE 2: SENTIMENT
// ══════════════════════════════════════════════
async function renderSentiment() {
    try {
        const [trend, locations, topics] = await Promise.all([
            fetchJSON('/api/sentiment-trend'),
            fetchJSON('/api/locations'),
            fetchJSON('/api/topics'),
        ]);
        renderLocationHeatmap(trend.data, locations);
        renderTopicHeatmap(trend.data, topics);
        renderDistribution();
    } catch (err) { console.error('Sentiment error:', err); }
}

function renderLocationHeatmap(trendData, locations) {
    const el = document.getElementById('locationHeatmap');
    const noData = document.getElementById('locHeatNoData');
    if (!trendData || trendData.length === 0) { el.innerHTML = ''; noData.style.display = 'block'; return; }
    noData.style.display = 'none';

    const locs = ['evanston', 'casper', 'cheyenne', 'statewide'];
    const dates = trendData.map(d => d.date);
    el.style.gridTemplateColumns = `90px repeat(${dates.length}, 34px)`;

    let html = '<div class="hm-label"></div>';
    dates.forEach(d => { html += `<div class="hm-header">${fmtDate(d)}</div>`; });

    locs.forEach(loc => {
        html += `<div class="hm-label">${capitalize(loc)}</div>`;
        dates.forEach((d, i) => {
            const locAvg = locations[loc]?.avg;
            const dayAvg = trendData[i].avg_sentiment;
            const val = locAvg !== null && locAvg !== undefined ? (dayAvg * 0.5 + locAvg * 0.5) : null;
            if (val !== null) {
                html += `<div class="hm-cell" style="background:${sentimentColor(val)};opacity:0.8" data-tip="${capitalize(loc)} | ${fmtDate(d)} | ${val.toFixed(1)}"></div>`;
            } else {
                html += `<div class="hm-cell empty" data-tip="${capitalize(loc)} | ${fmtDate(d)} | --"></div>`;
            }
        });
    });
    el.innerHTML = html;
    attachHmTooltips(el);
}

function renderTopicHeatmap(trendData, topicData) {
    const el = document.getElementById('topicHeatmap');
    const noData = document.getElementById('topicHeatNoData');
    if (!topicData.topics || topicData.topics.length === 0 || !trendData || trendData.length === 0) {
        el.innerHTML = ''; noData.style.display = 'block'; return;
    }
    noData.style.display = 'none';

    const topics = topicData.topics.slice(0, 8);
    const dates = trendData.map(d => d.date);
    el.style.gridTemplateColumns = `90px repeat(${dates.length}, 34px)`;

    let html = '<div class="hm-label"></div>';
    dates.forEach(d => { html += `<div class="hm-header">${fmtDate(d)}</div>`; });

    topics.forEach(t => {
        const name = topicDisplay(t.name);
        const short = name.length > 12 ? name.substring(0, 10) + '..' : name;
        html += `<div class="hm-label" title="${name}">${short}</div>`;
        dates.forEach((d, i) => {
            if (t.avg_sentiment !== null) {
                html += `<div class="hm-cell" style="background:${sentimentColor(t.avg_sentiment)};opacity:0.8" data-tip="${name} | ${fmtDate(d)} | ${t.avg_sentiment.toFixed(1)} (n=${t.count})"></div>`;
            } else {
                html += `<div class="hm-cell empty" data-tip="${name} | ${fmtDate(d)} | --"></div>`;
            }
        });
    });
    el.innerHTML = html;
    attachHmTooltips(el);
}

function attachHmTooltips(container) {
    const tip = document.getElementById('hmTooltip');
    container.querySelectorAll('.hm-cell').forEach(c => {
        c.addEventListener('mouseenter', () => { tip.textContent = c.dataset.tip; tip.classList.add('visible'); });
        c.addEventListener('mousemove', e => { tip.style.left = (e.clientX + 10) + 'px'; tip.style.top = (e.clientY - 6) + 'px'; });
        c.addEventListener('mouseleave', () => { tip.classList.remove('visible'); });
    });
}

async function renderDistribution() {
    const canvas = document.getElementById('distributionChart');
    const noData = document.getElementById('distNoData');
    destroyChart('dist');

    try {
        const data = await fetchJSON('/api/articles?limit=1000');
        const counts = { strongly_positive: 0, slightly_positive: 0, neutral: 0, slightly_negative: 0, strongly_negative: 0 };
        data.articles.forEach(a => { if (a.sentiment_label && counts.hasOwnProperty(a.sentiment_label)) counts[a.sentiment_label]++; });

        const total = Object.values(counts).reduce((a, b) => a + b, 0);
        if (total === 0) { canvas.style.display = 'none'; noData.style.display = 'block'; return; }
        canvas.style.display = 'block';
        noData.style.display = 'none';

        charts.dist = new Chart(canvas, {
            type: 'doughnut',
            data: {
                labels: ['V.POS', 'POS', 'NEU', 'NEG', 'V.NEG'],
                datasets: [{
                    data: [counts.strongly_positive, counts.slightly_positive, counts.neutral, counts.slightly_negative, counts.strongly_negative],
                    backgroundColor: ['#22c55e', '#84cc16', '#eab308', '#f97316', '#ef4444'],
                    borderWidth: 0,
                    hoverOffset: 4,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                cutout: '75%',
                plugins: {
                    legend: { position: 'bottom', labels: { boxWidth: 8, padding: 12, font: { family: "'JetBrains Mono', monospace", size: 9 }, color: '#6b7280' } },
                    tooltip: ttCfg,
                }
            }
        });
    } catch (err) { console.error('Distribution error:', err); }
}

// ══════════════════════════════════════════════
//  PAGE 3: ARTICLES (table with expandable rows)
// ══════════════════════════════════════════════
const STATE_ABBR = { wyoming: 'WY', texas: 'TX', michigan: 'MI', nationwide: 'US', other: '??' };
const STATE_REGIONS = {
    wyoming: ['statewide', 'evanston', 'casper', 'cheyenne'],
    texas: ['statewide', 'dallas'],
    michigan: ['statewide', 'ann_arbor', 'van_buren', 'benton_harbor'],
    nationwide: [],
};

function onStateFilterChange() {
    const state = document.getElementById('filterState').value;
    const locSelect = document.getElementById('filterLocation');
    const groups = {
        wyoming:  document.getElementById('filterLocWY'),
        texas:    document.getElementById('filterLocTX'),
        michigan: document.getElementById('filterLocMI'),
    };
    locSelect.value = '';
    if (!state) {
        Object.values(groups).forEach(g => g.style.display = '');
        locSelect.disabled = false;
    } else if (state === 'nationwide') {
        Object.values(groups).forEach(g => g.style.display = 'none');
        locSelect.disabled = true;
    } else {
        Object.entries(groups).forEach(([k, g]) => g.style.display = k === state ? '' : 'none');
        locSelect.disabled = false;
    }
    renderArticles();
}

async function renderArticles(append = false) {
    if (!append) articleOffset = 0;

    const state = document.getElementById('filterState').value;
    const loc = document.getElementById('filterLocation').value;
    const sent = document.getElementById('filterSentiment').value;
    let url = `/api/articles?limit=25&offset=${articleOffset}`;
    if (state) url += `&state=${state}`;
    if (loc) url += `&location=${loc}`;
    if (sent) url += `&sentiment_label=${sent}`;

    try {
        const data = await fetchJSON(url);
        articleTotal = data.total;

        const tbody = document.getElementById('articleBody');
        const noData = document.getElementById('articleNoData');
        const loadBtn = document.getElementById('btnLoadMore');

        if (data.articles.length === 0 && !append) {
            tbody.innerHTML = '';
            noData.style.display = 'block';
            loadBtn.style.display = 'none';
            return;
        }
        noData.style.display = 'none';

        const rows = data.articles.map((a, i) => {
            const idx = articleOffset + i;
            articleCache[a.id] = a;
            const titleTrunc = a.title && a.title.length > 70 ? a.title.substring(0, 67) + '...' : (a.title || '--');
            const topics = (a.topic_tags || []).map(t => `<span class="pill pill-topic">${topicDisplay(t)}</span>`).join(' ');
            const entities = (a.entities_mentioned || []).map(e => `<span class="pill pill-entity">${escapeHtml(e)}</span>`).join(' ');
            return `
                <tr style="cursor:pointer" onclick="toggleExpand(${idx})">
                    <td class="text-cell">${escapeHtml(titleTrunc)}</td>
                    <td>${escapeHtml(a.source || '')}</td>
                    <td>${fmtDate(a.published_date)}</td>
                    <td><span class="pill ${sentimentPillClass(a.sentiment_label)}">${sentimentLabel(a.sentiment_label)}</span></td>
                    <td><span class="pill pill-state">${STATE_ABBR[a.state] || (a.state ? a.state.toUpperCase() : '--')}</span></td>
                    <td><span class="pill pill-loc">${capitalize(a.location_relevance || '')}</span></td>
                    <td><span class="pill pill-${a.voice_type}">${(a.voice_type || '').toUpperCase()}</span></td>
                </tr>
                <tr class="expand-row hidden" id="expand-${idx}">
                    <td colspan="7">
                        <div class="article-detail" data-article-id="${a.id}">
                            ${a.url ? `<a href="${a.url}" target="_blank" rel="noopener" class="article-detail-url">${escapeHtml(a.url)}</a>` : ''}
                            <div class="article-detail-grid">
                                <div class="article-detail-section">
                                    <div class="article-detail-label">SENTIMENT</div>
                                    <div class="article-detail-field">
                                        <select class="detail-select" data-field="sentiment_label" onchange="markArticleDirty(this)">
                                            <option value="strongly_positive" ${a.sentiment_label==='strongly_positive'?'selected':''}>V. Positive</option>
                                            <option value="slightly_positive" ${a.sentiment_label==='slightly_positive'?'selected':''}>Positive</option>
                                            <option value="neutral" ${a.sentiment_label==='neutral'?'selected':''}>Neutral</option>
                                            <option value="slightly_negative" ${a.sentiment_label==='slightly_negative'?'selected':''}>Negative</option>
                                            <option value="strongly_negative" ${a.sentiment_label==='strongly_negative'?'selected':''}>V. Negative</option>
                                        </select>
                                        <span class="detail-score">${a.sentiment_score != null ? a.sentiment_score.toFixed(1) : '--'} / 5</span>
                                    </div>
                                </div>
                                <div class="article-detail-section">
                                    <div class="article-detail-label">STATE</div>
                                    <div class="article-detail-field">
                                        <select class="detail-select" data-field="state" onchange="onDetailStateChange(this); markArticleDirty(this)">
                                            <option value="wyoming" ${a.state==='wyoming'?'selected':''}>Wyoming</option>
                                            <option value="texas" ${a.state==='texas'?'selected':''}>Texas</option>
                                            <option value="michigan" ${a.state==='michigan'?'selected':''}>Michigan</option>
                                            <option value="nationwide" ${a.state==='nationwide'?'selected':''}>Nationwide</option>
                                            <option value="other" ${a.state==='other'?'selected':''}>Other</option>
                                        </select>
                                    </div>
                                </div>
                                <div class="article-detail-section">
                                    <div class="article-detail-label">REGION</div>
                                    <div class="article-detail-field">
                                        <select class="detail-select detail-region-select" data-field="location_relevance" onchange="markArticleDirty(this)">
                                            ${regionOptions(a.state, a.location_relevance)}
                                        </select>
                                    </div>
                                </div>
                                <div class="article-detail-section">
                                    <div class="article-detail-label">VOICE</div>
                                    <div class="article-detail-field">
                                        <select class="detail-select" data-field="voice_type" onchange="markArticleDirty(this)">
                                            <option value="elite" ${a.voice_type==='elite'?'selected':''}>Elite</option>
                                            <option value="public" ${a.voice_type==='public'?'selected':''}>Public</option>
                                        </select>
                                    </div>
                                </div>
                                <div class="article-detail-section">
                                    <div class="article-detail-label">SOURCE TYPE</div>
                                    <div class="article-detail-value">${capitalize(a.source_type || 'news')}</div>
                                </div>
                            </div>
                            <div class="article-detail-section">
                                <div class="article-detail-label">TOPICS</div>
                                <div class="article-detail-tags">${topics || '<span class="detail-empty">No topics</span>'}</div>
                            </div>
                            <div class="article-detail-section">
                                <div class="article-detail-label">ENTITIES</div>
                                <div class="article-detail-tags">${entities || '<span class="detail-empty">None detected</span>'}</div>
                            </div>
                            <div class="article-detail-section">
                                <div class="article-detail-label">KEY CLAIMS</div>
                                <div class="article-detail-claims">${a.key_claims ? escapeHtml(a.key_claims) : '<span class="detail-empty">No claims extracted</span>'}</div>
                            </div>
                            ${a.sentiment_justification ? `<div class="article-detail-section"><div class="article-detail-label">SCORING RATIONALE</div><div class="article-detail-claims article-detail-rationale">${escapeHtml(a.sentiment_justification)}</div></div>` : ''}
                            ${a.summary ? `<div class="article-detail-section"><div class="article-detail-label">SUMMARY</div><div class="article-detail-claims">${escapeHtml(a.summary)}</div></div>` : ''}
                            <div class="article-detail-actions">
                                <button class="btn-save-article hidden" onclick="saveArticle(this, ${a.id})">SAVE CHANGES</button>
                                <span class="save-status"></span>
                            </div>
                        </div>
                    </td>
                </tr>
            `;
        }).join('');

        if (append) { tbody.innerHTML += rows; }
        else { tbody.innerHTML = rows; }

        loadBtn.style.display = (articleOffset + data.articles.length < articleTotal) ? 'block' : 'none';

    } catch (err) { console.error('Articles error:', err); }
}

const REGION_OPTIONS = {
    wyoming:  [['statewide','Statewide'],['evanston','Evanston'],['casper','Casper'],['cheyenne','Cheyenne']],
    texas:    [['statewide','TX Statewide'],['dallas','Dallas']],
    michigan: [['statewide','MI Statewide'],['ann_arbor','Ann Arbor'],['van_buren','Van Buren'],['benton_harbor','Benton Harbor']],
    nationwide: [['nationwide','Nationwide']],
    other:   [['other','Other']],
};

function regionOptions(state, selected) {
    const opts = REGION_OPTIONS[state] || REGION_OPTIONS.other;
    return opts.map(([val, label]) =>
        `<option value="${val}" ${val===selected?'selected':''}>${label}</option>`
    ).join('');
}

function onDetailStateChange(stateSelect) {
    const panel = stateSelect.closest('.article-detail');
    const regionSelect = panel.querySelector('.detail-region-select');
    const newState = stateSelect.value;
    regionSelect.innerHTML = regionOptions(newState, '');
    regionSelect.disabled = newState === 'nationwide';
}

function toggleExpand(idx) {
    const row = document.getElementById(`expand-${idx}`);
    if (row) row.classList.toggle('hidden');
}

function markArticleDirty(el) {
    const panel = el.closest('.article-detail');
    const btn = panel.querySelector('.btn-save-article');
    btn.classList.remove('hidden');
    panel.querySelector('.save-status').textContent = '';
}

async function saveArticle(btn, articleId) {
    const panel = btn.closest('.article-detail');
    const selects = panel.querySelectorAll('.detail-select');
    const payload = {};
    selects.forEach(sel => { payload[sel.dataset.field] = sel.value; });

    // Map label back to score
    const scoreMap = { strongly_positive: 5, slightly_positive: 4, neutral: 3, slightly_negative: 2, strongly_negative: 1 };
    if (payload.sentiment_label) payload.sentiment_score = scoreMap[payload.sentiment_label] || 3;

    btn.disabled = true;
    btn.textContent = 'SAVING...';
    const status = panel.querySelector('.save-status');
    try {
        const resp = await fetch(`/api/articles/${articleId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (resp.ok) {
            status.textContent = 'Saved';
            status.style.color = 'var(--sent-5)';
            btn.classList.add('hidden');
            // Update the parent row's pills
            const expandRow = panel.closest('.expand-row');
            const dataRow = expandRow.previousElementSibling;
            if (dataRow) {
                const cells = dataRow.querySelectorAll('td');
                cells[3].innerHTML = `<span class="pill ${sentimentPillClass(payload.sentiment_label)}">${sentimentLabel(payload.sentiment_label)}</span>`;
                cells[4].innerHTML = `<span class="pill pill-state">${STATE_ABBR[payload.state] || (payload.state || '--').toUpperCase()}</span>`;
                cells[5].innerHTML = `<span class="pill pill-loc">${capitalize(payload.location_relevance || '')}</span>`;
                cells[6].innerHTML = `<span class="pill pill-${payload.voice_type}">${(payload.voice_type || '').toUpperCase()}</span>`;
            }
        } else {
            const err = await resp.json();
            status.textContent = err.error || 'Error';
            status.style.color = 'var(--sent-1)';
        }
    } catch (e) {
        status.textContent = 'Network error';
        status.style.color = 'var(--sent-1)';
    }
    btn.disabled = false;
    btn.textContent = 'SAVE CHANGES';
}

function loadMoreArticles() {
    articleOffset += 25;
    renderArticles(true);
}

// ══════════════════════════════════════════════
//  PAGE 4: SYSTEM
// ══════════════════════════════════════════════
async function renderSystem() {
    try {
        const [feedData, digestData, overview] = await Promise.all([
            fetchJSON('/api/feed-health'),
            fetchJSON('/api/digests'),
            fetchJSON('/api/overview'),
        ]);

        // Feed health
        const feedTbody = document.querySelector('#feedTable tbody');
        const feedNoData = document.getElementById('feedNoData');
        if (!feedData.feeds || feedData.feeds.length === 0) {
            feedTbody.innerHTML = '';
            feedNoData.style.display = 'block';
        } else {
            feedNoData.style.display = 'none';
            feedTbody.innerHTML = feedData.feeds.map(f => {
                let sc = 'never', st = 'N/A';
                if (f.status === 'success') { sc = 'ok'; st = 'OK'; }
                else if (f.status === 'error') { sc = 'error'; st = 'ERR'; }
                return `<tr>
                    <td class="text-cell">${escapeHtml(f.name)}</td>
                    <td>${fmtDateTime(f.last_run)}</td>
                    <td><span class="status-dot ${sc}"></span>${st}</td>
                    <td>${f.articles_found}</td>
                    <td>${f.articles_matched}</td>
                </tr>`;
            }).join('');
        }

        // Digests
        const digestTbody = document.querySelector('#digestTable tbody');
        const digestNoData = document.getElementById('digestNoData');
        if (!digestData.digests || digestData.digests.length === 0) {
            digestTbody.innerHTML = '';
            digestNoData.style.display = 'block';
        } else {
            digestNoData.style.display = 'none';
            digestTbody.innerHTML = digestData.digests.map(d => `<tr>
                <td>${fmtDate(d.period_start)} – ${fmtDate(d.period_end)}</td>
                <td>${d.article_count}</td>
                <td style="color:${sentimentColor(d.avg_sentiment)}">${d.avg_sentiment !== null ? d.avg_sentiment.toFixed(2) : '--'}</td>
                <td>${fmtDate(d.generated_date)}</td>
                <td><button class="btn-view" onclick="viewDigest(${d.id})">VIEW</button></td>
            </tr>`).join('');
        }

        // DB stats
        document.getElementById('dbStats').innerHTML = `
            <div class="kv-item"><span class="kv-label">Articles</span><span class="kv-val">${overview.total_articles}</span></div>
            <div class="kv-item"><span class="kv-label">Analyzed</span><span class="kv-val">${overview.analyzed_articles || 0}</span></div>
            <div class="kv-item"><span class="kv-label">Digests</span><span class="kv-val">${digestData.digests ? digestData.digests.length : 0}</span></div>
            <div class="kv-item"><span class="kv-label">Feeds</span><span class="kv-val">${feedData.feeds ? feedData.feeds.length : 0}</span></div>
        `;

    } catch (err) { console.error('System error:', err); }
}

// ── Modal ──
async function viewDigest(id) {
    try {
        const data = await fetchJSON(`/api/digest/${id}`);
        document.getElementById('digestModalContent').textContent = data.content || 'Empty';
        document.getElementById('digestModal').classList.add('show');
    } catch (err) { console.error('Digest error:', err); }
}

function closeModal(e) { if (e.target === e.currentTarget) e.currentTarget.classList.remove('show'); }

// ── Export ──
function exportReport() { window.location.href = '/api/export'; }

// ══════════════════════════════════════════════
//  PAGE 5: CONTROL
// ══════════════════════════════════════════════
let _pollTimers = {};

let _currentBatchId = null;

async function renderPendingQueue() {
    const list = document.getElementById('pendingQueueList');
    const empty = document.getElementById('pendingQueueEmpty');
    const panel = document.getElementById('batchReviewPanel');
    try {
        const data = await fetchJSON('/api/control/pending-batches');
        const batches = data.batches || [];

        // Clear existing batch rows (keep the empty message and review panel)
        Array.from(list.children).forEach(el => {
            if (el.id !== 'pendingQueueEmpty' && el.id !== 'batchReviewPanel') el.remove();
        });

        if (batches.length === 0) {
            empty.style.display = 'block';
            panel.style.display = 'none';
            _currentBatchId = null;
            return;
        }
        empty.style.display = 'none';

        batches.forEach(b => {
            const scoreText = b.avg_relevance !== null ? `avg score ${b.avg_relevance}` : 'unscored';
            const date = b.created_date ? fmtDateTime(b.created_date) : '--';
            const isActive = _currentBatchId === b.search_id;
            const row = document.createElement('div');
            row.className = 'batch-row' + (isActive ? ' batch-row-active' : '');
            row.id = `batch-row-${b.search_id}`;
            row.innerHTML = `
                <div class="batch-meta">
                    <span class="batch-id">${b.search_id}</span>
                    <span class="batch-date">${date}</span>
                    <span class="batch-count">${b.count} article${b.count !== 1 ? 's' : ''}</span>
                    <span class="batch-score">${scoreText}</span>
                </div>
                <div class="batch-actions">
                    <button class="btn-batch-review" onclick="openBatchReview('${b.search_id}', ${b.count})">
                        ${isActive ? 'Hide' : 'Review'}
                    </button>
                    <button class="btn-batch-discard" onclick="discardBatch('${b.search_id}', this)">Discard</button>
                </div>
            `;
            list.insertBefore(row, empty);
        });

        // Re-open active batch if still present
        if (_currentBatchId && batches.find(b => b.search_id === _currentBatchId)) {
            await loadBatchArticles(_currentBatchId);
        } else {
            panel.style.display = 'none';
            _currentBatchId = null;
        }
    } catch (err) { console.error('Pending queue error:', err); }
}

async function openBatchReview(searchId, count) {
    const panel = document.getElementById('batchReviewPanel');
    if (_currentBatchId === searchId && panel.style.display !== 'none') {
        panel.style.display = 'none';
        _currentBatchId = null;
        renderPendingQueue();
        return;
    }
    _currentBatchId = searchId;
    document.getElementById('batchReviewId').textContent = searchId;
    document.getElementById('batchReviewCount').textContent = count;
    document.getElementById('batchReviewStatus').innerHTML = '';
    await loadBatchArticles(searchId);
    renderPendingQueue();
}

async function loadBatchArticles(searchId) {
    const panel = document.getElementById('batchReviewPanel');
    const tbody = document.getElementById('batchReviewBody');
    try {
        const data = await fetchJSON(`/api/control/pending?search_id=${searchId}`);
        if (!data.articles || data.articles.length === 0) {
            panel.style.display = 'none';
            return;
        }
        panel.style.display = 'block';
        // Position panel after the batch row
        const batchRow = document.getElementById(`batch-row-${searchId}`);
        if (batchRow) batchRow.after(panel);

        document.getElementById('batchSelectAll').checked = true;
        tbody.innerHTML = data.articles.map(a => {
            const hasScore = a.relevance_score !== null && a.relevance_score !== undefined;
            const scoreCls = !hasScore ? 'rel-none' : a.relevance_score >= 8 ? 'rel-high' : a.relevance_score >= 5 ? 'rel-mid' : 'rel-low';
            const scoreText = hasScore ? a.relevance_score : '—';
            const titleTrunc = a.title && a.title.length > 60 ? a.title.substring(0, 57) + '...' : (a.title || '--');
            const reason = a.relevance_reason || (hasScore ? '' : 'Not scored');
            const reasonTrunc = reason.length > 50 ? reason.substring(0, 47) + '...' : reason;
            const summary = a.summary ? escapeHtml(a.summary) : '<span style="color:var(--text-muted)">No summary</span>';
            return `<tr style="cursor:pointer" onclick="togglePendingExpand(${a.id}, event)">
                <td><input type="checkbox" class="batch-check" data-id="${a.id}" checked onchange="updateBatchSelectedCount()"></td>
                <td><span class="relevance-score ${scoreCls}">${scoreText}</span></td>
                <td class="text-cell" title="${escapeHtml(a.title)}"><a href="${escapeHtml(a.url || '#')}" target="_blank" rel="noopener" class="article-link">${escapeHtml(titleTrunc)}</a></td>
                <td>${escapeHtml(a.source || '')}</td>
                <td>${fmtDate(a.published_date)}</td>
                <td class="text-cell" title="${escapeHtml(reason)}">${escapeHtml(reasonTrunc)}</td>
            </tr>
            <tr class="expand-row hidden" id="pending-expand-${a.id}">
                <td colspan="6" class="pending-detail">
                    <div class="pending-detail-reason"><strong>Relevance:</strong> ${escapeHtml(reason)}</div>
                    <div class="pending-detail-summary">${summary}</div>
                    ${a.url ? `<a href="${escapeHtml(a.url)}" target="_blank" rel="noopener" class="article-link" style="font-size:10px;word-break:break-all">${escapeHtml(a.url)}</a>` : ''}
                </td>
            </tr>`;
        }).join('');
        updateBatchSelectedCount();
    } catch (err) { console.error('Load batch error:', err); }
}

function toggleAllBatch(checked) {
    document.querySelectorAll('.batch-check').forEach(cb => { cb.checked = checked; });
    updateBatchSelectedCount();
}

function updateBatchSelectedCount() {
    const checked = document.querySelectorAll('.batch-check:checked').length;
    document.getElementById('batchReviewSelected').textContent = checked;
}

async function approveBatchSelected() {
    const ids = Array.from(document.querySelectorAll('.batch-check:checked')).map(cb => parseInt(cb.dataset.id));
    const unchecked = Array.from(document.querySelectorAll('.batch-check:not(:checked)')).map(cb => parseInt(cb.dataset.id));
    if (ids.length === 0) return;
    const status = document.getElementById('batchReviewStatus');
    try {
        const resp = await fetch('/api/control/approve', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ article_ids: ids }),
        });
        const data = await resp.json();
        if (data.success) {
            if (unchecked.length > 0) {
                await fetch('/api/control/reject', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ article_ids: unchecked }),
                });
            }
            status.innerHTML = `<span class="task-complete">Approved ${data.approved} article${data.approved !== 1 ? 's' : ''}</span>`;
            _currentBatchId = null;
            await renderPendingQueue();
        }
    } catch (err) { console.error('Approve batch error:', err); }
}

async function rejectBatchUnselected() {
    const ids = Array.from(document.querySelectorAll('.batch-check:not(:checked)')).map(cb => parseInt(cb.dataset.id));
    if (ids.length === 0) return;
    const status = document.getElementById('batchReviewStatus');
    try {
        await fetch('/api/control/reject', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ article_ids: ids }),
        });
        status.innerHTML = `<span class="task-complete">Rejected ${ids.length} article${ids.length !== 1 ? 's' : ''}</span>`;
        _currentBatchId = null;
        await renderPendingQueue();
    } catch (err) { console.error('Reject batch error:', err); }
}

async function discardCurrentBatch() {
    if (!_currentBatchId) return;
    await discardBatch(_currentBatchId, null);
}

async function discardBatch(searchId, btn) {
    if (btn) { btn.disabled = true; btn.textContent = '...'; }
    const status = document.getElementById('batchReviewStatus');
    try {
        const resp = await fetch('/api/control/discard-batch', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ search_id: searchId }),
        });
        const data = await resp.json();
        if (data.success) {
            if (status) status.innerHTML = `<span class="task-complete">Discarded ${data.discarded} article${data.discarded !== 1 ? 's' : ''}</span>`;
            if (_currentBatchId === searchId) _currentBatchId = null;
            await renderPendingQueue();
        }
    } catch (err) { console.error('Discard error:', err); }
}

async function renderControl() {
    // Set default date to today
    const dateEl = document.getElementById('ctrlDate');
    if (dateEl && !dateEl.value) {
        dateEl.value = new Date().toISOString().split('T')[0];
    }

    await renderPendingQueue();

    // Load recent tasks
    try {
        const data = await fetchJSON('/api/control/tasks');
        const tbody = document.getElementById('taskBody');
        const noData = document.getElementById('taskNoData');

        if (!data.tasks || data.tasks.length === 0) {
            tbody.innerHTML = '';
            noData.style.display = 'block';
            return;
        }
        noData.style.display = 'none';

        tbody.innerHTML = data.tasks.map(t => {
            const statusCls = t.status === 'completed' ? 'task-complete' :
                              t.status === 'error' ? 'task-error' : 'task-running';
            const statusText = t.status === 'completed' ? 'OK' :
                               t.status === 'error' ? 'ERR' : 'RUN';
            let dur = '--';
            if (t.started && t.finished) {
                const ms = new Date(t.finished) - new Date(t.started);
                dur = ms < 1000 ? ms + 'ms' : (ms / 1000).toFixed(1) + 's';
            } else if (t.status === 'running') {
                dur = 'running...';
            }
            let resultText = '--';
            if (t.error) resultText = t.error.substring(0, 80);
            else if (t.result) resultText = formatTaskResult(t.type, t.result);
            return `<tr>
                <td><span class="pill pill-loc">${t.type.toUpperCase()}</span></td>
                <td><span class="${statusCls}">${statusText}</span></td>
                <td>${fmtDateTime(t.started)}</td>
                <td>${dur}</td>
                <td class="text-cell">${escapeHtml(resultText)}</td>
            </tr>`;
        }).join('');
    } catch (err) { console.error('Control tasks error:', err); }
}

function formatTaskResult(type, result) {
    if (!result) return '--';
    if (typeof result === 'string') return result;
    switch (type) {
        case 'ingest':
            return `Feeds: ${result.feeds_checked || 0} | Entries: ${result.total_entries || 0} | Matches: ${result.keyword_matches || 0} | New: ${result.new_articles || 0}`;
        case 'websearch':
            return `Found: ${result.total_results || 0} | New: ${result.new_articles || 0} | URL dupes: ${result.skipped_url || 0} | Title dupes: ${result.skipped_title || 0}`;
        case 'analysis':
            return `Analyzed: ${result.analyzed || 0} | Errors: ${result.errors || 0}`;
        case 'digest':
            return result.filename || result.message || 'Done';
        default:
            return JSON.stringify(result).substring(0, 80);
    }
}

async function submitArticle(e) {
    e.preventDefault();
    const btn = document.getElementById('btnSubmitArticle');
    const status = document.getElementById('statusArticle');
    const title = document.getElementById('ctrlTitle').value.trim();

    if (!title) {
        status.innerHTML = '<span class="task-error">Title is required</span>';
        return;
    }

    btn.disabled = true;
    btn.classList.add('running');
    status.innerHTML = '<span class="task-running">Submitting...</span>';

    try {
        const payload = {
            source: document.getElementById('ctrlSource').value.trim() || 'Manual Entry',
            source_type: document.getElementById('ctrlSourceType').value,
            title: title,
            url: document.getElementById('ctrlUrl').value.trim(),
            published_date: document.getElementById('ctrlDate').value,
            full_text: document.getElementById('ctrlText').value,
        };

        const resp = await fetch('/api/control/add-article', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();

        if (data.success) {
            status.innerHTML = `<span class="task-complete">Article added (ID: ${data.article_id})</span>`;
            document.getElementById('articleForm').reset();
            document.getElementById('ctrlDate').value = new Date().toISOString().split('T')[0];
        } else {
            status.innerHTML = `<span class="task-error">${escapeHtml(data.error || 'Failed')}</span>`;
        }
    } catch (err) {
        status.innerHTML = `<span class="task-error">Error: ${escapeHtml(err.message)}</span>`;
    } finally {
        btn.disabled = false;
        btn.classList.remove('running');
    }
}

const TASK_ENDPOINTS = {
    ingest: '/api/control/run-ingest',
    analysis: '/api/control/run-analysis',
    digest: '/api/control/run-digest',
};

const TASK_BTN_MAP = {
    ingest: 'btnIngest',
    analysis: 'btnAnalysis',
    digest: 'btnDigest',
};

async function runTask(type) {
    const btnId = TASK_BTN_MAP[type];
    const statusId = 'status' + capitalize(type);
    const btn = document.getElementById(btnId);
    const status = document.getElementById(statusId);

    btn.disabled = true;
    btn.classList.add('running');
    status.innerHTML = '<span class="task-running">Starting...</span>';

    try {
        const resp = await fetch(TASK_ENDPOINTS[type], { method: 'POST' });
        const data = await resp.json();

        if (data.task_id) {
            pollTask(data.task_id, type, btnId, statusId);
        } else {
            status.innerHTML = '<span class="task-error">Failed to start</span>';
            btn.disabled = false;
            btn.classList.remove('running');
        }
    } catch (err) {
        status.innerHTML = `<span class="task-error">Error: ${escapeHtml(err.message)}</span>`;
        btn.disabled = false;
        btn.classList.remove('running');
    }
}

function pollTask(taskId, type, btnId, statusId) {
    const btn = document.getElementById(btnId);
    const status = document.getElementById(statusId);
    let elapsed = 0;

    // Clear any existing poll for this type
    if (_pollTimers[type]) clearInterval(_pollTimers[type]);

    _pollTimers[type] = setInterval(async () => {
        elapsed += 2;
        try {
            const data = await fetchJSON(`/api/control/task/${taskId}`);

            if (data.status === 'running') {
                status.innerHTML = `<span class="task-running">Running... (${elapsed}s)</span>`;
            } else if (data.status === 'completed') {
                clearInterval(_pollTimers[type]);
                _pollTimers[type] = null;
                const resultText = formatTaskResult(type, data.result);
                status.innerHTML = `<span class="task-complete">${escapeHtml(resultText)}</span>`;
                btn.disabled = false;
                btn.classList.remove('running');
                renderControl(); // Refresh task list
            } else if (data.status === 'error') {
                clearInterval(_pollTimers[type]);
                _pollTimers[type] = null;
                status.innerHTML = `<span class="task-error">Error: ${escapeHtml(data.error || 'Unknown')}</span>`;
                btn.disabled = false;
                btn.classList.remove('running');
            }
        } catch (err) {
            clearInterval(_pollTimers[type]);
            _pollTimers[type] = null;
            status.innerHTML = `<span class="task-error">Poll error: ${escapeHtml(err.message)}</span>`;
            btn.disabled = false;
            btn.classList.remove('running');
        }
    }, 2000);
}

// ══════════════════════════════════════════════
//  WEB SEARCH + REVIEW PIPELINE
// ══════════════════════════════════════════════
let _currentSearchId = null;

async function runWebSearch() {
    const btn = document.getElementById('btnWebsearch');
    const status = document.getElementById('statusWebsearch');
    const query = document.getElementById('ctrlSearchQuery').value.trim();
    const daysBack = document.getElementById('ctrlSearchDays').value;

    btn.disabled = true;
    btn.classList.add('running');
    status.innerHTML = '<span class="task-running">Searching...</span>';
    document.getElementById('reviewSection').style.display = 'none';

    try {
        const resp = await fetch('/api/control/run-websearch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: query || null, days_back: parseInt(daysBack) }),
        });
        const data = await resp.json();

        if (data.task_id) {
            pollWebSearch(data.task_id);
        } else {
            status.innerHTML = '<span class="task-error">Failed to start search</span>';
            btn.disabled = false;
            btn.classList.remove('running');
        }
    } catch (err) {
        status.innerHTML = `<span class="task-error">Error: ${escapeHtml(err.message)}</span>`;
        btn.disabled = false;
        btn.classList.remove('running');
    }
}

function pollWebSearch(taskId) {
    const btn = document.getElementById('btnWebsearch');
    const status = document.getElementById('statusWebsearch');
    let elapsed = 0;

    if (_pollTimers.websearch) clearInterval(_pollTimers.websearch);

    _pollTimers.websearch = setInterval(async () => {
        elapsed += 2;
        try {
            const data = await fetchJSON(`/api/control/task/${taskId}`);

            if (data.status === 'running') {
                status.innerHTML = `<span class="task-running">Searching & scoring... (${elapsed}s)</span>`;
            } else if (data.status === 'completed') {
                clearInterval(_pollTimers.websearch);
                _pollTimers.websearch = null;
                btn.disabled = false;
                btn.classList.remove('running');

                const r = data.result || {};
                let msg = `Found ${r.total_results || 0} results | ${r.new_articles || 0} new`;
                if (r.skipped_url > 0 || r.skipped_title > 0) {
                    msg += ` | ${(r.skipped_url || 0) + (r.skipped_title || 0)} duplicates filtered`;
                }
                if (r.api_key_set === false && r.new_articles > 0) {
                    msg += `</span><br><span class="task-warning">ANTHROPIC_API_KEY not set — relevance scoring skipped. Set the key and restart to enable.`;
                }
                status.innerHTML = `<span class="task-complete">${msg}</span>`;

                _currentSearchId = r.search_id;
                if (r.new_articles > 0) {
                    loadPendingArticles(r.search_id);
                }
                renderControl(); // Refresh task list
            } else if (data.status === 'error') {
                clearInterval(_pollTimers.websearch);
                _pollTimers.websearch = null;
                status.innerHTML = `<span class="task-error">Error: ${escapeHtml(data.error || 'Unknown')}</span>`;
                btn.disabled = false;
                btn.classList.remove('running');
            }
        } catch (err) {
            clearInterval(_pollTimers.websearch);
            _pollTimers.websearch = null;
            status.innerHTML = `<span class="task-error">Poll error: ${escapeHtml(err.message)}</span>`;
            btn.disabled = false;
            btn.classList.remove('running');
        }
    }, 2000);
}

async function loadPendingArticles(searchId) {
    const section = document.getElementById('reviewSection');
    const tbody = document.getElementById('reviewBody');

    try {
        let url = '/api/control/pending';
        if (searchId) url += `?search_id=${searchId}`;
        const data = await fetchJSON(url);

        if (!data.articles || data.articles.length === 0) {
            section.style.display = 'none';
            return;
        }

        section.style.display = 'block';
        document.getElementById('reviewCount').textContent = data.articles.length;
        document.getElementById('selectAll').checked = true;

        tbody.innerHTML = data.articles.map(a => {
            const hasScore = a.relevance_score !== null && a.relevance_score !== undefined;
            const scoreCls = !hasScore ? 'rel-none' :
                             a.relevance_score >= 8 ? 'rel-high' :
                             a.relevance_score >= 5 ? 'rel-mid' : 'rel-low';
            const scoreText = hasScore ? a.relevance_score : '—';
            const titleTrunc = a.title && a.title.length > 60 ? a.title.substring(0, 57) + '...' : (a.title || '--');
            const reason = a.relevance_reason || (hasScore ? '' : 'Not scored');
            const reasonTrunc = reason.length > 50 ? reason.substring(0, 47) + '...' : reason;
            const summary = a.summary ? escapeHtml(a.summary) : '<span style="color:var(--text-muted)">No summary available</span>';
            return `<tr data-id="${a.id}" style="cursor:pointer" onclick="togglePendingExpand(${a.id}, event)">
                <td><input type="checkbox" class="pending-check" data-id="${a.id}" checked onchange="updateSelectedCount()"></td>
                <td><span class="relevance-score ${scoreCls}">${scoreText}</span></td>
                <td class="text-cell" title="${escapeHtml(a.title)}"><a href="${escapeHtml(a.url || '#')}" target="_blank" rel="noopener" class="article-link">${escapeHtml(titleTrunc)}</a></td>
                <td>${escapeHtml(a.source || '')}</td>
                <td>${fmtDate(a.published_date)}</td>
                <td class="text-cell" title="${escapeHtml(reason)}">${escapeHtml(reasonTrunc)}</td>
            </tr>
            <tr class="expand-row hidden" id="pending-expand-${a.id}">
                <td colspan="6" class="pending-detail">
                    <div class="pending-detail-reason"><strong>Relevance:</strong> ${escapeHtml(reason)}</div>
                    <div class="pending-detail-summary">${summary}</div>
                    ${a.url ? `<a href="${escapeHtml(a.url)}" target="_blank" rel="noopener" class="article-link" style="font-size:10px;word-break:break-all">${escapeHtml(a.url)}</a>` : ''}
                </td>
            </tr>`;
        }).join('');

        updateSelectedCount();
    } catch (err) {
        console.error('Load pending error:', err);
    }
}

function togglePendingExpand(id, event) {
    // Don't toggle when clicking checkbox or link
    if (event.target.closest('input, a')) return;
    const row = document.getElementById(`pending-expand-${id}`);
    if (row) row.classList.toggle('hidden');
}

function toggleAllPending(checked) {
    document.querySelectorAll('.pending-check').forEach(cb => { cb.checked = checked; });
    updateSelectedCount();
}

function updateSelectedCount() {
    const checked = document.querySelectorAll('.pending-check:checked').length;
    document.getElementById('reviewSelected').textContent = checked;
}

function _getCheckedIds() {
    return Array.from(document.querySelectorAll('.pending-check:checked')).map(cb => parseInt(cb.dataset.id));
}

function _getUncheckedIds() {
    return Array.from(document.querySelectorAll('.pending-check:not(:checked)')).map(cb => parseInt(cb.dataset.id));
}

async function approveSelected() {
    const ids = _getCheckedIds();
    if (ids.length === 0) return;

    try {
        const resp = await fetch('/api/control/approve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ article_ids: ids }),
        });
        const data = await resp.json();

        if (data.success) {
            const statusEl = document.getElementById('statusWebsearch');
            statusEl.innerHTML = `<span class="task-complete">Approved ${data.approved} articles into database</span>`;

            // Reject any unchecked ones
            const unchecked = _getUncheckedIds();
            if (unchecked.length > 0) {
                await fetch('/api/control/reject', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ article_ids: unchecked }),
                });
            }

            document.getElementById('reviewSection').style.display = 'none';
            renderControl();
        }
    } catch (err) {
        console.error('Approve error:', err);
    }
}

async function rejectSelected() {
    const unchecked = _getUncheckedIds();
    if (unchecked.length === 0) return;

    try {
        await fetch('/api/control/reject', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ article_ids: unchecked }),
        });

        // Reload to show only remaining
        if (_currentSearchId) {
            loadPendingArticles(_currentSearchId);
        }
    } catch (err) {
        console.error('Reject error:', err);
    }
}

// ── Init ──
document.addEventListener('DOMContentLoaded', () => { renderDashboard(); });
