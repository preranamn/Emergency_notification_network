/* =====================================================================
   SDN Priority Emergency Notification Network — Interactive NOC Client
   ===================================================================== */

(() => {
  "use strict";

  const PROFILES = {
    MEDICAL: {
      category: "MEDICAL",
      priority: 1,
      priority_label: "HIGHEST",
      dscp: 46,
      dscp_name: "EF",
      queue_id: 1,
      queue_label: "Queue 1 (Highest)",
      color: "#f97316",
      cssKey: "med",
      default_message: "Urgent medical assistance required in Coach B2.",
    },
    FIRE: {
      category: "FIRE",
      priority: 2,
      priority_label: "MEDIUM",
      dscp: 34,
      dscp_name: "AF41",
      queue_id: 2,
      queue_label: "Queue 2 (Second-Highest)",
      color: "#ef4444",
      cssKey: "fire",
      default_message: "Fire detected in Coach A3. Immediate attention required.",
    },
    GENERAL: {
      category: "GENERAL",
      priority: 3,
      priority_label: "NORMAL",
      dscp: 0,
      dscp_name: "BE",
      queue_id: 3,
      queue_label: "Queue 3 (Normal / BE)",
      color: "#eab308",
      cssKey: "gen",
      default_message: "General emergency announcement. Contact railway security.",
    },
  };

  // Exact SVG coordinates matching index.html topology layout
  const NODE_POS = {
    H1: { x: 120, y: 150 },
    H5: { x: 120, y: 335 },
    S1: { x: 315, y: 235 },
    S2: { x: 605, y: 235 },
    H2: { x: 800, y: 95 },
    H3: { x: 800, y: 195 },
    H4: { x: 800, y: 295 },
    H6: { x: 800, y: 385 },
  };

  const LINK_ID = {
    "H1-S1": "link-h1-s1",
    "S1-H1": "link-h1-s1",
    "H5-S1": "link-h5-s1",
    "S1-S2": "link-s1-s2",
    "S2-S1": "link-s1-s2",
    "S2-H2": "link-s2-h2",
    "H2-S2": "link-s2-h2",
    "S2-H3": "link-s2-h3",
    "H3-S2": "link-s2-h3",
    "S2-H4": "link-s2-h4",
    "H4-S2": "link-s2-h4",
    "S2-H6": "link-s2-h6",
  };

  const state = {
    selectedCategory: "MEDICAL",
    customMessageEdited: false,
    dashboardScope: "latest", // "latest" | "aggregate"
    selectedAlertId: null,
    latestAlert: null,
    latestAlertMetrics: null,
    aggregateMetrics: null,
    recentAlerts: [],
    networkMode: "STOPPED",
    networkActive: false,
    congestionActive: false,
    congestionTimer: null,
    charts: { category: null, clients: null },
  };

  const $ = (id) => document.getElementById(id);

  // =====================================================================
  // Utility Formatters
  // =====================================================================
  function fmtMs(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return "N/A";
    return `${Number(val).toFixed(3)} ms`;
  }

  function fmtPct(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return "N/A";
    return `${Number(val).toFixed(1)}%`;
  }

  function fmtTime(wallMs) {
    if (!wallMs) return "--:--:--.---";
    const d = new Date(Number(wallMs));
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    const ms = String(d.getMilliseconds()).padStart(3, "0");
    return `${hh}:${mm}:${ss}.${ms}`;
  }

  function showToast(msg, level = "ok", durationMs = 5000) {
    const el = $("toast-banner");
    el.textContent = msg;
    el.className = `toast ${level}`;
    clearTimeout(el._t);
    if (durationMs > 0) {
      el._t = setTimeout(() => el.classList.add("hidden"), durationMs);
    }
  }

  async function apiPost(url, body = {}) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({ ok: false, error: "Invalid JSON response" }));
    if (!res.ok && data.ok !== false) data.ok = false;
    return data;
  }

  // =====================================================================
  // SVG Packet Journey Animation Engine (Driven by Live Backend Events)
  // =====================================================================
  function pulseNode(nodeId, cssKey, durationMs = 650) {
    const el = $(`node-${nodeId}`);
    if (!el) return;
    const cls = `node-hl-${cssKey}`;
    el.classList.add(cls);
    setTimeout(() => el.classList.remove(cls), durationMs);
  }

  function pulseLink(fromId, toId, cssKey, durationMs = 650) {
    const lid = LINK_ID[`${fromId}-${toId}`];
    if (!lid) return;
    const el = $(lid);
    if (!el) return;
    const cls = `link-pulse-${cssKey}`;
    el.classList.add(cls);
    setTimeout(() => el.classList.remove(cls), durationMs);
  }

  function animatePacketPath(pathNodes, color, cssKey, labelText, segDurationMs = 340, layerId = "packet-layer", onComplete = null) {
    const layer = $(layerId);
    if (!layer || pathNodes.length < 2) return;

    const svgNS = "http://www.w3.org/2000/svg";
    const g = document.createElementNS(svgNS, "g");
    const circle = document.createElementNS(svgNS, "circle");
    circle.setAttribute("r", layerId === "congestion-layer" ? "4.5" : "7.5");
    circle.setAttribute("fill", color);
    circle.setAttribute("stroke", "#06080a");
    circle.setAttribute("stroke-width", "1.5");
    if (layerId === "packet-layer") {
      circle.setAttribute("filter", "url(#pkt-glow)");
    }
    g.appendChild(circle);

    if (labelText && layerId === "packet-layer") {
      const txt = document.createElementNS(svgNS, "text");
      txt.setAttribute("y", "-11");
      txt.setAttribute("text-anchor", "middle");
      txt.setAttribute("fill", color);
      txt.setAttribute("font-size", "9.5");
      txt.setAttribute("font-weight", "700");
      txt.setAttribute("font-family", "JetBrains Mono, monospace");
      txt.textContent = labelText;
      g.appendChild(txt);
    }

    layer.appendChild(g);

    let segIdx = 0;
    let segStart = null;

    function triggerSegmentHighlight(idx) {
      if (layerId !== "packet-layer") return;
      const a = pathNodes[idx];
      const b = pathNodes[idx + 1];
      pulseNode(a, cssKey, segDurationMs + 120);
      pulseLink(a, b, cssKey, segDurationMs + 120);
    }

    triggerSegmentHighlight(0);

    function step(ts) {
      if (segStart === null) segStart = ts;
      const elapsed = ts - segStart;
      const t = Math.min(1, elapsed / segDurationMs);

      const from = NODE_POS[pathNodes[segIdx]];
      const to = NODE_POS[pathNodes[segIdx + 1]];
      if (!from || !to) {
        g.remove();
        return;
      }

      const x = from.x + (to.x - from.x) * t;
      const y = from.y + (to.y - from.y) * t;
      g.setAttribute("transform", `translate(${x.toFixed(1)}, ${y.toFixed(1)})`);

      if (t < 1) {
        requestAnimationFrame(step);
      } else {
        if (layerId === "packet-layer") {
          pulseNode(pathNodes[segIdx + 1], cssKey, 520);
        }
        segIdx += 1;
        if (segIdx < pathNodes.length - 1) {
          segStart = ts;
          triggerSegmentHighlight(segIdx);
          requestAnimationFrame(step);
        } else {
          g.remove();
          if (typeof onComplete === "function") onComplete();
        }
      }
    }

    requestAnimationFrame(step);
  }

  function startCongestionAnimation() {
    stopCongestionAnimation();
    const trunk = $("link-s1-s2");
    if (trunk) trunk.classList.add("congested");
    state.congestionTimer = setInterval(() => {
      animatePacketPath(["H5", "S1", "S2", "H6"], "#9ca3af", "gen", "", 270, "congestion-layer");
    }, 280);
  }

  function stopCongestionAnimation() {
    if (state.congestionTimer) {
      clearInterval(state.congestionTimer);
      state.congestionTimer = null;
    }
    const trunk = $("link-s1-s2");
    if (trunk) trunk.classList.remove("congested");
    const layer = $("congestion-layer");
    if (layer) layer.innerHTML = "";
  }

  function triggerForwardJourney(alert) {
    const prof = PROFILES[alert.category] || PROFILES.GENERAL;
    const clients = Object.keys(alert.clients || {});

    $("hud-status").textContent = `TRANSMITTING ${alert.category} UDP PACKETS...`;
    $("hud-status").style.color = prof.color;
    $("hud-alert-id").textContent = alert.alert_id;
    $("hud-route").textContent = `H1 → S1 → S2 → ${clients.join("/") || "H2/H3/H4"}`;
    $("hud-cat").textContent = `${prof.category} (P${prof.priority} | DSCP ${prof.dscp_name} ${prof.dscp} | Q${prof.queue_id})`;
    $("hud-cat").style.color = prof.color;
    $("hud-basis").textContent =
      alert.network_mode === "MININET_SDN"
        ? "Live Mininet/OVS Events + Topology Path Animation"
        : "Topology-Based Path Illustration (Live Loopback UDP Events)";

    clients.forEach((cid, idx) => {
      setClientSvgStatus(cid, "IN TRANSIT", "st-pending");
      setClientCardStatus(cid, "PENDING", "badge-pending", "pending", `Incoming ${alert.category} alert...`, null, null);
      setTimeout(() => {
        animatePacketPath(
          ["H1", "S1", "S2", cid],
          prof.color,
          prof.cssKey,
          `${prof.category.slice(0, 3)} Q${prof.queue_id}`,
          320,
          "packet-layer"
        );
      }, idx * 90);
    });
  }

  function triggerAckJourney(clientId, record, alertMessage) {
    setClientSvgStatus(clientId, "DELIVERED ✓", "st-delivered");
    pulseNode(clientId, "ack", 900);
    setClientCardStatus(
      clientId,
      "ACK RECEIVED",
      "badge-delivered",
      "delivered",
      alertMessage || "Emergency alert delivered & acknowledged.",
      record.delivery_latency_ms,
      record.rtt_ms
    );

    // Animate green ACK returning H2/H3/H4 -> S2 -> S1 -> H1
    setTimeout(() => {
      animatePacketPath(
        [clientId, "S2", "S1", "H1"],
        "#22c55e",
        "ack",
        `ACK ${clientId}`,
        300,
        "packet-layer",
        () => {
          setClientSvgStatus(clientId, `ACK ${record.rtt_ms !== null ? record.rtt_ms.toFixed(1) + "ms" : "OK"}`, "st-delivered");
        }
      );
    }, 320);
  }

  function setClientSvgStatus(clientId, text, cls) {
    const el = $(`svg-status-${clientId}`);
    if (!el) return;
    el.textContent = text;
    el.setAttribute("class", `node-status-badge ${cls || ""}`);
  }

  function setClientCardStatus(clientId, badgeText, badgeCls, cardCls, msgText, latMs, rttMs) {
    const card = $(`card-${clientId}`);
    const badge = $(`badge-${clientId}`);
    const msg = $(`msg-${clientId}`);
    const lat = $(`cc-lat-${clientId}`);
    const rtt = $(`cc-rtt-${clientId}`);
    if (!card || !badge) return;

    card.className = `client-card ${cardCls || ""}`;
    badge.className = `cc-badge ${badgeCls || "badge-idle"}`;
    badge.textContent = badgeText;
    if (msgText !== null && msg) msg.textContent = msgText;
    if (lat) lat.textContent = fmtMs(latMs);
    if (rtt) rtt.textContent = fmtMs(rttMs);
  }

  // =====================================================================
  // Category Console Selection
  // =====================================================================
  function selectCategory(cat) {
    const prof = PROFILES[cat] || PROFILES.MEDICAL;
    state.selectedCategory = prof.category;

    document.querySelectorAll(".cat-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.cat === prof.category);
    });

    $("qi-category").textContent = prof.category;
    $("qi-category").className = `txt-${prof.cssKey}`;
    $("qi-priority").textContent = `${prof.priority} — ${prof.priority_label}`;
    $("qi-dscp").textContent = `${prof.dscp_name} (${prof.dscp})`;
    $("qi-queue").textContent = prof.queue_label;

    const sendBtn = $("btn-send-alert");
    sendBtn.textContent = `SEND ${prof.category} ALERT TO ALL CLIENTS`;
    sendBtn.className = `btn btn-send btn-send-${prof.cssKey}`;

    const ta = $("alert-message");
    const isDefault = Object.values(PROFILES).some((p) => p.default_message === ta.value.trim()) || !ta.value.trim();
    if (isDefault || !state.customMessageEdited) {
      ta.value = prof.default_message;
      state.customMessageEdited = false;
    }
  }

  // =====================================================================
  // Status, Topology & SDN Queue Updates
  // =====================================================================
  function applyFullStatus(status) {
    if (!status) return;
    state.networkMode = status.mode || "STOPPED";
    state.networkActive = Boolean(status.network_active);
    state.congestionActive = Boolean(status.congestion_active);

    // Header pills
    const ryuOnline = Boolean(status.sdn && status.sdn.controller_online);
    setPill("pill-ryu", "val-ryu", ryuOnline ? "ONLINE (OF 1.3)" : "OFFLINE", ryuOnline ? "ok" : "");

    const netLabel = state.networkActive
      ? state.networkMode === "LOOPBACK_TEST"
        ? "LOOPBACK UDP"
        : "MININET SDN"
      : "STOPPED";
    setPill("pill-net", "val-net", netLabel, state.networkActive ? (state.networkMode === "LOOPBACK_TEST" ? "warn" : "ok") : "");
    $("hdr-live-dot").classList.toggle("online", state.networkActive);

    const clients = (status.server && status.server.clients) || [];
    setPill("pill-clients", "val-clients", `${clients.length}/3`, clients.length === 3 ? "ok" : clients.length > 0 ? "warn" : "");

    setPill(
      "pill-cong",
      "val-cong",
      state.congestionActive ? `ON (${status.congestion_rate_mbps} Mbps)` : "OFF",
      state.congestionActive ? "danger" : ""
    );

    // Buttons state
    $("btn-stop-mininet").disabled = !state.networkActive;
    $("btn-cong-start").disabled = !state.networkActive || state.networkMode !== "MININET_SDN" || state.congestionActive;
    $("btn-cong-stop").disabled = !state.congestionActive;
    $("btn-send-alert").disabled = !state.networkActive || clients.length === 0;
    $("btn-run-exp").disabled = !state.networkActive || clients.length === 0 || Boolean(status.experiment_running);

    if (state.congestionActive && !state.congestionTimer) {
      startCongestionAnimation();
    } else if (!state.congestionActive && state.congestionTimer) {
      stopCongestionAnimation();
    }

    // Topology node LEDs & links
    const nodes = status.nodes || {};
    Object.entries(nodes).forEach(([nid, info]) => {
      const g = $(`node-${nid}`);
      if (g) g.classList.toggle("online", Boolean(info.online));
    });

    const swOnline = Boolean(nodes.S1 && nodes.S1.online && nodes.S2 && nodes.S2.online);
    $("link-ryu-s1").classList.toggle("active", ryuOnline && Boolean(nodes.S1 && nodes.S1.online));
    $("link-ryu-s2").classList.toggle("active", ryuOnline && Boolean(nodes.S2 && nodes.S2.online));
    $("link-h1-s1").classList.toggle("online", Boolean(nodes.H1 && nodes.H1.online));
    $("link-s1-s2").classList.toggle("online", swOnline || state.networkActive);
    $("link-h5-s1").classList.toggle("online", Boolean(nodes.H5 && nodes.H5.online));
    $("link-s2-h6").classList.toggle("online", Boolean(nodes.H6 && nodes.H6.online));

    ["H2", "H3", "H4"].forEach((cid) => {
      const registered = Boolean(nodes[cid] && nodes[cid].registered);
      $(`link-s2-${cid.toLowerCase()}`).classList.toggle("online", registered);
      if (!state.latestAlert) {
        setClientSvgStatus(cid, registered ? "REGISTERED" : "OFFLINE", registered ? "st-delivered" : "");
        setClientCardStatus(
          cid,
          registered ? "REGISTERED" : "OFFLINE",
          registered ? "badge-reg" : "badge-idle",
          "",
          registered ? "Listening for UDP emergency notifications..." : "Client offline — activate network.",
          null,
          null
        );
      }
    });

    $("h5-state-txt").textContent = state.congestionActive
      ? `Flooding ${status.congestion_rate_mbps} Mbps`
      : "Congestion Source";

    // Update SDN queue stats
    if (status.sdn) updateSdnStats(status.sdn);

    // Update server alerts & metrics
    if (status.server) {
      state.aggregateMetrics = status.server.aggregate_metrics;
      state.recentAlerts = status.server.recent_alerts || [];
      if (!state.selectedAlertId && status.server.latest_alert) {
        state.latestAlert = status.server.latest_alert;
        state.latestAlertMetrics = status.server.latest_alert_metrics;
        renderClientCardsFromAlert(state.latestAlert, state.latestAlertMetrics);
        renderTimeline(state.latestAlert.timeline || [], state.latestAlert.category);
      }
      renderDashboard();
    }
  }

  function setPill(pillId, valId, text, cls) {
    const p = $(pillId);
    const v = $(valId);
    if (!p || !v) return;
    p.className = `pill ${cls || ""}`;
    v.textContent = text;
  }

  function updateSdnStats(sdn) {
    if (!sdn) return;
    const ryuOnline = Boolean(sdn.controller_online);
    setPill("pill-ryu", "val-ryu", ryuOnline ? "ONLINE (OF 1.3)" : "OFFLINE", ryuOnline ? "ok" : "");
    const ryuNode = $("node-RYU");
    if (ryuNode) ryuNode.classList.toggle("online", ryuOnline);

    const queues = sdn.queues || [];
    const s1Counts = { 1: 0, 2: 0, 3: 0 };
    queues.forEach((q) => {
      if (q.switch === "S1" && s1Counts[q.queue_id] !== undefined) {
        s1Counts[q.queue_id] += Number(q.tx_packets || 0);
      }
    });
    $("svg-q1").textContent = `Q1 EF: ${s1Counts[1]}`;
    $("svg-q2").textContent = `Q2 AF41: ${s1Counts[2]}`;
    $("svg-q3").textContent = `Q3 BE: ${s1Counts[3]}`;

    const tbody = $("ovs-queues-tbody");
    if (!queues.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-cell">No live OVS queue stats yet (requires Ryu + Mininet SDN mode).</td></tr>`;
      return;
    }
    tbody.innerHTML = queues
      .filter((q) => Number(q.queue_id) <= 3)
      .map(
        (q) => `<tr>
          <td class="mono">${q.switch}</td>
          <td class="mono">${q.port_no}</td>
          <td class="mono">Q${q.queue_id}</td>
          <td class="${catTextClass(q.label)}">${q.label}</td>
          <td class="mono">${q.tx_packets}</td>
          <td class="mono">${q.tx_bytes}</td>
          <td class="mono">${q.tx_errors}</td>
        </tr>`
      )
      .join("");
  }

  function catTextClass(cat) {
    if (cat === "MEDICAL") return "txt-med";
    if (cat === "FIRE") return "txt-fire";
    if (cat === "GENERAL") return "txt-gen";
    return "";
  }

  // =====================================================================
  // Client Cards, Timeline & Performance Dashboard Rendering
  // =====================================================================
  function renderClientCardsFromAlert(alert, metrics) {
    if (!alert || !metrics) return;
    ["H2", "H3", "H4"].forEach((cid) => {
      const c = metrics.per_client && metrics.per_client[cid];
      if (!c) return;
      let badgeCls = "badge-idle";
      let cardCls = "";
      let svgCls = "";
      let svgTxt = c.status.toUpperCase();

      if (c.status === "ACK Received") {
        badgeCls = "badge-delivered";
        cardCls = "delivered";
        svgCls = "st-delivered";
        svgTxt = `ACK ${c.rtt_ms !== null ? c.rtt_ms.toFixed(1) + "ms" : "OK"}`;
      } else if (c.status === "Delivered") {
        badgeCls = "badge-acklost";
        cardCls = "acklost";
        svgCls = "st-acklost";
        svgTxt = "DELIVERED (ACK LOST)";
      } else if (c.status === "Pending") {
        badgeCls = "badge-pending";
        cardCls = "pending";
        svgCls = "st-pending";
        svgTxt = "PENDING ACK";
      } else if (c.status === "Failed" || c.status === "Timeout") {
        badgeCls = "badge-failed";
        cardCls = "failed";
        svgCls = "st-failed";
        svgTxt = c.status.toUpperCase();
      }

      const displayStatus =
        c.failure_reason === "ACK_LOST"
          ? "DELIVERED (ACK LOST)"
          : c.failure_reason === "DELIVERY_FAILED"
          ? "DELIVERY FAILED"
          : c.status.toUpperCase();

      setClientSvgStatus(cid, svgTxt, svgCls);
      setClientCardStatus(
        cid,
        displayStatus,
        badgeCls,
        cardCls,
        c.status === "ACK Received" || c.status === "Delivered"
          ? `[${alert.category}] ${alert.message}`
          : c.status === "Pending"
          ? `Awaiting ACK for ${alert.alert_id}...`
          : `Not delivered (${c.failure_reason})`,
        c.delivery_latency_ms,
        c.rtt_ms
      );
    });
  }

  function renderTimeline(entries, category) {
    const list = $("timeline-list");
    if (!entries || !entries.length) return;
    const prof = PROFILES[category] || PROFILES.GENERAL;

    list.innerHTML = entries
      .map((ev) => {
        let cls = `tl-${prof.cssKey}`;
        if (ev.stage.includes("ACK") || ev.stage.includes("completed")) cls = "tl-ack";
        if (ev.stage.includes("Failed") || ev.stage.includes("Timeout")) cls = "tl-warn";
        const badge = ev.verified === false ? `<span class="tl-tag">[Topology Illustration]</span>` : "";
        return `<div class="tl-item ${cls}">
          <div class="tl-head">
            <span class="tl-stage">${escapeHtml(ev.stage)}</span>
            <span class="tl-time">${fmtTime(ev.ts_wall_ms)}</span>
          </div>
          <div class="tl-detail">${escapeHtml(ev.detail)} ${badge}</div>
        </div>`;
      })
      .join("");
    list.scrollTop = list.scrollHeight;
  }

  function appendTimelineEntry(ev, category) {
    const list = $("timeline-list");
    if (!list || !ev) return;
    const empty = list.querySelector(".timeline-empty");
    if (empty) empty.remove();

    const prof = PROFILES[category] || PROFILES.GENERAL;
    let cls = `tl-${prof.cssKey}`;
    if (ev.stage.includes("ACK") || ev.stage.includes("completed")) cls = "tl-ack";
    if (ev.stage.includes("Failed") || ev.stage.includes("Timeout")) cls = "tl-warn";
    const badge = ev.verified === false ? `<span class="tl-tag">[Topology Illustration]</span>` : "";

    const div = document.createElement("div");
    div.className = `tl-item ${cls}`;
    div.innerHTML = `
      <div class="tl-head">
        <span class="tl-stage">${escapeHtml(ev.stage)}</span>
        <span class="tl-time">${fmtTime(ev.ts_wall_ms)}</span>
      </div>
      <div class="tl-detail">${escapeHtml(ev.detail)} ${badge}</div>
    `;
    list.appendChild(div);
    list.scrollTop = list.scrollHeight;
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  function renderDashboard() {
    const useAgg = state.dashboardScope === "aggregate";
    const m = useAgg ? state.aggregateMetrics : state.latestAlertMetrics;

    $("inspecting-label").textContent = useAgg
      ? "(SESSION AGGREGATE)"
      : m
      ? `(${m.alert_id} • ${m.category}${m.congestion_active ? " • CONGESTED" : ""})`
      : "(NO ALERT YET)";

    if (!m) {
      ["kpi-sent", "kpi-delivered", "kpi-lost", "kpi-pdr", "kpi-avg-lat", "kpi-avg-rtt"].forEach(
        (id) => ($(id).textContent = "N/A")
      );
      return;
    }

    const intended = useAgg ? m.total_intended_deliveries : m.intended_deliveries;
    const udpTx = useAgg ? m.total_udp_transmissions : m.udp_transmissions;
    const delivered = useAgg ? m.total_packets_delivered : m.packets_delivered;
    const acked = useAgg ? m.total_packets_acked : m.packets_acked;
    const lost = useAgg ? m.total_packets_lost : m.packets_lost;
    const ackFail = m.ack_failures ?? 0;

    $("kpi-sent").textContent = intended ?? "N/A";
    $("kpi-sent-sub").textContent = `UDP Tx: ${udpTx ?? "N/A"}${useAgg ? ` (${m.total_logical_alerts} alerts)` : ""}`;
    $("kpi-delivered").textContent = delivered ?? "N/A";
    $("kpi-acked-sub").textContent = `ACKs Received: ${acked ?? "N/A"}`;
    $("kpi-lost").textContent = lost ?? "N/A";
    $("kpi-ackloss-sub").textContent = `ACK Lost (Delivered): ${ackFail}`;
    $("kpi-pdr").textContent = fmtPct(m.pdr_percent);
    $("kpi-plr").textContent = `PLR: ${fmtPct(m.plr_percent)}`;
    $("kpi-avg-lat").textContent = fmtMs(m.avg_delivery_latency_ms);
    $("kpi-minmax-lat").textContent = `Min: ${fmtMs(m.min_delivery_latency_ms)} / Max: ${fmtMs(m.max_delivery_latency_ms)}`;
    $("kpi-avg-rtt").textContent = fmtMs(m.avg_rtt_ms);
    $("kpi-hops").textContent = `Hops: ${m.forwarding_link_hops ?? 3} links (${m.forwarding_switch_hops ?? 2} switches)`;

    renderPerClientTable(state.latestAlertMetrics);
    renderCategoryComparisonTable(state.aggregateMetrics);
    renderRecentAlertsTable(state.recentAlerts);
    renderCharts(state.latestAlertMetrics, state.aggregateMetrics);
  }

  function renderPerClientTable(alertMetrics) {
    const tbody = $("per-client-tbody");
    if (!alertMetrics || !alertMetrics.per_client) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-cell">Send an emergency notification to measure H2, H3, and H4 performance.</td></tr>`;
      return;
    }
    tbody.innerHTML = ["H2", "H3", "H4"]
      .map((cid) => {
        const c = alertMetrics.per_client[cid];
        if (!c) return "";
        const stCls =
          c.status === "ACK Received"
            ? "txt-green"
            : c.failure_reason === "ACK_LOST"
            ? "txt-med"
            : c.status === "Pending"
            ? "txt-gen"
            : "txt-fire";
        const stLabel =
          c.failure_reason === "ACK_LOST"
            ? "Delivered (ACK Lost)"
            : c.failure_reason === "DELIVERY_FAILED"
            ? "Failed (Packet Lost)"
            : c.status;
        return `<tr>
          <td class="mono"><strong>${cid}</strong></td>
          <td class="${stCls}"><strong>${stLabel}</strong></td>
          <td class="mono">${fmtMs(c.delivery_latency_ms)}</td>
          <td class="mono">${fmtMs(c.rtt_ms)}</td>
          <td class="mono">${c.forwarding_path_str || `H1 → S1 → S2 → ${cid}`}</td>
          <td class="mono">${c.link_hops} links / ${c.switch_hops} switches</td>
        </tr>`;
      })
      .join("");
  }

  function renderCategoryComparisonTable(agg) {
    const tbody = $("category-compare-tbody");
    if (!agg || !agg.by_category_condition) return;

    const rows = [];
    ["MEDICAL", "FIRE", "GENERAL"].forEach((cat) => {
      ["normal", "congestion"].forEach((cond) => {
        const d = agg.by_category_condition[cat] && agg.by_category_condition[cat][cond];
        if (!d || d.alert_count === 0) return;
        rows.push(`<tr>
          <td class="${catTextClass(cat)}"><strong>${cat}</strong></td>
          <td class="mono">${cond === "congestion" ? "CONGESTION" : "NORMAL"}</td>
          <td class="mono">${d.alert_count}</td>
          <td class="mono txt-green">${fmtPct(d.pdr_percent)}</td>
          <td class="mono ${d.plr_percent > 0 ? "txt-fire" : ""}">${fmtPct(d.plr_percent)}</td>
          <td class="mono">${fmtMs(d.avg_delivery_latency_ms)}</td>
          <td class="mono">${fmtMs(d.min_delivery_latency_ms)} / ${fmtMs(d.max_delivery_latency_ms)}</td>
          <td class="mono">${fmtMs(d.avg_rtt_ms)}</td>
        </tr>`);
      });
    });

    tbody.innerHTML = rows.length
      ? rows.join("")
      : `<tr><td colspan="8" class="empty-cell">Send Medical, Fire, and General alerts (normal &amp; congested) to compare QoS priority performance.</td></tr>`;
  }

  function renderRecentAlertsTable(recent) {
    const tbody = $("recent-alerts-tbody");
    if (!recent || !recent.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-cell">No alerts transmitted yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = recent
      .map((a) => {
        const sel = state.latestAlertMetrics && state.latestAlertMetrics.alert_id === a.alert_id ? "selected-row" : "";
        return `<tr class="clickable ${sel}" data-alert-id="${a.alert_id}">
          <td class="mono">${a.alert_id}</td>
          <td class="${catTextClass(a.category)}"><strong>${a.category}</strong></td>
          <td class="mono">${a.congestion_active ? "CONG" : "NORM"}</td>
          <td class="mono">${a.packets_delivered}/${a.intended_deliveries}</td>
          <td class="mono">${fmtPct(a.pdr_percent)}</td>
          <td class="mono">${fmtMs(a.avg_delivery_latency_ms)}</td>
          <td class="mono">${fmtMs(a.avg_rtt_ms)}</td>
        </tr>`;
      })
      .join("");

    tbody.querySelectorAll("tr[data-alert-id]").forEach((tr) => {
      tr.addEventListener("click", () => inspectAlert(tr.dataset.alertId));
    });
  }

  async function inspectAlert(alertId) {
    try {
      const res = await fetch(`/api/alerts/${encodeURIComponent(alertId)}`);
      const data = await res.json();
      if (!data.ok) return;
      state.selectedAlertId = alertId;
      state.latestAlert = data.alert;
      state.latestAlertMetrics = data.alert_metrics;
      state.dashboardScope = "latest";
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.scope === "latest"));
      renderClientCardsFromAlert(data.alert, data.alert_metrics);
      renderTimeline(data.alert.timeline || [], data.alert.category);
      renderDashboard();
    } catch (_) {
      /* ignore */
    }
  }

  // =====================================================================
  // Chart.js Visualizations (Strict Palette: Orange, Red, Yellow, Green)
  // =====================================================================
  function renderCharts(alertMetrics, agg) {
    if (typeof window.Chart === "undefined") return;

    const gridColor = "#242933";
    const tickColor = "#9ca3af";

    // Chart 1: Category Comparison (Normal vs Congestion Avg Delivery Latency)
    const cats = ["MEDICAL", "FIRE", "GENERAL"];
    const normLats = cats.map((c) => agg?.by_category_condition?.[c]?.normal?.avg_delivery_latency_ms ?? 0);
    const congLats = cats.map((c) => agg?.by_category_condition?.[c]?.congestion?.avg_delivery_latency_ms ?? 0);
    const avgRtts = cats.map((c) => agg?.by_category?.[c]?.avg_rtt_ms ?? 0);

    const ctxCat = $("chart-category").getContext("2d");
    if (!state.charts.category) {
      state.charts.category = new Chart(ctxCat, {
        type: "bar",
        data: {
          labels: ["MEDICAL (EF 46)", "FIRE (AF41 34)", "GENERAL (BE 0)"],
          datasets: [
            { label: "Normal Latency (ms)", data: normLats, backgroundColor: ["#f97316", "#ef4444", "#eab308"], borderRadius: 4 },
            { label: "Congested Latency (ms)", data: congLats, backgroundColor: ["rgba(249,115,22,0.45)", "rgba(239,68,68,0.45)", "rgba(234,179,8,0.45)"], borderColor: ["#f97316", "#ef4444", "#eab308"], borderWidth: 1.5, borderRadius: 4 },
            { label: "Overall Avg RTT (ms)", data: avgRtts, backgroundColor: "#22c55e", borderRadius: 4 },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { labels: { color: "#d1d5db", font: { size: 11 } } } },
          scales: {
            x: { ticks: { color: tickColor }, grid: { color: gridColor } },
            y: { beginAtZero: true, ticks: { color: tickColor }, grid: { color: gridColor }, title: { display: true, text: "ms", color: tickColor } },
          },
        },
      });
    } else {
      state.charts.category.data.datasets[0].data = normLats;
      state.charts.category.data.datasets[1].data = congLats;
      state.charts.category.data.datasets[2].data = avgRtts;
      state.charts.category.update("none");
    }

    // Chart 2: Per-Client Latency & RTT (H2, H3, H4)
    const cIds = ["H2", "H3", "H4"];
    const cLats = cIds.map((cid) => alertMetrics?.per_client?.[cid]?.delivery_latency_ms ?? 0);
    const cRtts = cIds.map((cid) => alertMetrics?.per_client?.[cid]?.rtt_ms ?? 0);
    const barColor = PROFILES[alertMetrics?.category]?.color || "#f97316";

    const ctxCli = $("chart-clients").getContext("2d");
    if (!state.charts.clients) {
      state.charts.clients = new Chart(ctxCli, {
        type: "bar",
        data: {
          labels: ["H2 (Client 1)", "H3 (Client 2)", "H4 (Client 3)"],
          datasets: [
            { label: "Delivery Latency (ms)", data: cLats, backgroundColor: barColor, borderRadius: 4 },
            { label: "Round-Trip Time (ms)", data: cRtts, backgroundColor: "#22c55e", borderRadius: 4 },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { labels: { color: "#d1d5db", font: { size: 11 } } } },
          scales: {
            x: { ticks: { color: tickColor }, grid: { color: gridColor } },
            y: { beginAtZero: true, ticks: { color: tickColor }, grid: { color: gridColor }, title: { display: true, text: "ms", color: tickColor } },
          },
        },
      });
    } else {
      state.charts.clients.data.datasets[0].data = cLats;
      state.charts.clients.data.datasets[0].backgroundColor = barColor;
      state.charts.clients.data.datasets[1].data = cRtts;
      state.charts.clients.update("none");
    }
  }

  // =====================================================================
  // Server-Sent Events (SSE) Live Connection
  // =====================================================================
  function connectSSE() {
    const badge = $("sse-badge");
    const es = new EventSource("/api/events");

    es.onopen = () => {
      badge.textContent = "SSE: LIVE";
      badge.className = "sse-badge live";
    };

    es.onerror = () => {
      badge.textContent = "SSE: RECONNECTING";
      badge.className = "sse-badge";
    };

    es.addEventListener("connected", (e) => {
      const payload = JSON.parse(e.data);
      applyFullStatus(payload.data);
    });

    es.addEventListener("network_status", (e) => {
      const payload = JSON.parse(e.data);
      applyFullStatus(payload.data);
    });

    es.addEventListener("sdn_stats", (e) => {
      const payload = JSON.parse(e.data);
      updateSdnStats(payload.data);
    });

    es.addEventListener("client_registered", (e) => {
      const payload = JSON.parse(e.data);
      const c = payload.data;
      showToast(`Client ${c.client_id} (${c.ip}:${c.port}) registered with H1.`, "ok", 3000);
      refreshStatus();
    });

    es.addEventListener("congestion_changed", (e) => {
      const payload = JSON.parse(e.data);
      const d = payload.data;
      state.congestionActive = Boolean(d.congestion_active);
      if (state.congestionActive) startCongestionAnimation();
      else stopCongestionAnimation();
      refreshStatus();
    });

    es.addEventListener("alert_transmitted", (e) => {
      const payload = JSON.parse(e.data);
      const { alert, alert_metrics } = payload.data;
      state.selectedAlertId = null;
      state.latestAlert = alert;
      state.latestAlertMetrics = alert_metrics;
      triggerForwardJourney(alert);
      renderTimeline(alert.timeline || [], alert.category);
      renderDashboard();
    });

    es.addEventListener("ack_received", (e) => {
      const payload = JSON.parse(e.data);
      const d = payload.data;
      if (state.latestAlert && state.latestAlert.alert_id === d.alert_id) {
        state.latestAlertMetrics = d.alert_metrics;
        triggerAckJourney(d.client_id, d.record, state.latestAlert.message);
        (d.timeline_entries || []).forEach((te) => appendTimelineEntry(te, d.category));
        renderDashboard();
      }
    });

    es.addEventListener("packet_retransmitted", (e) => {
      const payload = JSON.parse(e.data);
      const d = payload.data;
      if (state.latestAlert && state.latestAlert.alert_id === d.alert_id) {
        const prof = PROFILES[state.latestAlert.category] || PROFILES.GENERAL;
        animatePacketPath(["H1", "S1", "S2", d.client_id], prof.color, prof.cssKey, `RETRY #${d.attempt - 1}`, 300);
        if (d.timeline_entry) appendTimelineEntry(d.timeline_entry, state.latestAlert.category);
      }
    });

    es.addEventListener("client_timeout", (e) => {
      const payload = JSON.parse(e.data);
      const d = payload.data;
      if (state.latestAlert && state.latestAlert.alert_id === d.alert_id) {
        state.latestAlertMetrics = d.alert_metrics;
        renderClientCardsFromAlert(state.latestAlert, d.alert_metrics);
        if (d.timeline_entry) appendTimelineEntry(d.timeline_entry, state.latestAlert.category);
        renderDashboard();
      }
    });

    es.addEventListener("alert_completed", (e) => {
      const payload = JSON.parse(e.data);
      const d = payload.data;
      state.aggregateMetrics = d.aggregate_metrics;
      if (state.latestAlert && state.latestAlert.alert_id === d.alert_id) {
        state.latestAlert = d.alert;
        state.latestAlertMetrics = d.alert_metrics;
        $("hud-status").textContent = `COMPLETED • DELIVERED ${d.alert_metrics.packets_delivered}/${d.alert_metrics.intended_deliveries} (PDR ${fmtPct(d.alert_metrics.pdr_percent)})`;
        $("hud-status").style.color = "#22c55e";
        renderClientCardsFromAlert(d.alert, d.alert_metrics);
        if (d.timeline_entry) appendTimelineEntry(d.timeline_entry, d.alert.category);
      }
      refreshStatus();
    });

    es.addEventListener("experiment_started", (e) => {
      const d = JSON.parse(e.data).data;
      showToast(`Running QoS Experiment ${d.run_id} (${d.total_scenarios} scenarios)...`, "warn", 0);
      $("btn-run-exp").disabled = true;
    });

    es.addEventListener("experiment_progress", (e) => {
      const d = JSON.parse(e.data).data;
      showToast(
        `Experiment [${d.step}/${d.total}] ${d.result.scenario}: Avg Lat ${fmtMs(d.result.avg_delivery_latency_ms)}, PDR ${fmtPct(d.result.pdr_percent)}`,
        "ok",
        0
      );
    });

    es.addEventListener("experiment_completed", (e) => {
      const d = JSON.parse(e.data).data;
      showToast(`Experiment ${d.run_id} finished! Results saved to data/experiment_results.csv.`, "ok", 7000);
      $("btn-run-exp").disabled = false;
      refreshStatus();
    });

    es.addEventListener("experiment_error", (e) => {
      const d = JSON.parse(e.data).data;
      showToast(`Experiment error: ${d.error}`, "error", 7000);
      $("btn-run-exp").disabled = false;
    });
  }

  async function refreshStatus() {
    try {
      const res = await fetch("/api/status");
      if (res.ok) applyFullStatus(await res.json());
    } catch (_) {
      /* ignore */
    }
  }

  // =====================================================================
  // User Interaction Bindings
  // =====================================================================
  function bindEvents() {
    document.querySelectorAll(".cat-btn").forEach((btn) => {
      btn.addEventListener("click", () => selectCategory(btn.dataset.cat));
    });

    $("alert-message").addEventListener("input", () => {
      state.customMessageEdited = true;
    });

    $("cong-rate").addEventListener("input", (e) => {
      $("cong-rate-val").textContent = `${e.target.value} Mbps`;
    });

    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.dashboardScope = btn.dataset.scope;
        document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
        renderDashboard();
      });
    });

    $("btn-start-mininet").addEventListener("click", async () => {
      showToast("Activating Mininet SDN topology, OVS HTB QoS queues, H1 server, and H2/H3/H4 clients...", "warn", 0);
      const res = await apiPost("/api/network/start", { mode: "mininet", bottleneck_mbps: 10 });
      if (res.ok) {
        showToast("Mininet SDN network active! OVS QoS queues configured and clients registering.", "ok");
        applyFullStatus(res.status);
      } else {
        showToast(res.error || "Failed to activate Mininet.", "error", 9000);
      }
    });

    $("btn-start-loopback").addEventListener("click", async () => {
      showToast("Starting Loopback UDP Test Mode (real UDP server & 3 clients on 127.0.0.1)...", "warn", 0);
      const res = await apiPost("/api/network/start", { mode: "loopback" });
      if (res.ok) {
        showToast("Loopback UDP Test Mode active! Note: run with sudo for real Mininet + OVS QoS.", "warn", 6000);
        applyFullStatus(res.status);
      } else {
        showToast(res.error || "Failed to start loopback mode.", "error");
      }
    });

    $("btn-stop-mininet").addEventListener("click", async () => {
      const res = await apiPost("/api/network/stop");
      if (res.ok) {
        state.latestAlert = null;
        state.latestAlertMetrics = null;
        stopCongestionAnimation();
        showToast("Network stopped and cleaned up.", "ok");
        applyFullStatus(res.status);
      }
    });

    $("btn-send-alert").addEventListener("click", async () => {
      const message = $("alert-message").value.trim();
      const ack_timeout_sec = parseFloat($("ack-timeout").value) || 2.0;
      const max_retries = parseInt($("max-retries").value, 10);
      const retry_interval_sec = parseFloat($("retry-interval").value) || 0.7;

      const res = await apiPost("/api/alert/send", {
        category: state.selectedCategory,
        message,
        ack_timeout_sec,
        max_retries: Number.isNaN(max_retries) ? 2 : max_retries,
        retry_interval_sec,
      });
      if (!res.ok) {
        showToast(res.error || "Failed to transmit alert.", "error");
      }
    });

    $("btn-cong-start").addEventListener("click", async () => {
      const rate = parseFloat($("cong-rate").value) || 18;
      const res = await apiPost("/api/congestion/start", { rate_mbps: rate });
      if (res.ok) {
        showToast(`Background congestion started: H5 → H6 @ ${rate} Mbps (DSCP 0 BE).`, "warn");
      } else {
        showToast(res.error || "Could not start congestion.", "error");
      }
    });

    $("btn-cong-stop").addEventListener("click", async () => {
      const res = await apiPost("/api/congestion/stop");
      if (res.ok) showToast("Background congestion stopped.", "ok");
    });

    $("btn-apply-loss").addEventListener("click", async () => {
      const client_id = $("fault-client").value;
      const forward_loss_pct = parseFloat($("fault-fwd").value) || 0;
      const ack_loss_pct = parseFloat($("fault-ack").value) || 0;
      const res = await apiPost("/api/fault/loss", { client_id, forward_loss_pct, ack_loss_pct });
      if (res.ok) {
        showToast(`Applied controlled loss on ${client_id}: ${forward_loss_pct}% forward, ${ack_loss_pct}% ACK.`, "warn");
      } else {
        showToast(res.error || "Failed to apply packet loss.", "error");
      }
    });

    $("btn-run-exp").addEventListener("click", async () => {
      const rate = parseFloat($("cong-rate").value) || 18;
      const res = await apiPost("/api/experiments/run", { alerts_per_scenario: 5, congestion_rate_mbps: rate });
      if (!res.ok) showToast(res.error || "Could not start experiment.", "error");
    });

    $("btn-reset-metrics").addEventListener("click", async () => {
      const res = await apiPost("/api/metrics/reset");
      if (res.ok) {
        state.latestAlert = null;
        state.latestAlertMetrics = null;
        state.selectedAlertId = null;
        $("timeline-list").innerHTML = `<div class="timeline-empty">Session metrics cleared. Send a new alert to begin.</div>`;
        showToast("Session metrics reset.", "ok");
        refreshStatus();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    selectCategory("MEDICAL");
    bindEvents();
    connectSSE();
    refreshStatus();
  });
})();
