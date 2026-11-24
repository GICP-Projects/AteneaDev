(function () {
    "use strict";

    var root = document.querySelector("[data-analytics]");
    var panel = root && root.querySelector("[data-entity-panel]");
    if (!panel) return;
    var labels = JSON.parse(document.getElementById("entity-cloud-labels").textContent);
    var number = new Intl.NumberFormat(document.documentElement.lang || "en");
    var frame = panel.querySelector("[data-entity-frame]");
    var cloud = panel.querySelector("[data-entity-cloud]");
    var order = panel.querySelector("[data-entity-order]");
    var status = panel.querySelector("[data-entity-status]");
    var readout = panel.querySelector("[data-entity-readout]");
    var controller = null;
    var requestId = 0;
    var layoutId = 0;
    var selection = null;
    var responseData = null;
    var waiting = true;
    var loading = false;
    var loadTimer;
    var canvas = document.createElement("canvas");
    var context = canvas.getContext("2d");

    function typeValues() {
        return Array.from(panel.querySelectorAll("[data-entity-type]:checked")).map(function (input) { return input.value; });
    }
    function updateTypeSummary() {
        var values = typeValues();
        panel.querySelector("[data-entity-type-summary]").textContent = !values.length ? labels.allTypes : values.length <= 2 ? values.join(", ") : values.length + " " + labels.types;
    }
    function describe(row) {
        return row.name + " · " + row.type + " · " + number.format(row.message_count) + " " + labels.messages.toLowerCase() + " · " + number.format(row.appearance_count) + " " + labels.appearances.toLowerCase();
    }
    function link(row) {
        var anchor = document.createElement("a");
        anchor.href = row.messages_url;
        anchor.textContent = row.name;
        anchor.title = describe(row);
        anchor.setAttribute("aria-label", describe(row));
        anchor.addEventListener("focus", function () { readout.textContent = describe(row); });
        anchor.addEventListener("pointerenter", function () { readout.textContent = describe(row); });
        return anchor;
    }
    function renderTable() {
        var body = panel.querySelector("[data-entity-table]");
        body.replaceChildren();
        panel.querySelectorAll("thead th").forEach(function (header, index) {
            header.removeAttribute("aria-sort");
            if (index === (responseData.order === "messages" ? 2 : 3)) header.setAttribute("aria-sort", "descending");
        });
        responseData.results.forEach(function (row) {
            var tr = document.createElement("tr");
            var name = document.createElement("td");
            name.appendChild(link(row));
            tr.appendChild(name);
            [row.type, number.format(row.message_count), number.format(row.appearance_count)].forEach(function (value) {
                var cell = document.createElement("td"); cell.textContent = value; tr.appendChild(cell);
            });
            body.appendChild(tr);
        });
        if (!responseData.results.length) {
            var tr = document.createElement("tr"), cell = document.createElement("td");
            cell.colSpan = 4; cell.textContent = labels.empty; tr.appendChild(cell); body.appendChild(tr);
        }
    }
    function renderCloud() {
        if (!responseData || !context || frame.clientWidth < 1) return;
        var currentLayout = ++layoutId;
        var rows = responseData.results;
        var size = frame.clientWidth;
        var metric = responseData.order === "messages" ? "message_count" : "appearance_count";
        var maximum = rows.length ? rows[0][metric] : 1;
        var minimum = rows.length ? rows[rows.length - 1][metric] : 1;
        var fontFamily = getComputedStyle(cloud).fontFamily;
        var placed = [];
        var fragment = document.createDocumentFragment();
        var index = 0;
        cloud.replaceChildren();
        panel.querySelector("[data-entity-empty]").hidden = Boolean(rows.length);

        // Pack a few terms per frame so controls remain usable while laying out.
        function pack() {
            if (currentLayout !== layoutId) return;
            var end = Math.min(index + 4, rows.length);
            for (; index < end; index++) {
                var row = rows[index];
                var ratio = maximum === minimum ? 0.5 : (Math.sqrt(row[metric]) - Math.sqrt(minimum)) / (Math.sqrt(maximum) - Math.sqrt(minimum));
                var fontSize = 16 + ratio * Math.min(38, size * 0.065);
                context.font = "700 " + fontSize + "px " + fontFamily;
                var characters = Array.from(row.name);
                var text = characters.slice(0, 80).join("") + (characters.length > 80 ? "…" : "");
                var maxWidth = size - 32;
                while (fontSize > 16 && context.measureText(text).width + 16 > maxWidth) {
                    fontSize -= 2; context.font = "700 " + fontSize + "px " + fontFamily;
                }
                if (context.measureText(text).width + 16 > maxWidth) {
                    while (text.length > 1 && context.measureText(text + "…").width + 16 > maxWidth) text = text.slice(0, -1);
                    text += "…";
                }
                var width = Math.max(44, Math.ceil(context.measureText(text).width) + 16);
                var height = Math.max(44, Math.ceil(fontSize * 1.15) + 12);
                var rectangle = null;
                for (var step = 0; step < 1400; step++) {
                    var angle = step * 0.4;
                    var radius = step * 0.45;
                    var x = (size - width) / 2 + Math.cos(angle) * radius;
                    var y = (size - height) / 2 + Math.sin(angle) * radius;
                    if (x < 8 || y < 8 || x + width > size - 8 || y + height > size - 8) continue;
                    if (placed.some(function (other) {
                        return x < other.x + other.width + 2 && x + width + 2 > other.x && y < other.y + other.height + 2 && y + height + 2 > other.y;
                    })) continue;
                    rectangle = {x: x, y: y, width: width, height: height}; break;
                }
                if (!rectangle) continue; // Every term remains available in the table.
                placed.push(rectangle);
                var item = document.createElement("li");
                item.style.left = rectangle.x + "px"; item.style.top = rectangle.y + "px";
                item.style.width = width + "px"; item.style.height = height + "px";
                item.style.fontSize = fontSize + "px";
                var anchor = link(row); anchor.textContent = text;
                item.appendChild(anchor); fragment.appendChild(item);
            }
            if (index < rows.length) { window.requestAnimationFrame(pack); return; }
            cloud.appendChild(fragment);
            panel.querySelector("[data-entity-coverage]").textContent = rows.length ? labels.showing + " " + placed.length + " " + labels.of + " " + rows.length + (responseData.has_more ? " · " + labels.more : "") : "";
        }
        window.requestAnimationFrame(pack);
    }
    function showError(text) {
        panel.querySelector("[data-entity-error-text]").textContent = text;
        panel.querySelector("[data-entity-error]").hidden = false;
        status.textContent = responseData ? labels.failedStale : labels.error;
        if (!responseData) {
            panel.querySelector("[data-entity-placeholder]").hidden = true;
            panel.querySelector("[data-entity-empty]").hidden = false;
            panel.querySelector("[data-entity-empty] strong").textContent = labels.error;
            panel.querySelector("[data-entity-empty] p").textContent = "";
            panel.querySelector("[data-entity-table] td").textContent = labels.error;
        }
    }
    function load() {
        window.clearTimeout(loadTimer);
        if (!selection || waiting) return;
        if (controller) controller.abort();
        controller = new AbortController();
        var currentRequest = ++requestId;
        var selectedRange = selection;
        var params = new URLSearchParams(selectedRange.params);
        params.set("order", order.value);
        typeValues().forEach(function (value) { params.append("entity_type", value); });
        loading = true;
        panel.classList.add("is-loading");
        panel.setAttribute("aria-busy", "true");
        panel.querySelector("[data-entity-error]").hidden = true;
        status.textContent = responseData ? labels.stale : labels.loading;
        panel.querySelector("[data-entity-retry]").disabled = true;
        fetch(panel.dataset.endpoint + "?" + params.toString(), {
            credentials: "same-origin", headers: {Accept: "application/json"}, signal: controller.signal
        }).then(function (response) {
            if (response.redirected || response.status === 401 || response.status === 403) throw new Error(labels.session);
            if (!response.ok) throw new Error(labels.error);
            return response.json();
        }).then(function (data) {
            if (currentRequest !== requestId) return;
            if (!Array.isArray(data.results)) throw new Error(labels.error);
            responseData = data;
            panel.querySelector("[data-entity-placeholder]").hidden = true;
            panel.querySelector("[data-entity-empty] strong").textContent = labels.empty;
            panel.querySelector("[data-entity-empty] p").textContent = labels.emptyHint;
            panel.querySelector("[data-entity-selection]").textContent = selectedRange.start + " — " + selectedRange.end;
            readout.textContent = "";
            status.textContent = labels.showing + " " + data.results.length;
            renderTable(); renderCloud();
        }).catch(function (error) {
            if (currentRequest !== requestId || error.name === "AbortError") return;
            showError(error.message === labels.session ? labels.session : labels.error);
        }).finally(function () {
            if (currentRequest !== requestId) return;
            loading = false;
            panel.classList.remove("is-loading");
            panel.setAttribute("aria-busy", "false");
            panel.querySelector("[data-entity-retry]").disabled = false;
        });
    }
    root.addEventListener("atenea:statistics-loading", function () {
        window.clearTimeout(loadTimer);
        waiting = true; loading = false; requestId++;
        if (controller) controller.abort();
        panel.classList.add("is-loading");
        panel.setAttribute("aria-busy", "true");
        panel.querySelector("[data-entity-error]").hidden = true;
        status.textContent = responseData ? labels.stale : labels.waiting;
    });
    root.addEventListener("atenea:statistics-loaded", function (event) {
        waiting = false; selection = event.detail; load();
    });
    root.addEventListener("atenea:statistics-failed", function () {
        waiting = false;
        panel.classList.remove("is-loading");
        panel.setAttribute("aria-busy", "false");
        panel.querySelector("[data-entity-retry]").disabled = false;
        showError(labels.error);
    });
    panel.querySelectorAll("[data-entity-type]").forEach(function (input) {
        input.addEventListener("change", function () {
            updateTypeSummary(); window.clearTimeout(loadTimer);
            loadTimer = window.setTimeout(load, 250);
        });
    });
    panel.querySelector("[data-entity-all-types]").addEventListener("click", function () {
        panel.querySelectorAll("[data-entity-type]").forEach(function (input) { input.checked = false; });
        updateTypeSummary(); load();
    });
    order.addEventListener("change", load);
    panel.querySelector("[data-entity-retry]").addEventListener("click", function () {
        if (loading) return;
        if (!selection) root.querySelector("[data-analytics-form]").requestSubmit();
        else load();
    });
    if (window.ResizeObserver) {
        var previousWidth = 0;
        new ResizeObserver(function () {
            if (Math.abs(frame.clientWidth - previousWidth) < 1) return;
            previousWidth = frame.clientWidth; renderCloud();
        }).observe(frame);
    }
    panel.querySelector("[data-entity-types]").addEventListener("keydown", function (event) {
        if (event.key === "Escape") { this.open = false; this.querySelector("summary").focus(); }
    });
    updateTypeSummary();
}());
