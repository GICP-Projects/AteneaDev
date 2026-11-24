(function () {
    "use strict";

    function preferredTheme() {
        var storedTheme = null;
        try {
            storedTheme = window.localStorage.getItem("atenea-dashboard-theme");
        } catch (error) {
            /* Private browsing modes may deny localStorage access. */
        }
        if (storedTheme === "dark" || storedTheme === "light") return storedTheme;
        return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }

    function applyTheme(theme, persist) {
        if (theme !== "dark" && theme !== "light") theme = "light";
        document.documentElement.dataset.theme = theme;
        if (persist) {
            try {
                window.localStorage.setItem("atenea-dashboard-theme", theme);
            } catch (error) {
                /* Keeping the current theme still works when persistence is unavailable. */
            }
        }

        var toggle = document.querySelector("[data-theme-toggle]");
        if (!toggle) return;
        var nextTheme = theme === "dark" ? "light" : "dark";
        var label = toggle.querySelector("[data-theme-label]");
        if (label) label.textContent = toggle.dataset[nextTheme + "Text"];
        toggle.setAttribute("aria-label", toggle.dataset["use" + nextTheme.charAt(0).toUpperCase() + nextTheme.slice(1)]);
        toggle.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    }

    function bindThemeToggle() {
        var toggle = document.querySelector("[data-theme-toggle]");
        var theme = document.documentElement.dataset.theme || preferredTheme();
        applyTheme(theme, false);
        if (!toggle) return;
        toggle.addEventListener("click", function () {
            applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
        });
    }

    function bindConfirmations() {
        document.querySelectorAll("form[data-confirm]").forEach(function (form) {
            form.addEventListener("submit", function (event) {
                if (!window.confirm(form.dataset.confirm)) {
                    event.preventDefault();
                }
            });
        });
    }

    function bindDateFilters() {
        document.querySelectorAll("[data-date-filter]").forEach(function (filter) {
            var state = filter.querySelector("[data-date-filter-state]");
            var inputs = filter.querySelectorAll("input[type='date']");
            if (!state || !inputs.length) return;

            function refresh() {
                var hasSelection = Array.prototype.some.call(inputs, function (input) {
                    return Boolean(input.value);
                });
                state.textContent = hasSelection ? filter.dataset.selectedLabel : filter.dataset.anyLabel;
                filter.classList.toggle("has-selection", hasSelection);
            }

            inputs.forEach(function (input) {
                input.addEventListener("input", refresh);
                input.addEventListener("change", refresh);
            });
            refresh();
        });
    }

    function bindScheduleSections() {
        var form = document.querySelector("[data-schedule-form]");
        var select = form && form.querySelector("select[name='schedule_kind']");
        if (!select) return;
        var sections = document.querySelectorAll("[data-schedule-section]");
        var calendarOptions = form.querySelector("[data-calendar-options]");
        var lifecycleOptions = form.querySelector("[data-schedule-policy-options]");
        var repeatEvery = form.querySelector("input[name='repeat_every']");
        var repeatPeriod = form.querySelector("select[name='repeat_period']");
        var retryGrace = form.querySelector("input[name='retry_grace_seconds']");
        var retryHelp = retryGrace && retryGrace.closest(".form-field")
            ? retryGrace.closest(".form-field").querySelector(".help-text")
            : null;
        var retryBaseHelp = retryHelp && retryHelp.textContent;
        var periodSeconds = {
            microseconds: 0.000001,
            milliseconds: 0.001,
            seconds: 1,
            minutes: 60,
            hours: 3600,
            days: 86400,
            weeks: 604800
        };

        function refreshRetryLimit() {
            if (!retryGrace) return;
            if (!repeatEvery || !repeatPeriod || select.value !== "repeat") {
                retryGrace.removeAttribute("max");
                if (retryHelp && retryBaseHelp) retryHelp.textContent = retryBaseHelp;
                return;
            }
            var every = Number(repeatEvery.value);
            var unit = periodSeconds[repeatPeriod.value];
            if (!Number.isFinite(every) || every < 1 || !unit) {
                retryGrace.removeAttribute("max");
                if (retryHelp && retryBaseHelp) retryHelp.textContent = retryBaseHelp;
                return;
            }
            var maximum = Math.max(1, Math.floor((every * unit) / 4));
            retryGrace.max = String(maximum);
            if (retryGrace.value && Number(retryGrace.value) > maximum) {
                retryGrace.value = String(maximum);
            }
            if (retryHelp && retryBaseHelp) {
                retryHelp.textContent = retryBaseHelp + " Maximum for this interval: " + maximum + " seconds.";
            }
        }

        function refresh() {
            sections.forEach(function (section) {
                section.hidden = section.dataset.scheduleSection !== select.value;
            });
            if (calendarOptions) {
                calendarOptions.open = select.value === "calendar";
            }
            if (lifecycleOptions) {
                lifecycleOptions.hidden = select.value === "once";
            }
            refreshRetryLimit();
        }
        select.addEventListener("change", refresh);
        if (repeatEvery) repeatEvery.addEventListener("input", refreshRetryLimit);
        if (repeatPeriod) repeatPeriod.addEventListener("change", refreshRetryLimit);
        refresh();
    }

    function bindScheduleAdvancedOptions() {
        document.querySelectorAll("[data-schedule-advanced]").forEach(function (details) {
            var marker = details.querySelector("[data-advanced-options-touched]");
            if (!marker) return;
            details.addEventListener("toggle", function () {
                if (details.open) marker.value = "1";
            });
        });
    }

    function bindScheduleToggles() {
        document.querySelectorAll("[data-schedule-toggle]").forEach(function (toggle) {
            toggle.addEventListener("change", function () {
                var form = toggle.form;
                if (!form) return;
                toggle.disabled = true;
                form.submit();
            });
        });
    }

    function bindListEditors() {
        document.querySelectorAll("[data-list-editor]:not([data-autocomplete-editor])").forEach(function (editor) {
            var hidden = editor.querySelector("input[data-list-value]");
            var entry = editor.querySelector("[data-list-entry]");
            var addButton = editor.querySelector("[data-list-add]");
            var valuesContainer = editor.querySelector("[data-list-values]");
            if (!hidden || !entry || !addButton || !valuesContainer) return;

            var values = (hidden.value || "").split(/[,\n]/)
                .map(function (value) { return value.trim(); })
                .filter(function (value, index, all) { return value && all.indexOf(value) === index; });

            function render() {
                valuesContainer.replaceChildren();
                if (!values.length) {
                    var empty = document.createElement("span");
                    empty.className = "list-empty";
                    empty.textContent = editor.dataset.emptyLabel;
                    valuesContainer.appendChild(empty);
                    hidden.value = "";
                    return;
                }
                values.forEach(function (value, index) {
                    var token = document.createElement("span");
                    token.className = "list-token";
                    var text = document.createElement("span");
                    text.textContent = value;
                    var remove = document.createElement("button");
                    remove.type = "button";
                    remove.className = "list-token-remove";
                    remove.setAttribute("aria-label", editor.dataset.removeLabel + " " + value);
                    remove.textContent = "×";
                    remove.addEventListener("click", function () {
                        values.splice(index, 1);
                        render();
                    });
                    token.appendChild(text);
                    token.appendChild(remove);
                    valuesContainer.appendChild(token);
                });
                hidden.value = values.join(",");
            }

            function addValue() {
                var value = entry.value.trim();
                if (!value || values.indexOf(value) !== -1) {
                    entry.focus();
                    return;
                }
                values.push(value);
                entry.value = "";
                render();
                entry.focus();
            }

            addButton.addEventListener("click", addValue);
            entry.addEventListener("keydown", function (event) {
                if (event.key === "Enter") {
                    event.preventDefault();
                    addValue();
                }
            });
            render();
        });
    }

    function bindAutocompleteEditors(root) {
        (root || document).querySelectorAll("[data-autocomplete-editor]").forEach(function (editor) {
            var hidden = editor.querySelector("input[data-list-value]");
            var entry = editor.querySelector("[data-autocomplete-entry]");
            var addButton = editor.querySelector("[data-autocomplete-add]");
            var valuesContainer = editor.querySelector("[data-list-values]");
            var optionsContainer = editor.querySelector("[data-autocomplete-options]");
            if (!hidden || !entry || !addButton || !valuesContainer || !optionsContainer) return;

            var valueSeparator = editor.dataset.valueSeparator || ",";
            var valueSeparatorPattern = valueSeparator === "__" ? /__|\n/ : /[\n,]/;
            var values = (hidden.value || "").split(valueSeparatorPattern)
                .map(function (value) { return value.trim(); })
                .filter(function (value, index, all) { return value && all.indexOf(value) === index; });
            var options = [];
            var activeOption = -1;
            var requestNumber = 0;

            function renderValues() {
                valuesContainer.replaceChildren();
                if (!values.length) {
                    var empty = document.createElement("span");
                    empty.className = "list-empty";
                    empty.textContent = editor.dataset.emptyLabel;
                    valuesContainer.appendChild(empty);
                }
                values.forEach(function (value, index) {
                    var token = document.createElement("span");
                    token.className = "list-token";
                    var text = document.createElement("span");
                    text.textContent = value;
                    var remove = document.createElement("button");
                    remove.type = "button";
                    remove.className = "list-token-remove";
                    remove.setAttribute("aria-label", editor.dataset.removeLabel + " " + value);
                    remove.textContent = "×";
                    remove.addEventListener("click", function () {
                        values.splice(index, 1);
                        renderValues();
                    });
                    token.appendChild(text);
                    token.appendChild(remove);
                    valuesContainer.appendChild(token);
                });
                sync();
            }

            function sync() {
                var nextValue = values.join(valueSeparator);
                if (hidden.value !== nextValue) {
                    hidden.value = nextValue;
                    hidden.dispatchEvent(new Event("change", {bubbles: true}));
                }
            }

            function closeOptions() {
                optionsContainer.hidden = true;
                entry.setAttribute("aria-expanded", "false");
                activeOption = -1;
                entry.removeAttribute("aria-activedescendant");
            }

            function showOptions(items) {
                options = items.filter(function (item) {
                    return item && values.indexOf(item.value) === -1;
                });
                optionsContainer.replaceChildren();
                activeOption = -1;
                if (!options.length) {
                    if (entry.value.trim()) {
                        var noResults = document.createElement("div");
                        noResults.className = "autocomplete-no-results";
                        noResults.textContent = editor.dataset.noResultsLabel;
                        optionsContainer.appendChild(noResults);
                        optionsContainer.hidden = false;
                        entry.setAttribute("aria-expanded", "true");
                    } else {
                        closeOptions();
                    }
                    return;
                }
                options.forEach(function (item, index) {
                    var option = document.createElement("button");
                    option.type = "button";
                    option.className = "autocomplete-option";
                    option.id = entry.id + "-option-" + index;
                    option.setAttribute("role", "option");
                    option.dataset.value = item.value;
                    var label = document.createElement("strong");
                    label.textContent = item.label || item.value;
                    option.appendChild(label);
                    if (item.description) {
                        var description = document.createElement("small");
                        description.textContent = item.description;
                        option.appendChild(description);
                    }
                    option.addEventListener("mousedown", function (event) {
                        event.preventDefault();
                    });
                    option.addEventListener("click", function () {
                        addValue(item.value);
                    });
                    optionsContainer.appendChild(option);
                });
                optionsContainer.hidden = false;
                entry.setAttribute("aria-expanded", "true");
            }

            function fetchOptions() {
                var query = entry.value.trim();
                var currentRequest = ++requestNumber;
                var url = editor.dataset.autocompleteUrl + "?q=" + encodeURIComponent(query);
                fetch(url, {headers: {"X-Requested-With": "XMLHttpRequest"}})
                    .then(function (response) {
                        if (!response.ok) throw new Error("autocomplete failed");
                        return response.json();
                    })
                    .then(function (payload) {
                        if (currentRequest !== requestNumber) return;
                        var items = Array.isArray(payload.results) ? payload.results : [];
                        showOptions(items.map(function (item) {
                            return typeof item === "string" ? {value: item, label: item} : item;
                        }));
                    })
                    .catch(function () {
                        if (currentRequest !== requestNumber) return;
                        optionsContainer.replaceChildren();
                        var error = document.createElement("div");
                        error.className = "autocomplete-error";
                        error.textContent = editor.dataset.errorLabel || "The options could not be loaded.";
                        optionsContainer.appendChild(error);
                        optionsContainer.hidden = false;
                        entry.setAttribute("aria-expanded", "true");
                    });
            }

            function addValue(value) {
                value = String(value || "").trim();
                if (!value || values.indexOf(value) !== -1) {
                    entry.focus();
                    return;
                }
                values.push(value);
                entry.value = "";
                closeOptions();
                renderValues();
                entry.focus();
            }

            function setActiveOption(index) {
                var optionElements = optionsContainer.querySelectorAll(".autocomplete-option");
                if (!optionElements.length) return;
                activeOption = (index + optionElements.length) % optionElements.length;
                optionElements.forEach(function (option, optionIndex) {
                    option.classList.toggle("is-active", optionIndex === activeOption);
                });
                entry.setAttribute("aria-activedescendant", optionElements[activeOption].id);
            }

            entry.addEventListener("focus", fetchOptions);
            entry.addEventListener("input", fetchOptions);
            entry.addEventListener("keydown", function (event) {
                if (event.key === "ArrowDown") {
                    event.preventDefault();
                    setActiveOption(activeOption + 1);
                } else if (event.key === "ArrowUp") {
                    event.preventDefault();
                    setActiveOption(activeOption - 1);
                } else if (event.key === "Enter") {
                    event.preventDefault();
                    if (activeOption >= 0 && options[activeOption]) addValue(options[activeOption].value);
                    else addValue(entry.value);
                } else if (event.key === "Escape") {
                    closeOptions();
                }
            });
            addButton.addEventListener("click", function () { addValue(entry.value); });
            document.addEventListener("click", function (event) {
                if (!editor.contains(event.target)) closeOptions();
            });
            renderValues();
        });
    }

    function bindPipelineReload() {
        document.querySelectorAll("[data-pipeline-form] select[name='pipeline'], [data-schedule-form] select[name='pipeline']:not([disabled])").forEach(function (select) {
            select.addEventListener("change", function () {
                var url = new URL(window.location.href);
                url.searchParams.set("pipeline", select.value);
                window.location.assign(url.toString());
            });
        });
    }

    var DAG_STATES = ["queued", "retrying", "running", "completed", "failed", "cancelled"];
    var DAG_TONES = {
        queued: "muted",
        retrying: "retry",
        running: "running",
        completed: "success",
        failed: "error",
        cancelled: "muted"
    };
    var DAG_STATUS_SYMBOLS = {
        queued: "○",
        retrying: "↻",
        running: "▶",
        completed: "✓",
        failed: "!",
        cancelled: "×"
    };

    function normaliseDagState(state) {
        state = String(state || "queued").toLowerCase();
        if (state === "pending" || state === "waiting") return "queued";
        if (state === "retry" || state === "retrying" || state === "retry_wait" || state === "waiting_retry") return "retrying";
        if (state === "cancelled" || state === "cancelling") return "cancelled";
        return DAG_STATES.indexOf(state) === -1 ? "queued" : state;
    }

    var DAG_VIRTUAL_ROW_GAP = 20;
    var DAG_VIRTUAL_OVERSCAN = 2;
    var DAG_VIRTUAL_CARD_CACHE_LIMIT = 64;
    var DAG_VIRTUAL_CARD_REQUEST_LIMIT = 40;

    function dagIndexGraph(graph) {
        var index = {nodes: Object.create(null), ports: Object.create(null), flows: Object.create(null), edges: []};
        graph.querySelectorAll("[data-dag-node][data-node-id]").forEach(function (node) {
            index.nodes[node.dataset.nodeId] = node;
        });
        graph.querySelectorAll(".dag-port[data-node-id][data-port]").forEach(function (port) {
            index.ports[port.dataset.nodeId + "::" + port.dataset.port] = port;
        });
        graph.querySelectorAll("[data-dag-edge-flow]").forEach(function (flow) {
            index.flows[flow.dataset.dagEdgeFlow] = flow;
        });
        graph.querySelectorAll("[data-dag-edge]").forEach(function (edge) { index.edges.push(edge); });
        graph.__dagIndex = index;
        return index;
    }

    function dagNode(graph, nodeId) {
        if (!graph) return null;
        var requested = String(nodeId || "");
        var nodes = (graph.__dagIndex || dagIndexGraph(graph)).nodes;
        return nodes[requested] || nodes["batch-" + requested] || null;
    }

    function dagInitSnapshot(graph, snapshot) {
        var script = graph.querySelector("#dag-pipeline-snapshot");
        if (!snapshot) {
            if (!script) return;
            snapshot = JSON.parse(script.textContent);
            script.remove();
        }
        graph.__dagSnapshot = snapshot;
        graph.__dagTasks = Object.create(null);
        graph.__dagJobStates = Object.create(null);
        graph.__dagIncoming = Object.create(null);
        graph.__dagOutgoing = Object.create(null);
        graph.__dagVirtualRecords = Object.create(null);
        graph.__dagVirtualLists = [];
        graph.__dagDetailCache = graph.__dagDetailCache || new Map();
        graph.__dagCardCache = graph.__dagCardCache || new Map();
        graph.__dagRowHeights = graph.__dagRowHeights || Object.create(null);
        var lists = Object.create(null);
        graph.querySelectorAll("[data-dag-virtual-list]").forEach(function (list) {
            lists[list.dataset.dagVirtualList] = list;
        });
        snapshot.columns.forEach(function (column) {
            column.jobs.forEach(function (job) {
                graph.__dagJobStates[job.job_id] = job.status_code;
                var list = lists[job.job_id];
                if (list) {
                    list.__dagRecords = [];
                    list.__dagMounted = new Map();
                    graph.__dagVirtualLists.push(list);
                }
                job.tasks.forEach(function (task, position) {
                    graph.__dagTasks[task.id] = task;
                    if (!list) return;
                    var estimatedHeight = task.placeholder ? 100 : 106 + (task.stages || []).length * 60 + (task.last_error ? 55 : 0);
                    var record = {task: task, position: position, list: list, rankId: column.id,
                        height: graph.__dagRowHeights[task.id] || estimatedHeight, offset: 0};
                    list.__dagRecords.push(record);
                    graph.__dagVirtualRecords[task.id] = record;
                });
                if (list) dagReflowVirtualList(list);
            });
        });
        snapshot.edges.forEach(function (edge) {
            (graph.__dagIncoming[edge.target] || (graph.__dagIncoming[edge.target] = [])).push(edge);
            (graph.__dagOutgoing[edge.source] || (graph.__dagOutgoing[edge.source] = [])).push(edge);
        });
        Object.keys(graph.__dagRowHeights).forEach(function (id) {
            if (!graph.__dagTasks[id]) delete graph.__dagRowHeights[id];
        });
        dagIndexGraph(graph);
    }

    function dagReflowVirtualList(list) {
        var offset = 0;
        list.__dagRecords.forEach(function (record) {
            record.offset = offset;
            offset += record.height + DAG_VIRTUAL_ROW_GAP;
        });
        list.style.height = Math.max(0, offset - DAG_VIRTUAL_ROW_GAP) + "px";
        list.__dagMounted.forEach(function (node, position) {
            node.style.top = list.__dagRecords[position].offset + "px";
        });
    }

    function dagVirtualPosition(list, offset) {
        var records = list.__dagRecords;
        var low = 0;
        var high = records.length;
        while (low < high) {
            var middle = Math.floor((low + high) / 2);
            if (records[middle].offset + records[middle].height + DAG_VIRTUAL_ROW_GAP <= offset) low = middle + 1;
            else high = middle;
        }
        return low;
    }

    function dagExpandedMetrics(node) {
        var expanded = [];
        node.querySelectorAll(".dag-metric-list").forEach(function (list, index) {
            var more = list.querySelector("[data-dag-metric-more]");
            if (more && more.getAttribute("aria-expanded") === "true") expanded.push(index);
        });
        return expanded;
    }

    function dagRestoreExpandedMetrics(node, expanded) {
        var lists = node.querySelectorAll(".dag-metric-list");
        (expanded || []).forEach(function (index) {
            var list = lists[index];
            var more = list && list.querySelector("[data-dag-metric-more]");
            if (!more) return;
            list.querySelectorAll("[data-dag-metric-overflow]").forEach(function (metric) {
                metric.hidden = false;
                metric.removeAttribute("aria-hidden");
            });
            more.setAttribute("aria-expanded", "true");
            more.setAttribute("aria-label", more.dataset.collapseLabel);
            more.textContent = "−" + more.dataset.moreCount;
        });
    }

    function dagVirtualCard(graph, record, html, expanded) {
        var template = document.createElement("template");
        template.innerHTML = html;
        var node = template.content.querySelector("[data-dag-node]");
        if (!node) return null;
        node.classList.add("dag-node--virtual");
        node.dataset.nodeRevision = record.task.revision;
        node.dataset.dagCardRevision = record.task.revision;
        node.style.top = record.offset + "px";
        node.setAttribute("role", "listitem");
        node.setAttribute("aria-posinset", record.position + 1);
        node.setAttribute("aria-setsize", record.list.__dagRecords.length);
        dagRestoreExpandedMetrics(node, expanded);
        return node;
    }

    function dagLoadingNode(graph, record) {
        var task = record.task;
        var node = detailText("article", "dag-node dag-node--virtual dag-node--" + task.state);
        node.dataset.dagNode = "";
        node.dataset.graphNode = task.id;
        node.dataset.nodeId = task.id;
        node.dataset.nodeLabel = task.label;
        node.dataset.state = task.state;
        node.dataset.nodeRevision = task.revision;
        DAG_STATES.forEach(function (state) {
            var key = "label" + state.charAt(0).toUpperCase() + state.slice(1);
            node.dataset[key] = graph.dataset[key];
        });
        if (!task.placeholder) node.dataset.batchOpen = "";
        node.setAttribute("role", "listitem");
        node.setAttribute("aria-posinset", record.position + 1);
        node.setAttribute("aria-setsize", record.list.__dagRecords.length);
        node.setAttribute("aria-label", task.label + " · " + task.status.label);
        node.style.top = record.offset + "px";
        node.style.minHeight = record.height + "px";
        ["in", "out"].forEach(function (side) {
            var port = detailText("span", "dag-port dag-port--" + side);
            port.dataset.nodeId = task.id;
            port.dataset.port = side;
            port.setAttribute("aria-hidden", "true");
            node.appendChild(port);
        });
        var header = detailText("div", "dag-node-header");
        var heading = detailText("div", "dag-batch-heading");
        heading.appendChild(detailText("span", "dag-node-kind", task.placeholder ? graph.dataset.labelInput : graph.dataset.labelBatch));
        var title = detailText("span", "dag-node-title");
        title.appendChild(detailText("strong", "", task.label));
        if (!task.placeholder) title.appendChild(detailText("code", "", task.node_id.slice(0, 14)));
        heading.appendChild(title);
        header.appendChild(heading);
        var status = detailText("span", "dag-badge dag-badge--" + task.status.tone + " dag-status-icon");
        var icon = detailText("span", "", task.status.icon || "○");
        icon.dataset.statusIcon = "";
        icon.setAttribute("aria-hidden", "true");
        status.appendChild(icon);
        var statusLabel = detailText("span", "visually-hidden", task.status.label);
        statusLabel.dataset.statusLabel = "";
        status.appendChild(statusLabel);
        status.dataset.nodeStatus = "";
        status.dataset.dagTooltip = task.status.label;
        status.setAttribute("role", "img");
        status.setAttribute("tabindex", "0");
        status.setAttribute("aria-label", task.status.label);
        status.title = task.status.label;
        header.appendChild(status);
        node.appendChild(header);
        if (task.placeholder) {
            node.dataset.dagCardRevision = task.revision;
            node.appendChild(detailText("p", "dag-placeholder-label", task.summary));
            if (task.waiting_message) node.appendChild(detailText("p", "dag-placeholder-label", task.waiting_message));
            return node;
        }
        var stages = detailText("div", "dag-stage-results");
        stages.setAttribute("aria-label", graph.dataset.labelStages);
        (task.stages || []).forEach(function (stage) {
            var button = detailText("button", "dag-stage-result dag-stage-result--" + stage.status.tone);
            button.type = "button";
            button.dataset.stageOpen = "";
            button.dataset.stageKey = stage.key;
            button.title = stage.label + " · " + stage.status.label;
            button.setAttribute("aria-label", button.title);
            button.appendChild(detailText("strong", "dag-stage-copy", stage.label));
            button.appendChild(detailText("span", "dag-stage-status dag-stage-status--" + stage.status.tone, stage.status.icon));
            stages.appendChild(button);
        });
        node.appendChild(stages);
        var loading = detailText("p", "dag-placeholder-label", graph.dataset.labelLoading);
        loading.dataset.dagCardLoading = "";
        node.appendChild(loading);
        var footer = detailText("div", "dag-node-footer");
        var detail = detailText("button", "dag-detail-link", graph.dataset.labelDetail + " ↗");
        detail.type = "button";
        detail.dataset.openBatch = "";
        footer.appendChild(detail);
        node.appendChild(footer);
        return node;
    }

    function dagMeasureVirtualRows(graph) {
        var scale = dagZoomScale(graph);
        var anchors = new Map();
        var changedLists = [];
        graph.__dagVirtualLists.forEach(function (list) {
            var container = list.closest("[data-dag-rank-scroll]");
            var bounds = container.getBoundingClientRect();
            list.__dagMounted.forEach(function (node) {
                var rect = node.getBoundingClientRect();
                if (rect.bottom <= bounds.top || rect.top >= bounds.bottom) return;
                var anchor = anchors.get(container);
                if (!anchor || rect.top < anchor.top) anchors.set(container, {node: node, top: rect.top});
            });
            var changed = false;
            list.__dagMounted.forEach(function (node, position) {
                if (!node.dataset.dagCardRevision) return;
                var record = list.__dagRecords[position];
                var height = node.offsetHeight;
                if (!height || Math.abs(height - record.height) < 1) return;
                record.height = height;
                graph.__dagRowHeights[record.task.id] = height;
                changed = true;
            });
            if (changed) changedLists.push(list);
        });
        if (!changedLists.length) return;
        changedLists.forEach(dagReflowVirtualList);
        anchors.forEach(function (anchor, container) {
            var delta = (anchor.node.getBoundingClientRect().top - anchor.top) / scale;
            if (Math.abs(delta) < 1) return;
            graph.__dagLayoutAdjusting = true;
            container.scrollTop += delta;
        });
        window.requestAnimationFrame(function () { graph.__dagLayoutAdjusting = false; });
    }

    function dagScheduleVirtualCards(graph) {
        if (graph.__dagCardTimer || graph.__dagCardRequest) return;
        var pending = Object.keys(graph.__dagIndex.nodes).some(function (id) {
            var task = graph.__dagTasks[id];
            var node = graph.__dagIndex.nodes[id];
            return task && !task.placeholder && node.dataset.dagCardRevision !== task.revision && !(node.__dagCardRetryAfter > Date.now());
        });
        if (!pending) return;
        graph.__dagCardTimer = window.setTimeout(function () {
            graph.__dagCardTimer = null;
            if (!graph.isConnected) return;
            var requested = new Map();
            Object.keys(graph.__dagIndex.nodes).forEach(function (id) {
                var task = graph.__dagTasks[id];
                var node = graph.__dagIndex.nodes[id];
                if (!task || task.placeholder || node.dataset.dagCardRevision === task.revision || node.__dagCardRetryAfter > Date.now() || requested.size >= DAG_VIRTUAL_CARD_REQUEST_LIMIT) return;
                requested.set(id, task.revision);
            });
            if (!requested.size) return;
            var controller = new AbortController();
            graph.__dagCardRequest = controller;
            var url = new URL(graph.dataset.batchDetailUrl, window.location.href);
            url.searchParams.set("cards", Array.from(requested.keys()).map(function (id) { return graph.__dagTasks[id].node_id; }).join(","));
            fetch(url.toString(), {signal: controller.signal, headers: {"X-Requested-With": "XMLHttpRequest"}})
                .then(function (response) {
                    if (!response.ok) throw new Error("batch cards failed");
                    return response.json();
                })
                .then(function (payload) {
                    if (!graph.isConnected || graph.__dagCardRequest !== controller) return;
                    var focusDescriptor = dagFocusDescriptor();
                    (payload.cards || []).forEach(function (card) {
                        var task = graph.__dagTasks[card.id];
                        var record = graph.__dagVirtualRecords[card.id];
                        if (!task || !record || task.revision !== requested.get(card.id)) return;
                        var previous = dagNode(graph, card.id);
                        var cached = graph.__dagCardCache.get(card.id);
                        var expanded = previous ? dagExpandedMetrics(previous) : cached && cached.expanded;
                        var next = dagVirtualCard(graph, record, card.html, expanded);
                        if (!next) return;
                        graph.__dagCardCache.delete(card.id);
                        graph.__dagCardCache.set(card.id, {revision: task.revision, html: card.html, expanded: expanded});
                        if (previous) {
                            if (dagMetricTooltipAnchor && previous.contains(dagMetricTooltipAnchor)) hideDagMetricTooltip();
                            previous.replaceWith(next);
                            record.list.__dagMounted.set(record.position, next);
                        }
                    });
                    while (graph.__dagCardCache.size > DAG_VIRTUAL_CARD_CACHE_LIMIT) graph.__dagCardCache.delete(graph.__dagCardCache.keys().next().value);
                    dagIndexGraph(graph);
                    restoreDagFocus(graph, focusDescriptor);
                    graph.__dagRender();
                    refreshOpenDagDetail(graph.closest("#job-graph"));
                })
                .catch(function (error) {
                    if (error.name === "AbortError") return;
                    requested.forEach(function (revision, id) {
                        var node = dagNode(graph, id);
                        if (!node) return;
                        node.__dagCardRetryAfter = Date.now() + 5000;
                        var loading = node.querySelector("[data-dag-card-loading]");
                        if (loading) loading.textContent = graph.dataset.labelLoadError;
                    });
                })
                .finally(function () {
                    if (graph.__dagCardRequest !== controller) return;
                    graph.__dagCardRequest = null;
                    if (graph.isConnected) dagScheduleVirtualCards(graph);
                });
        }, 60);
    }

    function dagMountVirtualRows(graph) {
        if (!graph.__dagVirtualLists || !graph.__dagVirtualLists.length) return;
        dagMeasureVirtualRows(graph);
        var scale = dagZoomScale(graph);
        var viewportRect = graph.querySelector("[data-dag-viewport]").getBoundingClientRect();
        var changed = false;
        graph.__dagVirtualLists.forEach(function (list) {
            var container = list.closest("[data-dag-rank-scroll]");
            var rect = list.getBoundingClientRect();
            var bounds = container.getBoundingClientRect();
            var count = list.__dagRecords.length;
            var start = Math.max(0, dagVirtualPosition(list, (bounds.top - rect.top) / scale) - DAG_VIRTUAL_OVERSCAN);
            var end = Math.min(count, dagVirtualPosition(list, (bounds.bottom - rect.top) / scale) + 1 + DAG_VIRTUAL_OVERSCAN);
            if (rect.top > bounds.bottom || rect.bottom < bounds.top) end = start;
            if (rect.right < viewportRect.left - 400 || rect.left > viewportRect.right + 400) end = start;
            var wanted = new Set();
            for (var position = start; position < end; position += 1) wanted.add(position);
            var focused = document.activeElement && document.activeElement.closest("[data-dag-node]");
            if (focused && focused.parentElement === list) {
                var focusedRecord = graph.__dagVirtualRecords[focused.dataset.nodeId];
                if (focusedRecord) wanted.add(focusedRecord.position);
            }
            list.__dagMounted.forEach(function (node, position) {
                if (wanted.has(position)) return;
                var cached = graph.__dagCardCache.get(node.dataset.nodeId);
                if (cached) cached.expanded = dagExpandedMetrics(node);
                if (dagMetricTooltipAnchor && node.contains(dagMetricTooltipAnchor)) hideDagMetricTooltip();
                node.remove();
                list.__dagMounted.delete(position);
                changed = true;
            });
            Array.from(wanted).sort(function (a, b) { return a - b; }).forEach(function (position) {
                if (list.__dagMounted.has(position)) return;
                var record = list.__dagRecords[position];
                var cached = graph.__dagCardCache.get(record.task.id);
                var node = cached && cached.revision === record.task.revision
                    ? dagVirtualCard(graph, record, cached.html, cached.expanded) : null;
                if (!node) node = dagLoadingNode(graph, record);
                var next = Array.prototype.find.call(list.children, function (child) {
                    var record = graph.__dagVirtualRecords[child.dataset.nodeId];
                    return record && record.position > position;
                });
                list.insertBefore(node, next || null);
                list.__dagMounted.set(position, node);
                changed = true;
            });
        });
        if (changed) dagIndexGraph(graph);
        dagScheduleVirtualCards(graph);
    }

    function dagVirtualRect(graph, nodeId, rects) {
        var record = graph.__dagVirtualRecords && graph.__dagVirtualRecords[nodeId];
        if (!record) return null;
        var listRect = rects && rects.get(record.list);
        if (!listRect) {
            listRect = record.list.getBoundingClientRect();
            if (rects) rects.set(record.list, listRect);
        }
        var scale = dagZoomScale(graph);
        var top = listRect.top + record.offset * scale;
        return {left: listRect.left, right: listRect.right, top: top, bottom: top + record.height * scale, height: record.height * scale};
    }

    function dagRevealVirtualRow(graph, nodeId) {
        var record = graph.__dagVirtualRecords && graph.__dagVirtualRecords[nodeId];
        if (!record) return dagNode(graph, nodeId);
        var container = record.list.closest("[data-dag-rank-scroll]");
        var scale = dagZoomScale(graph);
        var offset = (record.list.getBoundingClientRect().top - container.getBoundingClientRect().top) / scale + container.scrollTop;
        container.scrollTop = offset + record.offset;
        dagMountVirtualRows(graph);
        if (graph.__dagRender) graph.__dagRender();
        return dagNode(graph, nodeId);
    }

    function dagSyncVirtualEdges(graph, canvasRect) {
        if (!graph.__dagSnapshot || !graph.__dagSnapshot.virtual) return;
        var scale = dagZoomScale(graph);
        var viewport = graph.querySelector("[data-dag-viewport]").getBoundingClientRect();
        var rects = new Map();
        var bounds = new Map();
        var visible = new Set();
        var candidates = new Map();
        function rowBounds(record) {
            var container = record.list.closest("[data-dag-rank-scroll]");
            if (!bounds.has(container)) bounds.set(container, container.getBoundingClientRect());
            return bounds.get(container);
        }
        Object.keys(graph.__dagIndex.nodes).forEach(function (id) {
            var record = graph.__dagVirtualRecords[id];
            if (!record) return;
            var rect = dagVirtualRect(graph, id, rects);
            var clip = rowBounds(record);
            if (rect.bottom <= clip.top || rect.top >= clip.bottom || rect.right <= viewport.left || rect.left >= viewport.right) return;
            visible.add(id);
            (graph.__dagIncoming[id] || []).concat(graph.__dagOutgoing[id] || []).forEach(function (edge) {
                candidates.set(edge.id, edge);
            });
        });
        var groups = new Map();
        function endpoint(id, side) {
            var record = graph.__dagVirtualRecords[id];
            if (!record) return null;
            var rect = dagVirtualRect(graph, id, rects);
            var clip = rowBounds(record);
            var center = (rect.top + rect.bottom) / 2;
            var boundary = center < clip.top ? "above" : center > clip.bottom ? "below" : "inside";
            return {
                x: ((side === "out" ? rect.right + 6 * scale : rect.left - 6 * scale) - canvasRect.left) / scale,
                y: (Math.max(clip.top, Math.min(clip.bottom, center)) - canvasRect.top) / scale,
                key: visible.has(id) ? id : "boundary-" + record.rankId + "-" + boundary
            };
        }
        candidates.forEach(function (edge) {
            var source = endpoint(edge.source, "out");
            var target = endpoint(edge.target, "in");
            if (!source || !target) return;
            var state = graph.__dagTasks[edge.target].state;
            var key = source.key + "::" + target.key + "::" + state;
            var group = groups.get(key);
            if (!group) {
                group = {sourceId: edge.source, targetId: edge.target, source: source, target: target, state: state, sourceIds: new Set(), targetIds: new Set(), clipped: !visible.has(edge.source) || !visible.has(edge.target)};
                groups.set(key, group);
            }
            group.sourceIds.add(edge.source);
            group.targetIds.add(edge.target);
        });
        var displayed = graph.__dagDisplayedEdges || (graph.__dagDisplayedEdges = new Map());
        var bases = graph.querySelector("[data-dag-edge-bases]");
        var flows = graph.querySelector("[data-dag-edge-flows]");
        displayed.forEach(function (pair, key) {
            if (groups.has(key)) return;
            pair.edge.remove();
            pair.flow.remove();
            displayed.delete(key);
        });
        groups.forEach(function (group, key) {
            var pair = displayed.get(key);
            if (!pair) {
                var edge = document.createElementNS(DAG_SVG_NS, "path");
                edge.setAttribute("class", "dag-edge" + (group.clipped ? " dag-edge--clipped" : ""));
                edge.dataset.dagEdge = key;
                edge.setAttribute("marker-end", "url(#dag-arrow)");
                var flow = document.createElementNS(DAG_SVG_NS, "path");
                flow.setAttribute("class", "dag-edge-flow");
                flow.dataset.dagEdgeFlow = key;
                bases.appendChild(edge);
                flows.appendChild(flow);
                pair = {edge: edge, flow: flow};
                displayed.set(key, pair);
            }
            pair.edge.dataset.sourceId = group.sourceId;
            pair.edge.dataset.targetId = group.targetId;
            pair.edge.__dagSourceIds = group.sourceIds;
            pair.edge.__dagTargetIds = group.targetIds;
            pair.edge.__dagEndpointPoints = {source: group.source, target: group.target};
            pair.flow.dataset.sourceId = group.sourceId;
            pair.flow.dataset.targetId = group.targetId;
            setDagEdgeState(pair.edge, pair.flow, group.state);
        });
        graph.__dagIndex.edges = [];
        graph.__dagIndex.flows = Object.create(null);
        displayed.forEach(function (pair, key) {
            graph.__dagIndex.edges.push(pair.edge);
            graph.__dagIndex.flows[key] = pair.flow;
        });
    }

    function dagPort(canvas, nodeId, portName) {
        var graph = canvas.closest("[data-dag-graph]");
        var ports = (graph.__dagIndex || dagIndexGraph(graph)).ports;
        return ports[nodeId + "::" + portName] || ports["batch-" + nodeId + "::" + portName] || null;
    }

    function dagPortIndex(canvas) {
        var graph = canvas.closest("[data-dag-graph]");
        return (graph.__dagIndex || dagIndexGraph(graph)).ports;
    }

    var DAG_SVG_NS = "http://www.w3.org/2000/svg";
    var DAG_ZOOM_MIN = 0.6;
    var DAG_ZOOM_MAX = 1.6;
    var DAG_ZOOM_STEP = 0.1;

    function dagZoomScale(graph) {
        var scale = graph && Number(graph.__dagZoomScale);
        return scale > 0 ? scale : 1;
    }

    function dagFullscreenHost(graph) {
        return graph && graph.closest("[data-dag-fullscreen-host]") || graph;
    }

    function dagUpdateViewControls(graph) {
        if (!graph) return;
        var scale = dagZoomScale(graph);
        var percentage = Math.round(scale * 100) + "%";
        var zoomLabel = graph.querySelector("[data-dag-zoom-label]");
        var zoomOut = graph.querySelector("[data-dag-zoom-out]");
        var zoomIn = graph.querySelector("[data-dag-zoom-in]");
        if (zoomLabel) zoomLabel.textContent = percentage;
        if (zoomOut) zoomOut.disabled = scale <= DAG_ZOOM_MIN;
        if (zoomIn) zoomIn.disabled = scale >= DAG_ZOOM_MAX;

        var fullscreenButton = graph.querySelector("[data-dag-fullscreen]");
        if (fullscreenButton) {
            var host = dagFullscreenHost(graph);
            var isFullscreen = !!(host && (document.fullscreenElement === host || host.classList.contains("is-dag-pseudo-fullscreen")));
            var label = isFullscreen ? fullscreenButton.dataset.exitLabel : fullscreenButton.dataset.enterLabel;
            fullscreenButton.setAttribute("aria-pressed", isFullscreen ? "true" : "false");
            if (label) {
                fullscreenButton.setAttribute("aria-label", label);
                fullscreenButton.title = label;
            }
        }
    }

    function dagSetZoom(graph, nextScale) {
        if (!graph) return;
        var viewport = graph.querySelector("[data-dag-viewport]");
        var oldScale = dagZoomScale(graph);
        var centerX = viewport ? (viewport.scrollLeft + viewport.clientWidth / 2) / oldScale : 0;
        var scale = Math.max(DAG_ZOOM_MIN, Math.min(DAG_ZOOM_MAX, Math.round(nextScale * 100) / 100));
        graph.__dagZoomScale = scale;
        renderDagGraph(graph);
        if (viewport) {
            viewport.scrollLeft = Math.max(0, centerX * scale - viewport.clientWidth / 2);
        }
        dagUpdateViewControls(graph);
    }

    function dagExitPseudoFullscreen(host) {
        if (!host || !host.classList.contains("is-dag-pseudo-fullscreen")) return;
        host.classList.remove("is-dag-pseudo-fullscreen");
        document.body.classList.remove("is-dag-fullscreen-fallback");
    }

    function dagEnterPseudoFullscreen(host) {
        if (!host) return;
        host.classList.add("is-dag-pseudo-fullscreen");
        document.body.classList.add("is-dag-fullscreen-fallback");
    }

    function dagToggleFullscreen(graph) {
        var host = dagFullscreenHost(graph);
        if (!host) return;
        if (host.classList.contains("is-dag-pseudo-fullscreen")) {
            dagExitPseudoFullscreen(host);
            dagUpdateViewControls(graph);
            var exitButton = graph.querySelector("[data-dag-fullscreen]");
            if (exitButton) exitButton.focus({preventScroll: true});
            return;
        }
        if (document.fullscreenElement === host) {
            if (document.exitFullscreen) document.exitFullscreen();
            return;
        }
        if (!host.requestFullscreen) {
            dagEnterPseudoFullscreen(host);
            dagUpdateViewControls(graph);
            return;
        }
        try {
            var request = host.requestFullscreen();
            if (request && request.catch) {
                request.catch(function () {
                    dagEnterPseudoFullscreen(host);
                    dagUpdateViewControls(graph);
                });
            }
        } catch (error) {
            dagEnterPseudoFullscreen(host);
            dagUpdateViewControls(graph);
        }
    }

    function dagPortPoint(port, canvasRect, side) {
        var rect = port.getBoundingClientRect();
        var scale = dagZoomScale(port.closest("[data-dag-graph]"));
        var edgeX = side === "out" ? rect.right : rect.left;
        return {
            x: (edgeX - canvasRect.left) / scale,
            y: (rect.top + (rect.height / 2) - canvasRect.top) / scale
        };
    }

    function dagRankForNode(graph, nodeId) {
        var record = graph.__dagVirtualRecords && graph.__dagVirtualRecords[nodeId];
        if (record) return record.rankId;
        var node = dagNode(graph, nodeId);
        var rank = node && node.closest ? node.closest("[data-rank-id]") : null;
        return rank ? rank.dataset.rankId : "unranked";
    }

    function dagBundleKey(graph, edge) {
        return edge.dataset.sourceId + "::" + dagRankForNode(graph, edge.dataset.targetId);
    }

    function dagRoutePath(bundle, target) {
        // Every edge owns a complete orthogonal route. Since edges in one
        // bundle share source and X_mid, their overlapping segments form the
        // visible bus while retaining one stateful path per destination.
        return "M " + bundle.source.x + " " + bundle.source.y +
            " H " + bundle.xMid +
            " V " + target.y +
            " H " + target.x;
    }

    function dagTrunkPath(bundle) {
        var targetYs = bundle.members.map(function (member) { return member.target.y; });
        var minY = Math.min.apply(Math, targetYs.concat([bundle.source.y]));
        var maxY = Math.max.apply(Math, targetYs.concat([bundle.source.y]));
        var distance = Math.max(0, bundle.targetX - bundle.source.x);
        var xMid = bundle.source.x + (distance * 0.5);
        bundle.xMid = xMid;
        return "M " + bundle.source.x + " " + bundle.source.y + " H " + xMid + " M " + xMid + " " + minY + " V " + maxY;
    }

    // One bundle owns the shared route from a source batch to a destination
    // rank. Each handoff then needs only a short branch from that bus.
    function dagBuildBundles(graph, edges, canvasRect, ports) {
        var bundles = {};
        var canvas = graph.querySelector("[data-dag-canvas]");
        var points = Object.create(null);
        function portPoint(nodeId, side) {
            var key = nodeId + "::" + side;
            if (points[key]) return points[key];
            var port = ports[key] || dagPort(canvas, nodeId, side);
            return port ? (points[key] = dagPortPoint(port, canvasRect, side)) : null;
        }
        edges.forEach(function (edge) {
            var source = edge.__dagEndpointPoints ? edge.__dagEndpointPoints.source : portPoint(edge.dataset.sourceId, "out");
            var target = edge.__dagEndpointPoints ? edge.__dagEndpointPoints.target : portPoint(edge.dataset.targetId, "in");
            if (!source || !target) {
                delete edge.__dagGeometry;
                return;
            }

            var key = dagBundleKey(graph, edge);
            var bundle = bundles[key];
            if (!bundle) {
                bundle = bundles[key] = {
                    key: key,
                    source: source,
                    targetX: target.x,
                    members: []
                };
            }
            bundle.targetX = Math.max(bundle.targetX, target.x);
            bundle.members.push({edge: edge, source: source, target: target});
            edge.__dagGeometry = {bundle: bundle, source: source, target: target};
        });
        return bundles;
    }

    function createDagBusPath(layer, bundle) {
        var path = document.createElementNS(DAG_SVG_NS, "path");
        path.setAttribute("class", "dag-bus-trunk");
        path.setAttribute("data-dag-bus", bundle.key);
        path.setAttribute("d", dagTrunkPath(bundle));
        layer.appendChild(path);
    }

    function setDagEdgeState(edge, flow, state) {
        state = normaliseDagState(state);
        if (edge.dataset.edgeState === state && edge.classList.contains("dag-edge--" + state) && (!flow || flow.classList.contains("dag-edge-flow--" + state))) return;
        DAG_STATES.forEach(function (knownState) {
            edge.classList.remove("dag-edge--" + knownState);
            if (flow) flow.classList.remove("dag-edge-flow--" + knownState);
        });
        edge.classList.add("dag-edge--" + state);
        edge.dataset.edgeState = state;
        if (flow) flow.classList.add("dag-edge-flow--" + state);
    }

    function dagIdsMatch(left, right) {
        left = String(left || "");
        right = String(right || "");
        return left === right || left === "batch-" + right || right === "batch-" + left;
    }

    function applyDagHighlight(graph, nodeId) {
        var index = graph.__dagIndex || dagIndexGraph(graph);
        var edges = index.edges;
        var flows = Object.values(index.flows);
        var buses = graph.querySelectorAll("[data-dag-bus]");
        var nodes = Object.values(index.nodes);
        var activeEdges = {};
        var activeBuses = {};
        var incomingCount = 0;
        var activeCount = 0;

        if (nodeId) {
            edges.forEach(function (edge) {
                if (edge.__dagTargetIds ? edge.__dagTargetIds.has(nodeId) : dagIdsMatch(edge.dataset.targetId, nodeId)) {
                    activeEdges[edge.dataset.dagEdge] = true;
                    incomingCount += 1;
                }
            });
            if (!incomingCount) {
                edges.forEach(function (edge) {
                    if (edge.__dagSourceIds ? edge.__dagSourceIds.has(nodeId) : dagIdsMatch(edge.dataset.sourceId, nodeId)) activeEdges[edge.dataset.dagEdge] = true;
                });
            }
            activeCount = Object.keys(activeEdges).length;
        }

        edges.forEach(function (edge) {
            var active = Boolean(activeEdges[edge.dataset.dagEdge]);
            if (active && edge.dataset.dagBundle) activeBuses[edge.dataset.dagBundle] = true;
            edge.classList.toggle("is-active", active);
            edge.classList.toggle("is-dimmed", Boolean(nodeId) && activeCount > 0 && !active);
        });
        flows.forEach(function (flow) {
            var active = Boolean(activeEdges[flow.dataset.dagEdgeFlow]);
            flow.classList.toggle("is-active", active);
            flow.classList.toggle("is-dimmed", Boolean(nodeId) && activeCount > 0 && !active);
        });
        buses.forEach(function (bus) {
            var active = Boolean(activeBuses[bus.dataset.dagBus]);
            bus.classList.toggle("is-active", active);
            bus.classList.toggle("is-dimmed", Boolean(nodeId) && activeCount > 0 && !active);
        });
        nodes.forEach(function (node) {
            node.classList.toggle("is-focus", Boolean(nodeId) && dagIdsMatch(node.dataset.nodeId, nodeId));
        });
    }

    function setDagHighlight(graph, nodeId) {
        graph.__dagFocusNodeId = nodeId || null;
        applyDagHighlight(graph, graph.__dagFocusNodeId);
    }

    function dagConnectedBatches(graph, node) {
        if (!graph || !node) return [];
        var nodeId = node.dataset.nodeId;
        var neighbors = [];
        var seen = new Set();
        (graph.__dagIncoming[nodeId] || []).concat(graph.__dagOutgoing[nodeId] || []).forEach(function (edge) {
            var otherId = edge.target === nodeId ? edge.source : edge.target;
            var record = graph.__dagVirtualRecords[otherId];
            var other = dagNode(graph, otherId) || (record && record.list);
            if (other && !seen.has(other)) {
                seen.add(other);
                neighbors.push(other);
            }
        });
        return neighbors;
    }

    function dagCancelMagnet(graph) {
        if (!graph) return;
        var movingContainer = graph.__dagMagnetContainer;
        if (movingContainer && graph.__dagMagnetAdjusting) {
            movingContainer.scrollTo({top: movingContainer.scrollTop, behavior: "auto"});
        }
        graph.__dagMagnetContainer = null;
        graph.__dagMagnetAdjusting = false;
        if (graph.__dagMagnetUnlockTimer) {
            window.clearTimeout(graph.__dagMagnetUnlockTimer);
            graph.__dagMagnetUnlockTimer = null;
        }
    }

    function dagScheduleMagnetUnlock(graph, delay) {
        if (!graph) return;
        if (graph.__dagMagnetUnlockTimer) window.clearTimeout(graph.__dagMagnetUnlockTimer);
        graph.__dagMagnetUnlockTimer = window.setTimeout(function () {
            graph.__dagMagnetAdjusting = false;
            graph.__dagMagnetContainer = null;
            graph.__dagMagnetUnlockTimer = null;
        }, delay);
    }

    function setDagActiveColumn(graph, rank) {
        if (!graph || !rank || graph.__dagMagnetAdjusting) return;
        graph.__dagActiveRankId = rank.dataset.rankId;
        graph.querySelectorAll("[data-rank-id]").forEach(function (candidate) {
            candidate.classList.toggle("is-active-column", candidate === rank);
        });
    }

    function dagAlignPreviousColumn(graph, node, behavior) {
        if (!graph || !node || graph.__dagMagnetAdjusting) return false;
        var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        behavior = behavior || (reducedMotion ? "auto" : "smooth");
        var currentRank = node.closest("[data-rank-id]");
        var ranks = Array.prototype.slice.call(graph.querySelectorAll("[data-rank-id]"));
        var currentRankIndex = ranks.indexOf(currentRank);
        var previousRank = currentRankIndex > 0 ? ranks[currentRankIndex - 1] : null;
        var previousContainer = previousRank && previousRank.querySelector("[data-dag-rank-scroll]");
        if (!previousContainer) return false;

        var sourceRect = node.getBoundingClientRect();
        var sourceCenter = (sourceRect.top + sourceRect.bottom) / 2;
        var zoomScale = dagZoomScale(graph);
        var closest = null;
        var rects = new Map();
        (graph.__dagIncoming[node.dataset.nodeId] || []).forEach(function (edge) {
            var candidate = dagNode(graph, edge.source);
            var record = graph.__dagVirtualRecords[edge.source];
            if (record ? record.rankId !== previousRank.dataset.rankId : !candidate || candidate.closest("[data-rank-id]") !== previousRank) return;
            var rect = record ? dagVirtualRect(graph, edge.source, rects) : candidate.getBoundingClientRect();
            var center = (rect.top + rect.bottom) / 2;
            var distance = Math.abs(center - sourceCenter);
            if (!closest || distance < closest.distance) closest = {rect: rect, center: center, distance: distance};
        });
        if (!closest) return false;

        var previousContainerRect = previousContainer.getBoundingClientRect();
        var visibleTop = previousContainerRect.top + previousContainer.clientTop * zoomScale;
        var visibleBottom = visibleTop + previousContainer.clientHeight * zoomScale;
        var fullyVisible = closest.rect.top >= visibleTop && closest.rect.bottom <= visibleBottom;
        var needsReveal = !fullyVisible;

        // Keep the linked Batch aligned with the current selection, while
        // clamping the movement so a clipped Batch becomes fully visible.
        var delta = closest.center - sourceCenter;
        if (closest.rect.height <= previousContainer.clientHeight * zoomScale) {
            var minDelta = closest.rect.bottom - visibleBottom;
            var maxDelta = closest.rect.top - visibleTop;
            delta = Math.max(minDelta, Math.min(maxDelta, delta));
        }
        var nextTop = previousContainer.scrollTop + delta / zoomScale;
        var maxTop = Math.max(0, previousContainer.scrollHeight - previousContainer.clientHeight);
        nextTop = Math.max(0, Math.min(maxTop, nextTop));
        var movement = Math.abs(nextTop - previousContainer.scrollTop);
        if (movement < (needsReveal ? 0.01 : 4 / zoomScale)) return false;

        graph.__dagMagnetAdjusting = true;
        graph.__dagMagnetContainer = previousContainer;
        previousContainer.scrollTo({top: nextTop, behavior: behavior});
        dagScheduleMagnetUnlock(graph, behavior === "auto" ? 500 : 900);
        return true;
    }

    function dagAnchorNodeForColumn(graph, container) {
        if (!graph || !container) return null;
        var rank = container.closest("[data-rank-id]");
        var preferredIds = [graph.__dagHoverNodeId, graph.__dagMagnetAnchorNodeId, graph.__dagSelectedNodeId, graph.__dagAutoScrollNodeId];
        for (var index = 0; index < preferredIds.length; index += 1) {
            var preferred = dagNode(graph, preferredIds[index]);
            if (!preferred || preferred.closest("[data-rank-id]") !== rank) continue;
            var preferredRect = preferred.getBoundingClientRect();
            var containerRect = container.getBoundingClientRect();
            if (preferredRect.bottom > containerRect.top && preferredRect.top < containerRect.bottom) return preferred;
        }

        var containerRect = container.getBoundingClientRect();
        var zoomScale = dagZoomScale(graph);
        var centerY = containerRect.top + (container.clientTop + container.clientHeight / 2) * zoomScale;
        var closest = null;
        container.querySelectorAll("[data-dag-node][data-node-id]").forEach(function (candidate) {
            var rect = candidate.getBoundingClientRect();
            if (rect.bottom <= containerRect.top || rect.top >= containerRect.bottom) return;
            var distance = Math.abs((rect.top + rect.bottom) / 2 - centerY);
            if (!closest || distance < closest.distance) closest = {node: candidate, distance: distance};
        });
        return closest && closest.node;
    }

    function scrollDagBatchContextIntoView(graph, node, options) {
        var viewport = graph && graph.querySelector("[data-dag-viewport]");
        if (!viewport || !node) return;
        options = options || {};
        var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        var behavior = options.behavior || (reducedMotion ? "auto" : "smooth");

        var relatedNodes = dagConnectedBatches(graph, node);
        var selectedRect = node.getBoundingClientRect();
        var viewportRect = viewport.getBoundingClientRect();
        var originLeft = viewportRect.left + viewport.clientLeft;
        var selected = {
            left: selectedRect.left - originLeft + viewport.scrollLeft,
            right: selectedRect.right - originLeft + viewport.scrollLeft
        };
        var visible = [selected];
        relatedNodes.forEach(function (relatedNode) {
            var relatedRect = relatedNode.getBoundingClientRect();
            visible.push({
                left: relatedRect.left - originLeft + viewport.scrollLeft,
                right: relatedRect.right - originLeft + viewport.scrollLeft
            });
        });
        var bounds = {
            left: Math.min.apply(Math, visible.map(function (rect) { return rect.left; })),
            right: Math.max.apply(Math, visible.map(function (rect) { return rect.right; }))
        };
        var margin = 24;
        var availableWidth = Math.max(0, viewport.clientWidth - margin * 2);
        if (bounds.right - bounds.left > availableWidth) {
            bounds.left = selected.left;
            bounds.right = selected.right;
        }

        var nextLeft = viewport.scrollLeft;
        if (bounds.left < nextLeft + margin) nextLeft = bounds.left - margin;
        else if (bounds.right > nextLeft + viewport.clientWidth - margin) {
            nextLeft = bounds.right - viewport.clientWidth + margin;
        }

        var maxLeft = Math.max(0, viewport.scrollWidth - viewport.clientWidth);
        nextLeft = Math.max(0, Math.min(maxLeft, nextLeft));
        viewport.scrollTo({left: nextLeft, top: viewport.scrollTop, behavior: behavior});
        dagAlignPreviousColumn(graph, node, behavior);
    }

    function selectDagBatch(graph, node) {
        if (!graph || !node) return;
        dagCancelMagnet(graph);
        graph.__dagSelectedNodeId = node.dataset.nodeId;
        graph.__dagAutoScrollNodeId = node.dataset.nodeId;
        graph.__dagMagnetAnchorNodeId = node.dataset.nodeId;
        setDagActiveColumn(graph, node.closest("[data-rank-id]"));
        setDagHighlight(graph, node.dataset.nodeId);
        scrollDagBatchContextIntoView(graph, node);
    }

    var dagDetailDialog = document.querySelector("[data-batch-detail-dialog]");
    var dagDetailState = null;
    var dagMetricTooltip = null;
    var dagMetricTooltipAnchor = null;

    function detailText(tagName, className, value) {
        var element = document.createElement(tagName);
        if (className) element.className = className;
        if (value !== undefined && value !== null) element.textContent = String(value);
        return element;
    }

    function detailSection(title) {
        var section = detailText("section", "dag-detail-section");
        section.appendChild(detailText("h3", "", title));
        return section;
    }

    function detailRows(rows) {
        var list = detailText("dl", "dag-detail-facts");
        rows.forEach(function (row) {
            var wrapper = document.createElement("div");
            wrapper.appendChild(detailText("dt", "", row.label));
            var value = detailText("dd", row.mono ? "is-mono" : "", row.value);
            wrapper.appendChild(value);
            list.appendChild(wrapper);
        });
        return list;
    }

    function detailDate(value) {
        if (!value) return "—";
        var date = new Date(value);
        if (Number.isNaN(date.getTime())) return "—";
        try {
            return new Intl.DateTimeFormat(document.documentElement.lang || undefined, {
                dateStyle: "medium",
                timeStyle: "short"
            }).format(date);
        } catch (error) {
            return date.toLocaleString();
        }
    }

    function detailStatus(parent, label) {
        var status = detailText("p", "dag-detail-status", label || "—");
        status.setAttribute("role", "status");
        parent.appendChild(status);
    }

    function detailMetricGroup(parent, heading, entries) {
        var section = detailSection(heading);
        if (!entries || !entries.length) {
            section.appendChild(detailText("p", "dag-detail-empty", dagDetailDialog.dataset.labelNoData));
        } else {
            var list = detailText("dl", "dag-detail-metrics");
            entries.forEach(function (entry) {
                var row = document.createElement("div");
                row.appendChild(detailText("dt", "", entry.label || entry.key || "—"));
                var value = entry.value;
                if (entry.count === null) {
                    value = "— · " + dagDetailDialog.dataset.labelUnavailableCount;
                } else if (value === undefined || value === null || value === "") {
                    value = dagDetailDialog.dataset.labelUnavailableCount;
                }
                row.appendChild(detailText("dd", "is-mono", String(value) + (entry.unit ? " " + entry.unit : "")));
                list.appendChild(row);
            });
            section.appendChild(list);
        }
        parent.appendChild(section);
    }

    function detailReasons(parent, heading, entries) {
        var section = detailSection(heading);
        if (!entries || !entries.length) {
            section.appendChild(detailText("p", "dag-detail-empty", dagDetailDialog.dataset.labelNoData));
        } else {
            var list = detailText("dl", "dag-detail-metrics");
            entries.forEach(function (entry) {
                var row = document.createElement("div");
                row.appendChild(detailText("dt", "", entry.label || entry.key || "—"));
                var value = entry.value;
                if (entry.count === null) {
                    value = "— · " + dagDetailDialog.dataset.labelUnavailableCount;
                } else if (value === undefined || value === null || value === "") {
                    value = dagDetailDialog.dataset.labelUnavailableCount;
                }
                row.appendChild(detailText("dd", "is-mono", value));
                list.appendChild(row);
            });
            section.appendChild(list);
        }
        parent.appendChild(section);
    }

    function renderBatchDetail(data, body) {
        var context = detailSection(dagDetailDialog.dataset.labelContext);
        context.appendChild(detailRows([
            {label: dagDetailDialog.dataset.labelJob, value: data.job_label + " · " + data.job_id, mono: true},
            {label: dagDetailDialog.dataset.labelStatus, value: data.batch_status || data.status},
            {label: dagDetailDialog.dataset.labelCurrentStage, value: data.current_stage},
            {label: dagDetailDialog.dataset.labelCreated, value: detailDate(data.created_at)},
            {label: dagDetailDialog.dataset.labelFinished, value: detailDate(data.completed_at)},
            {label: dagDetailDialog.dataset.labelUpdated, value: detailDate(data.updated_at)}
        ]));
        body.appendChild(context);

        var counts = detailSection(dagDetailDialog.dataset.labelCounts);
        counts.appendChild(detailRows((data.counts || []).map(function (count) {
            return {label: count.label, value: count.value === null ? "— · " + dagDetailDialog.dataset.labelUnavailableCount : count.value, mono: true};
        })));
        body.appendChild(counts);

        var dependencies = detailSection(dagDetailDialog.dataset.labelDependencies);
        if (data.dependencies && data.dependencies.length) {
            var dependencyList = detailText("ul", "dag-detail-list");
            data.dependencies.forEach(function (dependency) {
                var item = detailText("li", "");
                item.appendChild(detailText("strong", "", dependency.label));
                item.appendChild(detailText("code", "", " · " + dependency.id));
                if (dependency.job_label) item.appendChild(detailText("span", "", " · " + dependency.job_label + " " + dependency.job_id));
                item.appendChild(detailText("span", "", " · " + dependency.status));
                if (dependency.transfer_count !== null && dependency.transfer_count !== undefined) {
                    item.appendChild(detailText("span", "", " · " + dagDetailDialog.dataset.labelTransfer + ": " + dependency.transfer_count + " " + dependency.transfer_unit));
                }
                dependencyList.appendChild(item);
            });
            dependencies.appendChild(dependencyList);
        } else {
            dependencies.appendChild(detailText("p", "dag-detail-empty", "—"));
        }
        body.appendChild(dependencies);

        var stages = detailSection(dagDetailDialog.dataset.labelStages);
        if (data.stages && data.stages.length) {
            var stageList = detailText("div", "dag-detail-stage-list");
            data.stages.forEach(function (stage) {
                var open = detailText("button", "dag-detail-stage-open");
                open.type = "button";
                open.dataset.detailStage = stage.key;
                open.appendChild(detailText("strong", "", stage.label));
                open.appendChild(detailText("span", "", stage.status.label));
                open.appendChild(detailText("span", "dag-detail-stage-arrow", "↗"));
                stageList.appendChild(open);
            });
            stages.appendChild(stageList);
        } else {
            stages.appendChild(detailText("p", "dag-detail-empty", dagDetailDialog.dataset.labelUnavailableCount));
        }
        body.appendChild(stages);

        if (data.last_error) {
            var error = detailSection(dagDetailDialog.dataset.labelLastError);
            error.appendChild(detailText("p", "dag-detail-error", data.last_error));
            body.appendChild(error);
        }
    }

    function renderStageDetail(data, stage, body) {
        detailStatus(body, stage.status.label);
        if (stage.notice) body.appendChild(detailText("p", "dag-detail-empty", stage.notice));
        var context = detailSection(dagDetailDialog.dataset.labelStageContext);
        context.appendChild(detailRows([
            {label: dagDetailDialog.dataset.labelJob, value: data.job_label + " · " + data.job_id, mono: true},
            {label: dagDetailDialog.dataset.labelAttempts, value: stage.attempts || 0, mono: true}
        ]));
        body.appendChild(context);
        detailMetricGroup(body, dagDetailDialog.dataset.labelInputs, stage.inputs);
        detailMetricGroup(body, dagDetailDialog.dataset.labelProcessed, stage.processed);
        detailMetricGroup(body, dagDetailDialog.dataset.labelOutputs, stage.outputs);
        detailReasons(body, dagDetailDialog.dataset.labelSkipped, stage.skipped);
        detailReasons(body, dagDetailDialog.dataset.labelFailed, stage.failed);

        var metadata = Object.entries(stage.metadata || {});
        var metadataSection = detailSection(dagDetailDialog.dataset.labelMetadata);
        if (metadata.length) {
            metadataSection.appendChild(detailRows(metadata.map(function (pair) {
                return {label: pair[0], value: pair[1] === null ? dagDetailDialog.dataset.labelUnavailableCount : pair[1], mono: true};
            })));
        } else {
            metadataSection.appendChild(detailText("p", "dag-detail-empty", dagDetailDialog.dataset.labelNoData));
        }
        body.appendChild(metadataSection);
        if (stage.state === "failed" && data.last_error) {
            var error = detailSection(dagDetailDialog.dataset.labelLastError);
            error.appendChild(detailText("p", "dag-detail-error", data.last_error));
            body.appendChild(error);
        }
    }

    function renderUnavailableDetail(body) {
        body.replaceChildren(detailText("p", "dag-detail-unavailable", dagDetailDialog.dataset.labelUnavailable));
    }

    function renderDagDetail() {
        if (!dagDetailDialog || !dagDetailState) return;
        var title = dagDetailDialog.querySelector("[data-dialog-title]");
        var kicker = dagDetailDialog.querySelector("[data-dialog-kicker]");
        var liveStatus = dagDetailDialog.querySelector("[data-dialog-live-status]");
        var back = dagDetailDialog.querySelector("[data-dialog-back]");
        var body = dagDetailDialog.querySelector("[data-dialog-body]");
        var previousScroll = body.scrollTop;
        var data = dagDetailState.data;
        var stage = data && dagDetailState.stageKey
            ? (data.stages || []).find(function (item) { return item.key === dagDetailState.stageKey; })
            : null;

        back.hidden = !dagDetailState.stageKey;
        if (dagDetailState.stageKey) {
            kicker.textContent = dagDetailDialog.dataset.stageTitle;
            title.textContent = stage ? stage.label : dagDetailState.stageLabel || dagDetailState.stageKey;
        } else {
            kicker.textContent = dagDetailDialog.dataset.batchTitle;
            title.textContent = data ? "Batch " + data.ordinal + " · " + data.batch_id.slice(0, 8) : "Batch " + dagDetailState.batchId.slice(0, 8);
        }
        liveStatus.textContent = dagDetailState.loading ? dagDetailDialog.dataset.labelLoading : dagDetailState.error ? dagDetailDialog.dataset.labelLoadError : !data || (dagDetailState.stageKey && !stage)
            ? dagDetailDialog.dataset.labelUnavailable
            : (stage ? stage.status.label : data.batch_status);
        body.replaceChildren();

        if (!data && (dagDetailState.loading || dagDetailState.error)) {
            body.appendChild(detailText("p", "dag-detail-empty", dagDetailState.loading ? dagDetailDialog.dataset.labelLoading : dagDetailDialog.dataset.labelLoadError));
        } else if (!data) {
            renderUnavailableDetail(body);
        } else if (dagDetailState.stageKey && !stage) {
            renderUnavailableDetail(body);
        } else if (stage) {
            renderStageDetail(data, stage, body);
        } else {
            renderBatchDetail(data, body);
        }
        body.scrollTop = previousScroll;
    }

    function batchDetailFromNode(node) {
        if (!node || !node.dataset.batchDetailJson) return null;
        if (node.dataset.dagCardRevision && node.dataset.dagCardRevision !== node.dataset.nodeRevision) return null;
        try {
            return JSON.parse(node.dataset.batchDetailJson);
        } catch (error) {
            return null;
        }
    }

    function openDagDetail(node, stageKey, origin) {
        if (!dagDetailDialog || !node) return;
        var data = batchDetailFromNode(node);
        var graph = node.closest("[data-dag-graph]");
        var task = graph && graph.__dagTasks[node.dataset.nodeId];
        if (!data && (!task || task.placeholder)) return;
        if (dagDetailState && dagDetailState.controller) dagDetailState.controller.abort();
        if (!dagDetailState) {
            dagDetailState = {origin: origin || document.activeElement};
        }
        dagDetailState.batchId = data ? data.batch_id : task.node_id;
        dagDetailState.stageKey = stageKey || null;
        dagDetailState.data = data;
        dagDetailState.graph = graph;
        dagDetailState.revision = task && task.revision;
        dagDetailState.loading = false;
        dagDetailState.error = false;
        if (stageKey) {
            var stage = ((data || task).stages || []).find(function (item) { return item.key === stageKey; });
            dagDetailState.stageLabel = stage ? stage.label : stageKey;
        }
        if (!dagDetailDialog.open) dagDetailDialog.showModal();
        renderDagDetail();
        if (!data) loadDagDetail(graph, task);
    }

    function loadDagDetail(graph, task) {
        if (!dagDetailState) return;
        if (dagDetailState.controller) dagDetailState.controller.abort();
        var cache = graph.__dagDetailCache;
        var cached = cache.get(task.id);
        dagDetailState.graph = graph;
        dagDetailState.revision = task.revision;
        if (cached && cached.revision === task.revision) {
            dagDetailState.data = cached.data;
            dagDetailState.loading = false;
            dagDetailState.error = false;
            renderDagDetail();
            return;
        }
        var controller = new AbortController();
        var state = dagDetailState;
        state.controller = controller;
        state.loading = true;
        state.error = false;
        renderDagDetail();
        var url = new URL(graph.dataset.batchDetailUrl, window.location.href);
        url.searchParams.set("batch", task.node_id);
        fetch(url.toString(), {signal: controller.signal, headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (response) {
                if (!response.ok) throw new Error("batch detail failed");
                return response.json();
            })
            .then(function (data) {
                if (dagDetailState !== state || state.controller !== controller) return;
                var current = graph.__dagTasks[task.id];
                if (current && current.revision !== task.revision) return loadDagDetail(graph, current);
                cache.delete(task.id);
                cache.set(task.id, {revision: task.revision, data: data});
                if (cache.size > 32) cache.delete(cache.keys().next().value);
                state.data = data;
                state.loading = false;
                state.controller = null;
                renderDagDetail();
            })
            .catch(function (error) {
                if (error.name === "AbortError" || dagDetailState !== state || state.controller !== controller) return;
                state.loading = false;
                state.error = true;
                state.controller = null;
                renderDagDetail();
            });
    }

    function refreshOpenDagDetail(graphWrapper) {
        if (!dagDetailDialog || !dagDetailDialog.open || !dagDetailState || !graphWrapper) return;
        var graph = graphWrapper.querySelector("[data-dag-graph]");
        if (!graph) return;
        var task = graph.__dagTasks["batch-" + dagDetailState.batchId];
        var node = dagNode(graph, "batch-" + dagDetailState.batchId);
        var data = batchDetailFromNode(node);
        if (task && !data) {
            if (dagDetailState.graph !== graph || dagDetailState.revision !== task.revision || (!dagDetailState.data && !dagDetailState.loading)) loadDagDetail(graph, task);
            setDagHighlight(graph, task.id);
            return;
        }
        dagDetailState.data = data;
        if (!task) {
            if (dagDetailState.controller) dagDetailState.controller.abort();
            dagDetailState.loading = false;
            dagDetailState.error = false;
        }
        if (dagDetailState.data && dagDetailState.stageKey) {
            var currentStage = (dagDetailState.data.stages || []).find(function (stage) { return stage.key === dagDetailState.stageKey; });
            if (currentStage) dagDetailState.stageLabel = currentStage.label;
        }
        renderDagDetail();
        if (graph && dagDetailState.data) setDagHighlight(graph, "batch-" + dagDetailState.batchId);
    }

    function closeDagDetail() {
        if (dagMetricTooltipAnchor) hideDagMetricTooltip();
        if (dagDetailDialog && dagDetailDialog.open) dagDetailDialog.close();
    }

    function bindDagDetailDialog() {
        if (!dagDetailDialog || dagDetailDialog.__bound) return;
        dagDetailDialog.__bound = true;
        var back = dagDetailDialog.querySelector("[data-dialog-back]");
        var close = dagDetailDialog.querySelector("[data-dialog-close]");
        var body = dagDetailDialog.querySelector("[data-dialog-body]");
        back.addEventListener("click", function () {
            if (!dagDetailState) return;
            dagDetailState.stageKey = null;
            renderDagDetail();
        });
        close.addEventListener("click", closeDagDetail);
        document.addEventListener("pointerdown", function (event) {
            if (!dagMetricTooltipAnchor) return;
            if (dagMetricTooltipAnchor.contains(event.target) || (dagMetricTooltip && dagMetricTooltip.contains(event.target))) return;
            hideDagMetricTooltip();
        });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && !(dagDetailDialog && dagDetailDialog.open)) hideDagMetricTooltip();
        });
        dagDetailDialog.addEventListener("click", function (event) {
            if (event.target === dagDetailDialog) closeDagDetail();
            var selection = window.getSelection ? window.getSelection() : null;
            if (selection && !selection.isCollapsed) return;
            var stageButton = event.target.closest("[data-detail-stage]");
            if (!stageButton || !dagDetailState || !dagDetailState.data) return;
            dagDetailState.stageKey = stageButton.dataset.detailStage;
            var stage = (dagDetailState.data.stages || []).find(function (item) { return item.key === dagDetailState.stageKey; });
            dagDetailState.stageLabel = stage ? stage.label : dagDetailState.stageKey;
            body.scrollTop = 0;
            renderDagDetail();
        });
        dagDetailDialog.addEventListener("close", function () {
            var state = dagDetailState;
            dagDetailState = null;
            if (!state) return;
            if (state.controller) state.controller.abort();
            if (state.origin && state.origin.isConnected) {
                state.origin.focus({preventScroll: true});
                return;
            }
            var graphWrapper = document.getElementById("job-graph");
            var graph = graphWrapper && graphWrapper.querySelector("[data-dag-graph]");
            var node = graph && (dagNode(graph, "batch-" + state.batchId) || dagRevealVirtualRow(graph, "batch-" + state.batchId));
            if (node) {
                var trigger = state.stageKey
                    ? Array.prototype.find.call(node.querySelectorAll("[data-stage-open][data-stage-key]"), function (item) {
                        return item.dataset.stageKey === state.stageKey;
                    })
                    : null;
                if (!trigger) trigger = node.querySelector("[data-open-batch]");
                if (trigger) {
                    trigger.focus({preventScroll: true});
                } else {
                    var graphTitle = document.getElementById("graph-title");
                    if (graphTitle) {
                        graphTitle.setAttribute("tabindex", "-1");
                        graphTitle.focus({preventScroll: true});
                    }
                }
            } else {
                var graphTitle = document.getElementById("graph-title");
                if (graphTitle) {
                    graphTitle.setAttribute("tabindex", "-1");
                    graphTitle.focus({preventScroll: true});
                }
            }
        });
    }

    function getDagMetricTooltip() {
        if (dagMetricTooltip) return dagMetricTooltip;
        dagMetricTooltip = document.createElement("div");
        dagMetricTooltip.id = "dag-metric-tooltip";
        dagMetricTooltip.className = "dag-metric-tooltip";
        dagMetricTooltip.setAttribute("role", "tooltip");
        dagMetricTooltip.hidden = true;
        document.body.appendChild(dagMetricTooltip);
        return dagMetricTooltip;
    }

    function showDagMetricTooltip(button) {
        if (!button || !button.dataset.dagTooltip) return;
        var tooltip = getDagMetricTooltip();
        dagMetricTooltipAnchor = button;
        tooltip.textContent = button.dataset.dagTooltip;
        tooltip.hidden = false;
        button.setAttribute("aria-describedby", tooltip.id);
        var rect = button.getBoundingClientRect();
        var margin = 12;
        var width = Math.min(320, window.innerWidth - margin * 2);
        tooltip.style.maxWidth = width + "px";
        tooltip.style.left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin)) + "px";
        var tooltipRect = tooltip.getBoundingClientRect();
        var top = rect.top - tooltipRect.height - 9;
        if (top < margin) top = Math.min(window.innerHeight - tooltipRect.height - margin, rect.bottom + 9);
        tooltip.style.top = Math.max(margin, top) + "px";
    }

    function hideDagMetricTooltip() {
        if (dagMetricTooltipAnchor) dagMetricTooltipAnchor.removeAttribute("aria-describedby");
        dagMetricTooltipAnchor = null;
        if (dagMetricTooltip) dagMetricTooltip.hidden = true;
    }

    function bindDagGraphInteractions(graph) {
        var pointerOrigin = null;
        var ignoreNextGraphClickNode = null;
        var hoverScrollTimer = null;
        var autoScrollUnlockTimer = null;
        var hoverScrollNode = null;
        var onClick = function (event) {
            var selection = window.getSelection ? window.getSelection() : null;
            var clickedNode = event.target.closest("[data-batch-open]");
            if (ignoreNextGraphClickNode && clickedNode === ignoreNextGraphClickNode) {
                ignoreNextGraphClickNode = null;
                return;
            }
            if (selection && !selection.isCollapsed) return;
            var more = event.target.closest("[data-dag-metric-more]");
            if (more && graph.contains(more)) {
                event.preventDefault();
                event.stopPropagation();
                hideDagMetricTooltip();
                var list = more.closest(".dag-metric-list");
                var expanded = more.getAttribute("aria-expanded") === "true";
                list.querySelectorAll("[data-dag-metric-overflow]").forEach(function (metric) {
                    metric.hidden = expanded;
                    if (expanded) metric.setAttribute("aria-hidden", "true");
                    else metric.removeAttribute("aria-hidden");
                });
                more.setAttribute("aria-expanded", expanded ? "false" : "true");
                more.textContent = expanded ? "+" + more.dataset.moreCount : "−" + more.dataset.moreCount;
                more.setAttribute("aria-label", expanded ? more.dataset.moreLabel : more.dataset.collapseLabel);
                if (graph.__dagRender) graph.__dagRender();
                return;
            }
            var metric = event.target.closest("[data-dag-tooltip]");
            if (metric && graph.contains(metric)) {
                event.preventDefault();
                event.stopPropagation();
                showDagMetricTooltip(metric);
                return;
            }
            var stageButton = event.target.closest("[data-stage-open]");
            if (stageButton && graph.contains(stageButton)) {
                event.preventDefault();
                event.stopPropagation();
                var stageNode = stageButton.closest("[data-dag-node]");
                selectDagBatch(graph, stageNode);
                openDagDetail(stageNode, stageButton.dataset.stageKey, stageButton);
                return;
            }
            var batchButton = event.target.closest("[data-open-batch]");
            if (batchButton && graph.contains(batchButton)) {
                event.preventDefault();
                event.stopPropagation();
                var batchNode = batchButton.closest("[data-dag-node]");
                selectDagBatch(graph, batchNode);
                openDagDetail(batchNode, null, batchButton);
                return;
            }
        };
        var onPointerOver = function (event) {
            var rank = event.target.closest("[data-rank-id]");
            if (rank && graph.contains(rank)) setDagActiveColumn(graph, rank);
            var batchNode = event.target.closest("[data-batch-open]");
            if (batchNode && graph.contains(batchNode) && !graph.__dagMagnetAdjusting) {
                graph.__dagHoverNodeId = batchNode.dataset.nodeId;
                graph.__dagMagnetAnchorNodeId = batchNode.dataset.nodeId;
            }
            var interactiveDetail = event.target.closest("[data-dag-tooltip], [data-stage-open], [data-dag-metric-more]");
            if (event.pointerType === "mouse" && !graph.__dagAutoScrollInProgress && batchNode && graph.contains(batchNode) && !interactiveDetail && !batchNode.contains(event.relatedTarget)) {
                if (hoverScrollTimer) window.clearTimeout(hoverScrollTimer);
                hoverScrollNode = batchNode;
                hoverScrollTimer = window.setTimeout(function () {
                    hoverScrollTimer = null;
                    if (!batchNode.isConnected || !batchNode.matches(":hover")) return;
                    graph.__dagAutoScrollNodeId = batchNode.dataset.nodeId;
                    graph.__dagMagnetAnchorNodeId = batchNode.dataset.nodeId;
                    graph.__dagAutoScrollInProgress = true;
                    setDagHighlight(graph, batchNode.dataset.nodeId);
                    scrollDagBatchContextIntoView(graph, batchNode);
                    if (autoScrollUnlockTimer) window.clearTimeout(autoScrollUnlockTimer);
                    autoScrollUnlockTimer = window.setTimeout(function () {
                        graph.__dagAutoScrollInProgress = false;
                        autoScrollUnlockTimer = null;
                    }, 700);
                }, 280);
            } else if (interactiveDetail && batchNode === hoverScrollNode && hoverScrollTimer) {
                window.clearTimeout(hoverScrollTimer);
                hoverScrollTimer = null;
                hoverScrollNode = null;
            }
            var metric = event.target.closest("[data-dag-tooltip]");
            if (metric && graph.contains(metric)) showDagMetricTooltip(metric);
        };
        var onPointerOut = function (event) {
            var rank = event.target.closest("[data-rank-id]");
            if (rank && graph.contains(rank) && !rank.contains(event.relatedTarget) && graph.__dagActiveRankId === rank.dataset.rankId) {
                rank.classList.remove("is-active-column");
                graph.__dagActiveRankId = null;
            }
            var batchNode = event.target.closest("[data-batch-open]");
            if (batchNode && graph.contains(batchNode) && !batchNode.contains(event.relatedTarget) && batchNode === hoverScrollNode) {
                if (hoverScrollTimer) window.clearTimeout(hoverScrollTimer);
                hoverScrollTimer = null;
                hoverScrollNode = null;
            }
            var metric = event.target.closest("[data-dag-tooltip]");
            if (!metric || !graph.contains(metric) || metric.contains(event.relatedTarget)) return;
            if (document.activeElement !== metric) hideDagMetricTooltip();
        };
        var onFocusIn = function (event) {
            var metric = event.target.closest("[data-dag-tooltip]");
            if (metric && graph.contains(metric)) showDagMetricTooltip(metric);
        };
        var onFocusOut = function (event) {
            var metric = event.target.closest("[data-dag-tooltip]");
            if (metric && graph.contains(metric) && !metric.contains(event.relatedTarget)) hideDagMetricTooltip();
        };
        var onPointerDown = function (event) {
            if (event.button !== 0) return;
            dagCancelMagnet(graph);
            pointerOrigin = {
                x: event.clientX,
                y: event.clientY,
                node: event.target.closest("[data-batch-open]")
            };
        };
        var onPointerUp = function (event) {
            if (!pointerOrigin) return;
            var distance = Math.hypot(event.clientX - pointerOrigin.x, event.clientY - pointerOrigin.y);
            var draggedNode = pointerOrigin.node;
            pointerOrigin = null;
            if (distance < 7 || !draggedNode) return;
            ignoreNextGraphClickNode = draggedNode;
            window.setTimeout(function () {
                if (ignoreNextGraphClickNode === draggedNode) ignoreNextGraphClickNode = null;
            }, 500);
        };
        graph.addEventListener("click", onClick);
        graph.addEventListener("pointerover", onPointerOver);
        graph.addEventListener("pointerout", onPointerOut);
        graph.addEventListener("focusin", onFocusIn);
        graph.addEventListener("focusout", onFocusOut);
        graph.addEventListener("pointerdown", onPointerDown, {passive: true});
        graph.addEventListener("pointerup", onPointerUp, {passive: true});
        return function () {
            if (hoverScrollTimer) window.clearTimeout(hoverScrollTimer);
            if (autoScrollUnlockTimer) window.clearTimeout(autoScrollUnlockTimer);
            graph.__dagAutoScrollInProgress = false;
            graph.removeEventListener("click", onClick);
            graph.removeEventListener("pointerover", onPointerOver);
            graph.removeEventListener("pointerout", onPointerOut);
            graph.removeEventListener("focusin", onFocusIn);
            graph.removeEventListener("focusout", onFocusOut);
            graph.removeEventListener("pointerdown", onPointerDown);
            graph.removeEventListener("pointerup", onPointerUp);
        };
    }

    function renderDagGraph(graph) {
        var canvas = graph && graph.querySelector("[data-dag-canvas]");
        var svg = canvas && canvas.querySelector("[data-dag-edges]");
        if (!canvas || !svg) return;

        var viewport = graph.querySelector("[data-dag-viewport]");
        var zoomSpace = graph.querySelector("[data-dag-zoom-space]");
        var columns = canvas.querySelector(".dag-columns");
        var scale = dagZoomScale(graph);
        var viewportWidth = viewport ? viewport.clientWidth : 0;
        var viewportHeight = viewport ? viewport.clientHeight : 0;
        var canvasStyle = window.getComputedStyle(canvas);
        var horizontalPadding = parseFloat(canvasStyle.paddingLeft) + parseFloat(canvasStyle.paddingRight);
        var width = Math.max((columns ? columns.offsetWidth : canvas.scrollWidth) + (horizontalPadding || 0), Math.ceil(viewportWidth / scale));
        var height = Math.ceil(viewportHeight / scale) || canvas.scrollHeight || canvas.offsetHeight;
        canvas.style.width = width + "px";
        canvas.style.height = height + "px";
        canvas.style.transform = "scale(" + scale + ")";
        if (zoomSpace) {
            zoomSpace.style.width = Math.ceil(width * scale) + "px";
            zoomSpace.style.height = Math.ceil(height * scale) + "px";
        }
        var canvasRect = canvas.getBoundingClientRect();
        svg.setAttribute("viewBox", "0 0 " + width + " " + height);
        svg.setAttribute("width", width);
        svg.setAttribute("height", height);

        // Match the scrolling Batch region so offscreen ports cannot draw
        // connectors above the Root/Stage headers or below the visible jobs.
        var clipTop = 0;
        var clipBottom = height;
        canvas.querySelectorAll("[data-dag-rank-scroll]").forEach(function (container) {
            var rect = container.getBoundingClientRect();
            clipTop = Math.max(clipTop, (rect.top - canvasRect.top) / scale);
            clipBottom = Math.min(clipBottom, (rect.bottom - canvasRect.top) / scale);
        });
        svg.style.clipPath = "inset(" + Math.max(0, clipTop) + "px 0 " + Math.max(0, height - clipBottom) + "px 0)";

        dagMountVirtualRows(graph);
        dagSyncVirtualEdges(graph, canvasRect);
        var index = graph.__dagIndex || dagIndexGraph(graph);
        var busLayer = svg.querySelector("[data-dag-buses]");
        if (!busLayer) {
            busLayer = document.createElementNS(DAG_SVG_NS, "g");
            busLayer.setAttribute("data-dag-buses", "");
            svg.insertBefore(busLayer, svg.firstChild);
        }
        while (busLayer.firstChild) busLayer.removeChild(busLayer.firstChild);

        var edges = index.edges;
        var bundles = dagBuildBundles(graph, edges, canvasRect, dagPortIndex(canvas));
        Object.keys(bundles).forEach(function (key) {
            createDagBusPath(busLayer, bundles[key]);
        });
        edges.forEach(function (edge) {
            var flow = index.flows[edge.dataset.dagEdge];
            var targetNode = dagNode(graph, edge.dataset.targetId);
            var state = targetNode ? targetNode.dataset.state : edge.dataset.edgeState;
            setDagEdgeState(edge, flow, state);
            var geometry = edge.__dagGeometry;
            if (!geometry) {
                delete edge.dataset.dagBundle;
                edge.removeAttribute("d");
                if (flow) flow.removeAttribute("d");
                return;
            }
            edge.dataset.dagBundle = geometry.bundle.key;
            var path = dagRoutePath(geometry.bundle, geometry.target);
            edge.setAttribute("d", path);
            if (flow) flow.setAttribute("d", path);
        });
        if (graph.__dagFocusNodeId) applyDagHighlight(graph, graph.__dagFocusNodeId);
    }

    function bindDagGraphs(scope) {
        var graphs = [];
        if (scope && scope.matches && scope.matches("[data-dag-graph]")) graphs.push(scope);
        if (scope && scope.querySelectorAll) {
            scope.querySelectorAll("[data-dag-graph]").forEach(function (graph) { graphs.push(graph); });
        }
        graphs.forEach(function (graph) {
            if (graph.__dagCleanup) graph.__dagCleanup();
            dagInitSnapshot(graph);
            var renderFrame = null;
            var render = function () {
                if (renderFrame !== null) return;
                renderFrame = window.requestAnimationFrame(function () {
                    renderFrame = null;
                    if (graph.isConnected) renderDagGraph(graph);
                });
            };
            var resize = render;
            var cleanupInteractions = bindDagGraphInteractions(graph);
            var zoomOut = graph.querySelector("[data-dag-zoom-out]");
            var zoomIn = graph.querySelector("[data-dag-zoom-in]");
            var zoomReset = graph.querySelector("[data-dag-zoom-reset]");
            var fullscreenButton = graph.querySelector("[data-dag-fullscreen]");
            var fullscreenHost = dagFullscreenHost(graph);
            var onZoomOut = function () { dagSetZoom(graph, dagZoomScale(graph) - DAG_ZOOM_STEP); };
            var onZoomIn = function () { dagSetZoom(graph, dagZoomScale(graph) + DAG_ZOOM_STEP); };
            var onZoomReset = function () { dagSetZoom(graph, 1); };
            var onFullscreen = function () { dagToggleFullscreen(graph); };
            var wasFullscreen = !!(fullscreenHost && (document.fullscreenElement === fullscreenHost || fullscreenHost.classList.contains("is-dag-pseudo-fullscreen")));
            var onFullscreenChange = function () {
                var isFullscreen = !!(fullscreenHost && (document.fullscreenElement === fullscreenHost || fullscreenHost.classList.contains("is-dag-pseudo-fullscreen")));
                dagUpdateViewControls(graph);
                render();
                if (wasFullscreen && !isFullscreen) {
                    var currentFullscreenButton = graph.querySelector("[data-dag-fullscreen]");
                    if (currentFullscreenButton) currentFullscreenButton.focus({preventScroll: true});
                }
                wasFullscreen = isFullscreen;
            };
            var onFullscreenKey = function (event) {
                if (event.key !== "Escape" || !fullscreenHost || !fullscreenHost.classList.contains("is-dag-pseudo-fullscreen")) return;
                dagExitPseudoFullscreen(fullscreenHost);
                dagUpdateViewControls(graph);
                var currentFullscreenButton = graph.querySelector("[data-dag-fullscreen]");
                if (currentFullscreenButton) currentFullscreenButton.focus({preventScroll: true});
            };
            if (zoomOut) zoomOut.addEventListener("click", onZoomOut);
            if (zoomIn) zoomIn.addEventListener("click", onZoomIn);
            if (zoomReset) zoomReset.addEventListener("click", onZoomReset);
            if (fullscreenButton) fullscreenButton.addEventListener("click", onFullscreen);
            document.addEventListener("fullscreenchange", onFullscreenChange);
            document.addEventListener("keydown", onFullscreenKey);
            window.addEventListener("resize", resize);
            var observer = null;
            var scrollBindings = [];
            var canvas = graph.querySelector("[data-dag-canvas]");
            if (window.ResizeObserver && canvas) {
                observer = new ResizeObserver(render);
                observer.observe(canvas);
            }
            var focusNode = function (event) {
                var node = event.target.closest("[data-dag-node]");
                if (!node || !graph.contains(node) || node.contains(event.relatedTarget)) return;
                if (graph.__dagAutoScrollInProgress && !node.contains(document.activeElement)) return;
                graph.__dagHoverNodeId = node.dataset.nodeId;
                graph.__dagMagnetAnchorNodeId = node.dataset.nodeId;
                setDagActiveColumn(graph, node.closest("[data-rank-id]"));
                setDagHighlight(graph, node.dataset.nodeId);
            };
            var blurNode = function (event) {
                var node = event.target.closest("[data-dag-node]");
                if (!node || !graph.contains(node) || node.contains(event.relatedTarget)) return;
                if (graph.__dagHoverNodeId === node.dataset.nodeId) graph.__dagHoverNodeId = null;
                setDagHighlight(graph, graph.__dagHoverNodeId || graph.__dagSelectedNodeId || graph.__dagAutoScrollNodeId || null);
            };
            graph.addEventListener("pointerover", focusNode);
            graph.addEventListener("pointerout", blurNode);
            graph.addEventListener("focusin", focusNode);
            graph.addEventListener("focusout", blurNode);
            var viewport = graph.querySelector("[data-dag-viewport]");
            if (viewport) viewport.addEventListener("scroll", render, {passive: true});
            var onVirtualKey = function (event) {
                if (["ArrowUp", "ArrowDown", "Home", "End"].indexOf(event.key) === -1 || event.altKey || event.ctrlKey || event.metaKey) return;
                var node = event.target.closest("[data-dag-node]");
                var record = node && graph.__dagVirtualRecords[node.dataset.nodeId];
                if (!record) return;
                var count = record.list.__dagRecords.length;
                var position = event.key === "Home" ? 0 : event.key === "End" ? count - 1 : record.position + (event.key === "ArrowDown" ? 1 : -1);
                position = Math.max(0, Math.min(count - 1, position));
                event.preventDefault();
                dagCancelMagnet(graph);
                var next = dagRevealVirtualRow(graph, record.list.__dagRecords[position].task.id);
                var trigger = next && next.querySelector("[data-open-batch], [data-node-status]");
                if (trigger) trigger.focus({preventScroll: true});
            };
            graph.addEventListener("keydown", onVirtualKey);
            graph.querySelectorAll("[data-dag-rank-scroll]").forEach(function (container) {
                var rank = container.closest("[data-rank-id]");
                var alignTimer = null;
                var scheduleAlignment = function () {
                    if (graph.__dagMagnetAdjusting) return;
                    if (alignTimer) window.clearTimeout(alignTimer);
                    alignTimer = window.setTimeout(function () {
                        var anchor = dagAnchorNodeForColumn(graph, container);
                        if (!anchor || graph.__dagMagnetAdjusting) return;
                        graph.__dagMagnetAnchorNodeId = anchor.dataset.nodeId;
                        dagAlignPreviousColumn(graph, anchor);
                    }, 110);
                };
                var onScroll = function () {
                    render();
                    if (graph.__dagLayoutAdjusting) return;
                    if (graph.__dagMagnetAdjusting) {
                        if (graph.__dagMagnetContainer === container) dagScheduleMagnetUnlock(graph, 180);
                        return;
                    }
                    if (rank) setDagActiveColumn(graph, rank);
                    scheduleAlignment();
                };
                container.addEventListener("scroll", onScroll, {passive: true});
                scrollBindings.push({
                    container: container,
                    onScroll: onScroll,
                    scheduleAlignment: scheduleAlignment,
                    clearTimer: function () { if (alignTimer) window.clearTimeout(alignTimer); }
                });
            });
            var onWheel = function (event) {
                var rank = event.target.closest("[data-rank-id]");
                var container = rank && rank.querySelector("[data-dag-rank-scroll]");
                if (!container) {
                    if (event.deltaY && event.target.closest("[data-dag-viewport]")) event.preventDefault();
                    return;
                }
                dagCancelMagnet(graph);
                setDagActiveColumn(graph, rank);
                if (!event.deltaY) return;
                if (!container.contains(event.target)) {
                    event.preventDefault();
                    container.scrollTop += event.deltaY / dagZoomScale(graph);
                }
            };
            var onTouchStart = function () { dagCancelMagnet(graph); };
            graph.addEventListener("wheel", onWheel, {passive: false});
            graph.addEventListener("touchstart", onTouchStart, {passive: true});
            graph.__dagRender = render;
            graph.__dagCleanup = function () {
                if (renderFrame !== null) window.cancelAnimationFrame(renderFrame);
                if (graph.__dagCardTimer) window.clearTimeout(graph.__dagCardTimer);
                graph.__dagCardTimer = null;
                if (graph.__dagCardRequest) graph.__dagCardRequest.abort();
                graph.__dagCardRequest = null;
                window.removeEventListener("resize", resize);
                document.removeEventListener("fullscreenchange", onFullscreenChange);
                document.removeEventListener("keydown", onFullscreenKey);
                if (zoomOut) zoomOut.removeEventListener("click", onZoomOut);
                if (zoomIn) zoomIn.removeEventListener("click", onZoomIn);
                if (zoomReset) zoomReset.removeEventListener("click", onZoomReset);
                if (fullscreenButton) fullscreenButton.removeEventListener("click", onFullscreen);
                if (observer) observer.disconnect();
                dagCancelMagnet(graph);
                graph.removeEventListener("wheel", onWheel);
                graph.removeEventListener("touchstart", onTouchStart);
                cleanupInteractions();
                graph.removeEventListener("pointerover", focusNode);
                graph.removeEventListener("pointerout", blurNode);
                graph.removeEventListener("focusin", focusNode);
                graph.removeEventListener("focusout", blurNode);
                graph.removeEventListener("keydown", onVirtualKey);
                if (viewport) viewport.removeEventListener("scroll", render);
                scrollBindings.forEach(function (binding) {
                    binding.container.removeEventListener("scroll", binding.onScroll);
                    binding.clearTimer();
                });
            };
            render();
            dagUpdateViewControls(graph);
        });
    }

    function updateBatchState(nodeId, newState) {
        var state = normaliseDagState(newState);
        document.querySelectorAll("[data-dag-graph]").forEach(function (graph) {
            var taskId = String(nodeId).indexOf("batch-") === 0 ? String(nodeId) : "batch-" + nodeId;
            var task = graph.__dagTasks && graph.__dagTasks[taskId];
            if (task) {
                task.state = state;
                task.status = {
                    label: graph.dataset["label" + state.charAt(0).toUpperCase() + state.slice(1)],
                    tone: DAG_TONES[state], icon: DAG_STATUS_SYMBOLS[state],
                };
            }
            var node = dagNode(graph, nodeId);
            if (!node) {
                if (task && graph.__dagRender) graph.__dagRender();
                return;
            }
            DAG_STATES.forEach(function (knownState) {
                node.classList.remove("dag-node--" + knownState);
            });
            node.classList.add("dag-node--" + state);
            node.classList.toggle("is-live", state === "running");
            node.dataset.state = state;
            var tone = DAG_TONES[state];
            var badge = node.querySelector("[data-node-status]");
            if (badge && tone) {
                ["muted", "retry", "running", "success", "error"].forEach(function (knownTone) {
                    badge.classList.remove("dag-badge--" + knownTone);
                });
                badge.classList.add("dag-badge--" + tone);
            }
            var labelKey = state.charAt(0).toUpperCase() + state.slice(1);
            var label = node.dataset["label" + labelKey];
            var status = node.querySelector("[data-node-status]");
            var statusLabel = status && status.querySelector("[data-status-label]");
            if (label && statusLabel) {
                statusLabel.textContent = label;
                status.setAttribute("aria-label", label);
                status.setAttribute("title", label);
                status.dataset.dagTooltip = label;
                node.setAttribute("aria-label", (node.dataset.nodeLabel || "Batch") + " · " + label);
                var statusIcon = status.querySelector("[data-status-icon]");
                if (statusIcon) statusIcon.textContent = DAG_STATUS_SYMBOLS[state] || "?";
            }
            if (graph.__dagRender) graph.__dagRender();
            else renderDagGraph(graph);
        });
    }

    // WebSocket/SSE consumers can call this without knowing anything about
    // the SVG layer. The server remains free to send either the UUID or the
    // rendered `batch-<uuid>` node identifier.
    window.updateBatchState = updateBatchState;

    function graphNodeKeys(wrapper) {
        var keys = {};
        if (!wrapper) return keys;
        wrapper.querySelectorAll("[data-graph-node]").forEach(function (node) {
            keys[node.dataset.graphNode] = true;
        });
        return keys;
    }

    function animateNewGraphNodes(wrapper, previousKeys) {
        if (!wrapper) return;
        wrapper.querySelectorAll("[data-graph-node]").forEach(function (node) {
            if (previousKeys[node.dataset.graphNode]) return;
            node.classList.add("is-arriving");
            window.setTimeout(function () {
                node.classList.remove("is-arriving");
                var graph = node.closest("[data-dag-graph]");
                if (graph && graph.__dagRender) graph.__dagRender();
            }, 1800);
        });
    }

    function dagFocusDescriptor() {
        var active = document.activeElement;
        var graphControl = active && active.closest
            ? active.closest("[data-dag-zoom-out], [data-dag-zoom-reset], [data-dag-zoom-in], [data-dag-fullscreen]")
            : null;
        if (graphControl) {
            var controlName = ["data-dag-zoom-out", "data-dag-zoom-reset", "data-dag-zoom-in", "data-dag-fullscreen"].find(function (name) {
                return graphControl.hasAttribute(name);
            });
            if (controlName) return {kind: "view-control", attribute: controlName};
        }
        var node = active && active.closest ? active.closest("[data-dag-node][data-node-id]") : null;
        if (!node || (dagDetailDialog && dagDetailDialog.contains(active))) return null;
        var descriptor = {nodeId: node.dataset.nodeId, kind: "node"};
        if (active.matches("[data-dag-tooltip]")) {
            descriptor.kind = "metric";
            descriptor.tooltip = active.dataset.dagTooltip;
        } else if (active.matches("[data-stage-open]")) {
            descriptor.kind = "stage";
            descriptor.stageKey = active.dataset.stageKey;
        } else if (active.matches("[data-dag-metric-more]")) {
            descriptor.kind = "more";
            descriptor.metricListIndex = Array.prototype.indexOf.call(node.querySelectorAll("[data-dag-metric-more]"), active);
        } else if (active.matches("[data-open-batch]")) {
            descriptor.kind = "batch";
        }
        return descriptor;
    }

    function restoreDagFocus(wrapper, descriptor) {
        if (!descriptor || !wrapper) return;
        if (descriptor.kind === "view-control") {
            var graphControl = wrapper.querySelector("[data-dag-graph] [" + descriptor.attribute + "]");
            if (graphControl && graphControl.focus) graphControl.focus({preventScroll: true});
            return;
        }
        var nodes = wrapper.querySelectorAll("[data-dag-node][data-node-id]");
        var node = Array.prototype.find.call(nodes, function (candidate) {
            return candidate.dataset.nodeId === descriptor.nodeId;
        });
        if (!node) return;
        var target = node;
        if (descriptor.kind === "stage") {
            target = Array.prototype.find.call(node.querySelectorAll("[data-stage-open]"), function (candidate) {
                return candidate.dataset.stageKey === descriptor.stageKey;
            }) || node;
        } else if (descriptor.kind === "batch") {
            target = node.querySelector("[data-open-batch]") || node;
        } else if (descriptor.kind === "metric") {
            target = Array.prototype.find.call(node.querySelectorAll("[data-dag-tooltip]"), function (candidate) {
                return candidate.dataset.dagTooltip === descriptor.tooltip;
            }) || node;
        } else if (descriptor.kind === "more") {
            target = node.querySelectorAll("[data-dag-metric-more]")[descriptor.metricListIndex] || node;
        }
        if (target && target.focus) target.focus({preventScroll: true});
    }

    function replaceGraphFragment(graphWrapper, graphHtml) {
        var previousKeys = graphNodeKeys(graphWrapper);
        var currentGraph = graphWrapper.querySelector("[data-dag-graph]");
        var currentViewport = graphWrapper.querySelector("[data-dag-viewport]");
        var scrollPosition = currentViewport ? {left: currentViewport.scrollLeft, top: currentViewport.scrollTop} : null;
        if (scrollPosition && currentGraph) {
            scrollPosition.centerX = (scrollPosition.left + currentViewport.clientWidth / 2) / dagZoomScale(currentGraph);
        }
        var rankScrollPositions = {};
        graphWrapper.querySelectorAll("[data-dag-rank-scroll]").forEach(function (container) {
            var rank = container.closest("[data-rank-id]");
            if (rank) rankScrollPositions[rank.dataset.rankId] = container.scrollTop;
        });
        var focusDescriptor = dagFocusDescriptor();
        var focusedNodeId = currentGraph && currentGraph.__dagFocusNodeId;
        var selectedNodeId = currentGraph && currentGraph.__dagSelectedNodeId;
        var autoScrollNodeId = currentGraph && currentGraph.__dagAutoScrollNodeId;
        var activeRankId = currentGraph && currentGraph.__dagActiveRankId;
        var magnetAnchorNodeId = currentGraph && currentGraph.__dagMagnetAnchorNodeId;
        var zoomScale = currentGraph && dagZoomScale(currentGraph);
        if (currentGraph && currentGraph.__dagCleanup) currentGraph.__dagCleanup();
        graphWrapper.innerHTML = graphHtml;
        animateNewGraphNodes(graphWrapper, previousKeys);
        bindDagGraphs(graphWrapper);
        var nextGraph = graphWrapper.querySelector("[data-dag-graph]");
        if (nextGraph) {
            nextGraph.__dagZoomScale = zoomScale || 1;
            nextGraph.__dagSelectedNodeId = selectedNodeId || null;
            nextGraph.__dagAutoScrollNodeId = autoScrollNodeId || null;
            nextGraph.__dagMagnetAnchorNodeId = magnetAnchorNodeId || null;
            if (activeRankId) {
                var activeRank = Array.prototype.find.call(nextGraph.querySelectorAll("[data-rank-id]"), function (rank) {
                    return rank.dataset.rankId === activeRankId;
                });
                if (activeRank) setDagActiveColumn(nextGraph, activeRank);
            }
            if (focusedNodeId || selectedNodeId || autoScrollNodeId) {
                setDagHighlight(nextGraph, focusedNodeId || selectedNodeId || autoScrollNodeId);
            }
        }
        refreshOpenDagDetail(graphWrapper);
        window.requestAnimationFrame(function () {
            var viewport = graphWrapper.querySelector("[data-dag-viewport]");
            if (viewport && scrollPosition) {
                viewport.scrollLeft = scrollPosition.left;
                viewport.scrollTop = scrollPosition.top;
            }
            graphWrapper.querySelectorAll("[data-dag-rank-scroll]").forEach(function (container) {
                var rank = container.closest("[data-rank-id]");
                if (rank && Object.prototype.hasOwnProperty.call(rankScrollPositions, rank.dataset.rankId)) {
                    container.scrollTop = rankScrollPositions[rank.dataset.rankId];
                }
            });
            if (nextGraph) {
                renderDagGraph(nextGraph);
                var nextViewport = nextGraph.querySelector("[data-dag-viewport]");
                if (nextViewport && scrollPosition) {
                    nextViewport.scrollLeft = Math.max(0, scrollPosition.centerX * dagZoomScale(nextGraph) - nextViewport.clientWidth / 2);
                }
                dagUpdateViewControls(nextGraph);
            }
            restoreDagFocus(graphWrapper, focusDescriptor);
        });
    }

    function updateDagJobHeaders(graph, jobs) {
        jobs.forEach(function (job) {
            graph.__dagJobStates[job.job_id] = job.status_code;
            var group = graph.querySelector("[data-dag-job='" + job.job_id + "']");
            if (!group) return;
            var count = group.querySelector("[data-job-batch-count]");
            if (count && job.batch_count_label) count.textContent = job.batch_count_label;
            if (group.dataset.jobStatus === job.status_code) return;
            group.dataset.jobStatus = job.status_code;
            var badge = group.querySelector(".dag-job-header .dag-status-icon");
            if (badge) {
                badge.className = "dag-badge dag-badge--" + job.status.tone + " dag-status-icon";
                badge.dataset.dagTooltip = job.status.label;
                badge.setAttribute("aria-label", job.status.label);
                badge.title = job.status.label;
                badge.querySelector("span").textContent = job.status.icon;
            }
        });
    }

    function applyDagStructure(graph, snapshot) {
        if (!snapshot.virtual || !graph.__dagSnapshot.virtual) return false;
        function ranks(data) {
            return JSON.stringify(data.columns.map(function (column) {
                return [column.id, column.jobs.map(function (job) { return job.job_id; })];
            }));
        }
        if (ranks(snapshot) !== ranks(graph.__dagSnapshot)) return false;
        var focusDescriptor = dagFocusDescriptor();
        var scrollPositions = [];
        graph.querySelectorAll("[data-dag-rank-scroll]").forEach(function (container) {
            scrollPositions.push({container: container, top: container.scrollTop});
        });
        hideDagMetricTooltip();
        graph.__dagVirtualLists.forEach(function (list) {
            list.__dagMounted.forEach(function (node) {
                var cached = graph.__dagCardCache.get(node.dataset.nodeId);
                if (cached) cached.expanded = dagExpandedMetrics(node);
                node.remove();
            });
        });
        dagInitSnapshot(graph, snapshot);
        updateDagJobHeaders(graph, snapshot.columns.flatMap(function (column) { return column.jobs; }));
        snapshot.columns.forEach(function (column) {
            var rank = graph.querySelector("[data-rank-id='" + column.id + "']");
            rank.dataset.taskCount = column.task_count;
            var count = rank.querySelector(".dag-rank-count");
            count.textContent = column.task_count;
            count.title = column.task_count_label;
        });
        graph.querySelector("[data-dag-task-count]").textContent = snapshot.task_count_label;
        scrollPositions.forEach(function (position) { position.container.scrollTop = position.top; });
        renderDagGraph(graph);
        restoreDagFocus(graph, focusDescriptor);
        refreshOpenDagDetail(graph.closest("#job-graph"));
        return true;
    }

    function applyDagUpdates(graph, payload) {
        var focusDescriptor = dagFocusDescriptor();
        var virtual = graph.__dagSnapshot.virtual;
        var changed = false;
        (payload.updates || []).forEach(function (task) {
            var previous = graph.__dagTasks[task.id];
            if (!previous || previous.revision === task.revision) return;
            graph.__dagTasks[task.id] = task;
            graph.__dagDetailCache.delete(task.id);
            var node = dagNode(graph, task.id);
            var record = graph.__dagVirtualRecords[task.id];
            if (record) record.task = task;
            if (!node) return;
            var next;
            if (virtual && !task.placeholder) {
                // Keep the existing flow visible while its changed card is fetched.
                node.dataset.nodeRevision = task.revision;
                updateBatchState(task.id, task.state);
                changed = true;
                return;
            } else if (virtual) {
                next = dagLoadingNode(graph, record);
                record.list.__dagMounted.set(record.position, next);
            } else if (task.html) {
                var template = document.createElement("template");
                template.innerHTML = task.html;
                next = template.content.querySelector("[data-dag-node]");
            }
            if (!next) return;
            delete task.html;
            dagRestoreExpandedMetrics(next, dagExpandedMetrics(node));
            if (dagMetricTooltipAnchor && node.contains(dagMetricTooltipAnchor)) hideDagMetricTooltip();
            node.replaceWith(next);
            changed = true;
        });
        updateDagJobHeaders(graph, payload.jobs || []);
        graph.__dagSnapshot.cursor = payload.cursor;
        graph.__dagSnapshot.revision = payload.revision;
        if (changed) {
            dagIndexGraph(graph);
            restoreDagFocus(graph, focusDescriptor);
        }
        graph.__dagRender();
        refreshOpenDagDetail(graph.closest("#job-graph"));
    }

    function refreshDagGraph(graphWrapper) {
        var graph = graphWrapper.querySelector("[data-dag-graph]");
        var snapshot = graph && graph.__dagSnapshot;
        var url = new URL(graphWrapper.dataset.graphUrl, window.location.href);
        if (snapshot) {
            url.searchParams.set("format", "json");
            url.searchParams.set("since", snapshot.cursor);
            url.searchParams.set("topology", snapshot.topology);
            url.searchParams.set("revision", snapshot.revision);
            url.searchParams.set("jobs", JSON.stringify(graph.__dagJobStates));
        }
        return fetch(url.toString(), {headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (response) {
                if (!response.ok) throw new Error("graph refresh failed");
                return snapshot ? response.json() : response.text();
            })
            .then(function (payload) {
                if (!snapshot) return replaceGraphFragment(graphWrapper, payload);
                if (payload.unchanged) {
                    snapshot.cursor = payload.cursor;
                    return;
                }
                if (!payload.structure) return applyDagUpdates(graph, payload);
                if (applyDagStructure(graph, payload.structure)) return;
                return fetch(graphWrapper.dataset.graphUrl, {headers: {"X-Requested-With": "XMLHttpRequest"}})
                    .then(function (response) {
                        if (!response.ok) throw new Error("graph refresh failed");
                        return response.text();
                    })
                    .then(function (html) { replaceGraphFragment(graphWrapper, html); });
            });
    }

    function refreshJob(url, wrapper, graphWrapper) {
        return fetch(url, {headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (response) { if (!response.ok) throw new Error("refresh failed"); return response.text(); })
            .then(function (html) {
                // Keep keyboard focus on retry controls while live metrics refresh.
                if (wrapper.__lastFragment !== html && !wrapper.contains(document.activeElement)) {
                    wrapper.__lastFragment = html;
                    wrapper.innerHTML = html;
                }
                var marker = wrapper.querySelector("[data-current-active]");
                if (marker) wrapper.dataset.active = marker.dataset.currentActive;
                if (graphWrapper && graphWrapper.dataset.graphUrl) {
                    return refreshDagGraph(graphWrapper);
                }
            })
            .catch(function () { /* A transient refresh failure must not break the page. */ });
    }

    function bindPolling() {
        var wrapper = document.getElementById("job-live");
        if (!wrapper || wrapper.dataset.active !== "true") return;
        var graphWrapper = document.getElementById("job-graph");
        var refreshing = false;
        wrapper.classList.add("is-polling");
        if (graphWrapper) graphWrapper.classList.add("is-polling");
        window.setInterval(function () {
            if (wrapper.dataset.active !== "true" || refreshing || document.hidden) return;
            refreshing = true;
            refreshJob(wrapper.dataset.liveUrl, wrapper, graphWrapper).finally(function () {
                refreshing = false;
            });
        }, 10000);
    }

    function bindMessageSearch() {
        var searchRoot = document.getElementById("message-search-content");
        if (!searchRoot || document.documentElement.dataset.messageSearchBound === "true") return;
        document.documentElement.dataset.messageSearchBound = "true";
        var activeSearchController = null;
        var retrievalCountController = null;
        var retrievalCountRequestId = 0;
        var retrievalCountActiveKey = null;
        var retrievalCountCache = new Map();
        var retrievalCountAttempts = new Set();
        var searchTotalCount = null;
        var searchSidebarCollapsed = false;

        function setSidebarCollapsed(root, collapsed) {
            if (!root) return;
            var layout = root.querySelector(".message-search-layout");
            var sidebar = root.querySelector("[data-search-sidebar]");
            var content = root.querySelector("[data-search-sidebar-content]");
            var toggle = root.querySelector("[data-search-sidebar-toggle]");
            if (!layout || !sidebar || !content || !toggle) return;
            layout.classList.toggle("message-search-layout--sidebar-collapsed", collapsed);
            sidebar.classList.toggle("is-collapsed", collapsed);
            content.hidden = collapsed;
            var label = collapsed ? toggle.dataset.expandLabel : toggle.dataset.collapseLabel;
            toggle.setAttribute("aria-expanded", String(!collapsed));
            toggle.setAttribute("aria-label", label);
            toggle.title = label;
        }

        function retrievalCountKey(url) {
            var allowedFilters = {
                room: true,
                tag: true,
                tag_match: true,
                is_reply: true,
                stored_since: true,
                createdat_min: true,
                createdat_max: true
            };
            var filters = new URLSearchParams();
            url.searchParams.forEach(function (value, name) {
                if (allowedFilters[name]) filters.append(name, value);
            });
            filters.sort();
            return url.pathname + "?" + filters.toString();
        }

        function parseGooglePhraseSearch(value) {
            var text = String(value || "").trim();
            if (!text || text.charAt(0) !== '"') return null;

            var phrases = [];
            var operator = null;
            var cursor = 0;
            while (cursor < text.length) {
                if (text.charAt(cursor) !== '"') return null;
                cursor += 1;
                var phrase = "";
                var closed = false;
                while (cursor < text.length) {
                    var character = text.charAt(cursor);
                    if (character === "\\" && cursor + 1 < text.length) {
                        phrase += text.charAt(cursor + 1);
                        cursor += 2;
                    } else if (character === '"') {
                        cursor += 1;
                        closed = true;
                        break;
                    } else {
                        phrase += character;
                        cursor += 1;
                    }
                }
                if (!closed || !phrase.trim()) return null;
                phrases.push(phrase.trim());

                var beforeSpacing = cursor;
                while (cursor < text.length && /\s/.test(text.charAt(cursor))) cursor += 1;
                if (cursor === text.length) break;
                if (cursor === beforeSpacing) return null;

                var operatorMatch = /^(AND|OR)\b/i.exec(text.slice(cursor));
                if (!operatorMatch) return null;
                var nextOperator = operatorMatch[1].toLowerCase();
                if (operator && operator !== nextOperator) return null;
                operator = nextOperator;
                cursor += operatorMatch[0].length;

                var afterOperator = cursor;
                while (cursor < text.length && /\s/.test(text.charAt(cursor))) cursor += 1;
                if (cursor === afterOperator || text.charAt(cursor) !== '"') return null;
            }

            return phrases.length ? {phrases: phrases, operator: operator || "or"} : null;
        }

        function compactSearchCount(value) {
            var number = Number(value);
            if (!Number.isFinite(number)) return String(value);
            var absolute = Math.abs(number);
            if (absolute < 1000) return String(Math.trunc(number));

            var units = [
                {size: 1e12, suffix: "T", precision: 2},
                {size: 1e9, suffix: "B", precision: 2},
                {size: 1e6, suffix: "M", precision: 2},
                {size: 1e3, suffix: "K", precision: 1}
            ];
            var unit = units.find(function (candidate) { return absolute >= candidate.size; });
            var factor = Math.pow(10, unit.precision);
            var compact = Math.floor((absolute / unit.size) * factor) / factor;
            var formatted = compact.toFixed(unit.precision).replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1");
            return (number < 0 ? "-" : "") + formatted + unit.suffix;
        }

        function renderSearchCount(root, loaded, total) {
            if (!root) return;
            var bubble = root.querySelector("[data-search-count-bubble]");
            if (!bubble) return;
            var loadedNode = bubble.querySelector("[data-search-count-loaded]");
            var totalNode = bubble.querySelector("[data-search-count-total]");
            loaded = Number(loaded);
            if (!Number.isFinite(loaded)) loaded = 0;
            if (loadedNode) loadedNode.textContent = compactSearchCount(loaded);
            if (totalNode) {
                totalNode.textContent = total === null || total === undefined || total === ""
                    ? "…"
                    : compactSearchCount(total);
            }
            root.dataset.searchLoadedCount = String(loaded);
            root.dataset.searchTotalCount = total === null || total === undefined || total === ""
                ? ""
                : String(total);
        }

        function cancelRetrievalCount() {
            retrievalCountRequestId += 1;
            if (retrievalCountActiveKey) retrievalCountAttempts.delete(retrievalCountActiveKey);
            if (retrievalCountController) {
                retrievalCountController.abort();
            }
            retrievalCountController = null;
            retrievalCountActiveKey = null;
        }

        function loadRetrievalCount(url) {
            if (!window.fetch) return;
            var key = retrievalCountKey(url);
            var root = document.getElementById("message-search-content");
            if (!root || !root.dataset.searchCountUrl) return;
            if (retrievalCountCache.has(key)) {
                searchTotalCount = retrievalCountCache.get(key);
                if (root.dataset.searchCountKey === key) {
                    renderSearchCount(root, root.dataset.searchLoadedCount, searchTotalCount);
                }
                return;
            }
            if (retrievalCountActiveKey === key) return;
            cancelRetrievalCount();
            retrievalCountController = window.AbortController ? new AbortController() : null;
            var requestId = ++retrievalCountRequestId;
            retrievalCountActiveKey = key;
            retrievalCountAttempts.add(key);
            searchTotalCount = null;

            var countUrl = new URL(root.dataset.searchCountUrl, window.location.href);
            countUrl.search = url.search;
            var options = {
                credentials: "same-origin",
                headers: {"X-Requested-With": "XMLHttpRequest"}
            };
            if (retrievalCountController) options.signal = retrievalCountController.signal;
            fetch(countUrl.href, options)
                .then(function (response) {
                    if (!response.ok) throw new Error("Search count failed");
                    return response.json();
                })
                .then(function (payload) {
                    if (requestId !== retrievalCountRequestId) return;
                    retrievalCountController = null;
                    retrievalCountActiveKey = null;
                    var total = Number(payload.count);
                    if (!Number.isFinite(total)) throw new Error("Invalid search count");
                    retrievalCountCache.set(key, total);
                    searchTotalCount = total;
                    var current = document.getElementById("message-search-content");
                    if (current && current.dataset.searchCountKey === key) {
                        renderSearchCount(current, current.dataset.searchLoadedCount, total);
                    }
                })
                .catch(function (error) {
                    if (error.name === "AbortError" || requestId !== retrievalCountRequestId) return;
                    retrievalCountController = null;
                    retrievalCountActiveKey = null;
                    retrievalCountCache.set(key, "?");
                    searchTotalCount = "?";
                    var current = document.getElementById("message-search-content");
                    if (current && current.dataset.searchCountKey === key) {
                        renderSearchCount(current, current.dataset.searchLoadedCount, "?");
                    }
                });
        }

        function syncSearchCount(root, url, requestMissingCount) {
            if (!root || !root.querySelector("[data-search-count-bubble]")) return;
            var loaded = root.dataset.searchLoadedCount;
            if (root.dataset.searchMode === "elasticsearch") {
                cancelRetrievalCount();
                var elasticCount = root.dataset.searchTotalCount;
                searchTotalCount = elasticCount === "" || !Number.isFinite(Number(elasticCount))
                    ? null
                    : Number(elasticCount);
                renderSearchCount(root, loaded, searchTotalCount);
                return;
            }

            var key = retrievalCountKey(url);
            root.dataset.searchCountKey = key;
            if (retrievalCountCache.has(key)) {
                searchTotalCount = retrievalCountCache.get(key);
            } else {
                searchTotalCount = null;
                if (requestMissingCount && !retrievalCountAttempts.has(key)) {
                    loadRetrievalCount(url);
                }
            }
            renderSearchCount(root, loaded, searchTotalCount);
        }

        function syncMediaTypeFilter(form) {
            if (!form) return;
            var dropdown = form.querySelector("[data-media-type-dropdown]");
            var valueInput = form.querySelector("[data-media-type-value]");
            if (!dropdown || !valueInput) return;
            var selected = Array.from(dropdown.querySelectorAll("[data-media-type-option]:checked"));
            valueInput.value = selected.map(function (option) { return option.value; }).join("__");
            var summary = dropdown.querySelector("[data-media-type-summary]");
            if (summary) {
                var labels = selected.map(function (option) {
                    var label = option.nextElementSibling;
                    return label ? label.textContent.trim() : option.value;
                });
                summary.textContent = labels.length ? labels.join(", ") : dropdown.dataset.allLabel;
            }
        }

        function updateSearchCountPosition() {
            var bubble = document.querySelector("#message-search-content [data-search-count-bubble]");
            var footer = document.querySelector(".site-footer");
            if (!bubble || !footer) return;
            var footerTop = footer.getBoundingClientRect().top;
            var footerOffset = Math.max(0, window.innerHeight - footerTop + 16);
            var moreResults = document.querySelector("#message-search-content [data-search-more]");
            if (footerTop < window.innerHeight && moreResults) {
                var moreBottom = moreResults.getBoundingClientRect().bottom;
                if (moreBottom >= bubble.offsetHeight && moreBottom < window.innerHeight) {
                    footerOffset = Math.max(footerOffset, window.innerHeight - moreBottom);
                }
            }
            bubble.style.setProperty("--search-count-footer-offset", footerOffset + "px");
        }

        function refreshMode(form) {
            if (!form) return;
            var modeToggle = form.querySelector("[data-search-mode-toggle]");
            var selectedMode = modeToggle && modeToggle.checked ? "elasticsearch" : "retrieval";
            var modeValue = form.querySelector("[data-search-mode-value]");
            if (modeValue) modeValue.value = selectedMode;
            form.querySelectorAll("[data-search-mode-fields]").forEach(function (fields) {
                var enabled = fields.dataset.searchModeFields === selectedMode;
                fields.hidden = !enabled;
                fields.querySelectorAll("input, select, textarea").forEach(function (field) {
                    field.disabled = !enabled;
                });
            });
            var query = form.querySelector("[data-search-query]");
            if (query) {
                query.disabled = selectedMode !== "elasticsearch";
                query.placeholder = selectedMode === "retrieval"
                    ? query.dataset.retrievalPlaceholder
                    : query.dataset.elasticsearchPlaceholder;
                if (query.disabled) query.value = "";
            }
        }

        function syncSearchOrdering(form, ascending) {
            if (!form) return;
            var ordering = form.querySelector("[data-search-ordering]");
            var toggle = form.querySelector("[data-search-order-toggle]");
            if (!ordering || !toggle) return;
            ordering.value = ascending ? "created_at,msg_id" : "-created_at,-msg_id";
            toggle.classList.toggle("is-ascending", ascending);
            toggle.setAttribute("aria-pressed", String(ascending));
            var label = ascending ? toggle.dataset.oldestLabel : toggle.dataset.newestLabel;
            toggle.setAttribute("aria-label", label);
            toggle.title = label;
        }

        function loadSearchContent(url, updateHistory) {
            var current = document.getElementById("message-search-content");
            if (!current) return Promise.resolve();
            if (activeSearchController) activeSearchController.abort();
            activeSearchController = window.AbortController ? new AbortController() : null;
            current.setAttribute("aria-busy", "true");
            var options = {
                credentials: "same-origin",
                headers: {"X-Requested-With": "XMLHttpRequest"}
            };
            if (activeSearchController) options.signal = activeSearchController.signal;
            return fetch(url.href, options)
                .then(function (response) {
                    if (!response.ok) throw new Error("Search refresh failed");
                    return response.text();
                })
                .then(function (html) {
                    var parsed = new DOMParser().parseFromString(html, "text/html");
                    var updated = parsed.getElementById("message-search-content");
                    if (!updated) {
                        window.location.assign(url.href);
                        return;
                    }
                    if (updateHistory) {
                        window.history.pushState({}, "", url.pathname + url.search);
                    }
                    current.replaceWith(updated);
                    bindAutocompleteEditors(updated);
                    var updatedForm = updated.querySelector("[data-message-search-form]");
                    refreshMode(updatedForm);
                    syncMediaTypeFilter(updatedForm);
                    setSidebarCollapsed(updated, searchSidebarCollapsed);
                    syncSearchCount(updated, url, false);
                    updateSearchCountPosition();
                    var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
                    window.scrollTo({top: 0, behavior: reduceMotion ? "auto" : "smooth"});
                })
                .catch(function (error) {
                    if (error.name === "AbortError") return;
                    window.location.assign(url.href);
                });
        }

        function loadMoreMessages(url) {
            var current = document.getElementById("message-search-content");
            var currentList = current && current.querySelector(".message-search-list");
            if (!current || !currentList) return loadSearchContent(url, false);
            if (activeSearchController) activeSearchController.abort();
            activeSearchController = window.AbortController ? new AbortController() : null;
            current.setAttribute("aria-busy", "true");
            var options = {
                credentials: "same-origin",
                headers: {"X-Requested-With": "XMLHttpRequest"}
            };
            if (activeSearchController) options.signal = activeSearchController.signal;
            return fetch(url.href, options)
                .then(function (response) {
                    if (!response.ok) throw new Error("Search page load failed");
                    return response.text();
                })
                .then(function (html) {
                    var parsed = new DOMParser().parseFromString(html, "text/html");
                    var updated = parsed.getElementById("message-search-content");
                    if (!updated) {
                        window.location.assign(url.href);
                        return;
                    }

                    var incomingList = updated.querySelector(".message-search-list");
                    if (incomingList) {
                        var knownLinks = new Set(Array.from(currentList.querySelectorAll(".message-search-result h2 a"))
                            .map(function (link) { return link.href; }));
                        Array.from(incomingList.children).forEach(function (article) {
                            var link = article.querySelector("h2 a");
                            var href = link ? link.href : "";
                            if (href && knownLinks.has(href)) return;
                            article.classList.add("message-search-result--incoming");
                            currentList.appendChild(article);
                            if (href) knownLinks.add(href);
                        });
                    }

                    var count = current.querySelector("[data-search-result-count]");
                    if (count) {
                        count.textContent = count.dataset.searchCountFormat
                            .replace("{count}", String(currentList.children.length));
                    }
                    if (updated.dataset.searchMode === "elasticsearch") {
                        var incomingTotal = updated.dataset.searchTotalCount;
                        if (incomingTotal !== "" && Number.isFinite(Number(incomingTotal))) {
                            searchTotalCount = Number(incomingTotal);
                        }
                    }
                    renderSearchCount(current, currentList.children.length, searchTotalCount);
                    updateSearchCountPosition();

                    var currentMore = current.querySelector("[data-search-more]");
                    var updatedMore = updated.querySelector("[data-search-more]");
                    if (currentMore && updatedMore) {
                        currentMore.href = updatedMore.href;
                    } else if (currentMore) {
                        currentMore.remove();
                    }
                    var pagination = current.querySelector(".message-search-pagination");
                    if (pagination && !pagination.querySelector("a")) pagination.remove();
                })
                .catch(function (error) {
                    if (error.name === "AbortError") return;
                    window.location.assign(url.href);
                })
                .finally(function () {
                    current.removeAttribute("aria-busy");
                    updateSearchCountPosition();
                });
        }

        var initialForm = searchRoot.querySelector("[data-message-search-form]");
        refreshMode(initialForm);
        syncMediaTypeFilter(initialForm);
        setSidebarCollapsed(searchRoot, searchSidebarCollapsed);
        syncSearchCount(searchRoot, new URL(window.location.href), true);
        updateSearchCountPosition();
        window.addEventListener("scroll", updateSearchCountPosition, {passive: true});
        window.addEventListener("resize", updateSearchCountPosition);
        document.addEventListener("change", function (event) {
            if (event.target.matches("[data-media-type-option]")) {
                syncMediaTypeFilter(event.target.form);
                return;
            }
            if (event.target.matches("[data-search-mode-toggle]")) {
                refreshMode(event.target.form);
            }
        });
        document.addEventListener("submit", function (event) {
            var form = event.target.closest("[data-message-search-form]");
            if (!form || !window.fetch || !window.DOMParser) return;
            event.preventDefault();
            var url = new URL(form.action, window.location.href);
            var submittedMode = form.querySelector("[data-search-mode-value]");
            var queryInput = form.querySelector("[data-search-query]");
            var useExactPhrase = submittedMode && submittedMode.value === "elasticsearch"
                && queryInput && queryInput.value.indexOf('"') !== -1;
            var googlePhraseSearch = useExactPhrase
                ? parseGooglePhraseSearch(queryInput.value)
                : null;
            var params = new URLSearchParams();
            new FormData(form).forEach(function (value, name) {
                value = String(value);
                if (!value.trim() && name !== "search" && name !== "mode") return;
                if (name === "q" && useExactPhrase) {
                    if (googlePhraseSearch) return;
                    name = "qem";
                }
                params.append(name, value);
            });
            if (googlePhraseSearch) {
                googlePhraseSearch.phrases.forEach(function (phrase) {
                    params.append("qem", phrase);
                });
                if (googlePhraseSearch.phrases.length > 1) {
                    params.append("qem_operator", googlePhraseSearch.operator);
                }
            }
            url.search = params.toString();
            if (submittedMode && submittedMode.value === "retrieval") {
                loadRetrievalCount(url);
            } else {
                cancelRetrievalCount();
            }
            loadSearchContent(url, true);
        });
        document.addEventListener("click", function (event) {
            var orderToggle = event.target.closest("[data-search-order-toggle]");
            if (orderToggle) {
                event.preventDefault();
                var orderForm = orderToggle.form;
                var currentOrdering = orderForm && orderForm.querySelector("[data-search-ordering]");
                var isAscending = currentOrdering
                    && currentOrdering.value.trim().split(",", 1)[0] === "created_at";
                syncSearchOrdering(orderForm, !isAscending);
                var currentResults = orderForm && orderForm.closest("#message-search-content");
                if (currentResults && currentResults.dataset.searchResults === "true") {
                    orderForm.requestSubmit();
                }
                return;
            }
            var sidebarToggle = event.target.closest("[data-search-sidebar-toggle]");
            if (sidebarToggle) {
                event.preventDefault();
                searchSidebarCollapsed = !searchSidebarCollapsed;
                setSidebarCollapsed(document.getElementById("message-search-content"), searchSidebarCollapsed);
                return;
            }
            var more = event.target.closest("[data-search-more]");
            if (more && window.fetch && window.DOMParser) {
                event.preventDefault();
                loadMoreMessages(new URL(more.href, window.location.href));
                return;
            }
            var link = event.target.closest("[data-search-pagination]");
            if (!link || !window.fetch || !window.DOMParser) return;
            event.preventDefault();
            loadSearchContent(new URL(link.href, window.location.href), true);
        });
        window.addEventListener("popstate", function () {
            loadSearchContent(new URL(window.location.href), false);
        });
    }

    bindThemeToggle();
    bindConfirmations();
    bindDateFilters();
    bindScheduleSections();
    bindScheduleAdvancedOptions();
    bindScheduleToggles();
    bindListEditors();
    bindAutocompleteEditors();
    bindPipelineReload();
    bindDagDetailDialog();
    bindDagGraphs(document);
    bindMessageSearch();
    if (document.fonts) {
        var renderAfterFonts = function () {
            window.requestAnimationFrame(function () {
                document.querySelectorAll("[data-dag-graph]").forEach(function (graph) {
                    if (graph.__dagRender) graph.__dagRender();
                    else renderDagGraph(graph);
                });
            });
        };
        document.fonts.ready.then(renderAfterFonts);
        if (document.fonts.addEventListener) document.fonts.addEventListener("loadingdone", renderAfterFonts);
    }
    bindPolling();
}());
