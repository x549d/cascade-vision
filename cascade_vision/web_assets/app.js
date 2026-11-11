"use strict";

const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("zh-CN");
const classes = {
  person: "人员", backpack: "背包", car: "汽车", bicycle: "自行车", motorcycle: "摩托车",
  bus: "公交车", truck: "卡车", dog: "狗", cat: "猫", handbag: "手提包", suitcase: "行李箱",
  bird: "鸟", "potted plant": "盆栽", "cell phone": "手机",
};
const statuses = {
  completed: "已完成", failed: "失败", dropped: "已丢弃", cancelled: "已取消",
  expired: "已过期", budget_rejected: "达到上限",
};
const triggers = {
  FIRST_SEEN_PRIORITY: "新出现的优先目标", LOW_CONF_HARD_EXAMPLE: "低置信度目标",
  OBJECT_INTERACTION: "目标重叠", AUDIT_RETRY: "重试", VERDICT_REFRESH: "刷新结果",
};
let selectedFile = null;
let busy = false;
let result = null;
let events = [];
let selected = -1;
let imageView = true;
let currentRows = new Map();
let ticks = new Map();

function label(key) {
  return Object.hasOwn(classes, key) ? classes[key] : (key || "未知目标");
}

function stamp(time, decimal = false) {
  if (time == null || !Number.isFinite(time)) return "—";
  const whole = Math.floor(Math.max(0, time));
  const base = String(Math.floor(whole / 60)).padStart(2, "0") + ":" + String(whole % 60).padStart(2, "0");
  return decimal ? base + "." + Math.floor((time - whole) * 10) : base;
}

function error(message) {
  $("error").textContent = message;
  $("error").hidden = !message;
}

function controls() {
  const api = $("mode").value === "api";
  $("api-options").hidden = !api;
  for (const id of ["drop", "demo", "mode", "limit", "cap", "consent", "detector"]) $(id).disabled = busy;
  $("run").disabled = busy || !selectedFile || (api && (!$("consent").checked || !$("cap").checkValidity()));
}

function choose(file) {
  if (busy || !file) return;
  if (!/\.(mp4|m4v|mov|avi|webm|mkv)$/i.test(file.name) || !file.size || file.size > 256 * 1024 ** 2) {
    error("请选择格式受支持、不超过 256 MiB 的非空视频。");
    return;
  }
  selectedFile = file;
  $("filename").textContent = file.name;
  $("file-info").textContent = (file.size / 1024 ** 2).toFixed(1) + " MiB · 已选中文件";
  error("");
  controls();
}

function progress(text, percent, count = "") {
  $("progress-box").hidden = false;
  $("progress-text").textContent = text;
  $("progress-count").textContent = count;
  if (percent == null) $("progress").removeAttribute("value");
  else $("progress").value = percent;
}

async function json(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error("读取结果失败，请刷新页面后重试。");
  return response.json();
}

function submit(url, file) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", url);
    request.setRequestHeader("X-Cascade-UI", "1");
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.responseType = "json";
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) progress("正在将视频导入本机服务…", 100 * event.loaded / event.total);
    };
    request.onload = () => {
      if (request.status === 202) resolve();
      else reject(new Error(request.response?.error || "无法启动分析。"));
    };
    request.onerror = () => reject(new Error("未能连接本机服务，请检查运行界面的终端。"));
    request.send(file);
  });
}

async function start(demo) {
  if (busy || (!demo && $("run").disabled)) return;
  busy = true;
  controls();
  error("");
  progress(demo ? "正在准备离线演示…" : "正在导入视频…", null);
  const query = new URLSearchParams({
    name: selectedFile?.name || "",
    limit: $("limit").value,
    mode: $("mode").value,
    detector: $("detector").value,
    cap: $("cap").value,
    consent: $("consent").checked ? "1" : "0",
  });
  try {
    await submit(demo ? "/api/demo" : "/api/run?" + query, demo ? null : selectedFile);
  } catch (err) {
    error(err.message);
    $("progress-box").hidden = true;
    busy = false;
    controls();
    return;
  }
  $("consent").checked = false;
  await sync();
}

async function sync() {
  try {
    const state = await json("/api/state");
    const option = $("mode").querySelector('option[value="api"]');
    option.hidden = option.disabled = !state.api_available;
    $("detector-field").hidden = !state.person_model_available;
    if (!state.person_model_available) $("detector").value = "general";
    const job = state.job;
    busy = job?.status === "running";
    if (job && result && result.id !== job.id) clearResult();
    if (busy) {
      const percent = job.total ? Math.min(99, 100 * job.processed / job.total) : null;
      const stage = !job.processed ? "正在加载模型或准备视频…"
        : job.total && job.processed >= job.total ? "正在整理分析结果…" : "正在分析画面…";
      progress(stage, percent, job.total ? `${job.processed} / ${job.total} 帧` : `${job.processed} 帧`);
      window.setTimeout(sync, 650);
    } else if (job?.status === "done") {
      if (result?.id !== job.id) {
        busy = true;
        controls();
        const records = await json("/api/jobs/" + job.id + "/events");
        render(job, records);
        busy = false;
      }
      progress("分析完成", 100, `${job.processed} 帧`);
      error("");
    } else if (job?.status === "error") {
      $("progress-box").hidden = true;
      error(job.error);
    }
  } catch (err) {
    error(err.message);
    if (busy) window.setTimeout(sync, 2000);
  } finally {
    controls();
  }
}

function clearResult() {
  result = null;
  events = [];
  selected = -1;
  const video = $("video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("video-tab").disabled = true;
  $("video-note").hidden = true;
  setView(true);
  $("placeholder").querySelector("strong").textContent = "等待新结果";
  $("placeholder").querySelector("small").textContent = "分析完成后，在这里复核目标画面。";
  for (const id of ["exports", "quality", "detail"]) $(id).hidden = true;
  for (const id of ["stat-frames", "stat-skipped", "stat-completed", "stat-calls", "time-end"]) $(id).textContent = "—";
  for (const id of ["stat-duration", "stat-skip-note", "stat-task-note", "stat-tokens"]) $(id).textContent = "等待分析";
  $("result-source").textContent = "分析完成后，结果将在这里显示。";
  $("preview-subtitle").textContent = "从右侧选择事件，查看对应画面。";
  $("category").replaceChildren(new Option("全部类别", ""));
  $("category").disabled = true;
  $("event-count").textContent = "0";
  const empty = document.createElement("p");
  empty.className = "empty-events";
  empty.textContent = "等待事件记录。";
  $("event-list").replaceChildren(empty);
  $("rail").replaceChildren();
  $("panel-foot").textContent = "同一目标可能产生多条记录。";
}

function render(job, records) {
  result = job;
  events = records;
  selected = -1;
  const data = job.summary;
  const m = data.metrics;
  const kind = { demo: "离线合成演示", local: "真实检测 · 描述模拟", api: "真实检测 · AI 分析" }[job.mode];
  const prefix = "/api/jobs/" + job.id;
  const detector = data.detector_profile === "person" ? " · 站台人员（实验）" : "";
  $("result-source").textContent = job.name + " · " + kind + detector;
  $("stat-frames").textContent = number.format(m.total_frames);
  $("stat-duration").textContent = data.processed_duration_s == null ? `耗时 ${m.elapsed_seconds} 秒`
    : `已分析 ${data.processed_duration_s.toFixed(1)} 秒 · 耗时 ${m.elapsed_seconds.toFixed(1)} 秒`;
  $("stat-skipped").textContent = number.format(m.stage1_intercepted);
  $("stat-skip-note").textContent = `占 ${m.stage1_filter_rate_pct}% · 定期复查 ${m.detector_refresh_frames} 帧`;
  $("stat-completed").textContent = number.format(m.vlm_completed_calls);
  $("stat-task-note").textContent = data.mock_vlm ? "均为模拟语义任务" : `触发 ${m.stage2_flagged_tasks} 个分析任务`;
  $("stat-calls").textContent = number.format(data.api_attempts);
  $("stat-tokens").textContent = m.actual_tokens == null ? "API 返回的用量不完整"
    : `${number.format(m.actual_tokens)} token${data.mock_vlm ? " · 未调用 API" : ""}`;
  const warnings = [];
  if (data.limited) warnings.push(`仅分析了视频的前 ${m.total_frames} 帧。`);
  const issues = [
    ["failed_tasks", "失败"], ["dropped_tasks", "丢弃"], ["expired_tasks", "过期"],
    ["cancelled_tasks", "取消"], ["budget_rejected_tasks", "达到上限"], ["event_export_errors", "导出错误"],
  ].filter(([key]) => m[key]).map(([key, title]) => title + " " + m[key]);
  if (issues.length) warnings.push("部分任务未正常完成：" + issues.join(" · ") + "。");
  $("quality").hidden = warnings.length === 0;
  $("quality").textContent = warnings.join(" ");
  $("exports").hidden = false;
  $("summary").href = prefix + "/summary";
  $("export").href = prefix + "/export";
  const video = $("video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("video-tab").disabled = job.mode === "demo";
  $("video-note").hidden = true;
  $("category").replaceChildren(new Option("全部类别", ""));
  for (const name of [...new Set(events.map((event) => event.yolo_class).filter(Boolean))].sort()) {
    $("category").append(new Option(label(name), name));
  }
  $("category").disabled = !events.length;
  $("time-end").textContent = data.processed_duration_s == null ? "—" : stamp(data.processed_duration_s, true);
  $("panel-foot").textContent = `在 ${data.detected_frames} 帧中检出目标。列表仅包含触发分析的记录，同一目标可能重复出现。`;
  setView(true);
  renderList();
}

function renderList() {
  const category = $("category").value;
  const list = $("event-list");
  const rail = $("rail");
  const rows = document.createDocumentFragment();
  const markers = document.createDocumentFragment();
  currentRows = new Map();
  ticks = new Map();
  const indexes = [];
  const duration = result.summary.processed_duration_s;
  events.forEach((event, index) => {
    if (category && category !== event.yolo_class) return;
    indexes.push(index);
    const row = document.createElement("button");
    row.type = "button";
    row.className = "event";
    row.setAttribute("aria-pressed", "false");
    const time = document.createElement("span");
    time.className = "stamp";
    time.textContent = stamp(event.media_timestamp_s, true);
    const main = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = `${label(event.yolo_class)} · #${event.track_id ?? "—"}`;
    const subtitle = document.createElement("small");
    subtitle.textContent = triggers[event.trigger_reason] || event.trigger_reason || "";
    main.append(title, subtitle);
    const state = document.createElement("span");
    const complete = event.status === "completed";
    state.className = "event-state " + (complete ? "good" : "warn");
    state.textContent = complete && event.simulated ? "模拟完成" : (statuses[event.status] || event.status);
    row.append(time, main, state);
    row.addEventListener("click", () => selectEvent(index));
    rows.append(row);
    currentRows.set(index, row);
    if (duration && event.media_timestamp_s != null) {
      const tick = document.createElement("span");
      tick.className = "tick";
      tick.style.left = Math.max(0, Math.min(100, 100 * event.media_timestamp_s / duration)) + "%";
      markers.append(tick);
      ticks.set(index, tick);
    }
  });
  $("event-count").textContent = String(indexes.length);
  if (!indexes.length) {
    const empty = document.createElement("p");
    empty.className = "empty-events";
    empty.textContent = "该范围没有触发分析的记录，可以扩大处理范围或换一段视频。";
    rows.append(empty);
  }
  list.replaceChildren(rows);
  list.scrollTop = 0;
  rail.replaceChildren(markers);
  const firstPriority = indexes.find((i) => events[i].status === "completed"
    && events[i].trigger_reason === "FIRST_SEEN_PRIORITY");
  const firstCompleted = indexes.find((i) => events[i].status === "completed");
  selectEvent(indexes.includes(selected) ? selected : (firstPriority ?? firstCompleted ?? indexes[0] ?? -1));
}

function selectEvent(index) {
  currentRows.get(selected)?.setAttribute("aria-pressed", "false");
  ticks.get(selected)?.classList.remove("active");
  selected = index;
  currentRows.get(index)?.setAttribute("aria-pressed", "true");
  ticks.get(index)?.classList.add("active");
  const event = events[index];
  $("detail").hidden = !event;
  $("preview-subtitle").textContent = event
    ? `${stamp(event.media_timestamp_s, true)} · 第 ${event.frame_idx} 帧`
      + (event.frame_target_count == null ? "" : ` · 本帧检测：${event.frame_person_count} 个人员框 / ${event.frame_target_count} 个目标框`)
    : "当前视频预览";
  if (event) {
    $("detail-category").textContent = label(event.yolo_class);
    const confidence = Number.isFinite(event.yolo_conf) ? " · 检测置信度 " + (100 * event.yolo_conf).toFixed(0) + "%" : "";
    $("detail-meta").textContent = `绿色框 · 跟踪 #${event.track_id ?? "—"}` + confidence;
    const verdict = event.verdict;
    const description = event.status !== "completed" ? (statuses[event.status] || event.status) + "，没有 AI 描述。"
      : event.simulated ? "本次只验证检测与跟踪。语义描述为预设模拟结果，可在 JSONL 中查看。"
        : [verdict?.refined_category, verdict?.activity_summary, ...(verdict?.observed_events || [])].filter(Boolean).join(" · ");
    $("description").textContent = description;
  }
  if (imageView) {
    $("placeholder").hidden = true;
    $("frame").hidden = false;
    $("frame").src = "/api/jobs/" + result.id + "/frame?index=" + index;
  } else {
    seekVideo();
  }
}

function seekVideo() {
  const video = $("video");
  const time = events[selected]?.media_timestamp_s;
  if (video.readyState >= 1 && Number.isFinite(time)) video.currentTime = Math.min(time, video.duration || time);
}

function setView(showImage) {
  imageView = showImage;
  $("image-tab").setAttribute("aria-pressed", String(showImage));
  $("video-tab").setAttribute("aria-pressed", String(!showImage));
  const video = $("video");
  $("frame").hidden = !showImage || !result;
  $("placeholder").hidden = Boolean(result);
  video.hidden = showImage;
  if (showImage) {
    video.pause();
    if (result) $("frame").src = "/api/jobs/" + result.id + "/frame?index=" + selected;
  } else {
    if (!video.getAttribute("src")) video.src = "/api/jobs/" + result.id + "/video";
    seekVideo();
  }
}

$("drop").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", () => choose($("file").files[0]));
for (const action of ["dragenter", "dragover", "dragleave", "drop"]) {
  $("drop").addEventListener(action, (event) => {
    event.preventDefault();
    $("drop").classList.toggle("dragging", !busy && ["dragenter", "dragover"].includes(action));
    if (action === "drop") choose(event.dataTransfer.files[0]);
  });
}
$("mode").addEventListener("change", () => { $("consent").checked = false; controls(); });
$("cap").addEventListener("input", controls);
$("consent").addEventListener("change", controls);
$("run").addEventListener("click", () => start(false));
$("demo").addEventListener("click", () => start(true));
$("category").addEventListener("change", renderList);
$("image-tab").addEventListener("click", () => setView(true));
$("video-tab").addEventListener("click", () => setView(false));
$("video").addEventListener("loadedmetadata", seekVideo);
$("video").addEventListener("error", () => {
  if (!imageView) { setView(true); $("video-note").hidden = false; }
});
$("frame").addEventListener("error", () => {
  if (!imageView || !result) return;
  $("frame").hidden = true;
  $("placeholder").hidden = false;
  $("placeholder").querySelector("strong").textContent = "此时刻的截图不可用";
  $("placeholder").querySelector("small").textContent = "可以试着选择其他记录，或切换到原视频。";
});
sync();
