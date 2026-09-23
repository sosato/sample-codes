const form = document.querySelector("#tripForm");
const modelSelect = document.querySelector("#model");
const submitButton = document.querySelector("#submitButton");
const emptyState = document.querySelector("#emptyState");
const loadingState = document.querySelector("#loadingState");
const loadingTitle = document.querySelector("#loadingTitle");
const loadingDetail = document.querySelector("#loadingDetail");
const liveProgress = document.querySelector("#liveProgress");
const answer = document.querySelector("#answer");
const sectionNav = document.querySelector("#sectionNav");
const meta = document.querySelector("#meta");
const citations = document.querySelector("#citations");
const citationList = document.querySelector("#citationList");
const errorBox = document.querySelector("#error");
const modelBadge = document.querySelector("#modelBadge");
const resultView = document.querySelector("#resultView");
const reviewView = document.querySelector("#reviewView");
const viewToggle = document.querySelector("#viewToggle");
const planTimeline = document.querySelector("#planTimeline");
const reviewStatus = document.querySelector("#reviewStatus");
const metadataGrid = document.querySelector("#metadataGrid");
const metadataJson = document.querySelector("#metadataJson");
const eventTable = document.querySelector("#eventTable");
const searchActions = document.querySelector("#searchActions");
const copyEventsButton = document.querySelector("#copyEvents");

let currentJob = null;
let currentEvents = [];
let currentSummary = null;
let eventSource = null;

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[char]);
}

function renderInline(source) {
  return source
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`(.+?)`/g, "<code>$1</code>");
}

function isTableSeparator(line) {
  return /^\s*\|?[\s:|-]+\|[\s:|:-]+\|?\s*$/.test(line);
}

function tableCells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "")
    .split("|").map(cell => renderInline(cell.trim()));
}

function renderMarkdown(source) {
  const lines = escapeHtml(source).split("\n");
  let html = "";
  let inList = false;
  let index = 0;
  while (index < lines.length) {
    const rawLine = lines[index];
    const line = renderInline(rawLine);
    if (rawLine.includes("|") && index + 1 < lines.length
        && isTableSeparator(lines[index + 1])) {
      if (inList) { html += "</ul>"; inList = false; }
      html += "<div class=\"table-scroll\"><table><thead><tr>";
      html += tableCells(rawLine).map(cell => `<th>${cell}</th>`).join("");
      html += "</tr></thead><tbody>";
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        html += `<tr>${tableCells(lines[index]).map(cell => `<td>${cell}</td>`).join("")}</tr>`;
        index += 1;
      }
      html += "</tbody></table></div>";
      continue;
    }
    if (line.startsWith("### ")) {
      if (inList) { html += "</ul>"; inList = false; }
      html += `<h4>${line.slice(4)}</h4>`;
    } else if (line.startsWith("## ")) {
      if (inList) { html += "</ul>"; inList = false; }
      html += `<h3>${line.slice(3)}</h3>`;
    } else if (line.startsWith("# ")) {
      if (inList) { html += "</ul>"; inList = false; }
      html += `<h2>${line.slice(2)}</h2>`;
    } else if (line.startsWith("&gt; ")) {
      if (inList) { html += "</ul>"; inList = false; }
      html += `<blockquote>${line.slice(5)}</blockquote>`;
    } else if (/^-{3,}$/.test(rawLine.trim())) {
      if (inList) { html += "</ul>"; inList = false; }
      html += "<hr>";
    } else if (/^[-*] /.test(line)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${line.slice(2)}</li>`;
    } else if (/^\d+\. /.test(line)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${line.replace(/^\d+\. /, "")}</li>`;
    } else if (line.trim()) {
      if (inList) { html += "</ul>"; inList = false; }
      html += `<p>${line}</p>`;
    }
    index += 1;
  }
  if (inList) html += "</ul>";
  return html;
}

const sectionIcons = {
  summary: "✦", flights: "✈", hotels: "▣",
  itinerary: "◷", budget: "¥", checklist: "✓",
};

function renderSections(sections, fallbackAnswer) {
  if (!sections?.length) {
    return `<section class="trip-section" id="section-summary">${renderMarkdown(fallbackAnswer)}</section>`;
  }
  return sections.map(section => `
    <section class="trip-section section-${section.key}" id="section-${section.key}">
      <header class="trip-section-header">
        <span class="section-icon">${sectionIcons[section.key] ?? "•"}</span>
        <h3>${escapeHtml(section.title)}</h3>
      </header>
      <div class="trip-section-body">${renderMarkdown(section.content)}</div>
    </section>`).join("");
}

function setHidden(element, hidden) {
  element.classList.toggle("hidden", hidden);
}

function buildSectionNav() {
  const sections = [...answer.querySelectorAll(".trip-section[id]")];
  sectionNav.innerHTML = sections.map(section => {
    const title = section.querySelector("h3")?.textContent ?? section.id;
    return `<a href="#${section.id}">${escapeHtml(title)}</a>`;
  }).join("");
  setHidden(sectionNav, sections.length === 0);
}

function switchView(view) {
  const review = view === "review";
  setHidden(resultView, review);
  setHidden(reviewView, !review);
  viewToggle.querySelectorAll("button").forEach(button => {
    button.classList.toggle("active", button.dataset.view === view);
  });
}

function renderPlan(plan, phases = {}) {
  planTimeline.innerHTML = plan.map((item, index) => {
    const status = phases[item.key] || "pending";
    return `<div class="plan-step ${status}">
      <span class="plan-marker">${status === "completed" ? "✓" : index + 1}</span>
      <div><strong>${escapeHtml(item.label)}</strong><small>${status}</small></div>
    </div>`;
  }).join("");
}

function metadataRows(summary) {
  return [
    ["Response ID", summary.response_id || "-"],
    ["Status", summary.status || "-"],
    ["Events", summary.event_count ?? 0],
    ["Last sequence", summary.last_sequence_number ?? "-"],
    ["Search queries", summary.search_queries?.length ?? 0],
    ["Sources", summary.source_count ?? 0],
    ["Reconnects", summary.reconnect_count ?? 0],
  ];
}

function renderReview(summary) {
  if (!summary) return;
  currentSummary = summary;
  renderPlan(currentJob?.plan || [], summary.phases);
  reviewStatus.textContent = summary.status;
  reviewStatus.className = `status-pill status-${summary.status}`;
  metadataGrid.innerHTML = metadataRows(summary)
    .map(([key, value]) => `<div><dt>${key}</dt><dd>${escapeHtml(value)}</dd></div>`)
    .join("");
  metadataJson.textContent = JSON.stringify(summary, null, 2);

  eventTable.innerHTML = `<div class="event-row event-header">
      <span>Seq</span><span>Phase</span><span>Event</span><span>Detail</span>
    </div>` + currentEvents.slice(-120).map(item => `<div class="event-row">
      <span>${item.sequence_number ?? "-"}</span>
      <span>${escapeHtml(item.phase)}</span>
      <code>${escapeHtml(item.type)}</code>
      <span>${escapeHtml(item.label)}</span>
    </div>`).join("");

  const queries = summary.search_queries || [];
  searchActions.innerHTML = queries.length
    ? queries.map((query, index) => `<div class="search-action">
        <span>${String(index + 1).padStart(2, "0")}</span>
        <p>${escapeHtml(query)}</p>
      </div>`).join("")
    : "<p class=\"muted\">検索 query を待機中です。</p>";
}

function updateLiveProgress(event, summary) {
  liveProgress.innerHTML = (currentJob?.plan || []).map(item => {
    const status = summary?.phases?.[item.key] || "pending";
    const marker = status === "completed" ? "✓" : status === "in_progress" ? "●" : "○";
    return `<div class="live-step ${status}"><span>${marker}</span><strong>${escapeHtml(item.label)}</strong></div>`;
  }).join("");
  if (event) {
    loadingTitle.textContent = event.label;
    loadingDetail.textContent = event.queries?.[0]
      ? `Query: ${event.queries[0]}`
      : `${event.type} · sequence ${event.sequence_number ?? "-"}`;
  }
}

function showResult(data) {
  answer.innerHTML = renderSections(data.sections, data.answer);
  buildSectionNav();
  meta.textContent = `${data.model} · Response ${data.response_id}`;
  citationList.innerHTML = data.citations.length
    ? data.citations.map((item, index) => `
        <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">
          <span>${String(index + 1).padStart(2, "0")}</span>
          <strong>${escapeHtml(item.title)}</strong>
        </a>`).join("")
    : "<p>引用 URL は回答本文を確認してください。</p>";
  setHidden(answer, false);
  setHidden(meta, false);
  setHidden(citations, false);
  setHidden(loadingState, true);
  answer.querySelector(".trip-section")?.scrollIntoView({ block: "start" });
}

function connectJob(job) {
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/jobs/${encodeURIComponent(job.job_id)}/events`);
  eventSource.addEventListener("metadata", message => {
    const summary = JSON.parse(message.data);
    updateLiveProgress(null, summary);
    renderReview(summary);
  });
  eventSource.addEventListener("progress", message => {
    const payload = JSON.parse(message.data);
    currentEvents.push(payload.event);
    updateLiveProgress(payload.event, payload.summary);
    renderReview(payload.summary);
  });
  eventSource.addEventListener("result", message => {
    const payload = JSON.parse(message.data);
    showResult(payload.result);
    renderReview(payload.summary);
    setHidden(viewToggle, false);
    submitButton.disabled = false;
  });
  eventSource.addEventListener("failure", message => {
    const payload = JSON.parse(message.data);
    errorBox.textContent = payload.message;
    setHidden(errorBox, false);
    setHidden(loadingState, true);
    renderReview(payload.summary);
    setHidden(viewToggle, false);
    submitButton.disabled = false;
    eventSource.close();
  });
  eventSource.addEventListener("done", () => eventSource.close());
  eventSource.onerror = () => {
    loadingDetail.textContent = "イベントストリームへ再接続しています。";
  };
}

async function loadModels() {
  const response = await fetch("/api/models");
  const data = await response.json();
  modelSelect.innerHTML = data.models
    .map(model => `<option value="${model.id}">${model.label}</option>`).join("");
  modelBadge.textContent = data.models[0]?.label ?? "Model";
}

function setSummerSample() {
  document.querySelector('input[value="private"]').checked = true;
  document.querySelector("#origin").value = "東京";
  document.querySelector("#destination").value = "タイ（バンコク中心）";
  document.querySelector("#startDate").value = "2027-08-08";
  document.querySelector("#endDate").value = "2027-08-11";
  document.querySelector("#adults").value = "2";
  document.querySelector("#children").value = "2";
  document.querySelector("#budget").value = "家族4人で総額60万円程度";
  document.querySelector("#interests").value = "家族向け、タイ料理、寺院、ナイトマーケット、プール、移動を詰め込みすぎない";
  document.querySelector("#notes").value = "子どもは8歳と11歳。暑さに配慮し、午後に休憩を入れてください。";
}

document.querySelector("#sampleButton").addEventListener("click", setSummerSample);
modelSelect.addEventListener("change", () => {
  modelBadge.textContent = modelSelect.options[modelSelect.selectedIndex].text;
});
viewToggle.addEventListener("click", event => {
  const button = event.target.closest("button[data-view]");
  if (button) switchView(button.dataset.view);
});
copyEventsButton.addEventListener("click", async () => {
  await navigator.clipboard.writeText(JSON.stringify({
    job_id: currentJob?.job_id,
    response_id: currentSummary?.response_id,
    progress_metadata: currentSummary,
    events: currentEvents,
  }, null, 2));
  copyEventsButton.textContent = "コピー済み";
  setTimeout(() => { copyEventsButton.textContent = "JSON をコピー"; }, 1500);
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  setHidden(emptyState, true);
  setHidden(answer, true);
  setHidden(sectionNav, true);
  setHidden(meta, true);
  setHidden(citations, true);
  setHidden(errorBox, true);
  setHidden(loadingState, false);
  setHidden(viewToggle, true);
  switchView("result");
  submitButton.disabled = true;
  currentEvents = [];
  currentSummary = null;

  const formData = new FormData(form);
  const payload = Object.fromEntries(formData.entries());
  payload.adults = Number(payload.adults);
  payload.children = Number(payload.children);

  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Trip planning failed");
    currentJob = data;
    modelBadge.textContent = modelSelect.options[modelSelect.selectedIndex].text;
    renderPlan(data.plan);
    updateLiveProgress(null, { phases: {} });
    connectJob(data);
  } catch (error) {
    errorBox.textContent = error.message;
    setHidden(errorBox, false);
    setHidden(loadingState, true);
    submitButton.disabled = false;
  }
});

loadModels().catch(error => {
  errorBox.textContent = `モデル一覧を取得できません: ${error.message}`;
  setHidden(errorBox, false);
});
setSummerSample();
