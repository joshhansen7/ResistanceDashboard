/* Prometheus Resistance Dashboard — Bloomberg Terminal Frontend */
/* D3.js map, Chart.js v4, dense data rendering */

// ── State ──
let charts = {};
let currentPage = 'dashboard';
let articleOffset = 0;
let articleTotal = 0;
let articleCache = {};  // id -> article data for detail panel
let mapView = 'none';        // 'none' | 'us' | 'county' | 'single-county'
let mapActiveState = null;    // 'wyoming' | 'texas' | null
let mapActiveCounty = null;   // county FIPS string | null
let mapTopoData = null;       // cached TopoJSON
let mapStateSentiment = {};   // cached state-level sentiment
let mapNavStack = [];         // navigation breadcrumb for back button
let _statesCache = null;      // cached /api/states response
let _stateFipsCache = null;   // cached /api/state-fips response

// ── Dynamic state loading ──
async function getStates() {
    if (!_statesCache) _statesCache = (await fetchJSON('/api/states')).states;
    return _statesCache;
}

const _STATE_ABBR = {"alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA","colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA","hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS","kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD","massachusetts":"MA","michigan":"MI","minnesota":"MN","mississippi":"MS","missouri":"MO","montana":"MT","nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ","new mexico":"NM","new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH","oklahoma":"OK","oregon":"OR","pennsylvania":"PA","rhode island":"RI","south carolina":"SC","south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT","virginia":"VA","washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY","district of columbia":"DC","nationwide":"US","international":"INTL"};

function getStateAbbr(stateKey) {
    if (!stateKey) return '--';
    return _STATE_ABBR[stateKey.toLowerCase()] || stateKey.toUpperCase().substring(0, 2);
}

function getStateName(stateKey) {
    if (!stateKey) return '';
    if (_statesCache) {
        const s = _statesCache.find(s => s.key === stateKey);
        if (s) return s.name;
    }
    return stateKey.charAt(0).toUpperCase() + stateKey.slice(1);
}

function getStateFips(stateKey) {
    if (_statesCache) {
        const s = _statesCache.find(s => s.key === stateKey);
        if (s) return s.fips;
    }
    return null;
}

function buildStateOptions(selected) {
    // Build <option> list for all states known to the dashboard.
    // Uses the cached states from /api/states (loaded at init), plus fixed entries.
    let html = '';
    if (_statesCache) {
        _statesCache.forEach(s => {
            html += `<option value="${s.key}" ${s.key === selected ? 'selected' : ''}>${s.name}</option>`;
        });
    }
    // Ensure the current value is always present (even if not in cache yet)
    if (selected && selected !== 'nationwide' && selected !== 'international' && selected !== 'other'
        && _statesCache && !_statesCache.find(s => s.key === selected)) {
        html += `<option value="${selected}" selected>${capitalize(selected)}</option>`;
    }
    html += `<option value="international" ${selected === 'international' ? 'selected' : ''}>International</option>`;
    html += `<option value="nationwide" ${selected === 'nationwide' ? 'selected' : ''}>Nationwide</option>`;
    html += `<option value="other" ${selected === 'other' ? 'selected' : ''}>Other</option>`;
    return html;
}

async function populateStateDropdown(selectId, includeAll = true, includeNationwide = false, includeInternational = false) {
    const states = await getStates();
    const sel = document.getElementById(selectId);
    if (!sel) return;
    const val = sel.value; // Preserve current selection
    sel.innerHTML = includeAll ? '<option value="">All States</option>' : '';
    states.forEach(s => {
        sel.innerHTML += `<option value="${s.key}">${s.name}</option>`;
    });
    if (includeNationwide) {
        sel.innerHTML += '<option value="nationwide">Nationwide</option>';
    }
    if (includeInternational) {
        sel.innerHTML += '<option value="international">International</option>';
    }
    if (val) sel.value = val; // Restore selection
}

async function initStateDropdowns() {
    await Promise.all([
        populateStateDropdown('dashStateFilter'),
        populateStateDropdown('sentStateFilter'),
        populateStateDropdown('filterState', true, true, true),
        populateStateDropdown('ctrlSearchState'),
        populateStateDropdown('ctrlSweepState'),
    ]);
}

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

function lowConfidenceBadgeHTML(article) {
    if (!article || !article.is_low_confidence) return '';
    return '<span class="pill pill-low-confidence" title="Thin Google News wrapper article. Visible in raw views, excluded from summary metrics until upgraded.">Low confidence</span> ';
}

function internationalBadgeHTML(article) {
    if (!article || !article.is_international) return '';
    return '<span class="pill pill-international" title="International article. Visible in raw views, excluded from US summary metrics and maps.">International</span> ';
}

function parseIsoDate(iso) {
    if (!iso) return null;
    const value = String(iso);
    const needsUtcSuffix = value.includes('T') && !/(Z|[+-]\d{2}:\d{2})$/i.test(value);
    return new Date(needsUtcSuffix ? `${value}Z` : value);
}

function fmtDate(iso) {
    if (!iso) return '--';
    const d = parseIsoDate(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function fmtDateTime(iso) {
    if (!iso) return '--';
    const d = parseIsoDate(iso);
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

function safeHttpUrl(url) {
    if (!url) return '';
    const trimmed = String(url).trim();
    if (!/^https?:\/\//i.test(trimmed)) return '';
    return escapeHtml(trimmed);
}

function articleUrl(url, table, resolvedUrl) {
    const safeResolved = safeHttpUrl(resolvedUrl);
    if (safeResolved) return safeResolved;
    if (!url) return '#';
    if (url.includes('news.google.com')) {
        return `/api/resolve-url?url=${encodeURIComponent(url)}&table=${table || 'articles'}`;
    }
    return safeHttpUrl(url) || '#';
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
    const m = Math.floor((Date.now() - parseIsoDate(iso).getTime()) / 60000);
    if (m < 1) return 'just now';
    if (m < 60) return m + 'm ago';
    const h = Math.floor(m / 60);
    if (h < 24) return h + 'h ago';
    return Math.floor(h / 24) + 'd ago';
}

function truncate(str, max) {
    if (!str) return '';
    return str.length > max ? str.slice(0, max) + '...' : str;
}

function elapsedSecondsFromIso(started) {
    if (!started) return null;
    const ms = Date.now() - parseIsoDate(started).getTime();
    return Math.max(0, Math.floor(ms / 1000));
}

function formatElapsedSeconds(totalSeconds) {
    if (totalSeconds === null || totalSeconds === undefined) return '--';
    const mins = Math.floor(totalSeconds / 60);
    const secs = totalSeconds % 60;
    if (mins >= 60) {
        const hours = Math.floor(mins / 60);
        const remMins = mins % 60;
        return `${hours}h ${remMins}m`;
    }
    return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
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

function stateNameToKey(name) {
    // Convert "Wyoming" -> "wyoming", handles all state names
    return name ? name.toLowerCase().trim() : null;
}

// ── Tooltip positioning helper ──
// Track timeout so we can cancel stale "No articles" hides
let _tooltipHideTimer = null;

function positionTooltip(event) {
    const tooltip = document.getElementById('mapTooltip');
    // Position relative to the tooltip's offset parent (.map-panel),
    // NOT the wyomingMap container, to account for the map header.
    const parent = tooltip.offsetParent || tooltip.parentElement;
    const rect = parent.getBoundingClientRect();
    let left = event.clientX - rect.left + 10;
    let top = event.clientY - rect.top - 10;
    // Bounds-check: keep tooltip inside the panel
    const tw = tooltip.offsetWidth;
    const th = tooltip.offsetHeight;
    if (left + tw > rect.width - 4) left = event.clientX - rect.left - tw - 10;
    if (top + th > rect.height - 4) top = event.clientY - rect.top - th - 10;
    if (top < 0) top = 4;
    tooltip.style.left = left + 'px';
    tooltip.style.top = top + 'px';
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
            .attr('viewBox', `0 0 ${width} ${height}`)
            .attr('preserveAspectRatio', 'xMidYMid meet')
            .attr('role', 'img')
            .attr('aria-label', 'United States sentiment map. Click a state to drill down.');

        svg.selectAll('.state-path')
            .data(states.features)
            .enter()
            .append('path')
            .attr('class', 'state-path')
            .attr('d', path)
            .attr('data-state-name', d => d.properties.name)
            .attr('fill', d => {
                const key = stateNameToKey(d.properties.name);
                if (key && stateSentiment[key] && stateSentiment[key].avg !== null) {
                    return sentimentColor(stateSentiment[key].avg);
                }
                return '#0f1520';
            })
            .attr('stroke', d => {
                const key = stateNameToKey(d.properties.name);
                return (key && stateSentiment[key]) ? '#14b8a6' : 'rgba(148,163,184,0.2)';
            })
            .attr('stroke-width', d => {
                const key = stateNameToKey(d.properties.name);
                return (key && stateSentiment[key]) ? 0.8 : 0.3;
            })
            .attr('cursor', 'pointer')
            .on('mouseenter', function(event, d) {
                if (_tooltipHideTimer) { clearTimeout(_tooltipHideTimer); _tooltipHideTimer = null; }
                const tooltip = document.getElementById('mapTooltip');
                const key = stateNameToKey(d.properties.name);
                if (key && stateSentiment[key] && stateSentiment[key].avg !== null) {
                    const s = stateSentiment[key];
                    tooltip.textContent = `${d.properties.name} | avg: ${s.avg.toFixed(1)} | n=${s.count}`;
                } else {
                    tooltip.textContent = d.properties.name;
                }
                positionTooltip(event);
                tooltip.classList.add('visible');
            })
            .on('mousemove', function(event) { positionTooltip(event); })
            .on('mouseleave', function() {
                document.getElementById('mapTooltip').classList.remove('visible');
            })
            .on('click', async function(event, d) {
                const key = stateNameToKey(d.properties.name);
                if (key && STATE_CONFIG[key]) {
                    drillIntoState(key);
                } else if (key && stateSentiment[key]) {
                    // Generic drill-down for any state with data
                    if (!_stateFipsCache) _stateFipsCache = await fetchJSON('/api/state-fips');
                    const fips = _stateFipsCache[key];
                    if (fips) drillIntoState(key, d.properties.name, fips);
                } else {
                    const tooltip = document.getElementById('mapTooltip');
                    tooltip.textContent = `${d.properties.name} — No data yet`;
                    positionTooltip(event);
                    tooltip.classList.add('visible');
                    if (_tooltipHideTimer) clearTimeout(_tooltipHideTimer);
                    _tooltipHideTimer = setTimeout(() => { tooltip.classList.remove('visible'); _tooltipHideTimer = null; }, 1500);
                }
            });

        mapView = 'us';
        mapActiveState = null;
        mapActiveCounty = null;
        mapNavStack = [];
        mapStateSentiment = stateSentiment;
        document.getElementById('mapTitle').textContent = 'United States';
        document.getElementById('mapBackBtn').style.display = 'none';

        // Update articles panel for current scope
        const stateFilter = (document.getElementById('dashStateFilter') || {}).value || '';
        updateArticlesPanel(stateFilter
            ? { state: stateFilter, title: getStateName(stateFilter) || 'Recent Articles' }
            : { title: 'Recent Articles' }
        );

    } catch (err) {
        console.error('US map render error:', err);
        container.innerHTML = '<div class="no-data">Map unavailable</div>';
    }
}

// ── State drill-down (county view) ──
async function drillIntoState(stateKey, stateNameArg, fipsArg) {
    const config = STATE_CONFIG[stateKey];
    const stateName = config ? config.name : stateNameArg;
    const fips = config ? config.fips : fipsArg;
    if (!fips) return;
    const container = document.getElementById('wyomingMap');
    document.getElementById('mapTooltip').classList.remove('visible');
    if (_tooltipHideTimer) { clearTimeout(_tooltipHideTimer); _tooltipHideTimer = null; }
    container.innerHTML = '';

    try {
        const us = await ensureTopoData();
        const counties = topojson.feature(us, us.objects.counties);
        const statesGeo = topojson.feature(us, us.objects.states);

        const stateCounties = {
            type: 'FeatureCollection',
            features: counties.features.filter(f => String(f.id).startsWith(fips))
        };
        const stateOutline = {
            type: 'FeatureCollection',
            features: statesGeo.features.filter(f => String(f.id) === fips)
        };

        const width = container.clientWidth;
        const height = container.clientHeight || 330;
        const projection = d3.geoAlbersUsa().fitSize([width, height], stateOutline);
        const path = d3.geoPath().projection(projection);

        const svg = d3.select(container)
            .append('svg')
            .attr('viewBox', `0 0 ${width} ${height}`)
            .attr('preserveAspectRatio', 'xMidYMid meet')
            .attr('role', 'img')
            .attr('aria-label', `${stateName} county sentiment map`);

        // Neighboring state outlines (rendered first, behind counties)
        const neighborStates = statesGeo.features.filter(f => String(f.id) !== fips);
        svg.selectAll('.neighbor-state')
            .data(neighborStates)
            .enter()
            .append('path')
            .attr('class', 'neighbor-state')
            .attr('d', path)
            .attr('fill', '#0a0e17')
            .attr('stroke', 'rgba(148,163,184,0.15)')
            .attr('stroke-width', 0.5);

        // Fetch location sentiment for this state (county-normalized)
        const locations = await fetchJSON(`/api/locations?state=${stateKey}`);

        // Build FIPS -> county name lookup from API response
        const fipsToCounty = {};
        Object.entries(locations).forEach(([name, data]) => {
            if (data.fips) fipsToCounty[data.fips] = name;
        });
        // Also include hardcoded cityCountyFips for marker rendering
        const cityFips = config ? config.cityCountyFips : undefined;

        // Draw counties
        svg.selectAll('.county-path')
            .data(stateCounties.features)
            .enter()
            .append('path')
            .attr('class', 'county-path')
            .attr('d', path)
            .attr('data-fips', d => d.id)
            .on('mouseenter', function(event, d) {
                if (_tooltipHideTimer) { clearTimeout(_tooltipHideTimer); _tooltipHideTimer = null; }
                const fips = String(d.id);
                const countyName = (d.properties && d.properties.name) ? d.properties.name : `FIPS ${fips}`;
                const tooltip = document.getElementById('mapTooltip');
                const county = fipsToCounty[fips];
                if (county && locations[county] && locations[county].avg !== null) {
                    tooltip.textContent = `${countyName} Co. | avg: ${locations[county].avg.toFixed(1)} | n=${locations[county].count}`;
                } else {
                    tooltip.textContent = `${countyName} Co.`;
                }
                positionTooltip(event);
                tooltip.classList.add('visible');
            })
            .on('mousemove', function(event) { positionTooltip(event); })
            .on('mouseleave', function() {
                document.getElementById('mapTooltip').classList.remove('visible');
            })
            .on('click', function(event, d) {
                const cfips = String(d.id);
                const county = fipsToCounty[cfips];
                const countyDisplayName = (d.properties && d.properties.name)
                    ? d.properties.name + ' Co.' : county || `FIPS ${cfips}`;
                if (county && locations[county] && locations[county].avg !== null) {
                    drillIntoCounty(stateKey, stateName, fips, cfips, countyDisplayName);
                } else {
                    const tooltip = document.getElementById('mapTooltip');
                    tooltip.textContent = `${countyDisplayName} — No articles`;
                    positionTooltip(event);
                    tooltip.classList.add('visible');
                    if (_tooltipHideTimer) clearTimeout(_tooltipHideTimer);
                    _tooltipHideTimer = setTimeout(() => { tooltip.classList.remove('visible'); _tooltipHideTimer = null; }, 1500);
                }
            });

        // Color counties using FIPS from API response
        Object.entries(locations).forEach(([name, data]) => {
            if (data.fips && data.avg !== null) {
                const el = document.querySelector(`.county-path[data-fips="${data.fips}"]`);
                if (el) {
                    el.style.fill = sentimentColor(data.avg);
                    el.style.stroke = '#14b8a6';
                    el.style.strokeWidth = '0.8';
                }
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

        // City markers (only for states with hardcoded config)
        if (config && config.cities) {
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
        }

        mapView = 'county';
        mapActiveState = stateKey;
        mapActiveCounty = null;
        mapNavStack = [{ view: 'us' }];
        document.getElementById('mapTitle').textContent = stateName;
        document.getElementById('mapBackBtn').style.display = '';

        // Sync the dashboard state dropdown
        const dashFilter = document.getElementById('dashStateFilter');
        if (dashFilter && dashFilter.value !== stateKey) dashFilter.value = stateKey;

        // Update key metrics and articles panel for this state
        updateMetrics(`?state=${stateKey}`);
        updateArticlesPanel({ state: stateKey, title: stateName });

    } catch (err) {
        console.error('State map render error:', err);
        container.innerHTML = '<div class="no-data">Map unavailable</div>';
    }
}

// ── County drill-down (single county zoom) ──
async function drillIntoCounty(stateKey, stateName, stateFips, countyFips, countyName) {
    const container = document.getElementById('wyomingMap');
    document.getElementById('mapTooltip').classList.remove('visible');
    if (_tooltipHideTimer) { clearTimeout(_tooltipHideTimer); _tooltipHideTimer = null; }
    container.innerHTML = '';

    try {
        const us = await ensureTopoData();
        const counties = topojson.feature(us, us.objects.counties);
        const statesGeo = topojson.feature(us, us.objects.states);

        // All counties in this state
        const stateCounties = {
            type: 'FeatureCollection',
            features: counties.features.filter(f => String(f.id).startsWith(stateFips))
        };

        // The target county
        const targetCounty = {
            type: 'FeatureCollection',
            features: counties.features.filter(f => String(f.id) === countyFips)
        };

        if (!targetCounty.features.length) {
            container.innerHTML = '<div class="no-data">County not found</div>';
            return;
        }

        const width = container.clientWidth;
        const height = container.clientHeight || 330;

        // Fit projection to the target county with some padding
        const projection = d3.geoAlbersUsa().fitExtent(
            [[width * 0.1, height * 0.1], [width * 0.9, height * 0.9]],
            targetCounty
        );
        const path = d3.geoPath().projection(projection);

        const svg = d3.select(container)
            .append('svg')
            .attr('viewBox', `0 0 ${width} ${height}`)
            .attr('preserveAspectRatio', 'xMidYMid meet');

        // Draw neighboring counties (rest of state) as background
        svg.selectAll('.neighbor-county')
            .data(stateCounties.features.filter(f => String(f.id) !== countyFips))
            .enter()
            .append('path')
            .attr('class', 'neighbor-county')
            .attr('d', path)
            .attr('fill', '#0a0e17')
            .attr('stroke', 'rgba(148,163,184,0.15)')
            .attr('stroke-width', 0.5);

        // Draw the target county highlighted
        svg.selectAll('.target-county')
            .data(targetCounty.features)
            .enter()
            .append('path')
            .attr('class', 'target-county')
            .attr('d', path)
            .attr('fill', (() => {
                // Re-fetch the sentiment for coloring
                return '#0f1520';  // will be colored below
            })())
            .attr('stroke', '#14b8a6')
            .attr('stroke-width', 2);

        // Fetch and apply sentiment color for this county
        const locations = await fetchJSON(`/api/locations?state=${stateKey}`);
        Object.entries(locations).forEach(([name, data]) => {
            if (data.fips === countyFips && data.avg !== null) {
                svg.select('.target-county')
                    .attr('fill', sentimentColor(data.avg));
            }
        });

        // State outline (faint)
        const stateOutline = statesGeo.features.filter(f => String(f.id) === stateFips);
        if (stateOutline.length) {
            svg.append('path')
                .datum(stateOutline[0])
                .attr('d', path)
                .attr('fill', 'none')
                .attr('stroke', 'rgba(20,184,166,0.15)')
                .attr('stroke-width', 0.5);
        }

        mapView = 'single-county';
        mapActiveState = stateKey;
        mapActiveCounty = countyFips;
        document.getElementById('mapTitle').textContent = countyName;
        document.getElementById('mapBackBtn').style.display = '';

        // Update metrics and articles panel for this county
        updateMetrics(`?county_fips=${countyFips}`);
        updateArticlesPanel({ county_fips: countyFips, state: stateKey, title: countyName });

    } catch (err) {
        console.error('County drill-down error:', err);
        container.innerHTML = '<div class="no-data">County view unavailable</div>';
    }
}

// ── Back navigation (handles 3 levels: county → state → US) ──
async function showUSMap() {
    document.getElementById('mapTooltip').classList.remove('visible');
    if (_tooltipHideTimer) { clearTimeout(_tooltipHideTimer); _tooltipHideTimer = null; }
    if (mapView === 'single-county' && mapActiveState) {
        // Go back to state view (not all the way to US)
        if (STATE_CONFIG[mapActiveState]) {
            drillIntoState(mapActiveState);
        } else {
            if (!_stateFipsCache) _stateFipsCache = await fetchJSON('/api/state-fips');
            const fips = _stateFipsCache[mapActiveState];
            if (fips) drillIntoState(mapActiveState, getStateName(mapActiveState), fips);
        }
        return;
    }

    // Go back to US map
    if (!mapStateSentiment || Object.keys(mapStateSentiment).length === 0) {
        mapStateSentiment = await fetchJSON('/api/state-sentiment');
    }

    // Reset dropdown to "All States"
    const dashFilter = document.getElementById('dashStateFilter');
    if (dashFilter) dashFilter.value = '';

    renderUSMap(mapStateSentiment);
    updateMetrics('');
}

// ══════════════════════════════════════════════
//  ARTICLES PANEL (left sidebar on dashboard)
// ══════════════════════════════════════════════
async function updateArticlesPanel(scope) {
    const panel = document.getElementById('articlesPanelList');
    const titleEl = document.getElementById('articlesPanelTitle');
    const viewAllBtn = document.getElementById('articlesPanelViewAll');
    if (!panel || !titleEl) return;

    // Build query string
    let qs = 'limit=5';
    if (scope.county_fips) qs += `&county_fips=${scope.county_fips}`;
    else if (scope.state) qs += `&state=${scope.state}&relevance=primary`;

    titleEl.textContent = scope.title || 'Recent Articles';

    try {
        const data = await fetchJSON(`/api/articles?${qs}`);
        if (!data.articles || !data.articles.length) {
            panel.innerHTML = '<div class="no-data">No articles found</div>';
            if (viewAllBtn) viewAllBtn.style.display = 'none';
            return;
        }

        panel.innerHTML = data.articles.map((a, i) => {
            const scoreColor = sentimentColor(a.sentiment_score);
            const scoreText = a.sentiment_score != null ? a.sentiment_score.toFixed(1) : '--';
            const title = truncate(a.title || 'Untitled', 80);
            const summary = truncate(a.summary || '', 120);
            const source = a.source || '';
            const ago = timeAgo(a.published_date);

            let expandedHtml = '';
            if (a.key_claims) expandedHtml += `<div class="card-claims"><strong>Key Claims: </strong>${escapeHtml(a.key_claims)}</div>`;
            if (a.sentiment_justification) expandedHtml += `<div class="card-rationale"><strong>Rationale: </strong>${escapeHtml(a.sentiment_justification)}</div>`;
            if (a.url) {
                const safeUrl = articleUrl(a.url, 'articles', a.resolved_url);
                if (safeUrl) expandedHtml += `<a href="${safeUrl}" target="_blank" rel="noopener" class="card-link">Read full article &rarr;</a>`;
            }

            const relTag = a.state_relevance === 'mentioned'
                ? '<span class="pill pill-relevance pill-mentioned">Mentioned</span> '
                : (a.state_relevance === 'primary' ? '<span class="pill pill-relevance pill-primary">Primary</span> ' : '');
            const lowConfidence = lowConfidenceBadgeHTML(a);
            return `<div class="article-card" onclick="toggleArticleCard(this)" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleArticleCard(this)}">
                <div class="card-header">
                    <span class="card-score" style="background:${scoreColor}">${scoreText}</span>
                    <span class="card-title">${escapeHtml(title)}</span>
                </div>
                <div class="card-meta">${relTag}${lowConfidence}${escapeHtml(source)} &middot; ${ago}</div>
                ${summary ? `<div class="card-summary">${escapeHtml(summary)}</div>` : ''}
                <div class="card-expanded">${expandedHtml}</div>
            </div>`;
        }).join('');

        if (viewAllBtn) viewAllBtn.style.display = data.total > 5 ? '' : 'none';
        window._articlesPanelScope = scope;
    } catch (err) {
        console.error('Articles panel error:', err);
        panel.innerHTML = '<div class="no-data">Error loading articles</div>';
    }
}

function toggleArticleCard(el) {
    el.classList.toggle('expanded');
}

function viewAllArticles() {
    const scope = window._articlesPanelScope || {};
    switchPage('articles');
    setTimeout(async () => {
        if (scope.state) {
            const filterState = document.getElementById('filterState');
            if (filterState) {
                filterState.value = scope.state;
                await onStateFilterChange();
                if (scope.county_fips) {
                    const filterLoc = document.getElementById('filterLocation');
                    if (filterLoc) filterLoc.value = scope.county_fips;
                }
                renderArticles();
            }
        }
    }, 50);
}

// ══════════════════════════════════════════════
//  PAGE 1: DASHBOARD
// ══════════════════════════════════════════════
async function onDashStateChange() {
    const dropdown = document.getElementById('dashStateFilter');
    const stateKey = (dropdown || {}).value || '';
    if (!stateKey) {
        mapView = 'none';
        renderDashboard();
        return;
    }
    // Disable dropdown during async render to prevent race conditions
    if (dropdown) dropdown.disabled = true;
    try {
        if (STATE_CONFIG[stateKey]) {
            await drillIntoState(stateKey);
        } else {
            if (!_stateFipsCache) _stateFipsCache = await fetchJSON('/api/state-fips');
            const fips = _stateFipsCache[stateKey];
            if (fips) {
                await drillIntoState(stateKey, getStateName(stateKey), fips);
            } else {
                renderDashboard();
            }
        }
    } finally {
        if (dropdown) dropdown.disabled = false;
    }
}

function applyMetrics(overview, wsiData) {
    document.getElementById('metricTotal').textContent = overview.total_articles;
    document.getElementById('metricAnalyzed').textContent = overview.analyzed_articles || 0;

    const avgEl = document.getElementById('metricSentiment');
    if (overview.avg_sentiment !== null) {
        avgEl.textContent = overview.avg_sentiment.toFixed(2);
        avgEl.style.color = sentimentColor(overview.avg_sentiment);
    } else { avgEl.textContent = '--'; }

    const wsiEl = document.getElementById('metricWSI');
    if (wsiData.current_wsi !== null) {
        wsiEl.textContent = wsiData.current_wsi.toFixed(2);
        wsiEl.style.color = sentimentColor(wsiData.current_wsi);
    } else { wsiEl.textContent = '--'; }

    renderSentDistBar(overview.sentiment_distribution);

    document.getElementById('lastSync').textContent = overview.last_ingestion ? timeAgo(overview.last_ingestion) : '--';
}

function renderSentDistBar(dist) {
    const container = document.getElementById('sentDistBar');
    if (!container) return;
    const bar = container.querySelector('.sent-dist-bar');
    const labels = container.querySelector('.sent-dist-labels');
    if (!dist) { bar.innerHTML = ''; labels.innerHTML = ''; return; }

    const keys = ['strongly_negative', 'slightly_negative', 'neutral', 'slightly_positive', 'strongly_positive'];
    const colors = ['var(--sent-1)', 'var(--sent-2)', 'var(--sent-3)', 'var(--sent-4)', 'var(--sent-5)'];
    const abbr = ['V.NEG', 'NEG', 'NEU', 'POS', 'V.POS'];
    const total = keys.reduce((s, k) => s + (dist[k] || 0), 0);

    if (total === 0) { bar.innerHTML = ''; labels.innerHTML = ''; return; }

    bar.innerHTML = keys.map((k, i) => {
        const pct = ((dist[k] || 0) / total) * 100;
        if (pct === 0) return '';
        return `<span style="width:${pct}%;background:${colors[i]}" title="${abbr[i]}: ${dist[k]}"></span>`;
    }).join('');

    labels.innerHTML = keys.map((k, i) => {
        const count = dist[k] || 0;
        return `
            <span class="sent-dist-label" style="color:${colors[i]}">
                <span class="sent-dist-label-text">${abbr[i]}</span>
                <span class="sent-dist-label-count">${count}</span>
            </span>
        `;
    }).join('');
}

async function updateMetrics(qs) {
    try {
        const [overview, wsiData] = await Promise.all([
            fetchJSON(`/api/overview${qs}`),
            fetchJSON(`/api/sentiment-index${qs}`),
        ]);
        applyMetrics(overview, wsiData);
        renderWSITrendChart(wsiData.trend);
        renderPeriodComparison(wsiData.period_comparison);
    } catch (err) { console.error('Metrics update error:', err); }
}

async function renderDashboard() {
    try {
        const stateFilter = (document.getElementById('dashStateFilter') || {}).value || '';
        const qs = stateFilter ? `?state=${stateFilter}` : '';

        // If drilled into a county or state on the map, metrics should reflect that scope
        let metricsQs = qs;
        if (mapView === 'single-county' && mapActiveCounty) {
            metricsQs = `?county_fips=${mapActiveCounty}`;
        } else if (mapView === 'county' && mapActiveState) {
            metricsQs = `?state=${mapActiveState}`;
        }

        const [stateSentiment, overview, wsiData] = await Promise.all([
            fetchJSON('/api/state-sentiment'),
            fetchJSON(`/api/overview${metricsQs}`),
            fetchJSON(`/api/sentiment-index${metricsQs}`),
        ]);

        applyMetrics(overview, wsiData);

        // Map — default to US view, but don't reset if user drilled into a state/county
        if (mapView === 'none' || mapView === 'us') {
            renderUSMap(stateSentiment);
        }

        // Charts + Period Comparison
        renderWSITrendChart(wsiData.trend);
        renderPeriodComparison(wsiData.period_comparison);

    } catch (err) { console.error('Dashboard error:', err); }
}

function buildWSIChartConfig({ trend, pointRadius, pointHoverRadius, borderWidth, maxTicksLimit }) {
    return {
        type: 'line',
        data: {
            labels: trend.map(d => fmtDate(d.week)),
            datasets: [
                {
                    label: 'WSI',
                    data: trend.map(d => d.wsi),
                    borderColor: '#14b8a6',
                    backgroundColor: 'transparent',
                    fill: false,
                    tension: 0.2,
                    pointRadius: pointRadius,
                    pointHoverRadius: pointHoverRadius,
                    pointBackgroundColor: '#14b8a6',
                    pointBorderColor: '#050810',
                    pointBorderWidth: 1,
                    borderWidth: borderWidth,
                    segment: {
                        borderDash: ctx => trend[ctx.p1DataIndex]?.carried ? [4, 3] : [],
                    },
                },
                {
                    label: 'Raw Avg',
                    data: trend.map(d => d.raw),
                    borderColor: 'rgba(107,114,128,0.5)',
                    backgroundColor: 'transparent',
                    fill: false,
                    tension: 0.2,
                    pointRadius: 0,
                    pointHoverRadius: 3,
                    borderWidth: 1,
                    borderDash: [2, 2],
                    segment: {
                        borderDash: ctx => trend[ctx.p1DataIndex]?.carried ? [1, 3] : [2, 2],
                    },
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            plugins: {
                legend: { display: true, labels: { boxWidth: 12, padding: 10, font: { family: "'JetBrains Mono', monospace", size: 9 }, color: '#6b7280' } },
                tooltip: { ...ttCfg, callbacks: {
                    afterLabel: ctx => {
                        const d = trend[ctx.dataIndex];
                        return d ? `articles: ${d.articles} | clusters: ${d.clusters}${d.carried ? ' (carried)' : ''}` : '';
                    }
                } }
            },
            scales: {
                y: { min: 1, max: 5, grid: { color: 'rgba(26,31,46,0.5)' },
                     ticks: { stepSize: 1, font: { family: "'JetBrains Mono', monospace", size: 9 },
                              callback: v => ({ 1:'V.NEG', 2:'NEG', 3:'NEU', 4:'POS', 5:'V.POS' }[v] || v) } },
                x: { grid: { color: 'rgba(26,31,46,0.3)' },
                     ticks: { maxTicksLimit: maxTicksLimit, font: { family: "'JetBrains Mono', monospace", size: 9 } } }
            }
        }
    };
}

function renderWSITrendChart(trend) {
    const canvas = document.getElementById('trendChart');
    const noData = document.getElementById('trendNoData');
    destroyChart('trend');

    const realData = trend ? trend.filter(t => t.wsi !== null) : [];
    if (realData.length === 0) {
        canvas.style.display = 'none';
        noData.style.display = 'block';
        return;
    }
    canvas.style.display = 'block';
    noData.style.display = 'none';

    charts.trend = new Chart(canvas, buildWSIChartConfig({
        trend,
        pointRadius: trend.length === 1 ? 4 : 2,
        pointHoverRadius: 5,
        borderWidth: 2,
        maxTicksLimit: 12,
    }));
}

function renderPeriodComparison(pc) {
    const container = document.getElementById('periodComparison');
    const noData = document.getElementById('periodNoData');

    if (!pc || (pc.current_4wk === null && pc.prior_4wk === null)) {
        container.style.display = 'none';
        noData.style.display = 'block';
        return;
    }
    container.style.display = '';
    noData.style.display = 'none';

    const curEl = document.getElementById('periodCurrent');
    const priorEl = document.getElementById('periodPrior');
    const changeEl = document.getElementById('periodChange');

    if (pc.current_4wk !== null) {
        curEl.textContent = pc.current_4wk.toFixed(2);
        curEl.style.color = sentimentColor(pc.current_4wk);
    } else { curEl.textContent = '--'; }

    if (pc.prior_4wk !== null) {
        priorEl.textContent = pc.prior_4wk.toFixed(2);
        priorEl.style.color = sentimentColor(pc.prior_4wk);
    } else { priorEl.textContent = '--'; }

    if (pc.change !== null) {
        const arrow = pc.direction === 'improving' ? '\u25B2' : pc.direction === 'declining' ? '\u25BC' : '\u25C6';
        const color = pc.direction === 'improving' ? 'var(--sent-5)' : pc.direction === 'declining' ? 'var(--sent-1)' : 'var(--sent-3)';
        changeEl.textContent = `${arrow} ${pc.change > 0 ? '+' : ''}${pc.change.toFixed(2)} ${pc.direction}`;
        changeEl.style.color = color;
    } else { changeEl.textContent = '--'; }
}

// ══════════════════════════════════════════════
//  PAGE 2: SENTIMENT
// ══════════════════════════════════════════════
let entitySortField = 'count';
let entitySortAsc = false;
let entityCache = [];

async function renderSentiment() {
    try {
        const stateFilter = (document.getElementById('sentStateFilter') || {}).value || '';
        const qs = stateFilter ? `?state=${stateFilter}` : '';

        const [overview, wsiData, topics, entities, articlesData, locWeekly] = await Promise.all([
            fetchJSON(`/api/overview${qs}`),
            fetchJSON(`/api/sentiment-index${qs}`),
            fetchJSON(`/api/topics${qs}`),
            fetchJSON(`/api/entities${qs}`),
            fetchJSON(`/api/articles?limit=100${stateFilter ? '&state=' + stateFilter : ''}`),
            fetchJSON(`/api/location-weekly${qs}`),
        ]);

        // A: WSI Trend Chart
        renderSentimentWSIChart(wsiData.trend);

        // B: Period Comparison Cards
        renderPeriodCards();

        // C: Location Heatmap (weekly, collapsible by state)
        renderLocationHeatmap(locWeekly);

        // D: Topic Sentiment Bars
        renderTopicBars(topics);

        // E: Entity Tracker
        renderEntityTable(entities.entities);

        // F: Key Articles
        renderKeyArticles(articlesData.articles);

        // G: Distribution
        renderDistribution(overview.sentiment_distribution);

    } catch (err) { console.error('Sentiment error:', err); }
}

function renderSentimentWSIChart(trend) {
    const canvas = document.getElementById('sentTrendChart');
    const noData = document.getElementById('sentTrendNoData');
    destroyChart('sentTrend');

    const realData = trend ? trend.filter(t => t.wsi !== null) : [];
    if (realData.length === 0) {
        canvas.style.display = 'none';
        noData.style.display = 'block';
        return;
    }
    canvas.style.display = 'block';
    noData.style.display = 'none';

    charts.sentTrend = new Chart(canvas, buildWSIChartConfig({
        trend,
        pointRadius: 3,
        pointHoverRadius: 6,
        borderWidth: 2.5,
        maxTicksLimit: 14,
    }));
}

async function renderPeriodCards() {
    const container = document.getElementById('periodCards');

    try {
        const data = await fetchJSON('/api/sentiment-index-cards');

        container.innerHTML = (data.cards || []).map((card, i) => {
            const pc = card.period_comparison;
            const wsi = card.current_wsi;
            const arrow = pc?.direction === 'improving' ? '\u25B2' : pc?.direction === 'declining' ? '\u25BC' : '\u25C6';
            const changeColor = pc?.direction === 'improving' ? 'var(--sent-5)' : pc?.direction === 'declining' ? 'var(--sent-1)' : 'var(--sent-3)';
            const isAll = i === 0;
            return `<div class="period-card${isAll ? ' period-card-all' : ''}" style="border-left: 3px solid ${sentimentColor(wsi)}">
                <div class="period-card-label">${card.name}</div>
                <div class="period-card-wsi" style="color:${sentimentColor(wsi)}">${wsi !== null ? wsi.toFixed(2) : '--'}</div>
                <div class="period-card-change" style="color:${changeColor}">${pc?.change !== null ? `${arrow} ${pc.change > 0 ? '+' : ''}${pc.change.toFixed(2)}` : '--'}</div>
            </div>`;
        }).join('');
    } catch (err) { console.error('Period cards error:', err); }
}

let _hmExpandedStates = new Set();

function renderLocationHeatmap(locWeekly) {
    const el = document.getElementById('locationHeatmap');
    const noData = document.getElementById('locHeatNoData');
    const data = locWeekly?.data;
    const weeks = locWeekly?.weeks;

    if (!data || !weeks || weeks.length === 0 || Object.keys(data).length === 0) {
        el.innerHTML = ''; noData.style.display = 'block'; return;
    }
    noData.style.display = 'none';
    el._lastData = locWeekly;  // Cache for toggle re-render

    // Sort states alphabetically, skip non-tracked ones
    const trackedStates = Object.keys(data)
        .filter(s => !['nationwide', 'international', 'other'].includes(s))
        .sort();

    // Auto-expand states on first render
    if (_hmExpandedStates.size === 0) {
        trackedStates.forEach(s => _hmExpandedStates.add(s));
    }

    el.setAttribute('role', 'grid');
    el.setAttribute('aria-label', 'Location sentiment heatmap');
    el.style.gridTemplateColumns = `140px repeat(${weeks.length}, 34px)`;

    let html = '<div class="hm-label"></div>';
    weeks.forEach(w => { html += `<div class="hm-header">${fmtDate(w)}</div>`; });

    trackedStates.forEach(state => {
        const stateAbbr = getStateAbbr(state);
        const stateName = capitalize(state);
        const isExpanded = _hmExpandedStates.has(state);
        const arrow = isExpanded ? '\u25BC' : '\u25B6';
        const locations = data[state] || {};
        const locKeys = Object.keys(locations).sort((a, b) => {
            if (a === 'statewide') return -1;
            if (b === 'statewide') return 1;
            return a.localeCompare(b);
        });

        // State header row (clickable)
        html += `<div class="hm-label hm-state-label" onclick="toggleHmState('${state}')" title="Click to ${isExpanded ? 'collapse' : 'expand'}">${arrow} ${stateAbbr} ${stateName}</div>`;
        // State-level aggregate: average across all locations per week
        weeks.forEach(w => {
            const allScores = [];
            locKeys.forEach(loc => {
                const cells = locations[loc] || [];
                const cell = cells.find(c => c.week === w);
                if (cell && cell.avg !== null && !cell.carried) allScores.push(cell.avg);
            });
            if (allScores.length > 0) {
                const avg = allScores.reduce((a, b) => a + b, 0) / allScores.length;
                html += `<div class="hm-cell" style="background:${sentimentColor(avg)}" data-tip="${stateName} | ${fmtDate(w)} | ${avg.toFixed(1)} (n=${allScores.length})"></div>`;
            } else {
                html += `<div class="hm-cell empty" data-tip="${stateName} | ${fmtDate(w)} | --"></div>`;
            }
        });

        // Location rows (collapsible)
        if (isExpanded) {
            locKeys.forEach(loc => {
                const locDisplay = loc === 'statewide' ? 'General' : loc.replace(/_/g, ' ');
                const locLabel = `${locDisplay}`;
                const cells = locations[loc] || [];
                html += `<div class="hm-label hm-loc-label">${locLabel}</div>`;
                weeks.forEach(w => {
                    const cell = cells.find(c => c.week === w);
                    if (cell && cell.avg !== null) {
                        const opacity = cell.carried ? '0.35' : '0.85';
                        const suffix = cell.carried ? ' (carried)' : ` (n=${cell.count})`;
                        html += `<div class="hm-cell" style="background:${sentimentColor(cell.avg)};opacity:${opacity}" data-tip="${stateName} / ${locDisplay} | ${fmtDate(w)} | ${cell.avg.toFixed(1)}${suffix}"></div>`;
                    } else {
                        html += `<div class="hm-cell empty" data-tip="${stateName} / ${locDisplay} | ${fmtDate(w)} | --"></div>`;
                    }
                });
            });
        }
    });

    el.innerHTML = html;
    attachHmTooltips(el);
}

function toggleHmState(state) {
    if (_hmExpandedStates.has(state)) _hmExpandedStates.delete(state);
    else _hmExpandedStates.add(state);
    // Re-render from cached data
    const el = document.getElementById('locationHeatmap');
    if (el._lastData) renderLocationHeatmap(el._lastData);
}

function renderTopicBars(topicData) {
    const canvas = document.getElementById('topicBarChart');
    const noData = document.getElementById('topicBarNoData');
    destroyChart('topicBar');

    if (!topicData.topics || topicData.topics.length === 0) {
        canvas.style.display = 'none';
        noData.style.display = 'block';
        return;
    }
    canvas.style.display = 'block';
    noData.style.display = 'none';

    // Sort by deviation from neutral (3.0) — most polarizing first
    const sorted = [...topicData.topics]
        .filter(t => t.avg_sentiment !== null)
        .sort((a, b) => Math.abs(b.avg_sentiment - 3.0) - Math.abs(a.avg_sentiment - 3.0));

    charts.topicBar = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: sorted.map(t => `${topicDisplay(t.name)}  (${t.count})`),
            datasets: [{
                data: sorted.map(t => t.avg_sentiment),
                backgroundColor: sorted.map(t => sentimentColor(t.avg_sentiment)),
                borderRadius: 0,
                barThickness: 18,
            }]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: { ...ttCfg, callbacks: {
                    label: ctx => {
                        const t = sorted[ctx.dataIndex];
                        const dev = Math.abs(t.avg_sentiment - 3.0).toFixed(1);
                        return `avg: ${t.avg_sentiment.toFixed(2)} | ${t.count} articles | ${dev}pt from neutral`;
                    }
                } },
            },
            scales: {
                x: { min: 1, max: 5, grid: { color: 'rgba(26,31,46,0.5)' },
                     ticks: { stepSize: 1, font: { family: "'JetBrains Mono', monospace", size: 9 },
                              callback: v => ({ 1:'V.NEG', 2:'NEG', 3:'NEU', 4:'POS', 5:'V.POS' }[v] || v) } },
                y: { grid: { display: false },
                     ticks: { font: { family: "'JetBrains Mono', monospace", size: 9 } } }
            }
        }
    });
    // Dynamic height based on number of topics
    canvas.style.height = Math.max(150, sorted.length * 28) + 'px';
}

function renderEntityTable(entities) {
    entityCache = entities || [];
    const tbody = document.getElementById('entityBody');
    const noData = document.getElementById('entityNoData');

    if (!entityCache.length) {
        tbody.innerHTML = '';
        noData.style.display = 'block';
        return;
    }
    noData.style.display = 'none';
    _renderEntityRows();
}

function sortEntityTable(field) {
    if (entitySortField === field) entitySortAsc = !entitySortAsc;
    else { entitySortField = field; entitySortAsc = field === 'name'; }
    _renderEntityRows();
}

function _renderEntityRows() {
    const sorted = [...entityCache].sort((a, b) => {
        let va = a[entitySortField], vb = b[entitySortField];
        if (va === null || va === undefined) va = entitySortAsc ? Infinity : -Infinity;
        if (vb === null || vb === undefined) vb = entitySortAsc ? Infinity : -Infinity;
        if (typeof va === 'string') return entitySortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
        return entitySortAsc ? va - vb : vb - va;
    });

    const tbody = document.getElementById('entityBody');
    tbody.innerHTML = sorted.slice(0, 20).map(e => {
        let trendHtml = '--';
        if (e.trend !== null && e.trend !== undefined) {
            const arrow = e.trend > 0.05 ? '\u25B2' : e.trend < -0.05 ? '\u25BC' : '\u25C6';
            const color = e.trend > 0.05 ? 'var(--sent-5)' : e.trend < -0.05 ? 'var(--sent-1)' : 'var(--sent-3)';
            trendHtml = `<span style="color:${color}">${arrow} ${e.trend > 0 ? '+' : ''}${e.trend.toFixed(2)}</span>`;
        }
        return `<tr>
            <td class="text-cell">${escapeHtml(e.name)}</td>
            <td>${e.count}${e.recent_count ? ` <span style="color:var(--text-muted);font-size:9px">(${e.recent_count} recent)</span>` : ''}</td>
            <td><span style="color:${sentimentColor(e.avg_sentiment)}">${e.avg_sentiment !== null ? e.avg_sentiment.toFixed(2) : '--'}</span></td>
            <td>${trendHtml}</td>
        </tr>`;
    }).join('');
}

function renderKeyArticles(articles) {
    const container = document.getElementById('keyArticlesList');
    const noData = document.getElementById('keyArticlesNoData');

    if (!articles || articles.length === 0) {
        container.innerHTML = '';
        noData.style.display = 'block';
        return;
    }
    noData.style.display = 'none';

    // Sort by deviation from neutral — most polarizing first
    const sorted = [...articles]
        .filter(a => a.sentiment_score !== null)
        .sort((a, b) => Math.abs(b.sentiment_score - 3.0) - Math.abs(a.sentiment_score - 3.0))
        .slice(0, 8);

    container.innerHTML = sorted.map(a => {
        const dev = Math.abs(a.sentiment_score - 3.0).toFixed(1);
        const direction = a.sentiment_score >= 3.0 ? 'positive' : 'negative';
        const reason = `${dev} points ${direction} of neutral \u2014 ` +
            (a.sentiment_score >= 4.5 ? 'strongly favorable framing' :
             a.sentiment_score >= 3.5 ? 'optimistic tone with caveats' :
             a.sentiment_score >= 2.5 ? 'mixed or balanced coverage' :
             a.sentiment_score >= 1.5 ? 'skeptical or cautionary tone' :
             'strong opposition framing');
        return `<div class="key-article">
            <div class="key-article-header">
                <span class="pill ${sentimentPillClass(a.sentiment_label)}">${sentimentLabel(a.sentiment_label)}</span>
                <span class="key-article-score" style="color:${sentimentColor(a.sentiment_score)}">${a.sentiment_score.toFixed(1)}</span>
                <span class="key-article-meta">${escapeHtml(a.source || '')} \u2014 ${fmtDate(a.published_date)}</span>
                <span class="pill pill-state">${getStateAbbr(a.state)}</span>
                ${lowConfidenceBadgeHTML(a)}
                ${internationalBadgeHTML(a)}
            </div>
            <div class="key-article-title">${a.url ? `<a href="${articleUrl(a.url, 'articles', a.resolved_url)}" target="_blank" rel="noopener">${escapeHtml(a.title)}</a>` : escapeHtml(a.title)}</div>
            <div class="key-article-reason">${reason}</div>
            ${a.key_claims ? `<div class="key-article-claims">${escapeHtml(a.key_claims)}</div>` : ''}
        </div>`;
    }).join('');
}

function attachHmTooltips(container) {
    const tip = document.getElementById('hmTooltip');
    container.querySelectorAll('.hm-cell').forEach(c => {
        c.addEventListener('mouseenter', () => { tip.textContent = c.dataset.tip; tip.classList.add('visible'); });
        c.addEventListener('mousemove', e => { tip.style.left = (e.clientX + 10) + 'px'; tip.style.top = (e.clientY - 6) + 'px'; });
        c.addEventListener('mouseleave', () => { tip.classList.remove('visible'); });
    });
}

async function renderDistribution(distribution) {
    const canvas = document.getElementById('distributionChart');
    const noData = document.getElementById('distNoData');
    destroyChart('dist');

    try {
        const counts = distribution || (await fetchJSON('/api/overview')).sentiment_distribution || {};
        const segments = [
            { key: 'strongly_positive', label: 'V.POS', color: '#22c55e' },
            { key: 'slightly_positive', label: 'POS', color: '#84cc16' },
            { key: 'neutral', label: 'NEU', color: '#eab308' },
            { key: 'slightly_negative', label: 'NEG', color: '#f97316' },
            { key: 'strongly_negative', label: 'V.NEG', color: '#ef4444' },
        ].filter(segment => (counts[segment.key] || 0) > 0);

        const total = segments.reduce((sum, segment) => sum + (counts[segment.key] || 0), 0);
        if (total === 0) { canvas.style.display = 'none'; noData.style.display = 'block'; return; }
        canvas.style.display = 'block';
        noData.style.display = 'none';

        charts.dist = new Chart(canvas, {
            type: 'doughnut',
            data: {
                labels: segments.map(segment => `${segment.label} ${counts[segment.key]}`),
                datasets: [{
                    data: segments.map(segment => counts[segment.key]),
                    backgroundColor: segments.map(segment => segment.color),
                    borderWidth: 0,
                    hoverOffset: 4,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                cutout: '75%',
                plugins: {
                    legend: { position: 'bottom', labels: { boxWidth: 8, padding: 8, font: { family: "'JetBrains Mono', monospace", size: 9 }, color: '#6b7280', usePointStyle: false } },
                    tooltip: ttCfg,
                }
            }
        });
    } catch (err) { console.error('Distribution error:', err); }
}

// ══════════════════════════════════════════════
//  PAGE 3: ARTICLES (table with expandable rows)
// ══════════════════════════════════════════════
async function onStateFilterChange() {
    const state = document.getElementById('filterState').value;
    const locSelect = document.getElementById('filterLocation');
    locSelect.value = '';

    if (!state || state === 'nationwide' || state === 'international') {
        locSelect.innerHTML = '<option value="">All Counties / General</option>';
        locSelect.disabled = state === 'nationwide' || state === 'international';
    } else {
        // Fetch normalized geography for this state: county FIPS plus a statewide bucket.
        try {
            const data = await fetchJSON(`/api/locations?state=${state}`);
            locSelect.innerHTML = '<option value="">All Counties / General</option>';
            if (data.statewide) {
                locSelect.innerHTML += `<option value="statewide">General (${data.statewide.count})</option>`;
            }
            Object.entries(data).sort((a, b) => a[0].localeCompare(b[0])).forEach(([loc, info]) => {
                if (info.fips) {
                    const label = loc.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
                    locSelect.innerHTML += `<option value="${info.fips}">${label} (${info.count})</option>`;
                }
            });
            locSelect.disabled = false;
        } catch (err) {
            locSelect.innerHTML = '<option value="">All Counties / General</option>';
            locSelect.disabled = false;
        }
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
    if (loc) {
        // FIPS codes are numeric; use county_fips param for precision
        if (/^\d+$/.test(loc)) url += `&county_fips=${loc}`;
        else url += `&location=${loc}`;
    }
    if (sent) url += `&sentiment_label=${sent}`;

    try {
        const data = await fetchJSON(url);
        articleTotal = data.total;
        const statesNeedingCounties = [...new Set(
            (data.articles || [])
                .map(a => a.state)
                .filter(state => state && !['nationwide', 'international', 'other'].includes(state))
        )];
        await Promise.all(statesNeedingCounties.map(state => _loadCountyOptions(state)));

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
            const relBadge = a.state_relevance === 'mentioned'
                ? ' <span class="pill pill-relevance pill-mentioned">Mentioned</span>'
                : (a.state_relevance === 'primary' ? ' <span class="pill pill-relevance pill-primary">Primary</span>' : '');
            const lowConfidence = lowConfidenceBadgeHTML(a);
            const international = internationalBadgeHTML(a);
            return `
                <tr style="cursor:pointer" onclick="toggleExpand(${idx})" tabindex="0" role="row" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleExpand(${idx})}">
                    <td class="text-cell"><span onclick="event.stopPropagation();showArticleDetail(${a.id})" style="cursor:pointer;color:var(--accent)">${escapeHtml(titleTrunc)}</span>${lowConfidence}${international}</td>
                    <td>${escapeHtml(a.source || '')}</td>
                    <td>${fmtDate(a.published_date)}</td>
                    <td><span class="pill ${sentimentPillClass(a.sentiment_label)}">${sentimentLabel(a.sentiment_label)}</span></td>
                    <td><span class="pill pill-state">${getStateAbbr(a.state)}</span>${relBadge}</td>
                    <td><span class="pill pill-loc">${escapeHtml(a.location_display || 'General')}</span></td>
                </tr>
                <tr class="expand-row hidden" id="expand-${idx}">
                    <td colspan="6">
                        <div class="article-detail" data-article-id="${a.id}">
                            ${a.url ? `<a href="${articleUrl(a.url, 'articles', a.resolved_url)}" target="_blank" rel="noopener" class="article-detail-url">${escapeHtml(a.resolved_url || a.url)}</a>` : ''}
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
                                            ${buildStateOptions(a.state)}
                                        </select>
                                    </div>
                                </div>
                                <div class="article-detail-section">
                                    <div class="article-detail-label">GEOGRAPHY</div>
                                    <div class="article-detail-value detail-geo-display">${escapeHtml(a.location_display || 'General')}</div>
                                </div>
                                <div class="article-detail-section">
                                    <div class="article-detail-label">RAW PLACE</div>
                                    <div class="article-detail-value detail-raw-place">${a.raw_primary_place ? escapeHtml(a.raw_primary_place) : '<span class="detail-empty">None</span>'}</div>
                                </div>
                                <div class="article-detail-section">
                                    <div class="article-detail-label">SCOPE</div>
                                    <div class="article-detail-field">
                                        <select class="detail-select detail-geo-scope-select" data-field="geography_scope" onchange="onDetailGeographyChange(this); markArticleDirty(this)">
                                            ${geographyScopeOptions(a.state, a.geography_scope)}
                                        </select>
                                    </div>
                                </div>
                                <div class="article-detail-section">
                                    <div class="article-detail-label">COUNTY</div>
                                    <div class="article-detail-field">
                                        <select class="detail-select detail-county-select" data-field="county_fips" onchange="markArticleDirty(this)" ${a.geography_scope !== 'county' ? 'disabled' : ''}>
                                            ${countyOptions(a.state, a.primary_county_fips, a.primary_county_name)}
                                        </select>
                                    </div>
                                </div>
                                <div class="article-detail-section">
                                    <div class="article-detail-label">SOURCE TYPE</div>
                                    <div class="article-detail-value">${capitalize(a.source_type || 'news')}</div>
                                </div>
                            </div>
                            ${a.is_low_confidence ? `<div class="article-detail-section"><div class="article-detail-label">QUALITY FLAG</div><div class="article-detail-value">Low confidence: thin Google News wrapper row. Excluded from summary metrics until upgraded.</div></div>` : ''}
                            ${a.is_international ? `<div class="article-detail-section"><div class="article-detail-label">GEOGRAPHY FLAG</div><div class="article-detail-value">International article: visible in raw views, excluded from US summary metrics and maps.</div></div>` : ''}
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
                                <button class="btn-delete-article" onclick="deleteArticle(this, ${a.id})">DELETE</button>
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

const _countyOptionsCache = {};

async function _loadCountyOptions(state) {
    if (!state || ['nationwide', 'international', 'other'].includes(state)) return [];
    if (_countyOptionsCache[state]) return _countyOptionsCache[state];
    try {
        const resp = await fetchJSON(`/api/state-counties?state=${encodeURIComponent(state)}`);
        _countyOptionsCache[state] = resp.counties || [];
    } catch {
        _countyOptionsCache[state] = [];
    }
    return _countyOptionsCache[state];
}

function geographyScopeOptions(state, selected) {
    if (state === 'nationwide') {
        return '<option value="nationwide" selected>Nationwide</option>';
    }
    if (state === 'international') {
        return '<option value="international" selected>International</option>';
    }
    const scope = selected === 'county' ? 'county' : 'statewide';
    return `
        <option value="statewide" ${scope === 'statewide' ? 'selected' : ''}>General</option>
        <option value="county" ${scope === 'county' ? 'selected' : ''}>County</option>
    `;
}

function countyOptions(state, selectedFips, selectedName) {
    if (!state || ['nationwide', 'international', 'other'].includes(state)) {
        return '<option value="">Not applicable</option>';
    }
    const counties = _countyOptionsCache[state] || [];
    let opts = [['', 'Select county']];
    counties.forEach(c => opts.push([c.fips, c.county]));
    if (selectedFips && !opts.find(([fips]) => fips === selectedFips)) {
        opts.push([selectedFips, selectedName || `FIPS ${selectedFips}`]);
    }
    return opts.map(([value, label]) =>
        `<option value="${value}" ${value === (selectedFips || '') ? 'selected' : ''}>${escapeHtml(label)}</option>`
    ).join('');
}

async function onDetailStateChange(stateSelect) {
    const panel = stateSelect.closest('.article-detail');
    const newState = stateSelect.value;
    const scopeSelect = panel.querySelector('.detail-geo-scope-select');
    const countySelect = panel.querySelector('.detail-county-select');
    await _loadCountyOptions(newState);
    if (scopeSelect) {
        const selectedScope = (newState === 'nationwide' || newState === 'international')
            ? newState
            : (scopeSelect.value === 'county' ? 'county' : 'statewide');
        scopeSelect.innerHTML = geographyScopeOptions(newState, selectedScope);
        scopeSelect.value = (newState === 'nationwide' || newState === 'international') ? newState : selectedScope;
    }
    if (countySelect) {
        countySelect.innerHTML = countyOptions(newState, '', '');
        countySelect.disabled = ['nationwide', 'international', 'other'].includes(newState) || (scopeSelect && scopeSelect.value !== 'county');
    }
}

function onDetailGeographyChange(scopeSelect) {
    const panel = scopeSelect.closest('.article-detail');
    const countySelect = panel.querySelector('.detail-county-select');
    const stateSelect = panel.querySelector('[data-field="state"]');
    const state = stateSelect ? stateSelect.value : '';
    if (!countySelect) return;
    countySelect.disabled = ['nationwide', 'international', 'other'].includes(state) || scopeSelect.value !== 'county';
    if (scopeSelect.value !== 'county') {
        countySelect.value = '';
    }
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
            const result = await resp.json();
            const article = result.article;
            status.textContent = 'Saved';
            status.style.color = 'var(--sent-5)';
            btn.classList.add('hidden');
            // Update the parent row's pills
            const expandRow = panel.closest('.expand-row');
            const dataRow = expandRow.previousElementSibling;
            if (dataRow) {
                const cells = dataRow.querySelectorAll('td');
                cells[3].innerHTML = `<span class="pill ${sentimentPillClass(payload.sentiment_label)}">${sentimentLabel(payload.sentiment_label)}</span>`;
                const updated = article || articleCache[articleId] || {};
                articleCache[articleId] = { ...(articleCache[articleId] || {}), ...updated };
                cells[4].innerHTML = `<span class="pill pill-state">${getStateAbbr(updated.state || payload.state)}</span>`;
                cells[5].innerHTML = `<span class="pill pill-loc">${escapeHtml(updated.location_display || 'General')}</span>`;
            }
            const geoDisplay = panel.querySelector('.detail-geo-display');
            if (geoDisplay) geoDisplay.textContent = (article && article.location_display) || 'General';
            const rawPlace = panel.querySelector('.detail-raw-place');
            if (rawPlace) rawPlace.innerHTML = (article && article.raw_primary_place)
                ? escapeHtml(article.raw_primary_place)
                : '<span class="detail-empty">None</span>';
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

async function deleteArticle(btn, articleId) {
    if (!confirm('Permanently delete this article? This cannot be undone.')) return;
    btn.disabled = true;
    btn.textContent = 'DELETING...';
    const status = btn.closest('.article-detail-actions').querySelector('.save-status');
    try {
        const resp = await fetch(`/api/articles/${articleId}`, { method: 'DELETE' });
        if (resp.ok) {
            // Remove both the data row and expand row from the table
            const expandRow = btn.closest('.expand-row');
            const dataRow = expandRow.previousElementSibling;
            if (dataRow) dataRow.remove();
            expandRow.remove();
            status.textContent = '';
        } else {
            const err = await resp.json();
            status.textContent = err.error || 'Delete failed';
            status.style.color = 'var(--sent-1)';
            btn.disabled = false;
            btn.textContent = 'DELETE';
        }
    } catch (e) {
        status.textContent = 'Network error';
        status.style.color = 'var(--sent-1)';
        btn.disabled = false;
        btn.textContent = 'DELETE';
    }
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

        // Config/Keywords section
        renderConfigSection();

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
let _pollFailures = {};
let _queueArticles = [];
let _queueSort = { key: 'relevance_score', dir: 'desc' };
let _queueOffset = 0;
let _queueTotal = 0;
let _queueHasMore = false;
let _queueSelectedIds = new Set();
const QUEUE_PAGE_SIZE = 250;
const QUEUE_FETCH_LIMIT = 1000;
let _queueDesiredCount = QUEUE_PAGE_SIZE;
let _analysisOffset = 0;
let _analysisTotal = 0;
let _analysisHasMore = false;

function sortQueueArticles() {
    const { key, dir } = _queueSort;
    _queueArticles.sort((a, b) => {
        let va = a[key], vb = b[key];
        if (va == null) va = '';
        if (vb == null) vb = '';
        if (typeof va === 'number' && typeof vb === 'number') return dir === 'asc' ? va - vb : vb - va;
        va = String(va).toLowerCase();
        vb = String(vb).toLowerCase();
        return dir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
    });
}

function onQueueSort(key) {
    if (_queueSort.key === key) {
        _queueSort.dir = _queueSort.dir === 'asc' ? 'desc' : 'asc';
    } else {
        _queueSort.key = key;
        _queueSort.dir = key === 'title' || key === 'source' || key === 'state' ? 'asc' : 'desc';
    }
    sortQueueArticles();
    renderQueueRows();
    updateQueueSortIndicators();
}

function updateQueueSortIndicators() {
    document.querySelectorAll('#queueTable th[data-sort]').forEach(th => {
        const key = th.dataset.sort;
        const arrow = th.querySelector('.sort-arrow');
        if (arrow) {
            if (key === _queueSort.key) {
                arrow.textContent = _queueSort.dir === 'asc' ? ' \u25B2' : ' \u25BC';
                arrow.style.opacity = '1';
            } else {
                arrow.textContent = ' \u25B2';
                arrow.style.opacity = '0.25';
            }
        }
    });
}

function renderQueueRows() {
    const tbody = document.getElementById('queueBody');
    tbody.innerHTML = _queueArticles.map(a => {
        const hasScore = a.relevance_score !== null && a.relevance_score !== undefined;
        const scoreCls = !hasScore ? 'rel-none' : a.relevance_score >= 8 ? 'rel-high' : a.relevance_score >= 5 ? 'rel-mid' : 'rel-low';
        const scoreText = hasScore ? a.relevance_score : '\u2014';
        const titleTrunc = a.title && a.title.length > 55 ? a.title.substring(0, 52) + '...' : (a.title || '--');
        const stateAbbr = getStateAbbr(a.state);
        const typeCls = a.source_type === 'rss' ? 'pill-rss' : 'pill-web';
        const typeLabel = a.source_type === 'rss' ? 'RSS' : 'WEB';
        const isChecked = _queueSelectedIds.has(a.id);
        const reason = a.relevance_reason || '';
        const summary = a.summary ? escapeHtml(a.summary) : '<span style="color:var(--text-muted)">No summary</span>';
        return `<tr style="cursor:pointer" onclick="togglePendingExpand(${a.id}, event)">
            <td><input type="checkbox" class="queue-check" data-id="${a.id}" ${isChecked ? 'checked' : ''} onchange="onQueueCheckChanged(this)" onclick="event.stopPropagation()"></td>
            <td><span class="relevance-score ${scoreCls}">${scoreText}</span></td>
            <td class="text-cell" title="${escapeHtml(a.title)}"><a href="${articleUrl(a.url, 'pending_articles')}" target="_blank" rel="noopener" class="article-link" onclick="event.stopPropagation()">${escapeHtml(titleTrunc)}</a></td>
            <td>${escapeHtml(a.source || '')}</td>
            <td>${stateAbbr ? `<span class="pill pill-state">${stateAbbr}</span>` : '--'}</td>
            <td>${fmtDate(a.published_date)}</td>
            <td><span class="pill ${typeCls}">${typeLabel}</span></td>
        </tr>
        <tr class="expand-row hidden" id="pending-expand-${a.id}">
            <td colspan="7" class="pending-detail">
                ${reason ? `<div class="pending-detail-reason"><strong>Relevance:</strong> ${escapeHtml(reason)}</div>` : ''}
                <div class="pending-detail-summary">${summary}</div>
                ${a.url ? `<a href="${articleUrl(a.url, 'pending_articles')}" target="_blank" rel="noopener" class="article-link" style="font-size:10px;word-break:break-all">${escapeHtml(a.url)}</a>` : ''}
            </td>
        </tr>`;
    }).join('');
    updateQueueSelectedCount();
}

async function renderReviewQueue(options = {}) {
    const { append = false, preserveLoadedCount = false } = options;
    const table = document.getElementById('queueTable');
    const empty = document.getElementById('queueEmpty');
    const actions = document.getElementById('queueActions');
    const statsEl = document.getElementById('queueStats');
    const loadMoreBtn = document.getElementById('queueLoadMore');

    if (append) {
        _queueDesiredCount = Infinity;
    } else if (preserveLoadedCount) {
        _queueDesiredCount = _queueHasMore ? Math.max(QUEUE_PAGE_SIZE, _queueArticles.length) : Infinity;
        _queueSelectedIds = new Set();
    } else {
        _queueDesiredCount = QUEUE_PAGE_SIZE;
        _queueOffset = 0;
        _queueArticles = [];
        _queueSelectedIds = new Set();
    }

    try {
        let articles = append ? _queueArticles.slice() : [];
        let stats = {};
        let total = 0;
        let hasMore = false;

        while (true) {
            const remaining = _queueDesiredCount === Infinity
                ? QUEUE_FETCH_LIMIT
                : Math.max(0, _queueDesiredCount - articles.length);
            if (_queueDesiredCount !== Infinity && remaining === 0) break;

            const limit = _queueDesiredCount === Infinity
                ? QUEUE_FETCH_LIMIT
                : Math.min(QUEUE_FETCH_LIMIT, remaining);
            const data = await fetchJSON(`/api/control/pending?limit=${limit}&offset=${articles.length}`);
            const batch = data.articles || [];

            stats = data.stats || {};
            total = data.total || 0;
            hasMore = !!data.has_more;

            if (!batch.length) break;
            articles = articles.concat(batch);

            if (!hasMore) break;
            if (_queueDesiredCount !== Infinity && articles.length >= Math.min(_queueDesiredCount, total)) break;
        }

        _queueTotal = total;
        _queueHasMore = hasMore && articles.length < total;

        // Stats summary
        const statParts = [];
        if (stats.rss) statParts.push(`${stats.rss} RSS`);
        if (stats.websearch) statParts.push(`${stats.websearch} Web`);
        if (stats.manual) statParts.push(`${stats.manual} Manual`);
        if (articles.length < _queueTotal) {
            statParts.push(`showing ${articles.length} of ${_queueTotal}`);
        }
        statsEl.textContent = statParts.length ? `(${statParts.join(' \u00B7 ')})` : '';

        if (_queueTotal === 0) {
            table.style.display = 'none';
            actions.style.display = 'none';
            if (loadMoreBtn) loadMoreBtn.style.display = 'none';
            empty.style.display = 'block';
            return;
        }
        table.style.display = '';
        actions.style.display = '';
        empty.style.display = 'none';

        document.getElementById('queueTotal').textContent = _queueTotal;
        if (!append) document.getElementById('queueSelectAll').checked = false;

        _queueArticles = articles;
        if (!append && !preserveLoadedCount && _queueSelectedIds.size === 0) {
            _queueArticles.forEach(a => {
                if (a.relevance_score !== null && a.relevance_score !== undefined && a.relevance_score >= 7) {
                    _queueSelectedIds.add(a.id);
                }
            });
        }
        _queueOffset = _queueArticles.length;
        sortQueueArticles();
        renderQueueRows();
        updateQueueSortIndicators();
        if (loadMoreBtn) loadMoreBtn.style.display = _queueHasMore ? '' : 'none';
    } catch (err) { console.error('Review queue error:', err); }
}

function toggleAllQueue(checked) {
    document.querySelectorAll('.queue-check').forEach(cb => {
        cb.checked = checked;
        onQueueCheckChanged(cb, false);
    });
    updateQueueSelectedCount();
}

function updateQueueSelectedCount() {
    document.getElementById('queueSelected').textContent = _queueSelectedIds.size;
}

function onQueueCheckChanged(cb, syncCount = true) {
    const id = parseInt(cb.dataset.id);
    if (cb.checked) _queueSelectedIds.add(id);
    else _queueSelectedIds.delete(id);
    if (syncCount) updateQueueSelectedCount();
}

async function approveQueueSelected() {
    const ids = Array.from(_queueSelectedIds);
    if (ids.length === 0) return;
    const status = document.getElementById('queueStatus');
    try {
        const resp = await fetch('/api/control/approve', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ article_ids: ids }),
        });
        const data = await resp.json();
        if (data.success) {
            status.innerHTML = `<span class="task-complete">Approved ${data.approved} article${data.approved !== 1 ? 's' : ''}</span>`;
            _queueSelectedIds.clear();
            await renderReviewQueue({ preserveLoadedCount: true });
        }
    } catch (err) { console.error('Approve error:', err); }
}

async function rejectQueueSelected() {
    const ids = Array.from(_queueSelectedIds);
    if (ids.length === 0) return;
    const status = document.getElementById('queueStatus');
    try {
        await fetch('/api/control/reject', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ article_ids: ids }),
        });
        status.innerHTML = `<span class="task-complete">Rejected ${ids.length} article${ids.length !== 1 ? 's' : ''}</span>`;
        _queueSelectedIds.clear();
        await renderReviewQueue({ preserveLoadedCount: true });
    } catch (err) { console.error('Reject error:', err); }
}

function toggleSelectDropdown(e) {
    e.stopPropagation();
    const dd = document.getElementById('selectDropdown');
    dd.classList.toggle('open');
    // Close on next outside click
    if (dd.classList.contains('open')) {
        setTimeout(() => document.addEventListener('click', closeSelectDropdown, { once: true }), 0);
    }
}

function closeSelectDropdown() {
    document.getElementById('selectDropdown').classList.remove('open');
}

function queueSelectFiltered(filter) {
    closeSelectDropdown();
    document.querySelectorAll('.queue-check').forEach(cb => {
        const id = parseInt(cb.dataset.id);
        const article = _queueArticles.find(a => a.id === id);
        if (!article) { cb.checked = false; return; }
        switch (filter) {
            case 'all':  cb.checked = true; break;
            case 'none': cb.checked = false; break;
            case 'score-4': cb.checked = (article.relevance_score === 4); break;
            case 'score-5': cb.checked = (article.relevance_score === 5); break;
            case 'score-6': cb.checked = (article.relevance_score === 6); break;
            case 'type-rss':       cb.checked = (article.source_type === 'rss'); break;
            case 'type-websearch': cb.checked = (article.source_type === 'websearch'); break;
            default: break;
        }
        onQueueCheckChanged(cb, false);
    });
    document.getElementById('queueSelectAll').checked =
        document.querySelectorAll('.queue-check').length === document.querySelectorAll('.queue-check:checked').length;
    updateQueueSelectedCount();
}

async function renderAnalysisQueue(append = false) {
    const table = document.getElementById('analysisTable');
    const tbody = document.getElementById('analysisBody');
    const empty = document.getElementById('analysisEmpty');
    const countEl = document.getElementById('analysisCount');
    const loadMoreBtn = document.getElementById('analysisLoadMore');
    const limit = 200;

    if (!append) {
        _analysisOffset = 0;
    }

    try {
        const data = await fetchJSON(`/api/control/unanalyzed?limit=${limit}&offset=${_analysisOffset}`);
        const articles = data.articles || [];
        _analysisTotal = data.total || 0;
        _analysisHasMore = !!data.has_more;
        const loadedCount = append ? (_analysisOffset + articles.length) : articles.length;

        if (_analysisTotal > 0) {
            countEl.textContent = loadedCount < _analysisTotal
                ? `(${loadedCount} of ${_analysisTotal})`
                : `(${_analysisTotal})`;
        } else {
            countEl.textContent = '';
        }

        if (_analysisTotal === 0) {
            table.style.display = 'none';
            if (loadMoreBtn) loadMoreBtn.style.display = 'none';
            empty.style.display = 'block';
            return;
        }
        table.style.display = '';
        empty.style.display = 'none';

        const rows = articles.map(a => {
            const titleTrunc = a.title && a.title.length > 65 ? a.title.substring(0, 62) + '...' : (a.title || '--');
            const stateAbbr = getStateAbbr(a.state);
            return `<tr>
                <td class="text-cell">${a.url ? `<a href="${articleUrl(a.url, 'articles', a.resolved_url)}" target="_blank" rel="noopener" class="article-link">${escapeHtml(titleTrunc)}</a>` : escapeHtml(titleTrunc)}${lowConfidenceBadgeHTML(a)}${internationalBadgeHTML(a)}</td>
                <td>${escapeHtml(a.source || '')}</td>
                <td>${fmtDate(a.published_date)}</td>
                <td>${stateAbbr ? `<span class="pill pill-state">${stateAbbr}</span>` : '--'}</td>
            </tr>`;
        }).join('');
        if (append) tbody.innerHTML += rows;
        else tbody.innerHTML = rows;
        _analysisOffset = loadedCount;
        if (loadMoreBtn) loadMoreBtn.style.display = _analysisHasMore ? '' : 'none';
    } catch (err) { console.error('Analysis queue error:', err); }
}

function loadMoreQueue() {
    renderReviewQueue({ append: true });
}

function loadMoreAnalysis() {
    renderAnalysisQueue(true);
}

async function renderControl() {
    await renderReviewQueue();
    await renderAnalysisQueue();

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
                const ms = parseIsoDate(t.finished) - parseIsoDate(t.started);
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
            return `Analyzed: ${result.analyzed || 0} | Errors: ${result.errors || 0}${result.scrape_scraped ? ` | Scraped: ${result.scrape_scraped}` : ''}`;
        case 'reprocess':
            return `Candidates: ${result.candidates || 0} | Upgraded: ${result.upgraded || 0} | Reanalyzed: ${result.reanalyzed || 0}`;
        case 'digest':
            return result.filename || result.message || 'Done';
        default:
            return JSON.stringify(result).substring(0, 80);
    }
}

function formatAnalysisProgress(progress, elapsed) {
    if (!progress || !progress.phase) return `Running... (${elapsed})`;

    if (progress.phase === 'scraping') {
        if (progress.total !== undefined) return `Scraping full text ${progress.current || 0} / ${progress.total}... (${elapsed})`;
        return `Scraping full text... (${elapsed})`;
    }
    if (progress.phase === 'preparing') return `Preparing analysis batch... (${elapsed})`;
    if (progress.phase === 'analyzing') {
        if (progress.total !== undefined) return `Analyzing ${progress.current || 0} / ${progress.total}... (${elapsed})`;
        return `Analyzing... (${elapsed})`;
    }
    if (progress.phase === 'starting') return `Starting analysis... (${elapsed})`;
    return `Running... (${elapsed})`;
}

function formatReprocessProgress(progress, elapsed) {
    if (!progress || !progress.phase) return `Running... (${elapsed})`;
    if (progress.phase === 'rescraping') {
        if (progress.total !== undefined) return `Upgrading ${progress.current || 0} / ${progress.total}... (${elapsed})`;
        return `Upgrading recent low-confidence rows... (${elapsed})`;
    }
    if (progress.phase === 'reanalyzing') {
        if (progress.total !== undefined) return `Reanalyzing ${progress.current || 0} / ${progress.total}... (${elapsed})`;
        return `Reanalyzing upgraded rows... (${elapsed})`;
    }
    return `Running... (${elapsed})`;
}

async function submitArticle(e) {
    e.preventDefault();
    const btn = document.getElementById('btnSubmitArticle');
    const status = document.getElementById('statusArticle');
    const url = document.getElementById('ctrlUrl').value.trim();

    if (!url) {
        status.innerHTML = '<span class="task-error">URL is required</span>';
        return;
    }

    btn.disabled = true;
    btn.classList.add('running');
    status.innerHTML = '<span class="task-running">Fetching article...</span>';

    try {
        const resp = await fetch('/api/control/add-article', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url }),
        });
        const data = await resp.json();

        if (data.success) {
            const titleSnip = data.title && data.title.length > 50 ? data.title.substring(0, 47) + '...' : (data.title || '');
            const stateTag = data.state ? ` [${data.state}]` : '';
            status.innerHTML = `<span class="task-complete">Added: ${escapeHtml(titleSnip)}${escapeHtml(stateTag)}</span>`;
            document.getElementById('articleForm').reset();
            await renderAnalysisQueue();
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
    analysis: '/api/control/run-analysis',
    reprocess: '/api/control/reprocess-low-confidence',
    digest: '/api/control/run-digest',
};

const TASK_BTN_MAP = {
    analysis: 'btnRunAnalysis',
    reprocess: 'btnReprocessLowConfidence',
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
        const fetchOpts = { method: 'POST' };
        // Pass batch size for analysis tasks
        if (type === 'analysis') {
            const batchVal = document.getElementById('analysisBatchSize')?.value;
            if (batchVal) {
                fetchOpts.headers = { 'Content-Type': 'application/json' };
                fetchOpts.body = JSON.stringify({ limit: parseInt(batchVal) });
            }
        } else if (type === 'reprocess') {
            const batchVal = document.getElementById('reprocessBatchSize')?.value || '250';
            fetchOpts.headers = { 'Content-Type': 'application/json' };
            fetchOpts.body = JSON.stringify({ limit: parseInt(batchVal), days_back: 30 });
        }
        const resp = await fetch(TASK_ENDPOINTS[type], fetchOpts);
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
    _pollFailures[type] = 0;

    // Clear any existing poll for this type
    if (_pollTimers[type]) clearInterval(_pollTimers[type]);

    _pollTimers[type] = setInterval(async () => {
        try {
            const data = await fetchJSON(`/api/control/task/${taskId}`);
            _pollFailures[type] = 0;

            if (data.status === 'running') {
                const elapsed = formatElapsedSeconds(elapsedSecondsFromIso(data.started));
                let msg = `Running... (${elapsed})`;
                if (type === 'analysis') msg = formatAnalysisProgress(data.progress || {}, elapsed);
                if (type === 'reprocess') msg = formatReprocessProgress(data.progress || {}, elapsed);
                status.innerHTML = `<span class="task-running">${msg}</span>`;
            } else if (data.status === 'completed') {
                clearInterval(_pollTimers[type]);
                _pollTimers[type] = null;
                _pollFailures[type] = 0;
                const resultText = formatTaskResult(type, data.result);
                status.innerHTML = `<span class="task-complete">${escapeHtml(resultText)}</span>`;
                btn.disabled = false;
                btn.classList.remove('running');
                renderControl(); // Refresh task list
            } else if (data.status === 'error') {
                clearInterval(_pollTimers[type]);
                _pollTimers[type] = null;
                _pollFailures[type] = 0;
                status.innerHTML = `<span class="task-error">Error: ${escapeHtml(data.error || 'Unknown')}</span>`;
                btn.disabled = false;
                btn.classList.remove('running');
            }
        } catch (err) {
            _pollFailures[type] = (_pollFailures[type] || 0) + 1;
            if (_pollFailures[type] < 4) {
                status.innerHTML = `<span class="task-running">Connection hiccup, retrying...</span>`;
                return;
            }
            clearInterval(_pollTimers[type]);
            _pollTimers[type] = null;
            status.innerHTML = `<span class="task-error">Poll error: ${escapeHtml(err.message)}</span>`;
            btn.disabled = false;
            btn.classList.remove('running');
        }
    }, 2000);
}

// ══════════════════════════════════════════════
//  WEB SEARCH
// ══════════════════════════════════════════════

async function runWebSearch() {
    const btn = document.getElementById('btnWebsearch');
    const status = document.getElementById('statusWebsearch');
    const query = document.getElementById('ctrlSearchQuery').value.trim();
    const daysBack = document.getElementById('ctrlSearchDays').value;
    const state = (document.getElementById('ctrlSearchState') || {}).value || '';

    btn.disabled = true;
    btn.classList.add('running');
    status.innerHTML = '<span class="task-running">Searching...</span>';

    try {
        const resp = await fetch('/api/control/run-websearch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: query || null, days_back: parseInt(daysBack), state: state || null }),
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

    if (_pollTimers.websearch) clearInterval(_pollTimers.websearch);

    _pollTimers.websearch = setInterval(async () => {
        try {
            const data = await fetchJSON(`/api/control/task/${taskId}`);

            if (data.status === 'running') {
                const elapsed = formatElapsedSeconds(elapsedSecondsFromIso(data.started));
                const progress = data.progress || {};
                let msg = 'Searching & scoring...';
                if (progress.phase === 'querying' && progress.total) {
                    msg = `Searching ${progress.current}/${progress.total}`;
                    if (progress.state) msg += ` (${progress.state})`;
                } else if (progress.phase === 'scoring' && progress.total) {
                    msg = `Scoring ${Math.min(progress.current, progress.total)}/${progress.total}`;
                }
                status.innerHTML = `<span class="task-running">${msg} (${elapsed})</span>`;
            } else if (data.status === 'completed') {
                clearInterval(_pollTimers.websearch);
                _pollTimers.websearch = null;
                btn.disabled = false;
                btn.classList.remove('running');

                const r = data.result || {};
                let msg = `Found ${r.total_results || 0} results | ${r.new_articles || 0} new`;
                if (r.auto_approved > 0) msg += ` | ${r.auto_approved} auto-approved`;
                if (r.skipped_url > 0 || r.skipped_title > 0) {
                    msg += ` | ${(r.skipped_url || 0) + (r.skipped_title || 0)} dupes filtered`;
                }
                if (r.api_key_set === false && r.new_articles > 0) {
                    msg += `</span><br><span class="task-warning">API key not set — relevance scoring skipped`;
                }
                status.innerHTML = `<span class="task-complete">${msg}</span>`;
                renderReviewQueue(); // Refresh the unified queue
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

async function runStateSweep() {
    const btn = document.getElementById('btnSweep');
    const status = document.getElementById('statusSweep');
    const daysBack = document.getElementById('ctrlSweepDays').value;
    const state = (document.getElementById('ctrlSweepState') || {}).value || '';
    const skipAnalysis = !(document.getElementById('ctrlSweepAnalyze') || {}).checked;
    const startDate = (document.getElementById('ctrlSweepStart') || {}).value || '';
    const endDate = (document.getElementById('ctrlSweepEnd') || {}).value || '';

    btn.disabled = true;
    btn.classList.add('running');
    status.innerHTML = '<span class="task-running">Starting sweep...</span>';

    try {
        const body = {
            days_back: parseInt(daysBack),
            state: state || null,
            skip_analysis: skipAnalysis,
        };
        if (startDate) body.start_date = startDate;
        if (endDate) body.end_date = endDate;

        const resp = await fetch('/api/control/run-sweep', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();

        if (data.task_id) {
            pollStateSweep(data.task_id);
        } else {
            status.innerHTML = '<span class="task-error">Failed to start sweep</span>';
            btn.disabled = false;
            btn.classList.remove('running');
        }
    } catch (err) {
        status.innerHTML = `<span class="task-error">Error: ${escapeHtml(err.message)}</span>`;
        btn.disabled = false;
        btn.classList.remove('running');
    }
}

function pollStateSweep(taskId) {
    const btn = document.getElementById('btnSweep');
    const status = document.getElementById('statusSweep');

    if (_pollTimers.sweep) clearInterval(_pollTimers.sweep);

    _pollTimers.sweep = setInterval(async () => {
        try {
            const data = await fetchJSON(`/api/control/task/${taskId}`);

            if (data.status === 'running') {
                const elapsed = formatElapsedSeconds(elapsedSecondsFromIso(data.started));
                const progress = data.progress || {};
                let msg = 'Sweeping states...';

                if (progress.phase === 'querying' && progress.total) {
                    msg = `Querying ${progress.current}/${progress.total}`;
                    if (progress.state) msg += ` (${progress.state})`;
                } else if (progress.phase === 'scoring' && progress.total) {
                    msg = `Scoring ${Math.min(progress.current, progress.total)}/${progress.total}`;
                    if (progress.state) msg += ` (${progress.state})`;
                } else if (progress.phase === 'analyzing') {
                    msg = 'Running post-sweep analysis';
                }

                status.innerHTML = `<span class="task-running">${msg} (${elapsed})</span>`;
            } else if (data.status === 'completed') {
                clearInterval(_pollTimers.sweep);
                _pollTimers.sweep = null;
                btn.disabled = false;
                btn.classList.remove('running');

                const r = data.result || {};
                let msg = `${r.states_searched || 50} states`;
                msg += ` | ${r.total_results || 0} results`;
                msg += ` | ${r.new_articles || 0} new`;
                if (r.auto_approved > 0) msg += ` | ${r.auto_approved} auto-approved`;
                if (r.analyzed > 0) msg += ` | ${r.analyzed} analyzed`;
                const dupes = (r.skipped_url || 0) + (r.skipped_title || 0);
                if (dupes > 0) msg += ` | ${dupes} dupes`;
                if (r.api_key_set === false && r.new_articles > 0) {
                    msg += `</span><br><span class="task-warning">API key not set — relevance scoring skipped`;
                }
                status.innerHTML = `<span class="task-complete">${msg}</span>`;
                renderReviewQueue();
                renderControl();
            } else if (data.status === 'error') {
                clearInterval(_pollTimers.sweep);
                _pollTimers.sweep = null;
                status.innerHTML = `<span class="task-error">Error: ${escapeHtml(data.error || 'Unknown')}</span>`;
                btn.disabled = false;
                btn.classList.remove('running');
            }
        } catch (err) {
            clearInterval(_pollTimers.sweep);
            _pollTimers.sweep = null;
            status.innerHTML = `<span class="task-error">Poll error: ${escapeHtml(err.message)}</span>`;
            btn.disabled = false;
            btn.classList.remove('running');
        }
    }, 3000);  // Poll every 3s (sweeps take longer)
}

function togglePendingExpand(id, event) {
    if (event.target.closest('input, a')) return;
    const row = document.getElementById(`pending-expand-${id}`);
    if (row) row.classList.toggle('hidden');
}

// ══════════════════════════════════════════════
//  ARTICLE DETAIL PAGE
// ══════════════════════════════════════════════

async function showArticleDetail(articleId) {
    // Switch to the article detail page
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById('page-article-detail').classList.add('active');
    document.getElementById('articleDetailTitle').textContent = 'Loading...';
    document.getElementById('articleDetailContent').innerHTML = '';

    try {
        const data = await fetchJSON(`/api/article/${articleId}`);
        document.getElementById('articleDetailTitle').textContent = 'ARTICLE DETAIL';

        const sentClass = sentimentPillClass(data.sentiment_label);
        const sentText = sentimentLabel(data.sentiment_label);
        const scoreText = data.sentiment_score != null ? data.sentiment_score.toFixed(1) : '--';

        // Build states section
        const statesHtml = (data.state_details && data.state_details.length > 0)
            ? data.state_details.map(s => {
                const abbr = getStateAbbr(s.state);
                const place = s.place ? escapeHtml(s.place) : 'Statewide';
                const county = s.county_name ? ` (${escapeHtml(s.county_name)})` : '';
                const rel = s.relevance === 'mentioned' ? ' <span style="color:var(--muted)">[mentioned]</span>' : '';
                return `<span class="state-card"><span class="state-name">${abbr}</span> ${place}${county}${rel}</span>`;
            }).join('')
            : (data.state ? `<span class="state-card"><span class="state-name">${getStateAbbr(data.state)}</span></span>` : '<span style="color:var(--muted)">Not categorized</span>');

        const topicsHtml = (data.topic_tags || []).map(t => `<span class="pill pill-topic">${topicDisplay(t)}</span>`).join(' ') || '<span style="color:var(--muted)">None</span>';
        const entitiesHtml = (data.entities_mentioned || []).map(e => `<span class="pill pill-entity">${escapeHtml(e)}</span>`).join(' ') || '<span style="color:var(--muted)">None</span>';

        const content = data.full_text || data.summary || '';
        const contentPreview = content.length > 2000 ? content.substring(0, 2000) + '...' : content;

        document.getElementById('articleDetailContent').innerHTML = `
            <div class="article-detail-page">
                <h2 style="color:var(--text);font-size:1.1rem;margin-bottom:4px">${escapeHtml(data.title)}</h2>
                <div class="meta-row">
                    ${escapeHtml(data.source || '')} &middot; ${fmtDate(data.published_date)} &middot; ${escapeHtml(data.source_type || '')}
                    ${data.is_low_confidence ? ' &middot; Low confidence' : ''}
                    ${data.url ? ` &middot; <a href="${articleUrl(data.url, 'articles', data.resolved_url)}" target="_blank" rel="noopener" style="color:var(--accent)">Open article &rarr;</a>` : ''}
                </div>

                <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:20px">
                    <div class="detail-section">
                        <h3>Sentiment</h3>
                        <span class="pill ${sentClass}" style="font-size:0.85rem">${sentText}</span>
                        <span style="color:var(--text);margin-left:8px;font-weight:600">${scoreText} / 5.0</span>
                    </div>
                </div>

                <div class="detail-section">
                    <h3>States & Locations</h3>
                    <div>${statesHtml}</div>
                </div>

                ${data.is_low_confidence ? `<div class="detail-section"><h3>Quality Flag</h3><div class="detail-text">Low confidence: this article still uses a thin Google News wrapper record, so it remains visible in raw browsing but is excluded from summary metrics until upgraded.</div></div>` : ''}

                <div class="detail-section">
                    <h3>Topics</h3>
                    <div>${topicsHtml}</div>
                </div>

                <div class="detail-section">
                    <h3>Entities</h3>
                    <div>${entitiesHtml}</div>
                </div>

                ${data.key_claims ? `<div class="detail-section"><h3>Key Claims</h3><div class="detail-text">${escapeHtml(data.key_claims)}</div></div>` : ''}

                ${data.sentiment_justification ? `<div class="detail-section"><h3>Sentiment Justification</h3><div class="detail-text">${escapeHtml(data.sentiment_justification)}</div></div>` : ''}

                ${contentPreview ? `<div class="detail-section"><h3>Content</h3><div class="detail-text" style="font-size:0.85rem">${escapeHtml(contentPreview)}</div></div>` : ''}

                <details style="margin-top:16px">
                    <summary style="color:var(--muted);cursor:pointer;font-size:0.8rem">Raw Analysis JSON</summary>
                    <pre class="raw-json">${escapeHtml(JSON.stringify(data, null, 2))}</pre>
                </details>
            </div>
        `;
    } catch (err) {
        document.getElementById('articleDetailContent').innerHTML =
            `<span class="task-error">Error loading article: ${escapeHtml(err.message)}</span>`;
    }
}

function showArticleList() {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById('page-articles').classList.add('active');
}

// ══════════════════════════════════════════════
//  CONFIG / KEYWORD VISIBILITY (System page)
// ══════════════════════════════════════════════

async function renderConfigSection() {
    const container = document.getElementById('configSection');
    if (!container) return;

    try {
        const [topicsData, kwData] = await Promise.all([
            fetchJSON('/api/config/topics'),
            fetchJSON('/api/config/keywords'),
        ]);

        let html = '<div class="sh" style="margin-top:24px">Topics</div>';
        html += '<table class="dt"><thead><tr><th>Key</th><th>Label</th><th>Description</th></tr></thead><tbody>';
        for (const t of (topicsData.topics || [])) {
            html += `<tr><td><span class="pill pill-topic">${escapeHtml(t.key)}</span></td><td>${escapeHtml(t.label)}</td><td style="color:var(--muted)">${escapeHtml(t.description)}</td></tr>`;
        }
        html += '</tbody></table>';

        html += '<div class="sh" style="margin-top:24px">Nationwide Keywords</div>';
        const nk = kwData.nationwide_keywords || {};
        html += '<div style="margin-bottom:8px"><strong style="color:var(--muted);font-size:0.75rem">PRIMARY:</strong> ';
        html += (nk.primary || []).map(k => `<span class="pill pill-topic">${escapeHtml(k)}</span>`).join(' ');
        html += '</div>';
        html += '<div style="margin-bottom:8px"><strong style="color:var(--muted);font-size:0.75rem">COMPANIES:</strong> ';
        html += (nk.companies || []).map(k => `<span class="pill pill-entity">${escapeHtml(k)}</span>`).join(' ');
        html += '</div>';
        html += '<div style="margin-bottom:12px"><strong style="color:var(--muted);font-size:0.75rem">SECONDARY:</strong> ';
        html += (nk.secondary || []).map(k => `<span class="pill pill-loc">${escapeHtml(k)}</span>`).join(' ');
        html += '</div>';

        const nq = kwData.nationwide_queries || [];
        if (nq.length) {
            html += '<div class="sh" style="margin-top:16px">Nationwide Search Queries</div>';
            html += `<div style="margin-bottom:12px">${nq.map(q => `<code style="color:var(--accent);font-size:0.8rem;margin-right:8px;display:inline-block;margin-bottom:4px">${escapeHtml(q)}</code>`).join('')}</div>`;
        }

        for (const [stateKey, cfg] of Object.entries(kwData.priority_states || {})) {
            const kw = cfg.keywords || {};
            html += `<div class="sh" style="margin-top:16px">${capitalize(stateKey)} (Priority State)</div>`;
            if (kw.primary && kw.primary.length) {
                html += `<div style="margin-bottom:4px"><strong style="color:var(--muted);font-size:0.75rem">PRIMARY:</strong> ${kw.primary.map(k => `<span class="pill pill-topic">${escapeHtml(k)}</span>`).join(' ')}</div>`;
            }
            if (kw.companies && kw.companies.length) {
                html += `<div style="margin-bottom:4px"><strong style="color:var(--muted);font-size:0.75rem">COMPANIES:</strong> ${kw.companies.map(k => `<span class="pill pill-entity">${escapeHtml(k)}</span>`).join(' ')}</div>`;
            }
            if (kw.secondary && kw.secondary.length) {
                html += `<div style="margin-bottom:4px"><strong style="color:var(--muted);font-size:0.75rem">SECONDARY:</strong> ${kw.secondary.map(k => `<span class="pill pill-loc">${escapeHtml(k)}</span>`).join(' ')}</div>`;
            }
            if (cfg.web_search_queries && cfg.web_search_queries.length) {
                html += `<div style="margin-bottom:4px"><strong style="color:var(--muted);font-size:0.75rem">SEARCH QUERIES:</strong> ${cfg.web_search_queries.map(q => `<code style="color:var(--accent);font-size:0.8rem;margin-right:8px">${escapeHtml(q)}</code>`).join('')}</div>`;
            }
        }

        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = `<span class="task-error">Error loading config: ${escapeHtml(err.message)}</span>`;
    }
}

// ── Init ──
document.addEventListener('DOMContentLoaded', async () => {
    await initStateDropdowns();
    renderDashboard();
});

// ── Keyboard Accessibility ──
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        const modal = document.getElementById('digestModal');
        if (modal && modal.classList.contains('show')) {
            modal.classList.remove('show');
        }
    }
});
