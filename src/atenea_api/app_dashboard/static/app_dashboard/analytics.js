(function () {
    "use strict";

    var root = document.querySelector("[data-analytics]");
    if (!root) return;
    var form = root.querySelector("[data-analytics-form]");
    var labels = JSON.parse(document.getElementById("analytics-labels").textContent);
    var presetRanges = JSON.parse(document.getElementById("analytics-presets").textContent);
    var locale = document.documentElement.lang || "en";
    var numberFormat = new Intl.NumberFormat(locale);
    var meanFormat = new Intl.NumberFormat(locale, {maximumFractionDigits: 1});
    var scoreFormat = new Intl.NumberFormat(locale, {minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "exceptZero"});
    var compactFormat = new Intl.NumberFormat(locale, {notation: "compact", maximumFractionDigits: 1});
    var dateFormat = new Intl.DateTimeFormat(locale, {day: "numeric", month: "short", year: "numeric", timeZone: "UTC"});
    var tickFormat = new Intl.DateTimeFormat(locale, {day: "numeric", month: "short", timeZone: "UTC"});
    var monthTickFormat = new Intl.DateTimeFormat(locale, {month: "short", timeZone: "UTC"});
    var monthYearTickFormat = new Intl.DateTimeFormat(locale, {month: "short", year: "numeric", timeZone: "UTC"});
    var DAY = 86400000;
    var controller = null;
    var requestNumber = 0;
    var payload = null;
    var appliedParams = null;
    var pendingParams = null;
    var retryNeeded = false;
    var lastLoadedStatus = "";
    var zscoreVisible = true;
    try { zscoreVisible = window.localStorage.getItem("atenea-analytics-zscore-visible") !== "false"; } catch (error) { /* Optional preference. */ }
    var charts = {};
    var tablePage = 0;
    var PAGE_SIZE = 50;
    var zoomOrigin = null;
    var committedZoomOrigin = null;
    var filterPanel = root.querySelector("[data-filter-panel]");
    var errorToast = root.querySelector("[data-analytics-error]");
    var errorTimer = null;
    var defaultValues = {};
    Array.from(form.elements).forEach(function (field) { if (field.name) defaultValues[field.name] = field.value; });

    function field(name) { return form.elements.namedItem(name); }
    function date(value) { return new Date(value + "T12:00:00Z"); }
    function dayNumber(value) { return Date.parse(value + "T00:00:00Z") / DAY; }
    function dayString(value) { return new Date(value * DAY).toISOString().slice(0, 10); }
    function formatDate(value) { return dateFormat.format(date(value)); }
    function periodLabel(point) {
        return point.range_start === point.range_end ? formatDate(point.range_start) : formatDate(point.range_start) + " — " + formatDate(point.range_end);
    }
    function selectionSpansYears() { return payload.start.slice(0, 4) !== payload.end.slice(0, 4); }
    function axisDateLabel(point) {
        if (payload.group_by === "year") return point.range_start.slice(0, 4);
        var formatter = payload.group_by === "month" ? (selectionSpansYears() ? monthYearTickFormat : monthTickFormat) : (selectionSpansYears() ? dateFormat : tickFormat);
        return formatter.format(date(point.range_start));
    }
    function params() {
        var result = new URLSearchParams();
        new FormData(form).forEach(function (value, name) { if (String(value).trim()) result.append(name, value); });
        return result;
    }
    function setStatus(text) {
        root.querySelector("[data-analytics-status-text]").textContent = text;
        root.querySelector("[data-analytics-status]").hidden = !text;
    }
    function filterSignature(selection) {
        var filters = new URLSearchParams(selection);
        if (filters.get("range") !== "all") filters.delete("range");
        filters.sort();
        return filters.toString();
    }
    function updateControls() {
        root.querySelectorAll("[data-range]").forEach(function (button) { button.setAttribute("aria-pressed", button.dataset.range === field("range").value ? "true" : "false"); });
        var busy = root.classList.contains("is-loading");
        root.querySelector("[data-zoom-tools]").hidden = !committedZoomOrigin;
        root.querySelector("[data-reset-zoom]").disabled = busy || !committedZoomOrigin;
        var baseline = busy ? pendingParams : appliedParams;
        var dirty = Boolean(baseline && filterSignature(params()) !== filterSignature(baseline));
        root.classList.toggle("is-dirty", dirty);
        root.querySelector(".analytics-refresh").disabled = busy ? !dirty : !dirty && !retryNeeded;
        var note = root.querySelector("[data-stale-note]");
        note.hidden = !(payload && (busy || retryNeeded));
        note.textContent = retryNeeded ? labels.failedStale : labels.stale;
        if (!busy && !retryNeeded) setStatus(lastLoadedStatus || labels.loading);
    }
    function setWindow(start, end, range) {
        field("createdat_min").value = dayString(start);
        field("createdat_max").value = dayString(end);
        field("range").value = range || "custom";
        updateControls();
    }
    function hideError() {
        window.clearTimeout(errorTimer);
        errorToast.hidden = true;
        errorToast.classList.remove("is-dismissing");
    }
    function dismissError() {
        window.clearTimeout(errorTimer);
        if (errorToast.hidden) return;
        if (errorToast.contains(document.activeElement)) root.querySelector(".analytics-refresh").focus();
        if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) hideError();
        else errorToast.classList.add("is-dismissing");
    }
    function scheduleErrorDismiss() {
        window.clearTimeout(errorTimer);
        if (!errorToast.hidden && !errorToast.classList.contains("is-dismissing") && !errorToast.matches(":hover, :focus-within")) {
            errorTimer = window.setTimeout(dismissError, 2000);
        }
    }
    function clearErrors() {
        hideError();
        root.querySelector("[data-analytics-login]").hidden = true;
        root.querySelectorAll("[data-field-error]").forEach(function (element) { element.textContent = ""; });
        root.querySelectorAll("[aria-invalid='true']").forEach(function (element) {
            element.removeAttribute("aria-invalid");
            var describedBy = (element.getAttribute("aria-describedby") || "").split(" ").filter(function (id) { return id.indexOf("analytics-error-") !== 0; }).join(" ");
            if (describedBy) element.setAttribute("aria-describedby", describedBy);
            else element.removeAttribute("aria-describedby");
        });
        // Initial server-side field errors become part of the live error summary.
        root.querySelectorAll(".field-error").forEach(function (element) { element.remove(); });
    }
    function showError(error) {
        retryNeeded = true;
        var messages = [];
        Object.keys(error.errors || {}).forEach(function (name) {
            var node = root.querySelector("[data-field-error='" + name + "']");
            var input = field(name);
            var text = error.errors[name].map(function (item) { return item.message; }).join(" ");
            if (node) {
                node.id = "analytics-error-" + name;
                node.textContent = text;
            }
            if (input && input.type === "hidden") {
                var editor = input.closest("[data-autocomplete-editor]");
                if (editor) input = editor.querySelector("[data-autocomplete-entry]");
            }
            if (input) {
                if (filterPanel.contains(input)) filterPanel.open = true;
                input.setAttribute("aria-invalid", "true");
                if (node) input.setAttribute("aria-describedby", node.id);
            }
            if (text && messages.indexOf(text) === -1) messages.push(text);
        });
        var text = error.session ? labels.session : error.errors ? messages.join(" ") || labels.invalid : labels.error;
        root.querySelector("[data-analytics-error-text]").textContent = text;
        errorToast.hidden = false;
        root.querySelector("[data-analytics-login]").hidden = !error.session;
        scheduleErrorDismiss();
        setStatus(lastLoadedStatus);
        var stale = root.querySelector("[data-stale-note]");
        stale.hidden = !payload;
        stale.textContent = labels.failedStale;
        if (!payload) {
            root.querySelectorAll("[data-stat]").forEach(function (node) { node.textContent = "—"; });
            root.querySelectorAll("[data-chart]").forEach(function (node) { renderEmpty(node, labels.error, labels.emptyHint); });
            renderTable();
        }
    }
    function load(updateHistory) {
        if (controller) controller.abort();
        controller = new AbortController();
        var currentRequest = ++requestNumber;
        var selection = params();
        var selectionZoomOrigin = zoomOrigin;
        if (!selectionZoomOrigin && committedZoomOrigin && appliedParams &&
            selection.get("createdat_min") === appliedParams.get("createdat_min") &&
            selection.get("createdat_max") === appliedParams.get("createdat_max") &&
            selection.get("range") === appliedParams.get("range")) selectionZoomOrigin = committedZoomOrigin;
        var selectionSignature = filterSignature(selection);
        pendingParams = new URLSearchParams(selection);
        root.dispatchEvent(new CustomEvent("atenea:statistics-loading"));
        retryNeeded = false;
        clearErrors();
        root.classList.add("is-loading");
        root.querySelectorAll("[data-analytics-results]").forEach(function (node) { node.setAttribute("aria-busy", "true"); });
        setStatus(payload ? labels.updating : labels.loading);
        var stale = root.querySelector("[data-stale-note]");
        stale.hidden = !payload;
        stale.textContent = labels.stale;
        updateControls();
        if (updateHistory && selection.toString() !== window.location.search.slice(1)) {
            window.history.pushState(null, "", window.location.pathname + "?" + selection.toString());
        }
        fetch(root.dataset.endpoint + "?" + selection.toString(), {
            credentials: "same-origin", headers: {Accept: "application/json"}, signal: controller.signal
        }).then(function (response) {
            if (response.redirected || response.status === 401 || response.status === 403) {
                var sessionError = new Error(labels.session);
                sessionError.session = true;
                throw sessionError;
            }
            return response.json().then(function (data) {
                if (!response.ok) {
                    var error = new Error(labels.error);
                    if (response.status === 400) error.errors = data.errors;
                    throw error;
                }
                return data;
            });
        }).then(function (data) {
            if (currentRequest !== requestNumber) return;
            if (!Array.isArray(data.series) || !data.summary) throw new Error("Invalid statistics response");
            payload = data;
            var draftUnchanged = filterSignature(params()) === selectionSignature;
            if (selection.get("range") === "all") {
                selection.set("createdat_min", data.start);
                selection.set("createdat_max", data.end);
                if (draftUnchanged) {
                    field("createdat_min").value = data.start;
                    field("createdat_max").value = data.end;
                    window.history.replaceState(null, "", window.location.pathname + "?" + selection.toString());
                }
            }
            appliedParams = new URLSearchParams(selection);
            pendingParams = new URLSearchParams(selection);
            committedZoomOrigin = selectionZoomOrigin && (data.start !== selectionZoomOrigin.start || data.end !== selectionZoomOrigin.end) ? selectionZoomOrigin : null;
            if (draftUnchanged) zoomOrigin = committedZoomOrigin;
            if (data.presets) presetRanges = data.presets;
            root.dataset.today = data.today;
            root.classList.add("has-data");
            stale.hidden = true;
            tablePage = 0;
            render();
            root.dispatchEvent(new CustomEvent("atenea:statistics-loaded", {
                detail: {params: appliedParams.toString(), start: data.start, end: data.end}
            }));
            var time = new Intl.DateTimeFormat(locale, {hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: data.timezone}).format(new Date(data.generated_at));
            lastLoadedStatus = labels.updated + " " + time + " · " + data.timezone;
            setStatus(lastLoadedStatus);
        }).catch(function (error) {
            if (currentRequest !== requestNumber || error.name === "AbortError") return;
            showError(error);
            root.dispatchEvent(new CustomEvent("atenea:statistics-failed"));
        }).finally(function () {
            if (currentRequest !== requestNumber) return;
            root.classList.remove("is-loading");
            pendingParams = null;
            root.querySelectorAll("[data-analytics-results]").forEach(function (node) { node.setAttribute("aria-busy", "false"); });
            updateControls();
        });
    }
    function markDirty() { updateControls(); }
    function svgElement(name, attrs, text) {
        var element = document.createElementNS("http://www.w3.org/2000/svg", name);
        Object.keys(attrs || {}).forEach(function (key) { element.setAttribute(key, attrs[key]); });
        if (text !== undefined) element.textContent = text;
        return element;
    }
    function append(svg, name, attrs, text) { var element = svgElement(name, attrs, text); svg.appendChild(element); return element; }
    function renderEmpty(frame, title, hint) {
        frame.replaceChildren();
        var empty = document.createElement("div");
        empty.className = "analytics-empty";
        var icon = svgElement("svg", {viewBox: "0 0 32 32", "aria-hidden": "true"});
        append(icon, "path", {d: "M4 4v24h24M9 22v-5m6 5V9m6 13v-9m6 9v-3", "stroke-width": 2});
        var heading = document.createElement("strong"); heading.textContent = title;
        var detail = document.createElement("p"); detail.textContent = hint;
        empty.append(icon, heading, detail);
        frame.appendChild(empty);
    }
    function renderChart(key) {
        var frame = root.querySelector("[data-chart='" + key + "']");
        if (!payload.summary.total_messages) { renderEmpty(frame, labels.empty, labels.emptyHint); return; }
        var points = payload.series;
        var width = Math.max(280, frame.clientWidth);
        var height = parseFloat(getComputedStyle(frame).minHeight) || 260;
        var left = 46, right = width - (zscoreVisible ? 48 : 12), top = 25, bottom = height - 32;
        var values = points.map(function (point) { return point.count; });
        var maximum = Math.max.apply(null, values.map(function (value) { return Math.abs(value || 0); }));
        var minY, maxY, ticks;
        var rough = Math.max(1, maximum / 4);
        var power = Math.pow(10, Math.floor(Math.log10(rough)));
        var factor = rough / power;
        var step = (factor <= 1 ? 1 : factor <= 2 ? 2 : factor <= 5 ? 5 : 10) * power;
        minY = 0;
        maxY = Math.ceil(maximum * 1.1 / step) * step;
        ticks = [];
        for (var tick = 0; tick <= maxY; tick += step) ticks.push(tick);
        function x(index) { return points.length === 1 ? (left + right) / 2 : left + index / (points.length - 1) * (right - left); }
        function y(value) { return bottom - (value - minY) / (maxY - minY) * (bottom - top); }
        var scores = points.map(function (point) { return point.z_score; });
        var scoreMax = Math.max(3, Math.ceil(Math.max.apply(null, scores.map(function (value) { return Math.abs(value || 0); }))));
        function yScore(value) { return bottom - (value + scoreMax) / (2 * scoreMax) * (bottom - top); }
        var svg = svgElement("svg", {viewBox: "0 0 " + width + " " + height, tabindex: 0, role: "img", "aria-label": (zscoreVisible ? labels.combined : labels.frequency) + ". " + labels.zoom});
        append(svg, "title", {}, zscoreVisible ? labels.combined : labels.frequency);
        if (zscoreVisible) append(svg, "rect", {x: left, y: yScore(2), width: right - left, height: yScore(-2) - yScore(2), class: "chart-score-band", "aria-hidden": "true"});
        append(svg, "text", {x: left, y: 11, class: "chart-axis-label"}, labels.messages);
        ticks.forEach(function (value) {
            append(svg, "line", {x1: left, x2: right, y1: y(value), y2: y(value), class: value === 0 ? "chart-zero" : "chart-grid"});
            append(svg, "text", {x: left - 9, y: y(value) + 3, "text-anchor": "end", class: "chart-axis-label"}, compactFormat.format(value));
        });
        var tickSpacing = selectionSpansYears() && payload.group_by !== "year" ? 140 : 110;
        var tickCount = Math.min(points.length, Math.max(2, Math.floor((right - left) / tickSpacing)));
        var indices = [];
        for (var i = 0; i < tickCount; i++) {
            var index = tickCount === 1 ? 0 : Math.round(i * (points.length - 1) / (tickCount - 1));
            if (indices.indexOf(index) === -1) indices.push(index);
        }
        indices.forEach(function (index) {
            var value = axisDateLabel(points[index]);
            append(svg, "text", {x: x(index), y: height - 8, "text-anchor": points.length === 1 ? "middle" : index === 0 ? "start" : index === points.length - 1 ? "end" : "middle", class: "chart-axis-label"}, value);
        });
        var area = "M" + x(0) + " " + bottom + points.map(function (point, index) { return " L" + x(index) + " " + y(point.count); }).join("") + " L" + x(points.length - 1) + " " + bottom + " Z";
        if (points.length > 1) append(svg, "path", {d: area, class: "chart-area"});
        append(svg, "line", {x1: left, x2: right, y1: y(payload.summary.mean_count), y2: y(payload.summary.mean_count), class: "chart-reference chart-mean"});
        function drawSeries(series, scale, name) {
            var segments = [], segment = [];
            series.forEach(function (value, index) {
                if (value === null) { if (segment.length) segments.push(segment); segment = []; }
                else segment.push({value: value, index: index});
            });
            if (segment.length) segments.push(segment);
            segments.forEach(function (items) {
                if (items.length === 1) append(svg, "circle", {cx: x(items[0].index), cy: scale(items[0].value), r: 3, class: "chart-point chart-point--" + name, "data-series": name});
                else append(svg, "path", {d: items.map(function (point, index) { return (index ? "L" : "M") + x(point.index) + " " + scale(point.value); }).join(" "), class: "chart-series chart-series--" + name, "data-series": name});
            });
        }
        drawSeries(values, y, "frequency");
        if (zscoreVisible) {
            append(svg, "text", {x: right, y: 11, "text-anchor": "end", class: "chart-axis-label chart-score-label", "data-score-axis": "true"}, labels.zscore);
            [-scoreMax, -2, 0, 2, scoreMax].filter(function (value, index, all) { return all.indexOf(value) === index; }).forEach(function (value) {
                append(svg, "text", {x: right + 9, y: yScore(value) + 3, class: "chart-axis-label chart-score-label", "data-score-axis": "true"}, String(value));
            });
            [-2, 2].forEach(function (value) { append(svg, "line", {x1: left, x2: right, y1: yScore(value), y2: yScore(value), class: "chart-reference chart-score-reference"}); });
            drawSeries(scores, yScore, "zscore");
            points.forEach(function (point, index) {
                if (point.z_score === null || Math.abs(point.z_score) < 2) return;
                var px = x(index), py = yScore(point.z_score);
                append(svg, "path", {d: "M" + px + " " + (py - 5) + "l5 5 -5 5 -5 -5Z", class: "chart-unusual"});
            });
        }
        var brush = append(svg, "rect", {x: left, y: top, width: 0, height: bottom - top, class: "chart-brush", visibility: "hidden"});
        var crosshair = append(svg, "line", {x1: left, x2: left, y1: top, y2: bottom, class: "chart-crosshair", visibility: "hidden"});
        var focusPoint = append(svg, "circle", {cx: left, cy: top, r: 4, class: "chart-focus-point", visibility: "hidden"});
        var focusScore = append(svg, "circle", {cx: left, cy: top, r: 4, class: "chart-focus-point chart-focus-score", visibility: "hidden"});
        var tooltip = document.createElement("div"); tooltip.className = "analytics-chart-tooltip"; tooltip.hidden = true;
        var tooltipDate = document.createElement("strong");
        var tooltipCount = document.createElement("span"); tooltipCount.appendChild(document.createTextNode(labels.messages));
        var tooltipCountValue = document.createElement("b"); tooltipCount.appendChild(tooltipCountValue);
        var tooltipScore = document.createElement("span"); tooltipScore.appendChild(document.createTextNode(labels.zscore));
        var tooltipScoreValue = document.createElement("b"); tooltipScore.appendChild(tooltipScoreValue);
        tooltipScore.hidden = !zscoreVisible;
        tooltip.append(tooltipDate, tooltipCount, tooltipScore);
        frame.replaceChildren(svg, tooltip);
        var chart = {svg: svg, frame: frame, x: x, y: y, yScore: yScore, values: values, crosshair: crosshair, focusPoint: focusPoint, focusScore: focusScore, tooltip: tooltip, tooltipDate: tooltipDate, tooltipCount: tooltipCountValue, tooltipScore: tooltipScoreValue, active: 0};
        charts[key] = chart;
        function pointerIndex(event) {
            var rect = svg.getBoundingClientRect();
            var px = (event.clientX - rect.left) * width / rect.width;
            return Math.min(points.length - 1, Math.max(0, Math.round((px - left) / (right - left) * (points.length - 1))));
        }
        var dragStart = null;
        svg.addEventListener("pointermove", function (event) {
            var index = pointerIndex(event);
            inspect(index, key, false);
            if (dragStart !== null) {
                brush.setAttribute("visibility", "visible");
                brush.setAttribute("x", Math.min(x(dragStart), x(index)));
                brush.setAttribute("width", Math.abs(x(index) - x(dragStart)));
            }
        });
        svg.addEventListener("pointerdown", function (event) {
            if (event.button !== 0 || points.length < 2) return;
            dragStart = pointerIndex(event);
            svg.setPointerCapture(event.pointerId);
            if (event.pointerType === "mouse") { event.preventDefault(); svg.focus({preventScroll: true}); }
        });
        svg.addEventListener("pointerup", function (event) {
            if (dragStart === null) return;
            var end = pointerIndex(event), start = dragStart;
            dragStart = null; brush.setAttribute("visibility", "hidden");
            if (svg.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId);
            if (start !== end) {
                rememberZoom();
                setWindow(dayNumber(points[Math.min(start, end)].range_start), dayNumber(points[Math.max(start, end)].range_end));
                load(true);
            }
        });
        svg.addEventListener("pointercancel", function () { dragStart = null; brush.setAttribute("visibility", "hidden"); hideInspection(); });
        svg.addEventListener("pointerleave", function () { if (dragStart === null && document.activeElement !== svg) hideInspection(); });
        svg.addEventListener("focus", function () { inspect(chart.active, key, true); });
        svg.addEventListener("blur", hideInspection);
        svg.addEventListener("keydown", function (event) {
            var index = chart.active;
            if (event.key === "ArrowRight") index++;
            else if (event.key === "ArrowLeft") index--;
            else if (event.key === "Home") index = 0;
            else if (event.key === "End") index = points.length - 1;
            else if (event.key === "+" || event.key === "=") { event.preventDefault(); zoom("in"); return; }
            else if (event.key === "-") { event.preventDefault(); zoom("out"); return; }
            else if (event.key === "Escape") { hideInspection(); return; }
            else return;
            event.preventDefault(); inspect(Math.min(points.length - 1, Math.max(0, index)), key, true);
        });
    }
    function inspect(index, activeKey, announce) {
        var point = payload.series[index];
        if (!point) return;
        Object.keys(charts).forEach(function (key) {
            var chart = charts[key], px = chart.x(index), value = chart.values[index];
            chart.active = index;
            chart.crosshair.setAttribute("x1", px); chart.crosshair.setAttribute("x2", px); chart.crosshair.setAttribute("visibility", "visible");
            chart.focusPoint.setAttribute("visibility", value === null ? "hidden" : "visible");
            if (value !== null) { chart.focusPoint.setAttribute("cx", px); chart.focusPoint.setAttribute("cy", chart.y(value)); }
            chart.focusScore.setAttribute("visibility", zscoreVisible && point.z_score !== null ? "visible" : "hidden");
            if (point.z_score !== null) { chart.focusScore.setAttribute("cx", px); chart.focusScore.setAttribute("cy", chart.yScore(point.z_score)); }
            chart.tooltip.hidden = key !== activeKey;
            if (key === activeKey) {
                chart.tooltipDate.textContent = periodLabel(point);
                chart.tooltipCount.textContent = numberFormat.format(point.count);
                chart.tooltipScore.textContent = point.z_score === null ? labels.noScore : scoreFormat.format(point.z_score);
                chart.tooltip.style.left = Math.max(0, Math.min(chart.frame.clientWidth - chart.tooltip.offsetWidth, px + 12)) + "px";
            }
        });
        root.querySelector("[data-readout='activity']").textContent = (selectionSpansYears() ? dateFormat : tickFormat).format(date(point.range_start)) + " · " + numberFormat.format(point.count) + (zscoreVisible ? " · z " + (point.z_score === null ? labels.noScore : scoreFormat.format(point.z_score)) : "");
        if (announce) root.querySelector("[data-chart-announcement]").textContent = periodLabel(point) + ". " + labels.messages + ": " + numberFormat.format(point.count) + (zscoreVisible ? ". " + labels.zscore + ": " + (point.z_score === null ? labels.noScore : scoreFormat.format(point.z_score)) : "");
    }
    function hideInspection() {
        Object.keys(charts).forEach(function (key) {
            charts[key].crosshair.setAttribute("visibility", "hidden"); charts[key].focusPoint.setAttribute("visibility", "hidden"); charts[key].tooltip.hidden = true;
            charts[key].focusScore.setAttribute("visibility", "hidden");
        });
        root.querySelectorAll("[data-readout]").forEach(function (node) { node.textContent = "—"; });
    }
    function renderCharts() {
        var focusedFrame = document.activeElement && document.activeElement.closest("[data-chart]");
        var focusedKey = focusedFrame && focusedFrame.dataset.chart;
        charts = {}; hideInspection(); renderChart("activity");
        if (focusedKey && charts[focusedKey]) charts[focusedKey].svg.focus({preventScroll: true});
    }
    function renderTable() {
        var body = root.querySelector("[data-period-table]"); body.replaceChildren();
        var series = payload && payload.summary.total_messages ? payload.series : [];
        var start = tablePage * PAGE_SIZE;
        var fragment = document.createDocumentFragment();
        series.slice(start, start + PAGE_SIZE).forEach(function (point) {
            var row = document.createElement("tr"); row.dataset.unusual = String(point.z_score !== null && Math.abs(point.z_score) >= 2);
            [periodLabel(point), numberFormat.format(point.count), point.z_score === null ? "—" : scoreFormat.format(point.z_score) + (Math.abs(point.z_score) >= 2 ? " · " + labels.unusual : "")].forEach(function (value) { var cell = document.createElement("td"); cell.textContent = value; row.appendChild(cell); });
            fragment.appendChild(row);
        });
        if (!series.length) { var row = document.createElement("tr"), cell = document.createElement("td"); cell.colSpan = 3; cell.textContent = labels.tableEmpty; row.appendChild(cell); fragment.appendChild(row); }
        body.appendChild(fragment);
        root.querySelector("[data-table-count]").textContent = series.length ? numberFormat.format(series.length) : "";
        root.querySelector("[data-table-page-label]").textContent = series.length ? labels.showing + " " + (start + 1) + "–" + Math.min(start + PAGE_SIZE, series.length) + " " + labels.of + " " + series.length : "";
        root.querySelector("[data-table-page='-1']").disabled = tablePage === 0;
        root.querySelector("[data-table-page='1']").disabled = start + PAGE_SIZE >= series.length;
    }
    function render() {
        root.querySelectorAll("[data-stat]").forEach(function (node) {
            var value = payload.summary[node.dataset.stat];
            var formatted = (node.dataset.stat === "mean_count" ? meanFormat : numberFormat).format(value);
            node.textContent = value >= 1000000 ? compactFormat.format(value) : formatted;
            node.title = formatted;
        });
        root.querySelector("[data-peak-date]").textContent = payload.summary.peak_date ? formatDate(payload.summary.peak_date) : "—";
        root.querySelector("[data-selection]").textContent = formatDate(payload.start) + " — " + formatDate(payload.end) + " · " + labels[payload.group_by] + " · " + payload.timezone;
        root.querySelector("[data-export]").disabled = !payload.summary.total_messages;
        var search = new URLSearchParams(appliedParams);
        search.delete("group_by"); search.delete("range"); search.set("mode", "retrieval"); search.set("search", "1");
        root.querySelector("[data-analytics-search]").href = root.dataset.searchUrl + "?" + search.toString();
        renderCharts(); renderTable(); updateControls();
    }
    function rememberZoom() {
        if (!zoomOrigin) zoomOrigin = committedZoomOrigin || {
            start: appliedParams ? appliedParams.get("createdat_min") : field("createdat_min").value,
            end: appliedParams ? appliedParams.get("createdat_max") : field("createdat_max").value,
            range: appliedParams ? appliedParams.get("range") : field("range").value
        };
    }
    function zoom(direction) {
        var start = dayNumber(field("createdat_min").value), end = dayNumber(field("createdat_max").value);
        if (!Number.isFinite(start) || !Number.isFinite(end) || start > end) return;
        var days = end - start + 1;
        if (direction === "in" && days <= 1) return;
        rememberZoom();
        var newDays = direction === "in" ? Math.max(1, Math.floor(days / 2)) : Math.min(365000, days * 2);
        var newStart = Math.max(dayNumber(field("createdat_min").min), Math.floor((start + end) / 2 - (newDays - 1) / 2));
        setWindow(newStart, newStart + newDays - 1); markDirty();
    }
    root.querySelector("[data-analytics-error-close]").addEventListener("click", dismissError);
    errorToast.addEventListener("animationend", function (event) {
        if (event.animationName === "analytics-toast-out") hideError();
    });
    errorToast.addEventListener("pointerenter", function () { window.clearTimeout(errorTimer); });
    errorToast.addEventListener("pointerleave", scheduleErrorDismiss);
    errorToast.addEventListener("focusin", function () { window.clearTimeout(errorTimer); });
    errorToast.addEventListener("focusout", function () { window.setTimeout(scheduleErrorDismiss, 0); });
    form.addEventListener("submit", function (event) { event.preventDefault(); load(true); });
    function filterChanged(event) {
        if (event.target.name === "createdat_min" || event.target.name === "createdat_max") {
            field("range").value = "custom"; zoomOrigin = null; updateControls();
        }
        if (event.target.name) markDirty();
    }
    form.addEventListener("change", filterChanged);
    form.addEventListener("input", filterChanged);
    root.querySelectorAll("[data-range]").forEach(function (button) {
        button.addEventListener("click", function () {
            var bounds = presetRanges[button.dataset.range];
            zoomOrigin = null; setWindow(dayNumber(bounds.start), dayNumber(bounds.end), button.dataset.range); load(true);
        });
    });
    root.querySelector("[data-reset-zoom]").addEventListener("click", function () {
        if (!committedZoomOrigin || root.classList.contains("is-loading")) return;
        var original = committedZoomOrigin; zoomOrigin = null;
        setWindow(dayNumber(original.start), dayNumber(original.end), original.range); load(true);
    });
    root.querySelector("[data-clear-filters]").addEventListener("click", function () {
        root.querySelectorAll("[data-autocomplete-editor] .list-token-remove").forEach(function (button) { button.click(); });
        field("tag_match").value = "any"; field("is_reply").value = ""; field("stored_since").value = "";
        markDirty();
    });
    function updateScoreToggle() {
        root.querySelector("[data-zscore-toggle]").setAttribute("aria-checked", String(zscoreVisible));
        root.querySelectorAll("[data-zscore-decoration]").forEach(function (node) { node.hidden = !zscoreVisible; });
    }
    root.querySelector("[data-zscore-toggle]").addEventListener("click", function () {
        zscoreVisible = !zscoreVisible;
        try { window.localStorage.setItem("atenea-analytics-zscore-visible", String(zscoreVisible)); } catch (error) { /* Optional preference. */ }
        updateScoreToggle();
        if (payload) renderCharts();
    });
    root.querySelectorAll("[data-table-page]").forEach(function (button) { button.addEventListener("click", function () { tablePage += Number(button.dataset.tablePage); renderTable(); }); });
    root.querySelector("[data-export]").addEventListener("click", function () {
        if (!payload) return;
        var rows = [[labels.csvPeriod, labels.csvEnd, labels.messages, labels.zscore]].concat(payload.series.map(function (point) { return [point.range_start, point.range_end, point.count, point.z_score === null ? "" : point.z_score]; }));
        var csv = rows.map(function (row) { return row.map(function (value) { return '"' + String(value).replace(/"/g, '""') + '"'; }).join(","); }).join("\r\n");
        var url = URL.createObjectURL(new Blob(["\uFEFF" + csv], {type: "text/csv;charset=utf-8"}));
        var link = document.createElement("a"); link.href = url; link.download = "atenea-activity-" + payload.start + "-" + payload.end + ".csv";
        document.body.appendChild(link); link.click(); link.remove(); window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    });
    window.addEventListener("popstate", function () {
        var selection = new URLSearchParams(window.location.search);
        // Use the editors' remove/add controls so their internal token state stays in sync.
        root.querySelectorAll("[data-autocomplete-editor]").forEach(function (editor) {
            var hidden = editor.querySelector("[data-list-value]");
            var values = selection.getAll(hidden.name).join(",").split(",").filter(Boolean);
            editor.querySelectorAll(".list-token-remove").forEach(function (button) { button.click(); });
            values.forEach(function (value) { editor.querySelector("[data-autocomplete-entry]").value = value; editor.querySelector("[data-autocomplete-add]").click(); });
        });
        ["range", "group_by", "tag_match", "is_reply", "stored_since", "createdat_min", "createdat_max"].forEach(function (name) { field(name).value = selection.has(name) ? selection.get(name) : defaultValues[name] || ""; });
        zoomOrigin = null; load(false);
    });
    var resizeTimer;
    if (window.ResizeObserver) {
        var chartFrame = root.querySelector("[data-chart='activity']");
        var lastWidth = chartFrame.clientWidth, lastHeight = chartFrame.clientHeight;
        new ResizeObserver(function (entries) {
            var width = entries[0].contentRect.width, height = entries[0].contentRect.height;
            if (Math.abs(width - lastWidth) < 1 && Math.abs(height - lastHeight) < 1) return;
            lastWidth = width; lastHeight = height;
            window.clearTimeout(resizeTimer);
            resizeTimer = window.setTimeout(function () { if (payload) renderCharts(); }, 100);
        }).observe(chartFrame);
    }
    updateScoreToggle();
    load(false);
}());
