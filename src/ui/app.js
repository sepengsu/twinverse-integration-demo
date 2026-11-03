"use strict";
// Supervisor API의 작업 상태와 A2A 통신 이벤트를 읽어 대시보드에 표시합니다.
const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const statusLabel = {pending:"대기",working:"진행 중",completed:"완료",failed:"실패",blocked:"선행 작업 실패",input_required:"입력 필요",accepted:"접수됨"};
const names = {supervisor:"Supervisor",cau_process:"중앙대",skku_robot:"성균관대",user:"사용자",unity:"Unity"};
const labels = {workflow_started:"워크플로 시작",delegation_started:"작업 요청 준비",http_request:"요청 전송",http_response:"응답 수신",task_received:"A2A 작업 결과",artifact_saved:"결과물 저장",payload_transformed:"로봇 작업으로 변환",delegation_failed:"에이전트 통신 실패",workflow_finished:"워크플로 종료"};
const capabilityNames = {robot_path_planning:"로봇 경로 계획",robot_state_query:"로봇 상태 조회",path_validation:"경로 검증",process_planning:"공정 계획",equipment_selection:"설비 선택",process_information:"공정 정보 조회"};
const defaultGoals = {robot_path_planning:"출발 개체에서 목적 개체까지 로봇 경로를 계획합니다.",process_then_robot:"부품의 공정 계획을 생성하고 필요한 로봇 운반 경로를 계획합니다.",robot_state_query:"로봇과 장면의 현재 상태를 조회합니다.",path_validation:"로봇 경로 요청의 검증 상태를 확인합니다.",process_planning:"부품의 공정 계획을 생성합니다.",equipment_selection:"부품의 공정에 사용할 설비를 선택합니다.",process_information:"지원하는 공정과 기능 정보를 조회합니다."};
let previewWanted = true;
let followLive = true;
let snapshot = {workflows:[],events:[],cursor:0}, agents = [], selectedRun = null, selectedEvent = null;
let selectedAgent = null, selectedArtifact = "robot_plan", filter = "all", tab = "payload", refreshTimer, toastTimer, snapshotBusy = false, snapshotAgain = false, stream;
Object.assign(labels, {plan_published:"경로 계획 발행",unity_waiting:"Unity 승인 대기",unity_accepted:"Unity 명령 승인",unity_running:"Unity 경로 재생",unity_completed:"Unity 재생 완료",unity_failed:"Unity 확인·재생 실패"});
function completionLabel(row) {
  if (!row) return "작업 대기";
  if (row.status === "completed") return row.unity?.status === "completed" ? (row.unity.playback_kind === "joint_waypoint_simulation" ? "관절 시뮬레이션 완료" : "TCP 경로 재생 완료") : row.state?.artifacts?.robot_plan ? "계획 발행 완료" : row.state?.artifacts?.process_plan ? "공정 계획 완료" : "기능 실행 완료";
  if (row.status === "working" && row.unity) return {pending:"Unity 승인 대기",accepted:"Unity 승인 확인",running:"Unity 경로 재생 중"}[row.unity.status] || "처리 중";
  return statusLabel[row.status] || row.status;
}
function renderExecution(current) {
  // MCP 경로 발행, Unity 명령 승인, 로컬 재생을 서로 다른 진행 단계로 보여줍니다.
  const plan = current?.state?.artifacts?.robot_plan, unity = current?.unity;
  $("execution-status").textContent = completionLabel(current);
  const requested = current?.request?.metadata?.unity_preview === true;
  const direct = current ? current.request.workflow_mode === "direct" : $("workflow-mode").value === "direct";
  const capability = current?.request?.capability || $("capability").value;
  const nodes = Object.values(current?.state?.tasks || {});
  const routeNames = nodes.map(node => names[node.assigned_agent_id] || agents.find(agent => agent.id === node.assigned_agent_id)?.name || names[current?.request?.agent_id] || node.capability);
  $("execution-route").textContent = direct ? `직접 실행 · Supervisor → ${routeNames[0] || names[$("target-agent").value] || capabilityNames[capability] || capability}${capability === "robot_path_planning" ? " → Twinverse → Unity" : ""}` : "공정 후 로봇 경로 · Supervisor → 중앙대 → Supervisor → 성균관대 → Twinverse → Unity";
  const expectedNodes = nodes.length ? nodes : direct ? [{capability}] : [{capability:"process_planning"},{capability:"robot_path_planning"}];
  const stages = expectedNodes.map(node => [node.capability === "robot_path_planning" ? "경로 발행" : capabilityNames[node.capability] || node.capability, {completed:"done",working:"active",failed:"failed",input_required:"failed"}[node.status] || "pending"]);
  if (expectedNodes.some(node => node.capability === "robot_path_planning")) stages.push(
    ["Unity 승인",unity?.accepted_command_ids?.length === unity?.expected_command_ids?.length && unity?.accepted_command_ids?.length ? "done" : requested && plan ? "active" : requested ? "pending" : "skipped"],
    ["경로 재생",unity?.status === "completed" ? "done" : unity?.status === "running" ? "active" : unity?.status === "failed" ? "failed" : requested ? "pending" : "skipped"]);
  $("execution-stages").innerHTML = stages.map(([name,state],i) => `<div class="execution-step ${state}"><span>${state === "done" ? "✓" : i+1}</span><strong>${name}</strong><small>${{done:"확인됨",active:"진행 중",pending:"대기",skipped:"선택 안 함",failed:"실패"}[state]}</small></div>`).join("");
  const progress = Math.round((unity?.progress || 0)*100);
  $("preview-progress").style.width = `${progress}%`;
  const collision = plan?.validation?.collision_free;
  const collisionText = collision === true && plan.validation.collision_check === "passed" ? "충돌 검사 통과" : collision === false ? "충돌 검사 실패" : "충돌 검사 미실행";
  $("execution-facts").innerHTML = current ? `<span><b>경로</b> ${escapeHtml(plan ? `${plan.source} → ${plan.destination}` : "생성 대기")}</span><span><b>타깃</b> 계획 ${plan?.targets?.length || 0} / Unity ${unity?.waypoint_count || 0}</span><span><b>이번 작업 명령</b> ${unity?.accepted_command_ids?.length || 0} / ${unity?.expected_command_ids?.length || 0} 승인</span><span class="validation-note">${collisionText}</span><span><b>재생</b> ${progress}% ${unity?.playback_kind === "joint_waypoint_simulation" ? "관절 시뮬레이션" : "TCP 경로 미리보기"}</span>${unity?.message ? `<span class="runtime-message">${escapeHtml(unity.message)}</span>` : ""}` : "새 작업을 실행하면 경로 발행과 Unity 확인 결과가 표시됩니다.";
  if (current && !expectedNodes.some(node => node.capability === "robot_path_planning")) $("execution-facts").innerHTML = `<span><b>기능</b> ${escapeHtml(capabilityNames[capability] || capability)}</span><span><b>결과물</b> ${escapeHtml(Object.keys(current.state?.artifacts || {}).join(", ") || "응답 대기")}</span>`;
  const connected = snapshot.unity_runtime?.connected;
  $("unity-live").textContent = connected ? "Unity 연결됨" : "Unity 응답 대기";
  $("unity-live").classList.toggle("connected", Boolean(connected));
  renderCauResult(current);
}

function renderCauResult(current) {
  // CAU가 반환한 설비 ID와 SKKU로 실제 전송한 목적지가 같은지 확인해 표시합니다.
  const plan = current?.state?.artifacts?.process_plan;
  $("cau-result").hidden = !plan;
  if (!plan) return;
  const operations = plan.operations || [], total = operations.reduce((sum,operation) => sum + Number(operation.estimated_duration_seconds || 0),0);
  const processNode = Object.values(current.state?.tasks || {}).find(node => node.capability === "process_planning");
  const producer = processNode?.assigned_agent_id || selectedEvents().find(event => event.kind === "artifact_saved" && event.data?.artifact_type === "process_plan")?.source;
  const dummy = agents.find(agent => agent.id === producer)?.mode === "dummy";
  $("cau-result-title").textContent = `${agentName(producer)} 공정 계획`;
  $("cau-result-title").title = `계획 ${plan.plan_id} · 부품 ${plan.part_id}`;
  $("cau-operations").innerHTML = operations.map(operation => `<span class="cau-operation" title="${escapeHtml(operation.description)}">${escapeHtml({load:"투입",machining:"가공",unload:"배출"}[operation.name] || operation.name)} <b>${escapeHtml(operation.estimated_duration_seconds)}초</b></span>`).join('<span class="operation-arrow">→</span>');
  $("cau-estimate").textContent = `${dummy ? "Dummy 예제" : "계획 기준"} · 예상 합계 ${total}초`;
  const transformed = selectedEvents().findLast(event => event.kind === "payload_transformed" && event.data?.from_artifact === "process_plan");
  const posted = transformed && selectedEvents().findLast(event => event.kind === "http_request" && event.target === transformed.target && event.task_node_id === transformed.task_node_id && event.data?.method === "POST");
  const sentRequest = posted?.data?.body?.message?.parts?.map(part => part.data).find(data => data?.request_id === transformed.data?.request?.request_id);
  const destination = sentRequest?.input?.destination ?? transformed?.data?.request?.input?.destination;
  const same = Boolean(destination && destination === plan.machine_id);
  const linked = Boolean(same && sentRequest);
  $("cau-machine").textContent = plan.machine_id;
  $("skku-destination").textContent = destination || "로봇 요청 없음";
  $("cau-handoff").classList.toggle("linked", linked);
  $("cau-handoff").classList.toggle("mismatch", Boolean(destination && !same));
  $("handoff-arrow").textContent = destination ? "→ Supervisor 변환 →" : "→ 협업 옵션에서 연결 →";
  $("handoff-status").textContent = linked ? "✓ 동일 설비 전송 확인" : destination ? same ? "로봇 요청 준비" : "목적지 불일치" : "CAU 단독 실행";
}

async function api(path, options = {}) {
  const response = await fetch(`/dashboard/api/${path}`, {...options, cache:"no-store"});
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || (data.error === "workflow_already_exists" ? "이미 실행한 작업 ID입니다." : data.error) || `HTTP ${response.status}`);
  return data;
}
function error(message) { $("error-banner").textContent = message; $("error-banner").hidden = !message; }
function toast(message) { clearTimeout(toastTimer); $("toast").textContent = message; $("toast").hidden = false; toastTimer = setTimeout(() => $("toast").hidden = true, 2600); }
function time(value) { return new Date(value).toLocaleTimeString("ko-KR", {hour12:false, hour:"2-digit", minute:"2-digit",second:"2-digit"}); }
function workflow() { return snapshot.workflows.find(row => row.global_task_id === selectedRun); }
function isError(event) { return event.status === "failed" || event.status === "input_required" || event.kind === "delegation_failed" || Number(event.data?.status_code) >= 400; }
function isRequest(event) { return event.kind === "http_request"; }
function isResponse(event) { return event.kind === "http_response" || event.kind === "task_received"; }
function selectedEvents() { return snapshot.events.filter(event => event.global_task_id === selectedRun); }
function agentName(id) { return names[id] || agents.find(agent => agent.id === id)?.name || id || "Agent"; }
function route(event) { return [event.source,event.target].filter(Boolean).map(agentName).join(" → "); }
function isAgentMessage(event) {
  return event.kind === "task_received" || event.kind === "delegation_failed" ||
    (["http_request","http_response"].includes(event.kind) && event.data?.method === "POST");
}
function preciseTime(value) { return new Date(value).toLocaleTimeString("ko-KR", {hour12:false, hour:"2-digit", minute:"2-digit", second:"2-digit", fractionalSecondDigits:3}); }
function agentForNode(node, current) {
  return node.assigned_agent_id || current?.request?.agent_id || agents.find(agent => agent.enabled && agent.capabilities.includes(node.capability))?.id;
}
function setFollowLive(value) {
  followLive = value;
  $("follow-live").setAttribute("aria-pressed", String(value));
  $("follow-live").textContent = `실시간 따라가기 ${value ? "ON" : "OFF"}`;
}
function renderNetwork(current) {
  const events = selectedEvents(), nodes = Object.values(current?.state?.tasks || {});
  const active = nodes.find(node => node.status === "working");
  const reply = events.findLast(event => isAgentMessage(event) && isResponse(event));
  const focus = events.find(event => event.id === selectedEvent);
  const unity = current?.unity;
  $("current-activity").classList.toggle("busy", current?.status === "working");
  $("current-activity").classList.toggle("failed", current?.status === "failed");
  $("activity-agent").textContent = active ? `${agentName(agentForNode(active,current))} 처리 중` : current?.status === "working" && unity ? "Unity 확인·재생 중" : completionLabel(current);
  $("activity-detail").textContent = active ? `${capabilityNames[active.capability] || active.capability} · Agent 응답 대기` : current?.status === "working" && unity ? `${{pending:"명령 승인 대기",accepted:"명령 승인 확인",running:"경로 재생"}[unity.status] || unity.status} · ${Math.round((unity.progress || 0)*100)}%` : current ? `${current.global_task_id} · ${current.status === "completed" ? "처리 종료" : statusLabel[current.status] || current.status}` : "새 작업을 실행해 주세요.";
  $("response-agent").textContent = reply ? route(reply) : "응답 대기";
  $("response-detail").textContent = reply ? `${capabilityNames[reply.capability] || reply.capability || "A2A 메시지"} · ${isError(reply) ? "오류 응답" : statusLabel[reply.status] || "수신 확인"} · ${preciseTime(reply.timestamp)}` : "선택한 작업에서 수신한 Agent 응답이 없습니다.";
  $("latest-response").classList.toggle("received", Boolean(reply));
  $("latest-response").classList.toggle("failed", Boolean(reply && isError(reply)));
  const direction = focus && (isRequest(focus) ? "request" : isResponse(focus) ? "response" : null);
  document.querySelector(".network-panel").dataset.direction = direction || "";
  $("communication-focus").className = `communication-focus ${direction || ""} ${focus && isError(focus) ? "failed" : ""}`;
  $("communication-focus").textContent = focus ? `${followLive ? "실시간 통신" : "선택한 기록"} · ${direction ? title(focus) : `${route(focus)} · ${title(focus)}`} · ${preciseTime(focus.timestamp)}` : "통신 기록을 선택하면 보내는 쪽과 받는 쪽을 강조합니다.";
  for (const id of ["supervisor","cau_process","skku_robot"]) {
    const card = $(`agent-${id}`), node = nodes.find(node => agentForNode(node,current) === id);
    const status = id === "supervisor" ? current?.status : node?.status;
    card.classList.remove("working","completed","failed","focus-source","focus-target");
    if (["working","completed","failed"].includes(status)) card.classList.add(status);
    if (direction && focus.source === id) card.classList.add("focus-source");
    if (direction && focus.target === id) card.classList.add("focus-target");
    const ownReply = events.findLast(event => event.source === id && isAgentMessage(event) && isResponse(event));
    card.querySelector(".node-state").textContent = id === "supervisor" ? completionLabel(current) : status === "working" ? "요청 처리 · 응답 대기" : node?.capability === "process_planning" && current?.state?.artifacts?.process_plan && status === "completed" ? `공정 ${current.state.artifacts.process_plan.operations?.length || 0}개 · 설비 반환` : ownReply ? `${isError(ownReply) ? "오류 응답" : "응답 수신"} · ${time(ownReply.timestamp)}` : statusLabel[status] || (current ? "이번 작업에 참여 안 함" : "작업 대기");
  }
  for (const [edge,id] of [["edge-cau","cau_process"],["edge-skku","skku_robot"]]) {
    const involved = direction && ((focus.source === id && focus.target === "supervisor") || (focus.target === id && focus.source === "supervisor"));
    const node = nodes.find(node => agentForNode(node,current) === id);
    $(edge).classList.toggle("active", node?.status === "working" && !involved);
    $(edge).classList.toggle("completed", node?.status === "completed");
    $(edge).classList.toggle("focus-request", Boolean(involved && direction === "request"));
    $(edge).classList.toggle("focus-response", Boolean(involved && direction === "response"));
    $(edge).classList.toggle("failed", Boolean(involved && isError(focus)));
  }
}
function title(event) {
  if (event.kind === "http_request" || event.kind === "http_response") {
    const card = (event.data?.url || "").includes("agent-card.json");
    return `${route(event)} · ${card ? "Agent Card" : "A2A 메시지"} ${event.kind === "http_request" ? "요청" : "응답"}`;
  }
  if (event.kind === "task_received") return `${route(event)} · A2A 작업 결과`;
  return labels[event.kind] || event.kind;
}
function summary(event) {
  const route = [names[event.source] || event.source, names[event.target] || event.target].filter(Boolean).join(" → ");
  if (isError(event)) return event.data?.error || event.data?.errors?.[0]?.error || `${route} · ${statusLabel[event.status] || "오류"}`;
  if (event.kind === "http_request" || event.kind === "http_response") {
    let path = event.data?.url || "";
    try { path = new URL(path).pathname; } catch (_) {}
    return `${route} · ${path}`;
  }
  if (event.kind === "workflow_started") return event.data?.goal || "";
  if (event.kind === "artifact_saved") return `${route} · ${event.data?.artifact_type || "artifact"}`;
  if (event.kind === "payload_transformed") return "process_plan → robot_path_planning · plan_only";
  if (event.kind === "workflow_finished") return `${statusLabel[event.status] || event.status} · 결과 ${Object.keys(event.data?.artifacts || {}).length}개`;
  return `${route}${event.capability ? ` · ${event.capability}` : ""}`;
}
function badge(event) {
  if (event.kind === "http_request") return event.data?.method || "SEND";
  if (event.kind === "http_response") return event.data?.status_code || "RECEIVE";
  if (event.status) return event.status;
  return event.task_node_id || "WORKFLOW";
}

function render() {
  const rows = snapshot.workflows;
  if (!rows.some(row => row.global_task_id === selectedRun)) selectedRun = rows[0]?.global_task_id || null;
  const current = workflow();
  if (followLive) {
    selectedAgent = null;
    selectedEvent = selectedEvents().findLast(isAgentMessage)?.id || selectedEvents().at(-1)?.id || null;
  }
  $("stat-runs").textContent = rows.length;
  const completed = rows.filter(row => row.status === "completed").length;
  const finished = rows.filter(row => row.status !== "working").length;
  const working = rows.filter(row => row.status === "working").length;
  $("stat-completed").textContent = completed;
  $("stat-ratio").textContent = finished ? `${Math.round(completed / finished * 100)}%` : "—";
  $("stat-messages").textContent = snapshot.events.filter(event => isRequest(event) && (event.data?.url || "").includes("message:")).length;
  $("stat-run-note").textContent = working ? `${working}개 작업 진행 중` : rows.length ? "모든 작업 처리됨" : "새로운 작업을 기다리는 중";
  $("history-count").textContent = rows.length;
  $("history").innerHTML = rows.length ? rows.map(row => `<button type="button" class="history-item ${row.global_task_id === selectedRun ? "selected" : ""}" data-run="${escapeHtml(row.global_task_id)}"><span class="status-dot ${escapeHtml(row.status)}"></span><span class="run-text"><strong title="${escapeHtml(row.global_task_id)}">${escapeHtml(row.global_task_id)}</strong><small>${escapeHtml(statusLabel[row.status] || row.status)} · ${time(row.created_at)}</small></span></button>`).join("") : '<div class="history-empty">첫 작업을 실행하면<br>이곳에 기록이 쌓입니다.</div>';
  $("run-select").innerHTML = rows.length ? rows.map(row => `<option value="${escapeHtml(row.global_task_id)}">${escapeHtml(row.global_task_id)} · ${escapeHtml(statusLabel[row.status] || row.status)}</option>`).join("") : '<option value="">작업 없음</option>';
  $("run-select").value = selectedRun || "";
  $("selected-run-label").textContent = current ? `${current.global_task_id} · ${completionLabel(current)}` : "작업을 선택해 주세요";
  $("export").disabled = !current;
  $("launch").disabled = current?.status === "working";
  $("launch").firstElementChild.textContent = current?.status === "working" ? "작업 진행 중…" : "작업 실행";
  renderNetwork(current); renderExecution(current); renderTimeline(); renderDetail();
  $("updated-at").textContent = `마지막 갱신 ${new Date().toLocaleTimeString("ko-KR", {hour12:false})}`;
}

function renderTimeline() {
  const events = selectedEvents();
  const visible = events.filter(event => filter === "all" || (filter === "requests" && isRequest(event)) || (filter === "responses" && isResponse(event)) || (filter === "errors" && isError(event)));
  $("event-count").textContent = `${visible.length} events`;
  const pane = $("timeline"), atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 50;
  if (!visible.length) {
    pane.innerHTML = `<div class="empty-state"><div class="empty-glyph">${filter === "errors" ? "✓" : "⇄"}</div><h3>${filter === "all" ? "첫 번째 통신을 기다립니다" : "해당 기록이 없습니다"}</h3><p>${filter === "all" ? "새 작업을 실행하면 실제 요청과 응답이<br>시간순으로 표시됩니다." : "다른 필터를 선택해 통신 흐름을 확인하세요."}</p></div>`;
    return;
  }
  pane.innerHTML = visible.map(event => `<button type="button" class="event-row ${isError(event) ? "error" : isResponse(event) ? "response" : ""} ${selectedEvent === event.id ? "selected" : ""}" data-event="${event.id}"><span class="event-icon">${isError(event) ? "!" : isRequest(event) ? "↗" : isResponse(event) ? "↙" : event.kind === "workflow_finished" ? "✓" : "·"}</span><span class="event-body"><div><span class="event-title">${escapeHtml(title(event))}</span><span class="event-badge">${escapeHtml(badge(event))}</span></div><div class="event-summary" title="${escapeHtml(summary(event))}">${escapeHtml(summary(event))}</div></span><span class="event-time">${time(event.timestamp)}</span></button>`).join("");
  if (atBottom) pane.scrollTop = pane.scrollHeight;
}

function renderDetail() {
  const current = workflow(), event = snapshot.events.find(item => item.id === selectedEvent);
  let data, meta = "왼쪽 기록이나 에이전트를 선택하세요.", heading = "통신 상세";
  if (tab === "state") { data = current?.state || (current ? {status:current.status,error:current.error} : null); heading = "작업 상태"; meta = current?.global_task_id || "선택한 작업이 없습니다."; }
  else if (tab === "artifacts") {
    const artifacts = current?.state?.artifacts || {};
    if (!artifacts[selectedArtifact]) selectedArtifact = Object.keys(artifacts)[0] || "robot_plan";
    data = artifacts[selectedArtifact] || null; heading = {process_plan:"공정 계획",robot_plan:"로봇 경로 계획",unity_preview:"Unity 재생 결과"}[selectedArtifact] || selectedArtifact;
    meta = selectedArtifact === "robot_plan" ? "출발·상승·접근·도착 타깃 / 충돌 검사 미실행" : "선택한 작업의 실제 반환 결과";
    $("artifact-picker").innerHTML = Object.keys(artifacts).map(key => `<button type="button" data-artifact="${escapeHtml(key)}" class="${key === selectedArtifact ? "active" : ""}">${escapeHtml(key)}</button>`).join("");
  }
  else if (selectedAgent) { const agent = agents.find(item => item.id === selectedAgent); data = agent?.card || agent; heading = `${names[selectedAgent]} Agent Card`; meta = agent?.error || agent?.url || "연결 상태 확인 중"; }
  else if (event) { data = event.data; heading = title(event); meta = `${summary(event)} · ${time(event.timestamp)}`; }
  $("detail-title").textContent = heading;
  $("artifact-picker").hidden = tab !== "artifacts";
  $("detail-meta").textContent = meta;
  const text = data != null ? JSON.stringify(data, null, 2) : "// 요청, 응답, Agent Card를 여기서 확인할 수 있습니다.";
  $("payload").textContent = text;
  $("copy").disabled = data == null;
  $("payload-size").textContent = data != null ? `${new TextEncoder().encode(text).length.toLocaleString()} bytes` : "—";
}
function chooseRun(id) {
  selectedRun = id; selectedAgent = null;
  setFollowLive(true); selectTab("payload");
  const events = selectedEvents(); selectedEvent = events.at(-1)?.id || null;
  render();
}
async function refreshSnapshot() {
  // SSE 알림이 몰려도 중복 조회를 한 번으로 합쳐 화면을 갱신합니다.
  if (snapshotBusy) { snapshotAgain = true; return; }
  snapshotBusy = true;
  try {
    snapshot = await api("snapshot");
    if (!snapshot.events.some(item => item.id === selectedEvent)) selectedEvent = selectedEvents().at(-1)?.id || null;
    render();
  } catch (exc) { error(`서버 기록을 가져오지 못했습니다. ${exc.message}`); }
  finally { snapshotBusy = false; if (snapshotAgain) { snapshotAgain = false; scheduleSnapshot(); } }
}
function scheduleSnapshot() { clearTimeout(refreshTimer); refreshTimer = setTimeout(refreshSnapshot, 90); }
async function refreshAgents() {
  $("refresh").disabled = true;
  try {
    agents = (await api("agents")).agents;
    for (const agent of agents) {
      const card = $(`agent-${agent.id}`); if (!card) continue;
      const badge = card.querySelector(".agent-status"); badge.className = `agent-status ${agent.status}`;
      badge.textContent = {online:"연결됨",offline:"연결 실패",disabled:"비활성"}[agent.status] || "확인 중";
      card.querySelector(".agent-endpoint").textContent = agent.url;
      card.querySelector(".agent-endpoint").title = agent.url;
      card.querySelector(".agent-latency").textContent = agent.latency_ms != null ? `${agent.latency_ms} ms` : "—";
      card.title = agent.error || "Agent Card 보기";
    }
    const online = agents.filter(agent => agent.status === "online").length;
    $("stat-agents").textContent = online;
    $("stat-agents").nextElementSibling.textContent = `/ ${agents.length}`;
    $("stat-agent-note").textContent = online === agents.length ? "모든 에이전트에 연결됨" : `${agents.length - online}개 에이전트 연결 확인 필요`;
    updateLaunchOptions(true); render();
  } catch (exc) { error(`에이전트 상태를 확인하지 못했습니다. ${exc.message}`); }
  finally { $("refresh").disabled = false; }
}
function connectEvents() {
  // 새 이벤트를 받으면 전체 기록을 다시 읽어 작업 상태와 세부 JSON을 맞춥니다.
  stream = new EventSource(`/dashboard/api/events?after=${snapshot.cursor}`);
  stream.onopen = () => { $("live").className = "live"; $("live").innerHTML = "<i></i>LIVE · 실시간 연결"; refreshSnapshot(); };
  stream.onmessage = scheduleSnapshot;
  stream.onerror = () => { $("live").className = "live offline"; $("live").innerHTML = "<i></i>재연결 중"; };
}

$("launch-form").addEventListener("submit", async event => {
  // 사용자 선택을 Supervisor의 WorkflowRequest 형식으로 묶어 제출합니다.
  event.preventDefault(); error("");
  if (!$("launch-form").checkValidity()) { if (!$("launch-settings").open) $("launch-settings").showModal(); $("launch-form").reportValidity(); return; }
  const button = $("launch"); button.disabled = true; button.firstElementChild.textContent = "요청 전송 중…";
  const request = {global_task_id:`TASK-${Date.now()}`, goal:$("goal").value.trim(), part_id:$("part").value.trim()};
  request.workflow_mode = $("workflow-mode").value;
  request.metadata = {unity_preview:!$("unity-preview").disabled && $("unity-preview").checked};
  if ($("source").value.trim()) request.source = $("source").value.trim();
  if ($("destination").value.trim()) request.requested_machine_id = $("destination").value.trim();
  try {
    if (request.workflow_mode === "direct") {
      request.capability = $("capability").value;
      if ($("target-agent").value) request.agent_id = $("target-agent").value;
      request.input = JSON.parse($("capability-input").value || "{}");
      if (!request.input || Array.isArray(request.input) || typeof request.input !== "object") throw new Error("추가 입력은 JSON 객체여야 합니다.");
    }
    const response = await api("workflows", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(request)});
    selectedRun = response.global_task_id; selectedEvent = null; selectedAgent = null; setFollowLive(true); tab = "payload"; selectTab(tab);
    $("launch-settings").close();
    toast("작업을 접수했습니다. 통신 기록이 실시간으로 갱신됩니다.");
    await refreshSnapshot();
  } catch (exc) { error(`작업을 시작하지 못했습니다. ${exc.message}`); }
  finally { button.disabled = workflow()?.status === "working"; button.firstElementChild.textContent = button.disabled ? "작업 진행 중…" : "작업 실행"; }
});
function updateLaunchOptions(loadAgents = false) {
  // 선택한 Agent가 제공하는 기능만 보여주고, Unity 옵션은 경로 작업에서만 엽니다.
  const direct = $("workflow-mode").value === "direct";
  if (loadAgents) {
    const previous = $("target-agent").value;
    $("target-agent").innerHTML = '<option value="">기능으로 자동 선택</option>' + agents.filter(agent => agent.id !== "supervisor" && agent.enabled).map(agent => `<option value="${escapeHtml(agent.id)}">${escapeHtml(names[agent.id] || agent.name)}</option>`).join("");
    $("target-agent").value = [...$("target-agent").options].some(option => option.value === previous) ? previous : "";
  }
  const previousCapability = $("capability").value;
  const available = [...new Set(agents.filter(agent => agent.id !== "supervisor" && agent.enabled && (!$("target-agent").value || agent.id === $("target-agent").value)).flatMap(agent => agent.capabilities))];
  if (available.length) {
    $("capability").innerHTML = available.map(capability => `<option value="${escapeHtml(capability)}">${escapeHtml(capabilityNames[capability] || capability)}</option>`).join("");
    $("capability").value = available.includes(previousCapability) ? previousCapability : available.includes("robot_path_planning") ? "robot_path_planning" : available[0];
  }
  const capability = $("capability").value;
  for (const id of ["direct-agent-field","direct-capability-field","direct-input"]) $(id).hidden = !direct;
  const robot = !direct || capability === "robot_path_planning";
  $("unity-preview").disabled = !robot;
  $("unity-preview").checked = robot && previewWanted;
  $("source").disabled = !robot; $("source").required = direct && robot;
  $("destination").disabled = direct && !["robot_path_planning","process_planning"].includes(capability);
  $("destination").required = direct && robot;
  $("part").required = !direct || ["robot_path_planning","process_planning","equipment_selection"].includes(capability);
  const goal = defaultGoals[direct ? capability : "process_then_robot"] || `선택한 ${capability} 기능을 실행합니다.`;
  if (Object.values(defaultGoals).includes($("goal").value)) $("goal").value = goal;
  $("launch-note").textContent = direct ? "공정 계획을 거치지 않고 선택한 Agent의 기능을 실행합니다." : "중앙대 공정 계획을 받은 뒤 성균관대 로봇 경로를 요청합니다.";
  if (!workflow()) renderExecution(null);
}
$("workflow-mode").addEventListener("change", () => updateLaunchOptions());
$("target-agent").addEventListener("change", () => { $("capability-input").value = "{}"; updateLaunchOptions(); });
$("capability").addEventListener("change", () => { $("capability-input").value = "{}"; updateLaunchOptions(); });
$("unity-preview").addEventListener("change", () => { previewWanted = $("unity-preview").checked; });
updateLaunchOptions();
$("history").addEventListener("click", event => { const button = event.target.closest("[data-run]"); if (button) chooseRun(button.dataset.run); });
$("run-select").addEventListener("change", event => chooseRun(event.target.value));
$("timeline").addEventListener("click", event => { const button = event.target.closest("[data-event]"); if (button) { setFollowLive(false); selectedEvent = Number(button.dataset.event); selectedAgent = null; selectTab("payload"); render(); } });
document.querySelectorAll("[data-agent]").forEach(button => button.addEventListener("click", () => { setFollowLive(false); selectedAgent = button.dataset.agent; selectedEvent = null; selectTab("payload"); render(); }));
document.querySelectorAll("[data-filter]").forEach(button => button.addEventListener("click", () => { filter = button.dataset.filter; document.querySelectorAll("[data-filter]").forEach(item => item.classList.toggle("active", item === button)); renderTimeline(); }));
function selectTab(value) { tab = value; document.querySelectorAll("[data-tab]").forEach(button => { const active = button.dataset.tab === value; button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active)); }); renderDetail(); }
document.querySelectorAll("[data-tab]").forEach(button => button.addEventListener("click", () => { setFollowLive(false); selectTab(button.dataset.tab); }));
$("artifact-picker").addEventListener("click", event => { const button = event.target.closest("[data-artifact]"); if (button) { selectedArtifact = button.dataset.artifact; renderDetail(); $("payload").scrollTop = 0; } });
$("show-cau-plan").addEventListener("click", () => {
  setFollowLive(false); selectedAgent = null; selectedArtifact = "process_plan";
  selectedEvent = selectedEvents().findLast(event => event.kind === "task_received" && event.capability === "process_planning")?.id || null;
  selectTab("artifacts"); render(); $("payload").scrollTop = 0;
});
$("follow-live").addEventListener("click", () => { setFollowLive(!followLive); if (followLive) selectTab("payload"); render(); });
$("open-settings").addEventListener("click", () => $("launch-settings").showModal());
$("close-settings").addEventListener("click", () => $("launch-settings").close());
$("presentation").addEventListener("click", () => { document.body.classList.toggle("presentation"); $("presentation").textContent = document.body.classList.contains("presentation") ? "기본 보기" : "촬영 보기"; });
$("presentation").textContent = "촬영 보기";
if (new URLSearchParams(location.search).get("presentation") === "1") { document.body.classList.add("presentation"); $("presentation").textContent = "기본 보기"; }
$("refresh").addEventListener("click", async () => { error(""); await Promise.all([refreshSnapshot(),refreshAgents()]); });
$("copy").addEventListener("click", async () => { try { await navigator.clipboard.writeText($("payload").textContent); toast("JSON을 복사했습니다."); } catch (_) { toast("복사 권한을 확인하거나 JSON을 직접 선택해 복사해 주세요."); } });
$("export").addEventListener("click", () => {
  const current = workflow(); if (!current) return;
  const blob = new Blob([JSON.stringify({workflow:current,events:selectedEvents()},null,2)], {type:"application/json"});
  const url = URL.createObjectURL(blob), link = document.createElement("a");
  link.href = url; link.download = `${current.global_task_id.replace(/[^a-zA-Z0-9_-]/g,"_")}-a2a-trace.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
window.addEventListener("beforeunload", () => stream?.close());
(async () => { await refreshSnapshot(); connectEvents(); refreshAgents(); setInterval(refreshAgents, 20000); setInterval(refreshSnapshot, 2500); })();
